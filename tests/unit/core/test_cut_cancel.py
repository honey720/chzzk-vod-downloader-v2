"""다운로드를 중단하면 컷의 ffmpeg가 바로 끝나는지 (#309).

컷 하나는 ffmpeg를 여러 번 띄운다(입력 읽기 · 조각마다 · 오디오 · 잇기). 중단이 구간 사이에서만
확인되면 도는 컷이 끝까지 돌아, 긴 구간에서는 앱이 워커를 기다리다 포기한다. 멈추라는 요청
(``should_stop``)이 어느 단계의 ffmpeg에도 닿아야 한다.

실제 ffmpeg를 쓴다. 단계가 도는 **도중**에 멈추는 것을 재려고, 재려는 단계의 명령을 오래 도는
명령(실시간 속도로 읽는 10분짜리 합성 입력)으로 바꿔 띄운다 — 멈추지 않으면 10분을 돈다.
테스트가 띄운 프로세스는 그 객체로만 확인하고 끝낸다(이름으로 찾지 않는다).
"""

import os
import threading
import time

import pytest

import core.utils.hybrid_cut as cut_module
from core.utils.ffmpeg import FFmpegCancelledError, run_ffmpeg
import core.utils.ts_cut as ts_cut_module
from core.utils.hybrid_cut import CutCancelled, hybrid_cut
from core.utils.paths import cut_temp_dir_for
from tests.unit.core.long_ffmpeg import LONG_RUNNING, end_all, record_processes
from tests.unit.core.test_hybrid_cut import mp4_source  # noqa: F401 — 모듈 범위 픽스처

STOP_AFTER = 0.4  # 프로세스를 띄운 뒤 멈추라고 하기까지(초) — 프로세스가 돌고 있는 동안이다
# 멈추라고 한 뒤 호출이 돌아오기까지의 상한(초). 제품의 확인 간격은 0.05초다 — 프로세스를 끝내고
# 파이프를 닫는 시간과 느린 러너를 넉넉히 본 값이고, 앱이 워커를 기다리는 2초보다 짧다
RETURN_WITHIN = 1.5


@pytest.fixture
def launched(monkeypatch):
    """ffmpeg 모듈이 띄운 프로세스들 — 테스트가 끝날 때 살아 있는 것이 있으면 그 객체로 끝낸다."""
    processes = record_processes(monkeypatch)
    yield processes
    end_all(processes)


class _Stop:
    """멈추라는 요청 — 켠 시각과, 켤 때 마지막 프로세스가 돌고 있었는지를 적는다."""

    def __init__(self, processes: list):
        self._processes = processes
        self._event = threading.Event()
        self.at = 0.0
        self.running_then = False

    def arm(self) -> None:
        threading.Timer(STOP_AFTER, self._set).start()

    def _set(self) -> None:
        self.running_then = bool(self._processes) and self._processes[-1].poll() is None
        self.at = time.perf_counter()
        self._event.set()
        # 제품이 멈추지 못하면 10분을 돈다 — 상한을 넘기면 테스트가 띄운 프로세스를 직접 끝내
        # 호출이 돌아오게 한다(그때는 걸린 시간의 단언이 실패한다)
        guard = threading.Timer(RETURN_WITHIN + 2.0, end_all, [self._processes])
        guard.daemon = True
        guard.start()

    def __call__(self) -> bool:
        return self._event.is_set()


@pytest.mark.parametrize("reporting", [False, True], ids=["출력을 모으는 실행", "진행을 받는 실행"])
def test_run_ffmpeg_ends_the_process_when_told_to_stop(launched, reporting):
    """도는 ffmpeg는 멈추라는 요청이 오면 바로 끝나고, 호출은 취소 예외로 돌아와야 한다.

    멈추지 않으면 10분을 도는 명령을 띄우고 0.4초 뒤 should_stop이 참을 돌려주게 함
    (출력을 모으는 실행 / 진행을 받는 실행)
    -> FFmpegCancelledError, 요청 뒤 1.5초 안에 돌아온다, 요청 때 프로세스는 돌고 있었고 지금은 끝났다
    """
    stop = _Stop(launched)
    extra = {"on_out_time": lambda seconds: None} if reporting else {}
    stop.arm()

    with pytest.raises(FFmpegCancelledError):
        run_ffmpeg(LONG_RUNNING, timeout=120, should_stop=stop, **extra)
    returned = time.perf_counter()

    assert stop.running_then, "전제: 멈추라고 할 때 프로세스가 돌고 있었다"
    assert returned - stop.at < RETURN_WITHIN
    # 실행 파일을 처음 찾을 때의 확인 실행이 하나 더 있을 수 있다 — 띄운 것이 모두 끝났는지 본다
    assert launched and all(process.poll() is not None for process in launched)


def test_a_stop_check_that_never_fires_changes_nothing_about_the_result(launched):
    """멈춤 확인을 줘도 멈추라는 요청이 없으면 결과(종료 코드 · 출력)는 주지 않았을 때와 같아야 한다.

    합성 영상 0.2초의 패킷 목록(framecrc)을 내는 명령을 멈춤 확인 없이 / 늘 거짓인 확인과 함께 실행
    -> 종료 코드 0, 두 실행의 stdout이 같고 비어 있지 않다
    """
    args = [
        "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=30:duration=0.2",
        "-f", "framecrc", "-",
    ]  # fmt: skip

    plain = run_ffmpeg(args, timeout=60)
    watched = run_ffmpeg(args, timeout=60, should_stop=lambda: False)

    assert (plain.returncode, watched.returncode) == (0, 0)
    assert watched.stdout == plain.stdout and plain.stdout.strip()


# 구간(프레임 35~80)의 컷이 띄우는 단계와, 그 단계의 명령에만 있는 글 — 머리 재인코딩 · 가운데 복사 ·
# 끝 재인코딩 · 오디오 · 잇기. 입력 읽기(probe)는 "trace_headers"로 가린다
STAGES = {
    "probe": "trace_headers",
    "0_head": "0_head.mp4",
    "1_mid": "1_mid.mp4",
    "2_tail": "2_tail.mp4",
    "audio": "audio.m4a",
    "mux": "list.txt",
}


@pytest.mark.parametrize("stage", list(STAGES))
def test_cut_stops_in_the_middle_of_any_stage(mp4_source, tmp_path, monkeypatch, launched, stage):  # noqa: F811
    """컷의 어느 단계가 도는 도중에 멈추라고 해도 그 ffmpeg가 바로 끝나고 아무것도 남지 않아야 한다.

    mp4, 프레임 35~80. 표의 단계(입력 읽기 · 머리 · 가운데 · 끝 · 오디오 · 잇기)의 명령을 10분 도는
    명령으로 바꿔 띄우고 0.4초 뒤 멈추라고 함
    -> CutCancelled, 요청 뒤 1.5초 안에 돌아온다, 요청 때 그 단계의 프로세스는 돌고 있었다
    -> 띄운 프로세스가 모두 끝났다, 산출물 자리에 쓰다 만 파일이 없다, 중간 파일 폴더 없음
    """
    path, frames = mp4_source
    output = str(tmp_path / "out.mp4")
    stop = _Stop(launched)
    marker = STAGES[stage]
    real = cut_module.run_ffmpeg
    swapped = []

    def slowed(args, **kwargs):
        if any(marker in str(arg) for arg in args) and not swapped:
            swapped.append(stage)
            kwargs.pop("on_out_time", None)  # 바꿔 띄운 명령은 진행을 내지 않는다
            if stage != "probe":  # 입력 읽기는 산출물 자리를 건드리기 전이다
                with open(output, "wb") as partial:
                    partial.write(b"partial")  # 쓰다 만 산출물 — 바꿔 띄운 명령은 쓰지 않는다
            stop.arm()
            return real(LONG_RUNNING, **kwargs)
        return real(args, **kwargs)

    monkeypatch.setattr(cut_module, "run_ffmpeg", slowed)

    with pytest.raises(CutCancelled):
        hybrid_cut(path, frames, 35, 80, output, should_stop=stop)
    returned = time.perf_counter()

    assert swapped == [stage], "전제: 그 단계의 명령을 바꿔 띄웠다"
    assert stop.running_then, "전제: 멈추라고 할 때 그 단계의 프로세스가 돌고 있었다"
    assert returned - stop.at < RETURN_WITHIN
    assert all(process.poll() is not None for process in launched)
    assert not os.path.exists(output)
    assert not os.path.exists(cut_temp_dir_for(output))


def test_cut_told_to_stop_before_it_starts_launches_nothing(mp4_source, tmp_path, launched):  # noqa: F811
    """이미 멈추라는 요청이 와 있으면 컷은 ffmpeg를 하나도 띄우지 않고 그만둬야 한다.

    should_stop이 처음부터 참 -> CutCancelled, 띄운 프로세스 0개, 산출물 없음
    """
    path, frames = mp4_source
    output = str(tmp_path / "out.mp4")

    with pytest.raises(CutCancelled):
        hybrid_cut(path, frames, 35, 80, output, should_stop=lambda: True)

    assert launched == []
    assert not os.path.exists(output)


def test_rewrapping_ts_segments_stops_when_told_to(tmp_path, launched):
    """TS 세그먼트를 mp4로 다시 싸는 도중 멈추라고 하면 그만두고 쓰다 만 파일을 남기지 않아야 한다.

    세그먼트 파일 둘(내용은 아무 바이트). should_stop이 둘째 조각을 흘려 넣기 전에 참이 됨
    -> CutCancelled, 띄운 ffmpeg가 끝났다, 다시 싼 파일이 없다
    """
    segments = []
    for number in range(2):
        path = tmp_path / f"{number}.ts"
        path.write_bytes(bytes(188) * 4)
        segments.append(str(path))
    joined = str(tmp_path / "joined.mp4")
    asked = []

    def should_stop() -> bool:
        asked.append(1)
        return len(asked) > 1

    with pytest.raises(CutCancelled):
        ts_cut_module._remux(segments, joined, should_stop)

    assert launched and all(process.poll() is not None for process in launched)
    assert not os.path.exists(joined)
