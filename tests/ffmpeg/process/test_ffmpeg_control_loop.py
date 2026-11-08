"""출력을 모으는 제어 실행(`_run_controlled`)의 루프가 일시정지 중에도 계속 도는지 (#533).

CI windows에서 일시정지 중 `communicate(timeout)` 안의 `Thread.join(timeout)`이 돌아오지 않아 루프가
서고 재개 · 중지 · 제한 시간이 모두 듣지 않았다(까닭은 미확정, 로컬 재현 없음). 루프는 자기 읽기
스레드 + `process.wait(timeout)`으로 바뀌었다. 이 파일은 그 루프의 계약을 잰다 — 일시정지 중 확인
(`_Control.poll`)이 시간에 따라 계속 불리고, 일시정지 중 중지가 듣고, 일시정지가 끝난 뒤에도 제한
시간이 듣고, 파이프가 찰 만큼 출력이 커도 멈추지 않는다. 실제 ffmpeg를 쓴다. 기다림은 고정 대기가
아니라 조건 대기(마감 시각까지 짧게 되풀이해 확인)다.
"""

import subprocess
import threading
import time
from collections.abc import Callable, Iterator

import pytest

import core.utils.ffmpeg as ffmpeg_module
from core.utils.ffmpeg import FFmpegCancelledError, FFmpegTimeoutError, run_ffmpeg
from tests.support.long_ffmpeg import end_all, record_processes

WAIT_LIMIT = 60.0  # 조건 대기의 마감(초) — 넘기면 멈춘 것으로 본다
POLLS_WHILE_PAUSED = 20  # 일시정지 중 더 불려야 하는 확인 횟수 — 0.05초 간격이면 약 1초


def _work(output: str, seconds: int = 20) -> list[str]:
    """몇 초 동안 CPU로 일하며 output을 써 나가는 명령 — 합성 영상을 한 스레드로 인코딩한다."""
    return [
        "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=30:duration={seconds}",
        "-c:v", "libx264", "-preset", "medium", "-threads", "1", "-f", "mp4", output,
    ]  # fmt: skip


def _wait_until(condition: Callable[[], bool], what: str) -> None:
    """condition이 참이 될 때까지 짧게 되풀이해 본다 — 마감을 넘기면 실패한다."""
    deadline = time.monotonic() + WAIT_LIMIT
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail(f"{WAIT_LIMIT:g}초 안에 {what}")
        time.sleep(0.02)


def _working(launched: list[subprocess.Popen], output: str) -> subprocess.Popen | None:
    """output을 쓰는 프로세스(일하는 명령) — 아직 안 떴으면 None. 실행 파일 확인 등 다른 프로세스는 거른다."""
    return next((p for p in launched if output in p.args), None)


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[subprocess.Popen]]:
    """ffmpeg 모듈이 띄운 프로세스들 — 끝날 때 살아 있는 것은 그 객체로 끝낸다."""
    processes = record_processes(monkeypatch)
    yield processes
    end_all(processes)


@pytest.fixture
def polls(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """`_Control.poll`이 불린 시각들 — 동작은 그대로다."""
    called: list[float] = []
    original = ffmpeg_module._Control.poll

    def poll(self: ffmpeg_module._Control) -> str | None:
        called.append(time.monotonic())
        return original(self)

    monkeypatch.setattr(ffmpeg_module._Control, "poll", poll)
    return called


class _Run:
    """`run_ffmpeg`를 다른 스레드에서 돌리고 결과 · 예외를 적는다."""

    def __init__(self, args: list[str], **kwargs: object) -> None:
        self.result: subprocess.CompletedProcess[str] | None = None
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, args=(args,), kwargs=kwargs, daemon=True)
        self.thread.start()

    def _run(self, args: list[str], **kwargs: object) -> None:
        try:
            self.result = run_ffmpeg(args, **kwargs)
        except BaseException as e:  # noqa: BLE001 — 시험 스레드는 무엇이든 적어 둔다
            self.error = e

    def finished(self) -> bool:
        return not self.thread.is_alive()


def test_the_loop_keeps_polling_while_paused(
    tmp_path: pytest.TempPathFactory, launched: list[subprocess.Popen], polls: list[float]
) -> None:
    """일시정지 중에도 확인(_Control.poll)이 계속 불리고, 풀면 끝까지 돌아 성공해야 한다.

    일하는 명령 · 일시정지 켬 -> 켠 뒤 확인이 20번 더 불림 -> 일시정지 끔 -> returncode 0
    """
    paused = threading.Event()
    output = str(tmp_path / "out.mp4")
    run = _Run(_work(output), timeout=300, should_pause=paused.is_set)
    _wait_until(lambda: _working(launched, output) is not None, "일하는 ffmpeg가 뜨지 않았다")
    paused.set()
    start = len(polls)
    _wait_until(
        lambda: len(polls) >= start + POLLS_WHILE_PAUSED, "일시정지 중 확인이 더 불리지 않았다"
    )
    assert not run.finished(), "전제: 일시정지 중에는 끝나지 않는다"
    paused.clear()
    _wait_until(run.finished, "일시정지를 푼 뒤 끝나지 않았다")
    assert run.error is None
    assert run.result is not None and run.result.returncode == 0


def test_a_stop_during_pause_ends_the_run(
    tmp_path: pytest.TempPathFactory, launched: list[subprocess.Popen], polls: list[float]
) -> None:
    """일시정지 중 중지를 요청하면 FFmpegCancelledError로 끝나고 프로세스가 남지 않아야 한다."""
    paused, stopped = threading.Event(), threading.Event()
    output = str(tmp_path / "out.mp4")
    run = _Run(_work(output), timeout=300, should_pause=paused.is_set, should_stop=stopped.is_set)
    _wait_until(lambda: _working(launched, output) is not None, "일하는 ffmpeg가 뜨지 않았다")
    paused.set()
    _wait_until(lambda: len(polls) >= 2, "확인이 불리지 않았다")
    start = len(polls)
    _wait_until(lambda: len(polls) > start, "일시정지 뒤 확인이 불리지 않았다")
    stopped.set()
    _wait_until(run.finished, "일시정지 중 중지가 듣지 않았다")
    assert isinstance(run.error, FFmpegCancelledError)
    assert _working(launched, output).poll() is not None


def test_the_timeout_still_applies_after_a_pause(
    tmp_path: pytest.TempPathFactory, launched: list[subprocess.Popen]
) -> None:
    """일시정지했다 풀어도 제한 시간은 듣고, 끝나기까지 걸린 시간에는 일시정지한 시간이 더해져야 한다.

    제한 2초 · 일하는 명령(20초 분량) · 뜨자마자 1초 일시정지 -> FFmpegTimeoutError, 걸린 시간 ≥ 2 + 1초
    """
    paused = threading.Event()
    output = str(tmp_path / "out.mp4")
    run = _Run(_work(output), timeout=2, should_pause=paused.is_set)
    _wait_until(lambda: _working(launched, output) is not None, "일하는 ffmpeg가 뜨지 않았다")
    begun = time.monotonic()
    paused.set()
    held = time.monotonic()
    _wait_until(lambda: time.monotonic() - held >= 1.0, "1초가 지나지 않았다")
    paused.clear()
    _wait_until(run.finished, "제한 시간이 지나도 끝나지 않았다")
    assert isinstance(run.error, FFmpegTimeoutError)
    # 제한 시간은 프로세스를 띄운 직후부터 센다 — 뜬 것을 본 시각(begun)이 조금 늦을 수 있어 0.2초를 덜어 준다
    assert time.monotonic() - begun >= 2.0 + 1.0 - 0.2


def test_a_large_output_does_not_stall_the_run(launched: list[subprocess.Popen]) -> None:
    """파이프 버퍼보다 훨씬 큰 출력을 내도 제어 실행이 끝나고, 제어 없는 실행과 같은 출력이어야 한다.

    합성 영상 프레임마다 한 줄(framecrc, 수 MB) -> 제어 실행(should_stop 줌)의 stdout == 제어 없는 실행의 stdout
    """
    args = [
        "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=1000:duration=60",
        "-f", "framecrc", "-",
    ]  # fmt: skip
    plain = run_ffmpeg(args, timeout=300)
    assert len(plain.stdout) > 1024 * 1024, "전제: 파이프 버퍼(수십 KB)보다 훨씬 큰 출력"
    run = _Run(args, timeout=300, should_stop=lambda: False)
    _wait_until(run.finished, "큰 출력에서 끝나지 않았다")
    assert run.error is None
    assert run.result is not None and run.result.returncode == 0
    assert run.result.stdout == plain.stdout
