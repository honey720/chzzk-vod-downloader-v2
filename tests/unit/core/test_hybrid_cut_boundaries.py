"""하이브리드 컷의 경계 전수 — 구간의 시작·끝이 키프레임 둘레에 놓이는 모든 조합 (#309).

재인코딩 조각이 1~2프레임뿐인 구간에서 컷이 실패하거나 조각의 재정렬 지연이 원본과
어긋났다 — libx264는 프레임 수가 재정렬 지연 이하인 조각에 DTS 지연을 주지 않는다.
조각의 길이마다 따로 재지 않고, 시작과 끝을 키프레임 기준 -4~+4프레임으로 조합해
전부 자르고 판정한다.

입력: 160x120 · 30fps · 4초 · 키프레임 0·30·36·60·90 (30~36은 6프레임짜리 짧은 GOP).
재정렬 지연이 1프레임인 것과 2프레임인 것 둘이다.

시작은 키프레임 30 둘레(26~34), 끝은 키프레임 36 둘레(32~40)다. 이 조합에 머리 1~4프레임,
꼬리 1~5프레임, 통째 1~9프레임, 가운데가 있는 것·없는 것, 머리 없이 복사로 시작하는 것이
모두 들어 있다.
"""

import subprocess

import pytest

from core.api.mp4 import read_mp4_index
from core.models.cut import CutFrames
from core.utils.cut_check import check_cut
from core.utils.ffmpeg import get_ffmpeg_exe
from core.utils.hybrid_cut import CutError, cut_frames_from_mp4, hybrid_cut, plan_cut

KEYFRAMES = (0, 30, 36, 60, 90)  # -force_key_frames 0,1,1.2,2,3 (30fps)
START_KEY = 30  # 구간의 시작을 이 키프레임 둘레에 둔다
END_KEY = 36  # 구간의 끝을 이 키프레임 둘레에 둔다 — 30~36은 짧은 GOP다
OFFSETS = range(-4, 5)  # 키프레임 기준 프레임 수
ITEMS = ("params", "decode", "seams", "start_end", "av_sync")

# 재정렬 지연(프레임) → 그 지연이 나오는 x264 설정 (-bf, b-pyramid)
ENCODER_SETTINGS = {1: ("2", "none"), 2: ("3", "normal")}


@pytest.fixture(scope="module", params=sorted(ENCODER_SETTINGS), ids=["delay1", "delay2"])
def source(request, tmp_path_factory) -> tuple[int, str, CutFrames]:
    """(재정렬 지연, 만든 mp4의 경로, 그 프레임 정보)."""
    delay = request.param
    b_frames, pyramid = ENCODER_SETTINGS[delay]
    path = str(tmp_path_factory.mktemp(f"cut_boundaries_{delay}") / "source.mp4")
    done = subprocess.run(
        [get_ffmpeg_exe(), "-hide_banner", "-v", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=30:duration=4",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4",
         "-c:a", "aac", "-ac", "2", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast",
         "-bf", b_frames, "-force_key_frames", "0,1,1.2,2,3",
         "-x264-params", f"b-pyramid={pyramid}:keyint=300:min-keyint=1:scenecut=0",
         path],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )  # fmt: skip
    assert done.returncode == 0, done.stderr

    def read(offset: int, size: int) -> bytes:
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read(size)

    return delay, path, cut_frames_from_mp4(read_mp4_index(read))


def test_generated_source_has_the_wanted_reorder_delay_and_short_gop(source):
    """만든 입력은 요청한 재정렬 지연과 짧은 GOP를 가져야 한다.

    -bf 2 · b-pyramid=none(지연 1) 또는 -bf 3 · b-pyramid=normal(지연 2), 키프레임 0·30·36·60·90
    -> 키프레임 30의 (PTS − DTS) == 지연 × 한 프레임, keyframes == (0, 30, 36, 60, 90)
    """
    delay, _path, frames = source

    lead = frames.frame_pts[START_KEY] - frames.frame_dts[START_KEY]

    assert frames.keyframes == KEYFRAMES
    assert lead == pytest.approx(delay * frames.frame_duration)


@pytest.mark.parametrize("start", OFFSETS, ids=[f"start{offset:+d}" for offset in OFFSETS])
def test_cut_passes_every_check_for_each_end_around_a_keyframe(source, tmp_path, start):
    """시작이 키프레임 30에서 start프레임 떨어진 구간은 끝이 키프레임 36의 -4~+4프레임 어디든 판정을 통과해야 한다.

    시작 프레임 30 + start, 끝 프레임 32~40 (시작보다 앞인 끝은 뺀다)
    -> 모든 조합에서 hybrid_cut이 성공하고 check_cut 다섯 항목이 통과한다
    """
    _delay, path, frames = source
    first = START_KEY + start
    failures = []

    for end in OFFSETS:
        last = END_KEY + end
        if last < first:
            continue
        pieces = "+".join(
            f"{p.kind}{p.end - p.first}" for p in plan_cut(frames, first, last).pieces
        )
        try:
            result = hybrid_cut(path, frames, first, last, str(tmp_path / "out.mp4"), inspect=True)
            check = check_cut(frames, result)
            bad = {item: check.notes[item] for item in ITEMS if not getattr(check, item)}
        except CutError as e:
            bad = {"cut": str(e)}
        if bad:
            failures.append(f"프레임 {first}~{last} ({pieces}): {bad}")

    assert not failures, "\n".join(failures)
