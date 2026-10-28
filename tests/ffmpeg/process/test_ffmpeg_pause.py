"""일시정지하면 도는 ffmpeg가 서고, 재개하면 멈춘 자리에서 이어 가는지 (#309).

구간을 자르는 도중 일시정지해도 도는 컷이 끝까지 돌면, 누른 사람에게는 일시정지가 듣지 않은
것으로 보인다(구간이 하나면 그대로 완료된다). 일시정지는 ffmpeg 프로세스를 OS 수준에서 멈추는
것으로 옮긴다(``core/utils/ffmpeg.py``의 ``_set_suspended`` — Windows는 NtSuspendProcess,
그 밖은 SIGSTOP). 이 파일의 테스트는 OS마다 그 OS의 길을 탄다 — 3-OS CI가 세 길을 모두 돈다.

실제 ffmpeg를 쓴다. "서 있다"는 **CPU로 일하는 명령**(합성 영상을 한 스레드로 인코딩해 파일에
쓴다)의 출력 파일이 자라지 않는 것으로 잰다 — 실시간 속도로 읽는 명령은 다시 돌 때 밀린 만큼을
한꺼번에 따라잡아 서 있었는지 가릴 수 없다. 테스트가 띄운 프로세스는 그 객체로만 다룬다.
"""

import os
import subprocess
import sys
import threading
import time
import traceback
from typing import Any

import pytest

import core.utils.ffmpeg as ffmpeg_module
import core.cutting.hybrid as cut_module
from core.utils.ffmpeg import FFmpegCancelledError, run_ffmpeg
import core.cutting.ts_cut as ts_cut_module
from core.cutting.hybrid import CutCancelled, CutError
from core.utils.paths import cut_temp_dir_for
from tests.support.long_ffmpeg import (
    LONG_RUNNING,
    end_all,
    is_long_running,
    record_processes,
)
from tests.ffmpeg.reference_cut import hybrid_cut
from tests.ffmpeg.cut.test_hybrid_cut import mp4_source  # noqa: F401 — 모듈 범위 픽스처

PAUSE_AFTER = 0.5  # 프로세스를 띄운 뒤 일시정지하기까지(초) — 일하는 명령이 도는 동안이다
PAUSE_FOR = 1.5  # 일시정지해 두는 시간(초)
# 감시 시간 = max(WATCH_FLOOR, WATCH_FACTOR × 혼자 돈 시간 D + WATCH_SLACK) — 넘기면 멈춘 것으로 보고
# 단서를 남긴다 (#458). 정상 경로가 가장 긴 것은 일시정지 시간 테스트다: 띄운 뒤 0.5초 + 일시정지
# (3 × D + 3초) + 일. 바쁜 러너에서 일이 2 × D 걸리면 0.5 + (3 × D + 3) + 2 × D ≈ 5 × D + 3.5초 —
# 감시 시간은 거기서 D + 6.5초 이상 남는다. D가 짧아도 120초 아래로 두지 않는다(오탐이면 멈춤과
# 가를 수 없다)
WATCH_FACTOR = 6
WATCH_SLACK = 10.0  # 초
WATCH_FLOOR = 120.0  # 감시 시간의 바닥(초)
WATCH_EVERY = 60.0  # 감시 동안 모든 스레드 스택을 남기는 주기(초)


def _work(output: str) -> list[str]:
    """몇 초 동안 CPU로 일하며 output을 써 나가는 명령 — 합성 영상 20초를 한 스레드로 인코딩한다.

    한 스레드라 결과가 실행마다 같다. 어느 러너에서도 ``PAUSE_AFTER``보다 오래 돈다.
    """
    return [
        "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=20",
        "-c:v", "libx264", "-preset", "medium", "-threads", "1", "-f", "mp4", output,
    ]  # fmt: skip


def _size(path: str) -> int:
    return os.path.getsize(path) if os.path.exists(path) else -1


@pytest.fixture
def launch_hooks() -> list:
    """프로세스가 뜰 때마다 ``(명령, 프로세스)``로 불릴 함수들 — 테스트가 넣는다."""
    return []


@pytest.fixture
def launched(monkeypatch, launch_hooks):
    """ffmpeg 모듈이 띄운 프로세스들 — 끝날 때 살아 있는 것은 다시 돌린 뒤 그 객체로 끝낸다."""

    def on_launch(command, process) -> None:
        for hook in list(launch_hooks):
            hook(command, process)

    processes = record_processes(monkeypatch, on_launch)
    yield processes
    end_all(processes)


class _Pause:
    """일시정지 요청 — 켜고 끄는 시각을 정해 두고, 켜져 있는 동안의 관측을 적는다."""

    def __init__(self):
        self._event = threading.Event()
        self.observed: dict[str, object] = {}

    def schedule(self, after: float, lasting: float, observe=None) -> None:
        """after초 뒤 켜고 lasting초 뒤 끈다. observe는 켜진 동안 두 번(조금 뒤 · 끄기 직전) 부른다."""
        threading.Timer(after, self._event.set).start()
        if observe is not None:
            threading.Timer(after + 0.4, lambda: observe("early")).start()
            threading.Timer(after + lasting - 0.1, lambda: observe("late")).start()
        threading.Timer(after + lasting, self._event.clear).start()

    def schedule_from_launch(
        self, launch_hooks: list, output: str, after: float, lasting: float, observe=None
    ) -> None:
        """output을 쓰는 프로세스가 뜬 것을 확인한 때부터 ``schedule``을 센다 — 처음 뜬 하나에만."""
        seen: list = []

        def on_launch(command, process) -> None:
            if output in command and not seen:
                seen.append(process)
                self.schedule(after, lasting, observe)

        launch_hooks.append(on_launch)

    def __call__(self) -> bool:
        return self._event.is_set()


def _all_stacks() -> str:
    """지금 모든 스레드의 이름과 스택 — faulthandler와 달리 캡처된 출력 · 실패 메시지에 넣을 수 있다."""
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    return "\n".join(
        f"--- 스레드 {names.get(ident, '?')} ({ident})\n" + "".join(traceback.format_stack(frame))
        for ident, frame in sys._current_frames().items()
    )


def _suspended_threads(pid: int) -> str:
    """그 프로세스의 일시중단 상태 — Windows는 Suspended 스레드 수, 리눅스는 멈춘(T) 스레드 수, macOS는 상태 글."""
    try:
        if sys.platform == "win32":
            script = (
                f"$t = (Get-Process -Id {pid} -ErrorAction Stop).Threads; "
                "\"suspended=$(@($t | Where-Object { $_.WaitReason -eq 'Suspended' }).Count)/$($t.Count)\""
            )
            command = ["powershell", "-NoProfile", "-Command", script]
        elif sys.platform.startswith("linux"):
            tasks = os.listdir(f"/proc/{pid}/task")
            stopped = 0
            for task in tasks:
                with open(f"/proc/{pid}/task/{task}/stat", encoding="utf-8") as stat:
                    stopped += stat.read().rsplit(")", 1)[1].split()[0] in ("T", "t")
            return f"stopped={stopped}/{len(tasks)}"
        else:
            command = ["ps", "-o", "stat=", "-p", str(pid)]
        done = subprocess.run(command, capture_output=True, text=True, timeout=20)
        return (done.stdout.strip() or done.stderr.strip()) or "(빈 출력)"
    except (OSError, subprocess.SubprocessError, IndexError) as e:
        return f"(읽지 못함: {e})"


def _process_state(processes: list[subprocess.Popen]) -> str:
    """띄운 프로세스마다 PID · 살아 있는지 · 일시중단 상태."""
    lines = []
    for process in processes:
        alive = process.poll() is None
        line = f"pid={process.pid} alive={alive} returncode={process.returncode}"
        if alive:
            line += f" {_suspended_threads(process.pid)}"
        lines.append(line)
    return "\n".join(lines) or "(띄운 프로세스 없음)"


def _watch_limit(alone: float) -> float:
    """혼자 돈 시간 alone(초)에서 감시 시간을 낸다 — max(``WATCH_FLOOR``, ``WATCH_FACTOR`` × alone + ``WATCH_SLACK``)."""
    return max(WATCH_FLOOR, WATCH_FACTOR * alone + WATCH_SLACK)


def _run_watched(
    limit: float, launched: list[subprocess.Popen], *args: Any, **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    """``run_ffmpeg``를 다른 스레드로 돌리고 limit초 안에 돌아오는지 지킨다 (#458).

    그동안 ``WATCH_EVERY``초마다 모든 스레드 스택을 표준 오류에 남긴다(실패하면 캡처된 출력으로
    보인다). limit을 넘기면 모든 스레드 스택과 띄운 ffmpeg의 상태를 실패 메시지에 담고, ffmpeg를
    끝낸 뒤 실패한다 — CI 잡 시간 제한까지 아무 말 없이 서 있지 않게 한다. 돌아오면 그 결과를
    돌려주고, 예외로 끝났으면 그 예외를 그대로 낸다.
    """
    outcome: dict[str, subprocess.CompletedProcess[str] | BaseException] = {}

    def work() -> None:
        try:
            outcome["done"] = run_ffmpeg(*args, **kwargs)
        except BaseException as e:  # noqa: BLE001 — 테스트 스레드에서 그대로 다시 낸다
            outcome["error"] = e

    worker = threading.Thread(target=work, name="감시 대상 run_ffmpeg", daemon=True)
    started = time.monotonic()
    worker.start()
    while True:
        elapsed = time.monotonic() - started
        worker.join(max(0.0, min(WATCH_EVERY, limit - elapsed)))
        elapsed = time.monotonic() - started
        if not worker.is_alive():
            break
        if elapsed >= limit:
            stacks, state = _all_stacks(), _process_state(launched)
            end_all(launched)  # 멈춘 것은 다시 돌린 뒤 끝낸다 — 감시 대상이 풀려 돌아올 수 있게
            worker.join(10.0)
            pytest.fail(
                f"run_ffmpeg가 {limit:.1f}초 안에 돌아오지 않았다(멈춤으로 본다)\n"
                f"## 띄운 ffmpeg(끝내기 전)\n{state}\n## 모든 스레드(끝내기 전)\n{stacks}",
                pytrace=False,
            )
        print(
            f"[감시] {elapsed:.0f}초 / {limit:.0f}초 — 모든 스레드\n{_all_stacks()}",
            file=sys.stderr,
        )
    if "error" in outcome:
        raise outcome["error"]
    return outcome["done"]


@pytest.mark.parametrize("reporting", [False, True], ids=["출력을 모으는 실행", "진행을 받는 실행"])
def test_a_paused_ffmpeg_stands_still_and_writes_the_same_file_when_resumed(
    tmp_path, launched, launch_hooks, reporting
):
    """일시정지 중인 ffmpeg는 살아 있되 아무것도 쓰지 않고, 재개하면 일시정지 없이 돈 것과 같은 파일을 내야 한다.

    일하는 명령을 일시정지 없이 한 번(기준), 띄운 지 0.5초 뒤부터 1.5초 동안 일시정지하며 한 번
    -> 일시정지 중 두 시점(0.4초 뒤 · 끝나기 0.1초 전)에 프로세스가 살아 있고 출력 파일 크기가 같다
    -> 진행을 받는 실행: 그 두 시점 사이에 진행 알림이 없다
    -> 종료 코드 0, 걸린 시간 >= 1.5초, 결과 파일의 바이트 == 기준 파일의 바이트
    -> 일시정지하는 실행이 max(120초, 6 × D + 10초) 안에 돌아온다 — D는 기준이 걸린 시간(넘기면 스택 · ffmpeg 상태를 남기고 실패, #458)
    """
    plain, paused_output = str(tmp_path / "plain.mp4"), str(tmp_path / "paused.mp4")
    began = time.perf_counter()
    assert run_ffmpeg(_work(plain), timeout=300).returncode == 0
    alone = time.perf_counter() - began
    pause = _Pause()
    reports: list[float] = []
    extra = (
        {"on_out_time": lambda seconds: reports.append(time.perf_counter())} if reporting else {}
    )

    def observe(name: str) -> None:
        pause.observed[name] = (
            launched[-1].poll() is None,
            _size(paused_output),
            time.perf_counter(),
        )

    started = time.perf_counter()
    pause.schedule_from_launch(launch_hooks, paused_output, PAUSE_AFTER, PAUSE_FOR, observe)
    # 제한 시간은 일하는 시간만 센다(멈춰 있던 시간은 빼고) — 재개가 듣지 않으면 여기서 끝난다
    done = _run_watched(
        _watch_limit(alone),
        launched,
        _work(paused_output),
        timeout=90,
        should_pause=pause,
        **extra,
    )
    took = time.perf_counter() - started

    (alive_early, size_early, at_early), (alive_late, size_late, at_late) = (
        pause.observed["early"],
        pause.observed["late"],
    )
    assert alive_early and alive_late, (
        "전제: 일시정지 중에 프로세스가 살아 있다(아직 끝나지 않았다)"
    )
    assert size_early == size_late and size_early > 0
    assert [at for at in reports if at_early < at < at_late] == []
    assert done.returncode == 0 and took >= PAUSE_FOR
    with open(plain, "rb") as wanted, open(paused_output, "rb") as made:
        assert made.read() == wanted.read()


@pytest.mark.parametrize("reporting", [False, True], ids=["출력을 모으는 실행", "진행을 받는 실행"])
def test_time_spent_paused_does_not_count_towards_the_timeout(
    tmp_path, launched, launch_hooks, reporting
):
    """일시정지해 둔 시간은 제한 시간에 세지 않아야 한다 — 일시정지가 길었다고 시간 초과가 되지 않는다.

    일하는 명령이 혼자 도는 시간 D를 먼저 잼. 제한 시간을 3 × D + 3초로 주고, 띄운 지 0.5초
    뒤부터 그 제한 시간만큼 일시정지(일시정지를 합친 전체 시간은 제한 시간을 넘는다)
    -> 종료 코드 0(시간 초과 예외 없음), 걸린 시간 > 제한 시간
    -> 일시정지하는 실행이 max(120초, 6 × D + 10초) 안에 돌아온다(넘기면 스택 · ffmpeg 상태를 남기고 실패, #458)
    """
    output = str(tmp_path / "out.mp4")
    started = time.perf_counter()
    assert run_ffmpeg(_work(output), timeout=300).returncode == 0
    alone = time.perf_counter() - started
    # 일하는 시간은 러너가 바쁘면 혼자 돌 때의 두 배도 걸린다 — 그래도 제한 시간 안이게 넉넉히 준다
    timeout = 3.0 * alone + 3.0
    pause = _Pause()
    extra = {"on_out_time": lambda seconds: None} if reporting else {}

    started = time.perf_counter()
    # 혼자 돌린 것은 이미 끝났다 — 이제부터 뜨는 것만 본다
    pause.schedule_from_launch(launch_hooks, output, PAUSE_AFTER, timeout)
    done = _run_watched(
        _watch_limit(alone),
        launched,
        _work(output),
        timeout=timeout,
        should_pause=pause,
        **extra,
    )
    took = time.perf_counter() - started

    assert done.returncode == 0
    assert took > timeout, "전제: 일시정지를 합친 시간이 제한 시간을 넘었다"


@pytest.mark.parametrize("reporting", [False, True], ids=["출력을 모으는 실행", "진행을 받는 실행"])
def test_a_paused_ffmpeg_ends_when_told_to_stop(launched, launch_hooks, reporting):
    """일시정지로 멈춰 있는 ffmpeg도 멈추라는 요청이 오면 바로 끝나야 한다 — 멈춘 채로 남지 않는다.

    10분 도는 명령을 띄우고, 그 프로세스가 뜬 0.3초 뒤 일시정지, 0.8초 뒤 멈추라고 함
    -> FFmpegCancelledError, 요청 뒤 1.5초 안에 돌아온다, 띄운 프로세스가 모두 끝났다
    """
    pause, stop = threading.Event(), threading.Event()
    stopped_at = []
    extra = {"on_out_time": lambda seconds: None} if reporting else {}

    def on_launch(command, process) -> None:
        if is_long_running(command):  # 그 프로세스가 떴다 — 이때부터 센다
            threading.Timer(0.3, pause.set).start()
            threading.Timer(
                0.8, lambda: (stopped_at.append(time.perf_counter()), stop.set())
            ).start()
            guard = threading.Timer(6.0, end_all, [launched])  # 제품이 못 끝내면 테스트가 끝낸다
            guard.daemon = True
            guard.start()

    launch_hooks.append(on_launch)

    with pytest.raises(FFmpegCancelledError):
        run_ffmpeg(
            LONG_RUNNING, timeout=120, should_stop=stop.is_set, should_pause=pause.is_set, **extra
        )
    returned = time.perf_counter()

    assert returned - stopped_at[0] < 1.5
    assert launched and all(process.poll() is not None for process in launched)


def test_no_ffmpeg_is_launched_while_paused(tmp_path, launched):
    """일시정지 중에는 새 ffmpeg를 띄우지 않고, 풀리면 띄워야 한다.

    처음부터 일시정지 상태로 짧은 명령을 실행하고 0.6초 뒤 일시정지를 품. 0.3초 시점에 띄운
    프로세스 수를 적음
    -> 0.3초 시점: 0개. 끝난 뒤: 1개 이상 · 종료 코드 0 · 걸린 시간 >= 0.6초
    """
    get_exe = ffmpeg_module.get_ffmpeg_exe()  # 실행 파일을 찾는 확인 실행을 미리 치른다
    assert get_exe
    launched.clear()
    pause = threading.Event()
    pause.set()
    seen = []
    threading.Timer(0.3, lambda: seen.append(len(launched))).start()
    threading.Timer(0.6, pause.clear).start()
    args = [
        "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=30:duration=0.2",
        "-f", "null", "-",
    ]  # fmt: skip

    started = time.perf_counter()
    done = run_ffmpeg(args, timeout=60, should_pause=pause.is_set)
    took = time.perf_counter() - started

    assert seen == [0]
    assert len(launched) >= 1 and done.returncode == 0 and took >= 0.6


def test_a_stop_while_waiting_to_launch_launches_nothing(launched):
    """일시정지로 띄우기를 기다리는 동안 멈추라는 요청이 오면 아무것도 띄우지 않고 그만둬야 한다.

    처음부터 일시정지 상태, 0.3초 뒤 멈추라고 함 -> FFmpegCancelledError, 띄운 프로세스 0개
    """
    assert ffmpeg_module.get_ffmpeg_exe()
    launched.clear()
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()

    with pytest.raises(FFmpegCancelledError):
        run_ffmpeg(LONG_RUNNING, timeout=60, should_stop=stop.is_set, should_pause=lambda: True)

    assert launched == []


# ================================================================ 컷

# 구간(프레임 35~80)의 컷이 띄우는 단계와, 그 단계의 명령에만 있는 글
STAGES = {
    "0_head": "0_head.mp4",
    "1_mid": "1_mid.mp4",
    "2_tail": "2_tail.mp4",
    "audio": "audio.m4a",
    "mux": "list.txt",
}


# 컷 한 번을 기다려 주는 시간(초). 일시정지 0.6초를 합친 컷이 로컬에서 가장 길게 1.3초다 — 그
# 8배다(CI 러너는 로컬의 두세 배 느리고, 잠깐 바쁘면 거기서 또 두 배쯤 늘어난다)
_CUT_LIMIT_SECONDS = 10.0


def _end_if_still_running(processes: list) -> threading.Timer:
    """컷이 ``_CUT_LIMIT_SECONDS`` 안에 끝나지 않으면 띄운 ffmpeg를 끝내는 타이머를 걸어 돌려준다.

    재개가 닿지 않으면 멈춘 ffmpeg는 컷의 제한 시간(10분 · 1시간)까지 서 있다 — 테스트가 끝낸다.
    끝난 뒤 ``cancel()``한다.
    """
    guard = threading.Timer(_CUT_LIMIT_SECONDS, end_all, [processes])
    guard.daemon = True
    guard.start()
    return guard


@pytest.fixture(scope="module")
def plain_cut(mp4_source, tmp_path_factory) -> bytes:  # noqa: F811
    """일시정지 없이 자른 결과의 바이트 — mp4, 프레임 35~80."""
    path, frames = mp4_source
    output = str(tmp_path_factory.mktemp("plain_cut") / "out.mp4")
    hybrid_cut(path, frames, 35, 80, output)
    with open(output, "rb") as f:
        return f.read()


@pytest.mark.parametrize("stage", list(STAGES))
def test_cut_paused_during_any_stage_resumes_and_writes_the_same_file(
    mp4_source,  # noqa: F811
    plain_cut,
    tmp_path,
    monkeypatch,
    stage,
):
    """컷의 어느 단계가 도는 도중 일시정지해도 그동안 컷이 끝나지 않고, 재개하면 일시정지 없이 자른 것과 같은 파일을 내야 한다.

    mp4, 프레임 35~80. 표의 단계(머리 · 가운데 · 끝 · 오디오 · 잇기)의 ffmpeg가 뜬 직후부터
    0.6초 동안 일시정지. 0.4초 시점에 그 프로세스가 살아 있는지와 산출물이 다 쓰였는지를 적음
    -> 0.4초 시점: 그 단계의 프로세스가 살아 있다(서 있다), 컷이 돌아오지 않았다
    -> 컷이 끝난다, 걸린 시간 >= 0.6초, 산출물의 바이트 == 일시정지 없이 자른 것의 바이트,
       중간 파일 폴더 없음, 띄운 프로세스가 모두 끝났다
    """
    path, frames = mp4_source
    output = str(tmp_path / "out.mp4")
    pause = threading.Event()
    marker = STAGES[stage]
    armed, seen, returned = [], [], []

    def on_launch(command, process) -> None:
        if any(marker in str(part) for part in command) and not armed:
            armed.append(time.perf_counter())
            pause.set()  # 막 뜬 그 프로세스가 도는 도중이다
            threading.Timer(
                0.4, lambda: seen.append((process.poll() is None, bool(returned)))
            ).start()
            threading.Timer(0.6, pause.clear).start()

    launched = record_processes(monkeypatch, on_launch)
    guard = _end_if_still_running(launched)
    try:
        hybrid_cut(path, frames, 35, 80, output, should_pause=pause.is_set)
        returned.append(time.perf_counter())

        assert seen == [(True, False)]
        assert returned[0] - armed[0] >= 0.6
        with open(output, "rb") as made:
            assert made.read() == plain_cut
        assert not os.path.exists(cut_temp_dir_for(output))
        assert all(process.poll() is not None for process in launched)
    finally:
        guard.cancel()
        end_all(launched)


def test_cut_paused_between_stages_launches_the_next_stage_only_after_resuming(
    mp4_source,  # noqa: F811
    plain_cut,
    tmp_path,
    monkeypatch,
    launched,
):
    """단계 사이에서 일시정지 중이면 다음 단계의 ffmpeg가 뜨지 않고, 재개하면 떠야 한다.

    mp4, 프레임 35~80. 오디오 단계를 띄우려는 순간 일시정지를 켜고 0.6초 뒤 끔. 0.3초 시점에
    그때까지 띄운 프로세스 수를 적음
    -> 0.3초 시점의 프로세스 수 == 오디오 단계 직전까지 띄운 수(오디오의 ffmpeg는 아직 없다)
    -> 컷이 끝나고 산출물의 바이트 == 일시정지 없이 자른 것의 바이트
    """
    path, frames = mp4_source
    output = str(tmp_path / "out.mp4")
    pause = threading.Event()
    real = cut_module.run_ffmpeg
    before, during = [], []

    def pausing(args, **kwargs):
        if args[-1] == "audio.m4a":
            pause.set()  # 띄우기 전에 켠다 — 단계 사이의 일시정지다
            before.append(len(launched))
            threading.Timer(0.3, lambda: during.append(len(launched))).start()
            threading.Timer(0.6, pause.clear).start()
        return real(args, **kwargs)

    monkeypatch.setattr(cut_module, "run_ffmpeg", pausing)
    guard = _end_if_still_running(launched)

    try:
        hybrid_cut(path, frames, 35, 80, output, should_pause=pause.is_set)
    finally:
        guard.cancel()

    assert during == before and len(launched) > before[0]
    with open(output, "rb") as made:
        assert made.read() == plain_cut


def test_cut_stopped_while_paused_ends_its_ffmpeg_and_leaves_nothing(
    mp4_source,  # noqa: F811
    tmp_path,
    monkeypatch,
    launched,
):
    """일시정지로 멈춰 있는 컷을 중단하면 멈춘 ffmpeg가 끝나고 아무것도 남지 않아야 한다.

    mp4, 프레임 35~80. 오디오 단계를 10분 도는 명령으로 바꿔 띄우고 0.3초 뒤 일시정지, 0.8초 뒤 중단
    -> CutCancelled, 중단 뒤 1.5초 안에 돌아온다, 띄운 프로세스가 모두 끝났다,
       산출물 없음, 중간 파일 폴더 없음
    """
    path, frames = mp4_source
    output = str(tmp_path / "out.mp4")
    pause, stop = threading.Event(), threading.Event()
    stopped_at = []
    real = cut_module.run_ffmpeg

    def slowed(args, **kwargs):
        if args[-1] == "audio.m4a":
            kwargs.pop("on_out_time", None)
            threading.Timer(0.3, pause.set).start()
            threading.Timer(
                0.8, lambda: (stopped_at.append(time.perf_counter()), stop.set())
            ).start()
            guard = threading.Timer(6.0, end_all, [launched])
            guard.daemon = True
            guard.start()
            return real(LONG_RUNNING, **kwargs)
        return real(args, **kwargs)

    monkeypatch.setattr(cut_module, "run_ffmpeg", slowed)

    with pytest.raises(CutCancelled):
        hybrid_cut(path, frames, 35, 80, output, should_stop=stop.is_set, should_pause=pause.is_set)
    returned = time.perf_counter()

    assert returned - stopped_at[0] < 1.5
    assert all(process.poll() is not None for process in launched)
    assert not os.path.exists(output) and not os.path.exists(cut_temp_dir_for(output))


def test_rewrapping_ts_segments_waits_while_paused(tmp_path, launched, monkeypatch):
    """TS 세그먼트를 mp4로 다시 싸는 동안 일시정지 중이면 세그먼트를 흘려 넣지 않고 기다려야 한다.

    조각 넷(188바이트씩)으로 읽히는 세그먼트 파일 하나(내용은 아무 바이트 — 다시 싸기는 결국
    실패한다). 처음부터 일시정지 상태로 두고 0.6초 뒤 풂. 0.4초 시점에 띄운 ffmpeg가 살아
    있는지와, ffmpeg에 넣으려고 흘려 넣기에서 꺼낸 조각 수를 적음
    -> 0.4초 시점: ffmpeg가 살아 있다(입력을 기다린다), 꺼낸 조각 0개
    -> 풀린 뒤: 조각 넷을 다 꺼냈다, 걸린 시간 >= 0.6초(끝은 입력이 틀려 CutError다)
    """
    segment = tmp_path / "0.ts"
    segment.write_bytes(bytes(188) * 4)
    monkeypatch.setattr(
        ts_cut_module, "read_in_chunks", lambda path: ffmpeg_module.read_in_chunks(path, 188)
    )
    taken = []  # remux_stream이 흘려 넣기에서 꺼낸 조각
    real_remux_stream = ts_cut_module.remux_stream

    def counting_remux_stream(chunks, *args, **kwargs):
        def counted():
            for chunk in chunks:
                taken.append(chunk)
                yield chunk

        real_remux_stream(counted(), *args, **kwargs)

    monkeypatch.setattr(ts_cut_module, "remux_stream", counting_remux_stream)
    pause = threading.Event()
    pause.set()
    seen = []

    def look_then_resume():
        # 적고 나서 푼다 — 한 스레드라 적는 쪽이 늦어도 푼 뒤의 조각이 섞이지 않는다
        time.sleep(0.4)
        seen.append((bool(launched) and launched[-1].poll() is None, len(taken)))
        time.sleep(0.2)
        pause.clear()

    threading.Thread(target=look_then_resume, daemon=True).start()

    started = time.perf_counter()
    with pytest.raises(CutError):
        ts_cut_module._remux([str(segment)], str(tmp_path / "joined.mp4"), None, pause.is_set)
    took = time.perf_counter() - started

    assert seen == [(True, 0)]
    assert len(taken) == 4
    assert took >= 0.6
