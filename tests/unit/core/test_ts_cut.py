"""암호화 TS 구간의 끝까지 경로 — 계획 → 받기·복호화·검사 → 구간 파일 쓰기 → 판정 (#309).

실제 ffmpeg(imageio-ffmpeg 동봉)로 6초짜리 영상을 HLS MPEG-TS로 만들고, 세그먼트를
AES-128-CBC로 암호화해 tests/unit/core/range_host.py로 내준다. 저장소에 영상 파일을 두지
않는다. 엔진(다운로더)은 쓰지 않는다 — 모듈들을 엔진이 부를 순서대로 부른다.

입력: 320x240 · 30fps · 6초 · 1초 세그먼트 · 키프레임 15프레임마다 · B프레임(재정렬 지연 1) ·
오디오 48kHz AAC. 영상이 오디오보다 늦게 시작한다 — 그 차이가 1ms의 배수가 아니라서, 다시 싼
mp4의 영상 시각이 TS와 어긋난다.

핵심 계약:
- 세그먼트 경계를 걸치고 양 끝이 키프레임이 아닌 구간이 판정 다섯 항목을 통과한다
- 구간 파일의 영상 패킷은 세그먼트 전부를 이어 다시 싼 뒤 하이브리드 컷으로 자른 것과 같고,
  오디오 패킷은 원본 그대로다. 구간의 끝이 세그먼트의 끝이면 마지막 GOP는 원본 그대로다
- 경계 세그먼트를 다시 싼 mp4의 시각이 TS와 어긋나도 구간 계획이 고른 프레임을 자른다
  (프레임 번호로 잇는다). 다시 싼 mp4의 프레임이 TS와 다르면 자르지 않고 실패한다
- 이어지는 세그먼트가 키프레임으로 시작하지 않으면 자르지 않고 실패한다
"""

import math
import os
import subprocess
from dataclasses import replace

import pytest
from Crypto.Cipher import AES

import core.api.hls_ts as hls_ts_module
import core.utils.ts_cut as ts_cut_module
from core.api.hls import parse_media_playlist
from core.api.hls_ts import fetch_ts_head, segment_streams, ts_segment_file_name
from core.downloaders.decrypt import sequence_iv
from core.models.plan import TimeRange
from core.utils.cut_check import check_cut
from core.utils.ffmpeg import get_ffmpeg_exe, run_ffmpeg
from core.utils.hybrid_cut import CUT_FAILED, CUT_UNSUPPORTED, CutCancelled, CutError
from core.utils.ts_cut import cut_ts_section, ts_frame_number
from core.utils.ts_sections import TsSectionSource, choose_ts_frame_rate, plan_ts_sections
from tests.unit.core.range_host import RangeHost
from tests.unit.core.section_packets import JoinedSource, assert_section_matches

KEY = bytes.fromhex("5b0e7c91a2d34f6880b1c3e5f7092a4d")  # 테스트용 키 — 실제 키가 아니다
FPS = 30
KEY_EVERY = 15  # 키프레임 간격(프레임)
SEGMENT_FRAMES = 30  # 세그먼트 하나의 프레임 수(1초)
SELECTION = TimeRange(0.8, 2.3)  # 세그먼트 0 · 1 · 2에 걸치고, 양 끝이 키프레임이 아니다
# 첫 영상 프레임의 VOD 시각(초) — 영상을 2048표본(0.042667초) 늦췄고 AAC 인코더가 오디오를
# 1024표본 늦춘다. VOD의 0초는 가장 이른 것(오디오)이다
LEAD = 1024 / 48000


def _ffmpeg(*args: str, cwd=None) -> subprocess.CompletedProcess:
    done = subprocess.run(
        [get_ffmpeg_exe(), "-hide_banner", "-v", "error", "-y", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert done.returncode == 0, done.stderr
    return done


class _Vod:
    """만든 HLS 하나 — 암호화해 내줄 파일들과, 복호화 전의 세그먼트(대조용)."""

    def __init__(self, folder):
        _ffmpeg(
            "-itsoffset", "0.042667", "-f", "lavfi",
            "-i", f"testsrc2=size=320x240:rate={FPS}:duration=6",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6",
            "-c:a", "aac", "-ac", "2", "-pix_fmt", "yuv420p", "-c:v", "libx264",
            "-preset", "veryfast", "-bf", "2",
            "-force_key_frames", f"expr:gte(n,n_forced*{KEY_EVERY})",
            "-x264-params", "b-pyramid=none:keyint=300:min-keyint=1:scenecut=0",
            "-f", "hls", "-hls_time", "1", "-hls_list_size", "0",
            "-hls_segment_filename", "segment-%06d.ts", "media.m3u8",
            cwd=str(folder),
        )  # fmt: skip
        text = (folder / "media.m3u8").read_text(encoding="utf-8")
        self.playlist = parse_media_playlist(text)
        self.plain = [(folder / name).read_bytes() for name in self.playlist.segments]
        keyed = text.replace(
            "#EXTINF", '#EXT-X-KEY:METHOD=AES-128,URI="https://key.test/k"\n#EXTINF', 1
        )
        self.files = {"vod/media.m3u8": keyed.encode("utf-8")}
        for number, (name, plain) in enumerate(zip(self.playlist.segments, self.plain)):
            pad = 16 - len(plain) % 16
            cipher = AES.new(KEY, AES.MODE_CBC, sequence_iv(number))
            self.files[f"vod/{name}"] = cipher.encrypt(plain + bytes([pad]) * pad)
        self.folder = folder


@pytest.fixture(scope="module")
def vod(tmp_path_factory) -> _Vod:
    """ffmpeg로 만든 6초짜리 HLS 하나 — 모듈의 테스트가 함께 쓴다."""
    return _Vod(tmp_path_factory.mktemp("hls_ts"))


@pytest.fixture
def host(vod, monkeypatch) -> RangeHost:
    """vod의 플레이리스트와 암호화한 세그먼트를 내주는 호스트 — 모듈의 요청이 이 호스트로 간다."""
    served = RangeHost(vod.files)
    monkeypatch.setattr(hls_ts_module, "get_thread_session", served.session)
    return served


class _Planned:
    """구간 하나를 계획하고 그 세그먼트를 받아 둔 상태 — 엔진이 컷을 부르기 직전이다."""

    def __init__(self, host: RangeHost, folder, selection: TimeRange = SELECTION):
        url = host.url("vod/media.m3u8")
        self.head = fetch_ts_head(url, str(folder))

        def segment_at(index: int):
            return segment_streams(self.head, url, index, KEY)

        self.fps = choose_ts_frame_rate([segment_at(0)]).rate
        self.section = plan_ts_sections(self.head.playlist, [selection], segment_at, self.fps)[0]
        indexes = range(self.section.first_segment, self.section.last_segment + 1)
        for index in indexes:
            segment_at(index)  # 계획이 읽지 않은 가운데 세그먼트도 받는다
        count = len(self.head.playlist.segments)
        self.paths = [os.path.join(str(folder), ts_segment_file_name(count, i)) for i in indexes]
        self.segments = [segment_at(index) for index in indexes]
        source = TsSectionSource(self.head.playlist, segment_at, self.fps)
        self.ts_frames = source.frames_of(
            self.section.first_segment, self.section.last_segment, self.section.origin
        )

    def cut(self, work_dir, output_path: str, **options):
        """계획한 구간을 그대로 자른다 — 엔진이 부르는 것과 같은 인자다."""
        return cut_ts_section(
            str(work_dir),
            self.segments,
            self.ts_frames,
            self.paths,
            output_path,
            self.section.first_pts,
            self.section.last_pts,
            **options,
        )


def _decoded(path: str) -> list[str]:
    """디코드한 영상 프레임마다의 해시 — 표시 순서.

    제품의 실행 함수(run_ffmpeg)로 돌린다 — 리눅스 동봉 ffmpeg는 가드(#97) 없이 TS를 읽으면
    죽는다.
    """
    done = run_ffmpeg(
        ["-v", "error", "-i", path, "-map", "0:v:0", "-fps_mode", "passthrough",
         "-f", "framemd5", "-"],
        timeout=120,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    return [line.rsplit(",", 1)[-1].strip() for line in done.stdout.splitlines() if line[0] != "#"]


def _nearest_frame(seconds: float, lead: float) -> int:
    """seconds에 가장 가까운 프레임의 번호 — 프레임은 lead + n ÷ 30초에 있다."""
    return round((seconds - lead) * FPS)


def _frame_time(number: int) -> float:
    """영상 전체에서 number번째 프레임의 VOD 시각(초) — 입력을 만든 값으로 센다."""
    return LEAD + number / FPS


@pytest.fixture(scope="module")
def joined(vod, tmp_path_factory) -> JoinedSource:
    """복호화 전의 세그먼트 전부를 이어 다시 싼 mp4 — 패킷 대조의 원본."""
    return JoinedSource(vod.plain, tmp_path_factory.mktemp("ts_joined"))


# ================================================================ 끝까지


def test_generated_vod_has_video_starting_off_the_millisecond_grid(vod, host, tmp_path):
    """만든 입력은 영상이 오디오보다 늦게 시작하고 그 차이가 1ms의 배수가 아니어야 한다.

    만든 HLS의 세그먼트 0 ~ 2
    -> 세그먼트 여섯 개 이상, 첫 영상 프레임의 VOD 시각이 0보다 크고 1ms 격자에서 0.1ms 넘게 벗어난다,
       키프레임이 15프레임마다 있다
    """
    planned = _Planned(host, tmp_path / "segments")
    lead = planned.ts_frames.frame_pts[0]

    assert len(vod.playlist.segments) >= 6
    assert lead > 0
    assert lead * 1000 - math.floor(lead * 1000) > 0.1
    assert planned.ts_frames.keyframes[:4] == (0, 15, 30, 45)


def test_section_across_segments_is_cut_and_passes_every_check(vod, host, tmp_path):
    """세그먼트 경계를 걸치고 양 끝이 키프레임이 아닌 구간을 자르면 판정 다섯 항목을 모두 통과해야 한다.

    구간 0.8 ~ 2.3초. 계획 → 받기·복호화·검사 → 구간 파일 쓰기
    -> 세그먼트 0 ~ 2, 조각 (head, mid, tail) — 가운데 조각은 통째로 넘긴 세그먼트 1(프레임 30 ~ 59)이고
       넘겼다고 알린 자리 == [1], 결과의 audio_overhang == 1024 ÷ 48000초, check.ok,
       자른 프레임 == 구간 계획이 고른 프레임,
       세그먼트 요청은 구간의 세그먼트 0 ~ 2와 영상 길이를 재는 마지막 세그먼트뿐이고
       세그먼트마다 1건 · Range 머리 없음, 작업 폴더가 지워졌다
    """
    planned = _Planned(host, tmp_path / "segments")
    lead = planned.ts_frames.frame_pts[0]
    first = _nearest_frame(SELECTION.start, lead)
    last = _nearest_frame(SELECTION.end, lead)
    work = tmp_path / "work"
    fed = []

    result, frames = planned.cut(work, str(tmp_path / "out.mp4"), inspect=True, on_fed=fed.append)
    check = check_cut(frames, result)

    assert (planned.section.first_segment, planned.section.last_segment) == (0, 2)
    assert first % KEY_EVERY and last % KEY_EVERY  # 양 끝이 키프레임이 아니다
    assert first < SEGMENT_FRAMES and last >= 2 * SEGMENT_FRAMES  # 세그먼트 경계를 둘 넘는다
    pieces = result.plan.pieces
    assert tuple(piece.kind for piece in pieces) == ("head", "mid", "tail")
    assert (pieces[1].first, pieces[1].end) == (SEGMENT_FRAMES, 2 * SEGMENT_FRAMES)
    assert fed == [1]
    assert result.audio_overhang == pytest.approx(1024 / 48000)  # AAC 덩어리 하나의 길이
    assert check.ok, check.notes
    assert (result.plan.first, result.plan.last) == (first, last)
    segment_requests = [(n, h) for _m, n, h in host.requests if n.endswith(".ts")]
    last_segment = len(vod.playlist.segments) - 1
    assert sorted(segment_requests) == [
        (f"vod/segment-{n:06d}.ts", None) for n in (0, 1, 2, last_segment)
    ]
    assert not os.path.exists(work)


def test_copied_frames_are_the_frames_the_plan_picked(vod, host, tmp_path):
    """구간 파일의 통째로 넘긴 조각은 원본 TS에서 구간 계획이 고른 번호의 프레임과 화소가 같아야 한다.

    구간 0.8 ~ 2.3초의 구간 파일과, 복호화 전의 세그먼트 0 ~ 2를 이은 TS를 각각 디코드
    -> 넘긴 조각(프레임 30 ~ 59번)의 프레임 해시가 원본의 같은 번호와 모두 같다
    """
    planned = _Planned(host, tmp_path / "segments")
    output = str(tmp_path / "out.mp4")
    source = tmp_path / "source.ts"
    source.write_bytes(b"".join(vod.plain[:3]))

    result, _frames = planned.cut(tmp_path / "work", output)
    original, cut = _decoded(str(source)), _decoded(output)
    middle = next(piece for piece in result.plan.pieces if piece.kind == "mid")

    assert (middle.first, middle.end) == (30, 60)
    assert len(cut) == result.plan.last - result.plan.first + 1
    for number in range(middle.first, middle.end):
        assert cut[number - result.plan.first] == original[number]


@pytest.mark.parametrize(
    ("first", "last", "kinds", "original_tail"),
    [
        (23, 68, ("head", "mid", "tail"), False),
        (5, 80, ("head", "mid", "mid", "mid", "tail"), False),
        (30, 68, ("mid", "tail"), False),
        (23, 89, ("head", "mid"), True),
        (30, 89, ("mid",), True),
        (35, 55, ("head", "tail"), False),
        (35, 70, ("head", "mid", "tail"), False),
    ],
    ids=[
        "양 끝이 세그먼트 중간",
        "경계 세그먼트에 복사할 GOP가 있다",
        "시작이 세그먼트 경계",
        "끝이 세그먼트 경계",
        "양 끝이 세그먼트 경계",
        "세그먼트 하나 안",
        "세그먼트 둘",
    ],
)
def test_section_file_has_the_video_packets_of_the_hybrid_cut_and_the_original_audio_packets(
    vod, host, joined, tmp_path, first, last, kinds, original_tail
):
    """구간 파일의 영상 패킷은 전부 이어 다시 싼 파일에서 자른 것과 같고, 오디오 패킷은 원본 그대로여야 한다.

    영상 전체에서 센 프레임 first ~ last(세그먼트는 30프레임 · 키프레임 15프레임마다)
    -> 조각의 종류 == kinds, 영상 패킷(시각 · 내용) == 하이브리드 컷 대조
       (끝이 세그먼트 끝이면 마지막 GOP 앞까지 같고 마지막 GOP는 원본 패킷 그대로 · PTS는 대조와 같다),
       오디오 패킷 == 원본에서 구간 시작에 걸친 덩어리 ~ 끝에 걸친 덩어리, check.ok
    """
    start, end = _frame_time(first), _frame_time(last + 1)
    # 그 프레임들의 시각 바로 앞 — 구간 계획이 고르는 프레임이 first · last가 된다
    selection = TimeRange(start - 0.001, end - 1 / FPS - 0.001)
    planned = _Planned(host, tmp_path / "segments", selection)
    output = str(tmp_path / "out.mp4")

    result, frames = planned.cut(tmp_path / "work", output, inspect=True)

    base = planned.section.first_segment * SEGMENT_FRAMES
    assert (result.plan.first + base, result.plan.last + base) == (first, last)
    assert tuple(piece.kind for piece in result.plan.pieces) == kinds
    assert_section_matches(joined, output, first, last, start, end, original_tail)
    check = check_cut(frames, result)
    assert check.ok, check.notes


def test_remuxed_mp4_is_off_the_ts_time_by_the_truncated_lead(vod, host, tmp_path):
    """다시 싼 mp4는 영상 앞의 빈 구간을 1ms 단위로 내림해 적어, 영상 시각이 TS보다 1ms 미만으로 앞서야 한다.

    세그먼트 0 ~ 2를 다시 싼 mp4의 프레임 시각과 TS의 프레임 시각(VOD 시각 — 0초가 가장 이른 PTS)
    -> 프레임 수 · 키프레임 목록이 같다, mp4의 첫 프레임 시각 == TS의 첫 프레임 시각을 1ms 단위로 내림한 값,
       (TS − mp4)가 모든 프레임에서 같고 0.1ms보다 크다
    """
    planned = _Planned(host, tmp_path / "segments")
    ts_frames = planned.ts_frames
    joined = str(tmp_path / "joined.mp4")

    frames = ts_cut_module._remux(planned.paths, joined)
    offsets = [ts - mp4 for ts, mp4 in zip(ts_frames.frame_pts, frames.frame_pts)]

    assert len(frames.frame_pts) == len(ts_frames.frame_pts) == 3 * SEGMENT_FRAMES
    assert tuple(frames.keyframes) == tuple(ts_frames.keyframes)
    lead = ts_frames.frame_pts[0]
    assert frames.frame_pts[0] == pytest.approx(math.floor(lead * 1000) / 1000, abs=2 / 90_000)
    assert max(offsets) - min(offsets) <= 2 / 90_000
    assert 1e-4 < offsets[0] < 1e-3


# ================================================================ 프레임이 다르면 자르지 않는다


@pytest.mark.parametrize(
    ("difference", "key"),
    [("frame-count", CUT_UNSUPPORTED), ("keyframes", CUT_FAILED), ("spacing", CUT_FAILED)],
)
def test_cut_fails_when_the_frames_of_a_boundary_segment_differ_from_the_ts_frames(
    vod, host, tmp_path, difference, key
):
    """받은 세그먼트의 프레임이 TS에서 읽은 프레임 정보와 다르면 자르지 않고 실패해야 한다.

    구간 0.8 ~ 1.4초(세그먼트 0 · 1). TS 쪽 프레임 정보를 다르게 준다 —
    frame-count: 세그먼트 0 ~ 2의 것(세그먼트는 0 · 1만 넘긴다) / keyframes: 세그먼트 0의 키프레임 하나를
    옆 프레임으로 / spacing: 세그먼트 0의 뒤쪽 절반의 시각을 한 프레임씩 늦춤
    -> CutError — frame-count는 CUT_UNSUPPORTED(프레임 수가 맞지 않는다),
       나머지는 CUT_FAILED(다시 싼 경계 세그먼트가 TS와 다르다). 산출물 · 작업 폴더 없음
    """
    planned = _Planned(host, tmp_path / "segments", TimeRange(0.8, 1.4))
    ts_frames = planned.ts_frames
    assert len(planned.paths) == 2
    if difference == "frame-count":
        wider = _Planned(host, tmp_path / "wider", TimeRange(0.8, 2.3))
        ts_frames = wider.ts_frames
    elif difference == "keyframes":
        keys = list(ts_frames.keyframes)
        assert keys[1] == KEY_EVERY  # 세그먼트 0의 둘째 키프레임
        keys[1] += 1
        ts_frames = replace(ts_frames, keyframes=tuple(keys))
    else:
        shifted = tuple(
            pts + (1 / FPS if KEY_EVERY <= number < SEGMENT_FRAMES else 0.0)
            for number, pts in enumerate(ts_frames.frame_pts)
        )
        ts_frames = replace(ts_frames, frame_pts=shifted)
    output, work = str(tmp_path / "out.mp4"), tmp_path / "work"

    with pytest.raises(CutError) as info:
        cut_ts_section(
            str(work),
            planned.segments,
            ts_frames,
            planned.paths,
            output,
            ts_frames.frame_pts[23],
            ts_frames.frame_pts[40],
        )

    assert info.value.message_key == key
    assert not os.path.exists(output)
    assert not os.path.exists(work)


def test_boundary_segment_is_checked_against_its_own_frames_not_the_first_segments(
    vod, host, tmp_path
):
    """경계 세그먼트가 넘긴 첫 세그먼트가 아닐 때도, 다시 싼 mp4는 그 세그먼트의 프레임 정보와 견줘야 한다.

    구간 프레임 35 ~ 70(넘기는 세그먼트 0 ~ 2, 머리 조각은 세그먼트 1에서 만든다). TS 쪽 프레임
    정보에서 세그먼트 1의 키프레임 45를 46으로 바꿔 넘김(세그먼트 0의 것은 그대로다)
    -> CutError(CUT_FAILED), 산출물 · 작업 폴더 없음
    """
    selection = TimeRange(_frame_time(35) - 0.001, _frame_time(70) - 0.001)
    planned = _Planned(host, tmp_path / "segments", selection)
    assert (planned.section.first_segment, planned.section.last_segment) == (0, 2)
    keys = list(planned.ts_frames.keyframes)
    assert keys[:4] == [0, 15, 30, 45]
    keys[3] += 1
    ts_frames = replace(planned.ts_frames, keyframes=tuple(keys))
    output, work = str(tmp_path / "out.mp4"), tmp_path / "work"

    with pytest.raises(CutError) as info:
        cut_ts_section(
            str(work),
            planned.segments,
            ts_frames,
            planned.paths,
            output,
            planned.section.first_pts,
            planned.section.last_pts,
        )

    assert info.value.message_key == CUT_FAILED
    assert not os.path.exists(output)
    assert not os.path.exists(work)


def test_segment_that_does_not_start_with_a_keyframe_is_unsupported(vod, host, tmp_path):
    """이어지는 세그먼트가 키프레임으로 시작하지 않으면 자르지 않고 CUT_UNSUPPORTED로 실패해야 한다.

    구간 0.8 ~ 2.3초(세그먼트 0 ~ 2). 세그먼트 1을 읽은 정보의 키프레임을 (0, 15)에서 (3, 15)로 바꿔 넘김
    -> CutError(CUT_UNSUPPORTED), 산출물 · 작업 폴더 없음. 바꾸지 않으면 같은 인자로 잘린다
    """
    planned = _Planned(host, tmp_path / "segments")
    assert planned.segments[1].video_keyframes == (0, KEY_EVERY)
    loose = [*planned.segments]
    loose[1] = replace(loose[1], video_keyframes=(3, KEY_EVERY))
    output, work = str(tmp_path / "out.mp4"), tmp_path / "work"

    with pytest.raises(CutError) as info:
        cut_ts_section(
            str(work),
            loose,
            planned.ts_frames,
            planned.paths,
            output,
            planned.section.first_pts,
            planned.section.last_pts,
        )

    assert info.value.message_key == CUT_UNSUPPORTED
    assert not os.path.exists(output)
    assert not os.path.exists(work)
    planned.cut(work, output)
    assert os.path.getsize(output) > 0


def test_stop_request_cancels_the_cut_and_leaves_nothing(vod, host, tmp_path):
    """멈추라는 요청이 와 있으면 컷은 CutCancelled로 그만두고 산출물과 작업 폴더를 남기지 않아야 한다.

    구간 0.8 ~ 2.3초, should_stop이 늘 참
    -> CutCancelled, 산출물 · 작업 폴더 없음, 세그먼트 파일은 그대로 있다
    """
    planned = _Planned(host, tmp_path / "segments")
    output, work = str(tmp_path / "out.mp4"), tmp_path / "work"

    with pytest.raises(CutCancelled):
        planned.cut(work, output, should_stop=lambda: True)

    assert not os.path.exists(output)
    assert not os.path.exists(work)
    assert all(os.path.exists(path) for path in planned.paths)


def test_frame_number_of_a_pts_that_is_not_a_frame_is_an_error(vod, host, tmp_path):
    """구간 계획이 정한 PTS가 받은 세그먼트의 프레임이 아니면 번호를 정하지 못하고 실패해야 한다.

    세그먼트 0 ~ 2의 프레임 정보. 23번 프레임의 PTS와, 그보다 반 프레임 뒤의 시각
    -> 23 / CutError(CUT_FAILED)
    """
    ts_frames = _Planned(host, tmp_path / "segments").ts_frames

    assert ts_frame_number(ts_frames, ts_frames.frame_pts[23]) == 23
    with pytest.raises(CutError) as info:
        ts_frame_number(ts_frames, ts_frames.frame_pts[23] + 0.5 / FPS)
    assert info.value.message_key == CUT_FAILED
