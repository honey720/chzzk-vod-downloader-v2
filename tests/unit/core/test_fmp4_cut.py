"""cut_fmp4_section — 받아 둔 fMP4 세그먼트에서 구간 파일을 바로 쓴다 (#309).

엔진을 거친 끝까지 경로(지금 방식과의 대조 · 재시도 · 세그먼트 지우기)는
tests/unit/core/test_m3u8_sections.py가 잰다. 여기서는 함수 하나의 계약을 잰다.

입력은 test_m3u8_sections.py의 plain과 같은 HLS다 — 320x240 · 30fps · 6초 · 1초 세그먼트 6개
(세그먼트마다 30프레임) · 키프레임 0·30·36·60·90·120·150.
loose는 키프레임 간격 45프레임을 시간으로 나눈(-hls_flags split_by_time) HLS다 — 세그먼트가
키프레임이 아닌 프레임으로 시작한다.
"""

import dataclasses
import os
import shutil
import subprocess

import pytest

import core.utils.fmp4_cut as fmp4_cut_module
from core.api.fmp4 import fmp4_origin, parse_media_segment
from core.models.cut import CutPiece
from core.models.fmp4_index import Fmp4Head
from core.models.plan import TimeRange
from core.utils.cut_check import check_cut
from core.utils.ffmpeg import extract_audio_stream, get_ffmpeg_exe, read_in_chunks
from core.utils.fmp4_cut import cut_fmp4_section, frame_at
from core.utils.fmp4_sections import Fmp4Section
from core.utils.hybrid_cut import CUT_FAILED, CUT_UNSUPPORTED, CutCancelled, CutError
from tests.unit.core.test_m3u8_sections import _contents, _make_hls, _packets, _Source

SEGMENT_FRAMES = 30  # 세그먼트 하나의 프레임 수 (1초 · 30fps)


def _make_loose_hls(folder) -> None:
    """키프레임 간격 45프레임을 1초마다 나눈 HLS — 둘째 세그먼트(프레임 30~)가 키프레임으로 시작하지 않는다."""
    done = subprocess.run(
        [get_ffmpeg_exe(), "-hide_banner", "-v", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=3",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
         "-c:a", "aac", "-ac", "2", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast",
         "-bf", "0", "-g", "45", "-keyint_min", "45", "-sc_threshold", "0",
         "-f", "hls", "-hls_time", "1", "-hls_list_size", "0", "-hls_segment_type", "fmp4",
         "-hls_flags", "split_by_time",
         "-hls_fmp4_init_filename", "init.mp4", "-hls_segment_filename", "seg-%03d.m4s",
         "media.m3u8"],
        cwd=str(folder),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )  # fmt: skip
    assert done.returncode == 0, done.stderr


@pytest.fixture(scope="module")
def made(tmp_path_factory) -> dict[str, tuple]:
    """이름 → (세그먼트 파일이 든 폴더, 그 HLS)."""
    plain = tmp_path_factory.mktemp("fmp4_cut_plain")
    _make_hls(plain, b_frames="3", pyramid="normal")
    loose = tmp_path_factory.mktemp("fmp4_cut_loose")
    _make_loose_hls(loose)
    return {"plain": (plain, _Source(plain)), "loose": (loose, _Source(loose))}


class _Cut:
    """구간 하나를 자르는 데 넘길 것들 — 세그먼트는 tmp_path에 복사해 쓴다(자르는 쪽이 지워도 되게)."""

    def __init__(self, made, tmp_path, first: int, last: int, segments: tuple[int, int]):
        folder, source = made
        self.source = source
        names = source.playlist.segments
        origin = fmp4_origin(source.init, parse_media_segment(source.files[names[0]], source.init))
        self.section = Fmp4Section(
            selection=TimeRange(source.time_of(first), source.time_of(last)),
            first_segment=segments[0],
            last_segment=segments[1],
            first_pts=source.time_of(first),
            last_pts=source.time_of(last),
            origin=origin,
        )
        self.head = Fmp4Head(
            playlist=source.playlist, init_data=source.files["init.mp4"], init=source.init
        )
        kept = tmp_path / "segments"
        kept.mkdir(parents=True, exist_ok=True)
        self.paths = []
        for index in range(segments[0], segments[1] + 1):
            self.paths.append(str(shutil.copy(folder / names[index], kept / names[index])))
        self.work = str(tmp_path / "work")
        self.output = str(tmp_path / "section.mp4")

    def run(self, **kwargs):
        return cut_fmp4_section(
            self.work, self.head, self.section, self.paths, self.output, **kwargs
        )


# ================================================================ 입력의 전제


def test_loose_hls_has_a_segment_that_does_not_start_with_a_keyframe(made):
    """loose의 둘째 세그먼트는 키프레임이 아닌 프레임으로 시작해야 한다.

    -g 45 · -hls_flags split_by_time · -hls_time 1
    -> 세그먼트가 둘 이상, 둘째 세그먼트의 첫 샘플이 sync_samples에 없다
    """
    _folder, source = made["loose"]
    names = source.playlist.segments

    second = parse_media_segment(source.files[names[1]], source.init)

    assert len(names) >= 2
    assert 0 not in second.video.sync_samples


# ================================================================ 조각 · 넘기기


def test_section_with_both_ends_inside_segments_pipes_only_the_segments_between(made, tmp_path):
    """양 끝이 세그먼트 중간인 구간은 그 사이의 세그먼트만 통째로 넘기고 양 끝은 조각으로 만들어야 한다.

    plain, 구간 프레임 40~100, 세그먼트 0~3(프레임 40은 세그먼트 1, 100은 세그먼트 3)
    -> on_fed == [2], 조각 == 머리 [40, 60) · 통째로 [60, 90) · 꼬리 [90, 101),
       단계 == read · audio · 0_head · 4_tail · join, 작업 폴더 없음
    """
    cut = _Cut(made["plain"], tmp_path, 40, 100, (0, 3))
    fed, stages = [], []

    result, _frames = cut.run(on_fed=fed.append, on_stage=lambda name, _s: stages.append(name))

    assert fed == [2]
    assert result.plan.pieces == (
        CutPiece("head", 40, 60),
        CutPiece("mid", 60, 90),
        CutPiece("tail", 90, 101),
    )
    assert stages == ["read", "audio", "0_head", "4_tail", "join"]
    assert not os.path.exists(cut.work)


def test_section_made_of_whole_segments_reencodes_nothing_and_keeps_the_original_packets(
    made, tmp_path
):
    """양 끝이 세그먼트의 처음 · 끝과 맞는 구간은 다시 만드는 조각 없이 원본의 영상 패킷 그대로여야 한다.

    plain, 구간 프레임 30~119(세그먼트 1~3 전체)
    -> on_fed == [0, 1, 2], 조각 == 통째로 하나(넘긴 세그먼트의 프레임 0~89), 단계 == read · audio · join,
       영상 패킷의 (크기, CRC) == 원본의 프레임 30~119의 것, 오디오를 넘는 길이 == 1024/48000초
    """
    cut = _Cut(made["plain"], tmp_path, 30, 119, (1, 3))
    fed, stages = [], []

    result, _frames = cut.run(on_fed=fed.append, on_stage=lambda name, _s: stages.append(name))

    assert fed == [0, 1, 2]
    assert result.plan.pieces == (CutPiece("mid", 0, 90),)
    assert stages == ["read", "audio", "join"]
    original = _contents(_packets(cut.source.joined)["v:0"])
    assert _contents(_packets(cut.output)["v:0"]) == original[30:120]
    assert result.audio_overhang == pytest.approx(1024 / 48000)


def test_section_ending_with_its_segment_copies_the_rest_of_the_head_segment(made, tmp_path):
    """세그먼트 하나 안에서 그 세그먼트의 끝까지인 구간은 경계 GOP만 다시 만들고 나머지 GOP는 복사해야 한다.

    plain, 구간 프레임 32~59, 세그먼트 1(프레임 30~59, 키프레임 30 · 36)
    -> 조각 == 머리 · 복사(넘긴 세그먼트의 프레임 [2, 6) · [6, 30)), 단계에 1_rest가 있다,
       on_fed가 불리지 않는다, 영상 패킷 28개
    """
    cut = _Cut(made["plain"], tmp_path, 32, 59, (1, 1))
    fed, stages = [], []

    result, _frames = cut.run(on_fed=fed.append, on_stage=lambda name, _s: stages.append(name))

    assert fed == []
    assert result.plan.pieces == (CutPiece("head", 2, 6), CutPiece("mid", 6, 30))
    assert stages == ["read", "audio", "0_head", "1_rest", "join"]
    assert len(_packets(cut.output)["v:0"]) == 28


def test_frame_at_rejects_a_time_that_is_not_a_frame(made, tmp_path):
    """받은 세그먼트에 그 PTS의 프레임이 없으면 CutError(CUT_FAILED)여야 한다.

    plain의 프레임 정보, 프레임 40과 41의 PTS 가운데 값
    -> 프레임 40의 PTS는 40, 가운데 값은 CutError(CUT_FAILED)
    """
    frames = made["plain"][1].frames
    between = (frames.frame_pts[40] + frames.frame_pts[41]) / 2

    assert frame_at(frames, frames.frame_pts[40]) == 40
    with pytest.raises(CutError) as caught:
        frame_at(frames, between)
    assert caught.value.message_key == CUT_FAILED


# ================================================================ 만들 수 없는 입력 · 실패


def test_segment_that_does_not_start_with_a_keyframe_is_unsupported(made, tmp_path):
    """이어지는 세그먼트가 키프레임으로 시작하지 않으면 CutError(CUT_UNSUPPORTED)이고 아무것도 남지 않아야 한다.

    loose, 구간 프레임 10~50, 세그먼트 0~1
    -> CutError(CUT_UNSUPPORTED), 구간 파일 없음, 작업 폴더 없음, 세그먼트 파일은 그대로
    """
    cut = _Cut(made["loose"], tmp_path, 10, 50, (0, 1))

    with pytest.raises(CutError) as caught:
        cut.run()

    assert caught.value.message_key == CUT_UNSUPPORTED
    assert not os.path.exists(cut.output)
    assert not os.path.exists(cut.work)
    assert all(os.path.exists(path) for path in cut.paths)


def test_dts_warning_from_the_joining_ffmpeg_fails_the_cut(made, tmp_path, monkeypatch):
    """잇는 ffmpeg가 DTS가 뒤로 간다고 경고하면 CutError(CUT_FAILED)이고 구간 파일이 남지 않아야 한다.

    plain, 구간 프레임 40~100. remux_through가 구간 파일을 다 쓴 뒤 경고 문구를 돌려주게 바꿈
    -> 경고를 돌려주기 전에 구간 파일이 있었다, CutError(CUT_FAILED), 구간 파일 없음
    """
    cut = _Cut(made["plain"], tmp_path, 40, 100, (0, 3))
    real = fmp4_cut_module.remux_through
    existed = []

    def warns(*args, **kwargs):
        real(*args, **kwargs)
        existed.append(os.path.exists(cut.output))
        return "[mp4 @ 0] Non-monotonic DTS in output stream 0:0; previous: 10, current: 5"

    monkeypatch.setattr(fmp4_cut_module, "remux_through", warns)

    with pytest.raises(CutError) as caught:
        cut.run()

    assert existed == [True]
    assert caught.value.message_key == CUT_FAILED
    assert not os.path.exists(cut.output)


# ================================================================ 중단 · 일시정지


def test_stop_while_feeding_segments_cancels_and_leaves_nothing(made, tmp_path):
    """세그먼트를 넘기는 도중에 멈추라는 요청이 오면 CutCancelled이고 구간 파일 · 작업 폴더가 남지 않아야 한다.

    plain, 구간 프레임 30~119(세그먼트 1~3을 통째로 넘긴다), 첫 세그먼트를 다 넘긴 뒤부터 should_stop이 참
    -> CutCancelled, on_fed == [0], 구간 파일 없음, 작업 폴더 없음
    """
    cut = _Cut(made["plain"], tmp_path, 30, 119, (1, 3))
    fed = []

    with pytest.raises(CutCancelled):
        cut.run(on_fed=fed.append, should_stop=lambda: bool(fed))

    assert fed == [0]
    assert not os.path.exists(cut.output)
    assert not os.path.exists(cut.work)


def test_pause_while_feeding_holds_and_then_makes_the_same_file(made, tmp_path, monkeypatch):
    """넘기는 도중 일시정지하면 그동안 넘기지 않고, 풀리면 일시정지 없이 만든 것과 같은 구간 파일을 만들어야 한다.

    plain, 구간 프레임 30~119. 첫 세그먼트를 다 넘긴 뒤 should_pause가 세 번 참(확인 간격 0.001초)
    -> 일시정지가 참인 동안 on_fed가 늘지 않았다, 끝난 뒤 on_fed == [0, 1, 2],
       영상 · 오디오 패킷 == 일시정지 없이 만든 것
    """
    monkeypatch.setattr(fmp4_cut_module, "_PAUSE_POLL_SECONDS", 0.001)
    plain = _Cut(made["plain"], tmp_path / "plain", 30, 119, (1, 3))
    plain.run()
    cut = _Cut(made["plain"], tmp_path / "paused", 30, 119, (1, 3))
    fed: list[int] = []
    seen_while_paused: list[int] = []

    def should_pause() -> bool:
        if fed and len(seen_while_paused) < 3:
            seen_while_paused.append(len(fed))
            return True
        return False

    cut.run(on_fed=fed.append, should_pause=should_pause)

    assert seen_while_paused == [1, 1, 1]
    assert fed == [0, 1, 2]
    assert _packets(cut.output) == _packets(plain.output)


# ================================================================ 오디오 통과


def test_extract_audio_stream_copies_the_audio_packets_and_drops_the_video(made, tmp_path):
    """extract_audio_stream은 넘긴 fMP4에서 오디오 패킷만 내용 그대로 옮겨야 한다.

    plain의 초기화 세그먼트 + 세그먼트 전부를 이은 파일을 조각으로 공급
    -> 산출물에 영상 스트림이 없다, 오디오 패킷의 (크기, CRC) == 이은 파일의 오디오 패킷의 것
    """
    _folder, source = made["plain"]
    out = str(tmp_path / "audio.m4a")

    extract_audio_stream(read_in_chunks(source.joined, 4096), out)

    probe = subprocess.run(
        [get_ffmpeg_exe(), "-hide_banner", "-i", out], capture_output=True, text=True
    )
    assert "Audio:" in probe.stderr and "Video:" not in probe.stderr
    done = subprocess.run(
        [get_ffmpeg_exe(), "-v", "error", "-i", out, "-map", "0:a:0", "-c", "copy",
         "-f", "framecrc", "-"],
        capture_output=True,
        text=True,
    )  # fmt: skip
    copied = [line for line in done.stdout.splitlines() if line and not line.startswith("#")]
    assert copied
    assert _contents(copied) == _contents(_packets(source.joined)["a:0"])


# ================================================================ 판정 (check_cut)


def test_section_made_of_whole_segments_passes_the_check_without_piece_information(made, tmp_path):
    """다시 만든 조각이 하나도 없는 구간 파일은 조각 정보 없이도 판정 다섯 항목을 통과해야 한다.

    plain, 구간 프레임 30~119(세그먼트 1~3 전체), inspect=True
    -> 컷 결과의 조각 정보가 비어 있다, check.ok
    """
    cut = _Cut(made["plain"], tmp_path, 30, 119, (1, 3))

    result, frames = cut.run(inspect=True)
    check = check_cut(frames, result)

    assert result.pieces == ()
    assert check.ok, check.notes


def test_check_accepts_copied_audio_ending_up_to_one_chunk_late_and_no_later(made, tmp_path):
    """오디오를 복사한 컷은 오디오가 구간 끝보다 한 덩어리까지 늦게 끝나도 A/V 판정을 통과하고, 그 허용이 없으면 통과하지 못해야 한다.

    plain, 구간 프레임 40~100. 판정에 넘기는 프레임 정보의 한 프레임 길이를 0.1ms로 줄여
    1프레임 허용을 없앤다(오디오가 늦는 양은 그대로다)
    -> 구간 파일의 오디오가 영상보다 1ms 넘게 늦게 끝난다,
       audio_overhang(1024/48000초) 그대로면 av_sync 통과, 0으로 바꾸면 av_sync 실패
    """
    cut = _Cut(made["plain"], tmp_path, 40, 100, (0, 3))
    result, frames = cut.run(inspect=True)
    packets = _packets(cut.output)
    video_end = max(int(line.split(",")[2]) + int(line.split(",")[3]) for line in packets["v:0"])
    audio_last = packets["a:0"][-1].split(",")
    audio_end = int(audio_last[2]) + int(audio_last[3])
    late = audio_end / 48000 - video_end / frames.timescale
    tight = dataclasses.replace(frames, frame_duration=0.0001)

    with_overhang = check_cut(tight, result)
    without = check_cut(tight, dataclasses.replace(result, audio_overhang=0.0))

    assert 0.001 < late <= result.audio_overhang
    assert with_overhang.av_sync, with_overhang.notes
    assert not without.av_sync, without.notes


# ================================================================ 읽기 · 오디오 통과의 진행


def test_reading_the_segments_reports_the_share_read_after_each_segment(made, tmp_path):
    """프레임 정보를 읽는 동안 on_read는 세그먼트를 하나 읽을 때마다 읽은 비율로 불려야 한다.

    plain, 구간 프레임 40~100, 세그먼트 0~3(넷)
    -> on_read == [0.25, 0.5, 0.75, 1.0], 그 호출은 모두 read 단계가 끝나기 전이다
    """
    cut = _Cut(made["plain"], tmp_path, 40, 100, (0, 3))
    events = []

    cut.run(on_read=events.append, on_stage=lambda name, _s: events.append(name))

    assert events[: events.index("read")] == [0.25, 0.5, 0.75, 1.0]
    assert not [event for event in events[events.index("read") :] if isinstance(event, float)]


def test_audio_pass_raises_the_progress_once_per_segment_before_it_ends(made, tmp_path):
    """오디오 통과는 세그먼트를 하나 넘길 때마다 진행을 올려야 한다 — 단계가 끝날 때 한 번이 아니다.

    plain, 구간 프레임 40~100, 세그먼트 0~3(넷)
    -> read 단계와 audio 단계의 끝 사이에 온 진행이 다섯(세그먼트 넷 + 단계의 끝), 값이 오르기만 한다
    """
    cut = _Cut(made["plain"], tmp_path, 40, 100, (0, 3))
    events = []

    cut.run(on_progress=events.append, on_stage=lambda name, _s: events.append(name))

    during = events[events.index("read") + 1 : events.index("audio")]
    assert len(during) == len(cut.paths) + 1
    assert during == sorted(set(during))
