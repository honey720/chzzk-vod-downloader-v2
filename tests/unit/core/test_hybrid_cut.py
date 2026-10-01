"""하이브리드 컷(core/utils/hybrid_cut.py)과 정합 판정(core/utils/cut_check.py) 테스트 (#309).

실제 ffmpeg(imageio-ffmpeg 동봉 — 기존 remux 테스트와 같은 바이너리)를 실행한다. 입력
영상은 lavfi 소스로 테스트 안에서 몇 초짜리를 만든다. 저장소에 영상 파일을 두지 않는다.

입력 두 가지:
- mp4: 320x240 · 30fps · 4초(120프레임) · B프레임(재정렬 지연 1) · 키프레임 0·30·42·72·90
  (GOP 길이가 30 · 12 · 30 · 18 · 30프레임으로 고르지 않다)
- fMP4: 부호화 1280x736 + 아래 16줄 크롭 · 30fps · 4초(120프레임) · 재정렬 지연 2 ·
  키프레임 0·30·60·90 · 1초 세그먼트 넷을 초기화 세그먼트 뒤에 이어 붙인 파일

핵심 계약:
- 잘라 낸 파일이 정합 판정 다섯 항목을 모두 통과한다
- 시작이 키프레임이면 머리를 재인코딩하지 않고, 한 GOP 안의 구간은 통째로 재인코딩한다
- MPEG-TS 입력은 거부하고, 중간 파일은 성공·실패 모두 지운다
"""

import os
import subprocess
from fractions import Fraction

import pytest

import core.utils.hybrid_cut as cut_module
from core.api.fmp4 import build_fmp4_index, fmp4_origin, parse_init_segment, parse_media_segment
from core.api.hls import parse_media_playlist
from core.api.mp4 import parse_moov, read_mp4_index
from core.models.cut import CutFrames
from core.utils.cut_check import check_cut
from core.utils.ffmpeg import FFmpegTimeoutError, get_ffmpeg_exe, run_ffmpeg
from core.utils.hybrid_cut import (
    CUT_FAILED,
    CUT_TIMEOUT,
    CUT_UNSUPPORTED,
    CutError,
    cut_frames_from_fmp4,
    cut_frames_from_mp4,
    hybrid_cut,
    plan_cut,
)
from core.utils.paths import cut_temp_dir_for
from tests.unit.core.mp4_builder import audio_spec, build_mp4, video_spec
from tests.unit.core.ts_builder import Frame, build_ts

MP4_KEYFRAMES = (0, 30, 42, 72, 90)  # mp4 입력의 키프레임 — -force_key_frames 0,1,1.4,2.4,3 (30fps)
FMP4_KEYFRAMES = (0, 30, 60, 90)  # fMP4 입력의 키프레임 — keyint 30


def _ffmpeg(*args: str, cwd=None) -> None:
    """테스트 입력을 만드는 ffmpeg 실행 — 실패하면 stderr와 함께 테스트를 실패시킨다."""
    done = subprocess.run(
        [get_ffmpeg_exe(), "-hide_banner", "-v", "error", "-y", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert done.returncode == 0, done.stderr


def _lavfi(size: str, seconds: int) -> list[str]:
    return [
        "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}",
        "-c:a", "aac", "-ac", "2", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast",
    ]  # fmt: skip


@pytest.fixture(scope="module")
def mp4_source(tmp_path_factory) -> tuple[str, CutFrames]:
    """mp4 입력과 그 프레임 정보."""
    path = str(tmp_path_factory.mktemp("cut_mp4") / "source.mp4")
    _ffmpeg(
        *_lavfi("320x240", 4),
        "-bf", "2", "-force_key_frames", "0,1,1.4,2.4,3",
        "-x264-params", "b-pyramid=none:keyint=300:min-keyint=1:scenecut=0",
        path,
    )  # fmt: skip

    def read(offset: int, size: int) -> bytes:
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read(size)

    return path, cut_frames_from_mp4(read_mp4_index(read))


@pytest.fixture(scope="module")
def fmp4_parts(tmp_path_factory):
    """fMP4 입력의 재료 — (폴더, 초기화 세그먼트 bytes, 세그먼트 bytes 목록)."""
    folder = tmp_path_factory.mktemp("cut_fmp4")
    _ffmpeg(
        *_lavfi("1280x736", 4),
        "-bf", "3", "-x264-params", "b-pyramid=normal:crop-rect=0,0,0,16:keyint=30:min-keyint=30:scenecut=0",
        "-f", "hls", "-hls_time", "1", "-hls_list_size", "0", "-hls_segment_type", "fmp4",
        "-hls_fmp4_init_filename", "init.mp4", "-hls_segment_filename", "seg-%03d.m4s", "media.m3u8",
        cwd=str(folder),
    )  # fmt: skip
    playlist = parse_media_playlist((folder / "media.m3u8").read_text(encoding="utf-8"))
    init_bytes = (folder / playlist.init_uri).read_bytes()
    return folder, init_bytes, [(folder / name).read_bytes() for name in playlist.segments]


def _fmp4_input(
    folder, name: str, init_bytes: bytes, media: list[bytes], skip: int
) -> tuple[str, CutFrames]:
    """초기화 세그먼트 + media[skip:]를 이어 붙인 입력 파일과 그 프레임 정보."""
    init = parse_init_segment(init_bytes)
    parsed = [parse_media_segment(data, init) for data in media]
    origin = fmp4_origin(init, parsed[0])  # VOD 시작은 언제나 첫 세그먼트에서 구한다
    path = str(folder / name)
    with open(path, "wb") as f:
        f.write(init_bytes + b"".join(media[skip:]))
    index = build_fmp4_index(init, parsed[skip:], origin)
    input_start = float(fmp4_origin(init, parsed[skip]) - origin)
    return path, cut_frames_from_fmp4(init, parsed[skip:], index, input_start)


@pytest.fixture(scope="module")
def fmp4_source(fmp4_parts) -> tuple[str, CutFrames]:
    """세그먼트 전부를 이은 fMP4 입력."""
    folder, init_bytes, media = fmp4_parts
    return _fmp4_input(folder, "joined.mp4", init_bytes, media, skip=0)


def _cut(source: tuple[str, CutFrames], first: int, last: int, tmp_path):
    path, frames = source
    result = hybrid_cut(path, frames, first, last, str(tmp_path / "out.mp4"), inspect=True)
    return result, check_cut(frames, result)


def _kinds(result) -> tuple[str, ...]:
    return tuple(piece.kind for piece in result.plan.pieces)


def _synthetic_frames(count: int = 12, keyframes=(0, 4, 8), delay: float = 0.1) -> CutFrames:
    """ffmpeg 없이 계획·명령을 볼 때 쓰는 프레임 정보 — 10fps, DTS는 PTS보다 delay초 앞선다."""
    return CutFrames(
        frame_pts=tuple(n / 10 for n in range(count)),
        frame_dts=tuple(n / 10 - delay for n in range(count)),
        keyframes=tuple(keyframes),
        timescale=1000,
        frame_duration=0.1,
        audio_start=0.0,
        audio_end=count / 10,
    )


# ================================================================ 입력의 전제


def test_generated_mp4_has_uneven_gops_and_b_frames(mp4_source):
    """만든 mp4 입력은 고르지 않은 GOP와 B프레임 재정렬 지연을 가져야 한다.

    -force_key_frames 0,1,1.4,2.4,3 · -bf 2 · b-pyramid=none
    -> 키프레임 (0, 30, 42, 72, 90), 프레임 120개, 키프레임의 PTS − DTS = 1프레임
    """
    _path, frames = mp4_source

    assert frames.keyframes == MP4_KEYFRAMES
    assert len(frames.frame_pts) == 120
    assert frames.frame_pts[30] - frames.frame_dts[30] == pytest.approx(frames.frame_duration)


def test_generated_fmp4_is_coded_larger_than_displayed(fmp4_source, tmp_path):
    """만든 fMP4 입력은 1280x736으로 부호화되고 아래 16줄을 크롭하며 재정렬 지연이 2프레임이어야 한다.

    testsrc2 1280x736 · crop-rect=0,0,0,16 · b-pyramid=normal
    -> coded (1280, 736), crop (0, 0, 0, 16), reorder_delay 2, 키프레임 (0, 30, 60, 90)
    """
    path, frames = fmp4_source

    video = hybrid_cut(path, frames, 0, 5, str(tmp_path / "probe.mp4")).source.video

    assert (video.coded_width, video.coded_height, video.crop) == (1280, 736, (0, 0, 0, 16))
    assert video.reorder_delay == 2
    assert frames.keyframes == FMP4_KEYFRAMES


# ================================================================ 컷 — mp4


def test_cut_between_non_keyframes_passes_every_check(mp4_source, tmp_path):
    """시작과 끝이 키프레임이 아닌 구간을 자르면 머리·가운데·꼬리로 나뉘고 판정 다섯 항목을 모두 통과해야 한다.

    mp4, 프레임 35~80 (키프레임 30 · 42 · 72 사이)
    -> 조각 (head 35~41, mid 42~71, tail 72~80), check.ok
    """
    result, check = _cut(mp4_source, 35, 80, tmp_path)

    assert [(p.kind, p.first, p.end) for p in result.plan.pieces] == [
        ("head", 35, 42),
        ("mid", 42, 72),
        ("tail", 72, 81),
    ]
    assert check.ok, check.notes


def test_cut_starting_on_keyframe_does_not_reencode_the_head(mp4_source, tmp_path):
    """구간의 시작이 키프레임이면 머리 조각 없이 그 프레임부터 복사해야 한다.

    mp4, 프레임 42~80 (42는 키프레임)
    -> 조각 (mid, tail), 첫 조각이 프레임 42부터, check.ok
    """
    result, check = _cut(mp4_source, 42, 80, tmp_path)

    assert _kinds(result) == ("mid", "tail")
    assert result.plan.pieces[0].first == 42
    assert check.ok, check.notes


@pytest.mark.parametrize(
    ("first", "last"), [(45, 60), (42, 50)], ids=["inside-gop", "from-keyframe"]
)
def test_cut_inside_one_gop_reencodes_the_whole_range(mp4_source, tmp_path, first, last):
    """시작과 끝이 같은 GOP 안이면 구간 전체를 조각 하나로 재인코딩해야 한다.

    mp4, 프레임 45~60 · 42~50 (둘 다 키프레임 42와 72 사이)
    -> 조각 (whole,), check.ok
    """
    result, check = _cut(mp4_source, first, last, tmp_path)

    assert _kinds(result) == ("whole",)
    assert check.ok, check.notes


def test_cut_up_to_last_frame_passes_every_check(mp4_source, tmp_path):
    """구간의 끝이 파일의 마지막 프레임이면 꼬리를 끝까지 재인코딩하고 판정을 통과해야 한다.

    mp4, 프레임 75~119 (마지막 키프레임 90, 가운데 없음)
    -> 조각 (head 75~89, tail 90~119), check.ok
    """
    result, check = _cut(mp4_source, 75, 119, tmp_path)

    assert [(p.kind, p.first, p.end) for p in result.plan.pieces] == [
        ("head", 75, 90),
        ("tail", 90, 120),
    ]
    assert check.ok, check.notes


def test_cut_from_first_frame_passes_every_check(mp4_source, tmp_path):
    """구간이 파일의 첫 프레임에서 시작하면 탐색 없이 복사부터 시작하고 판정을 통과해야 한다.

    mp4, 프레임 0~50
    -> 조각 (mid 0~41, tail 42~50), check.ok
    """
    result, check = _cut(mp4_source, 0, 50, tmp_path)

    assert [(p.kind, p.first, p.end) for p in result.plan.pieces] == [
        ("mid", 0, 42),
        ("tail", 42, 51),
    ]
    assert check.ok, check.notes


# ================================================================ 컷 — fMP4


def test_cut_matches_coded_size_and_reorder_delay_of_fmp4_source(fmp4_source, tmp_path):
    """부호화 크기가 화면보다 큰 fMP4를 자르면 재인코딩 조각의 부호화 크기·크롭·재정렬 지연이 원본과 같아야 한다.

    fMP4(부호화 1280x736, 크롭 16, 지연 2), 프레임 10~75
    -> 머리·꼬리 조각의 coded (1280, 736) · crop (0, 0, 0, 16) · reorder_delay 2, check.ok
    """
    result, check = _cut(fmp4_source, 10, 75, tmp_path)

    assert _kinds(result) == ("head", "mid", "tail")
    for info in result.pieces:
        if info.piece.reencoded:
            assert (info.video.coded_width, info.video.coded_height) == (1280, 736)
            assert info.video.crop == (0, 0, 0, 16)
            assert info.video.reorder_delay == 2
    assert check.ok, check.notes


def test_cut_reads_input_that_starts_mid_vod(fmp4_parts, tmp_path):
    """VOD의 뒤쪽 세그먼트만 이어 붙인 입력에서도 요청한 프레임을 잘라 판정을 통과해야 한다.

    초기화 세그먼트 + 둘째~넷째 세그먼트(VOD의 1초부터), 프레임 5~70 (이 입력 안의 번호)
    -> input_start가 0보다 크고, check.ok
    """
    folder, init_bytes, media = fmp4_parts
    source = _fmp4_input(folder, "partial.mp4", init_bytes, media, skip=1)

    result, check = _cut(source, 5, 70, tmp_path)

    assert source[1].input_start > 0.9
    assert _kinds(result) == ("head", "mid", "tail")
    assert check.ok, check.notes


# ================================================================ 실패·정리


def test_cut_rejects_transport_stream(tmp_path):
    """입력이 MPEG-TS면 미지원 키로 CutError를 내야 한다.

    188바이트 패킷으로 된 TS 파일
    -> message_key == CUT_UNSUPPORTED
    """
    path = tmp_path / "segment.ts"
    path.write_bytes(build_ts([Frame(pts=3000, idr=True), Frame(pts=6000)]))

    with pytest.raises(CutError) as info:
        hybrid_cut(str(path), _synthetic_frames(), 0, 1, str(tmp_path / "out.mp4"))

    assert info.value.message_key == CUT_UNSUPPORTED


def test_cut_removes_temp_folder_after_success(mp4_source, tmp_path):
    """컷이 성공하면 중간 파일 폴더가 남지 않아야 한다.

    mp4, 프레임 35~80
    -> 산출물은 있고 cut_temp_dir_for(산출물) 폴더는 없음
    """
    result, _check = _cut(mp4_source, 35, 80, tmp_path)

    assert os.path.isfile(result.output_path)
    assert not os.path.exists(cut_temp_dir_for(result.output_path))


def test_cut_removes_temp_folder_and_output_after_failure(mp4_source, tmp_path, monkeypatch):
    """컷 도중 ffmpeg가 실패하면 실패 키로 CutError를 내고 중간 파일 폴더와 산출물을 남기지 않아야 한다.

    둘째 ffmpeg 실행(가운데 복사)이 종료 코드 1로 끝나도록 바꿈
    -> message_key == CUT_FAILED, 임시 폴더 없음, 산출물 없음
    """
    path, frames = mp4_source
    output = str(tmp_path / "out.mp4")
    real = cut_module.run_ffmpeg
    calls = []

    def flaky(args, **kwargs):
        calls.append(args)
        # 0번은 입력 읽기, 1번은 머리, 2번이 가운데 복사다
        if len(calls) == 3:
            return subprocess.CompletedProcess(args, 1, "", "boom")
        return real(args, **kwargs)

    monkeypatch.setattr(cut_module, "run_ffmpeg", flaky)

    with pytest.raises(CutError) as info:
        hybrid_cut(path, frames, 35, 80, output)

    assert info.value.message_key == CUT_FAILED
    assert not os.path.exists(cut_temp_dir_for(output))
    assert not os.path.exists(output)


def test_cut_reports_timeout_with_its_own_key(mp4_source, tmp_path, monkeypatch):
    """ffmpeg가 제한 시간을 넘기면 시간 초과 키로 CutError를 내야 한다.

    ffmpeg 실행이 FFmpegTimeoutError를 내도록 바꿈
    -> message_key == CUT_TIMEOUT
    """
    path, frames = mp4_source

    def slow(args, **kwargs):
        raise FFmpegTimeoutError("너무 오래 걸렸다")

    monkeypatch.setattr(cut_module, "run_ffmpeg", slow)

    with pytest.raises(CutError) as info:
        hybrid_cut(path, frames, 35, 80, str(tmp_path / "out.mp4"))

    assert info.value.message_key == CUT_TIMEOUT


def test_check_cut_needs_inspected_result(mp4_source, tmp_path):
    """check_cut은 조각 정보 없이 자른 결과를 받으면 ValueError를 내야 한다.

    hybrid_cut(inspect=False)의 결과
    -> ValueError
    """
    path, frames = mp4_source
    result = hybrid_cut(path, frames, 45, 60, str(tmp_path / "out.mp4"))

    with pytest.raises(ValueError):
        check_cut(frames, result)


# ================================================================ 계획 (ffmpeg 없음)


@pytest.mark.parametrize(
    ("first", "last", "expected"),
    [
        (1, 9, [("head", 1, 4), ("mid", 4, 8), ("tail", 8, 10)]),  # 양 끝이 키프레임이 아니다
        (4, 9, [("mid", 4, 8), ("tail", 8, 10)]),  # 시작이 키프레임
        (1, 6, [("head", 1, 4), ("tail", 4, 7)]),  # 복사할 GOP가 없다
        (5, 7, [("whole", 5, 8)]),  # 한 GOP 안
        (4, 6, [("whole", 4, 7)]),  # 키프레임에서 시작해 같은 GOP 안에서 끝난다
        (9, 11, [("whole", 9, 12)]),  # 뒤에 키프레임이 없다
        (4, 8, [("mid", 4, 8), ("tail", 8, 9)]),  # 끝이 키프레임 — 그 한 프레임이 꼬리다
    ],
)
def test_plan_cut_splits_range_at_keyframes(first, last, expected):
    """plan_cut은 구간을 키프레임 위치에 따라 머리·가운데·꼬리 또는 통째 조각으로 나눠야 한다.

    프레임 12개, 키프레임 (0, 4, 8)
    -> 주석의 경우마다 (종류, 첫 프레임, 끝 + 1) 목록
    """
    plan = plan_cut(_synthetic_frames(), first, last)

    assert [(p.kind, p.first, p.end) for p in plan.pieces] == expected


@pytest.mark.parametrize(("first", "last"), [(-1, 3), (5, 4), (0, 12)])
def test_plan_cut_rejects_range_outside_frames(first, last):
    """plan_cut은 프레임 번호가 범위를 벗어나거나 뒤집혔으면 ValueError를 내야 한다.

    프레임 12개, 구간 (−1, 3) · (5, 4) · (0, 12)
    -> ValueError
    """
    with pytest.raises(ValueError):
        plan_cut(_synthetic_frames(), first, last)


def test_plan_cut_rejects_range_before_first_keyframe():
    """plan_cut은 구간 시작보다 앞(또는 같은) 키프레임이 없으면 ValueError를 내야 한다.

    키프레임 (4, 8), 구간 1~6
    -> ValueError
    """
    with pytest.raises(ValueError):
        plan_cut(_synthetic_frames(keyframes=(4, 8)), 1, 6)


# ================================================================ 명령 (ffmpeg 없음)


def _option(args: list[str], name: str, nth: int = 0) -> str:
    """args에서 nth번째 name 옵션의 값."""
    positions = [i for i, arg in enumerate(args) if arg == name]
    return args[positions[nth] + 1]


def test_audio_command_starts_decoding_before_the_range():
    """오디오 명령은 구간 시작보다 50ms 앞에서 디코드를 시작하고 그 50ms를 버려야 한다.

    10fps 프레임 정보, 구간 프레임 5~9 (0.5~1.0초)
    -> 입력측 -ss 0.45, 출력측 -ss 0.05, -t 0.5
    """
    frames = _synthetic_frames()

    args = cut_module._audio_command("in.mp4", frames, plan_cut(frames, 5, 9), 192)

    assert float(_option(args, "-ss", 0)) == pytest.approx(0.45)
    assert args.index("-ss") < args.index("-i") < len(args) - args[::-1].index("-ss") - 1
    assert float(_option(args, "-ss", 1)) == pytest.approx(0.05)
    assert float(_option(args, "-t")) == pytest.approx(0.5)
    assert _option(args, "-b:a") == "192k"


def test_audio_command_does_not_seek_before_file_start():
    """오디오 명령은 구간이 파일의 시작이면 탐색하지 않아야 한다.

    구간 프레임 0~3 (0.0~0.4초)
    -> -ss 없음, -t 0.4
    """
    frames = _synthetic_frames()

    args = cut_module._audio_command("in.mp4", frames, plan_cut(frames, 0, 3), None)

    assert "-ss" not in args
    assert "-b:a" not in args
    assert float(_option(args, "-t")) == pytest.approx(0.4)


def test_copy_command_seeks_exactly_to_keyframe_and_stops_by_dts():
    """복사 명령은 키프레임 시각 그대로 탐색하고, 길이를 끝 키프레임의 DTS와 그 직전 DTS의 중간으로 줘야 한다.

    10fps, DTS = PTS − 0.1, 조각 mid 프레임 4~7 (끝 키프레임 8의 DTS 0.7, 직전 DTS 0.6)
    -> -ss 0.4, -t 0.25 (= 0.65 − 0.4)
    """
    frames = _synthetic_frames()
    piece = plan_cut(frames, 1, 9).pieces[1]

    args = cut_module._copy_command("in.mp4", frames, piece, "mid.mp4")

    assert float(_option(args, "-ss")) == pytest.approx(0.4)
    assert float(_option(args, "-t")) == pytest.approx(0.25)
    assert "-to" not in args


def test_encode_command_pads_to_coded_size_and_writes_crop(fmp4_source, tmp_path):
    """재인코딩 명령은 원본의 부호화 크기까지 화면을 늘리고 늘린 만큼 크롭을 적어야 한다.

    원본 부호화 1280x736 · 크롭 아래 16 · 재정렬 지연 2
    -> -vf pad=1280:736:0:0,fillborders=bottom=16:mode=smear · x264-params에 crop-rect=0,0,0,16과 b-pyramid=normal
    """
    path, frames = fmp4_source
    video = hybrid_cut(path, frames, 0, 5, str(tmp_path / "probe.mp4")).source.video

    args = cut_module._x264_args(video, frames.timescale)

    assert _option(args, "-vf") == "pad=1280:736:0:0,fillborders=bottom=16:mode=smear"
    assert _option(args, "-x264-params") == "b-pyramid=normal:crop-rect=0,0,0,16"


# ================================================================ 주변 함수


def test_cut_frames_from_mp4_reads_dts_per_frame():
    """cut_frames_from_mp4는 프레임마다 그 프레임의 DTS를 표시 순서로 돌려줘야 한다.

    합성 mp4: 디코드 순서 I P B B, DTS = 0 · 100 · 200 · 300틱, media_time 100, timescale 1000
    -> 표시 순서(샘플 0, 2, 3, 1)의 frame_dts = −0.1, 0.1, 0.2, 0.0초
    """
    frames = cut_frames_from_mp4(parse_moov(build_mp4([video_spec(), audio_spec()]).moov))

    assert frames.frame_dts[:4] == pytest.approx([-0.1, 0.1, 0.2, 0.0])
    assert frames.frame_duration == pytest.approx(0.1)
    assert (frames.audio_start, frames.audio_end) == pytest.approx((0.0, 1.92))


def test_cut_temp_dir_sits_next_to_the_output():
    """cut_temp_dir_for는 산출물과 같은 폴더에 산출물 이름에서 파생한 경로를 돌려줘야 한다.

    산출물 "<폴더>/방송 1080p_1.mp4"
    -> "<폴더>/CVDv2_cut_방송 1080p_1"
    """
    output = os.path.join("folder", "방송 1080p_1.mp4")

    assert cut_temp_dir_for(output) == os.path.join("folder", "CVDv2_cut_방송 1080p_1")


def test_run_ffmpeg_returns_exit_code_and_output():
    """run_ffmpeg는 ffmpeg가 끝나면 종료 코드와 출력을 돌려줘야 한다.

    인자 -version
    -> returncode 0, stdout이 "ffmpeg version"으로 시작
    """
    done = run_ffmpeg(["-version"], timeout=30)

    assert done.returncode == 0
    assert done.stdout.startswith("ffmpeg version")


def test_run_ffmpeg_raises_timeout_error_when_time_runs_out():
    """run_ffmpeg는 제한 시간을 넘기면 FFmpegTimeoutError를 내야 한다.

    1시간짜리 lavfi 입력을 실시간 속도(-re)로 읽기, 제한 0.5초
    -> FFmpegTimeoutError
    """
    with pytest.raises(FFmpegTimeoutError):
        run_ffmpeg(
            ["-re", "-f", "lavfi", "-i", "testsrc2=duration=3600", "-f", "null", "-"], timeout=0.5
        )


def test_mp4_source_frame_rate_is_thirty(mp4_source):
    """만든 mp4 입력의 프레임 길이는 1/30초여야 한다.

    testsrc2 rate=30
    -> frame_duration == 1/30
    """
    assert Fraction(mp4_source[1].frame_duration).limit_denominator(1000) == Fraction(1, 30)
