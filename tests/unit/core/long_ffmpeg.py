"""오래 도는 ffmpeg 명령과, 테스트가 띄운 ffmpeg 프로세스를 적어 두는 도우미 (#309).

중단이 도는 ffmpeg에 닿는지를 재는 테스트들이 함께 쓴다. 재려는 단계의 명령을 ``LONG_RUNNING``으로
바꿔 띄우면 멈추지 않는 한 10분을 돈다 — "도는 도중에 멈췄다"가 단계의 길이에 기대지 않는다.

프로세스는 **띄운 객체로만** 확인하고 끝낸다. 이름으로 찾아 끝내지 않는다 — 같은 머신에서
도는 다른 ffmpeg를 건드리지 않는다.
"""

import subprocess

import core.utils.ffmpeg as ffmpeg_module

# 멈추지 않으면 10분을 도는 명령 — 실시간 속도로 읽는 합성 영상을 아무 데도 쓰지 않는다
LONG_RUNNING = [
    "-v", "error", "-re", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=30:duration=600",
    "-f", "null", "-",
]  # fmt: skip


def record_processes(monkeypatch) -> list[subprocess.Popen]:
    """ffmpeg 모듈이 띄우는 프로세스를 적는 목록을 돌려준다 — 띄울 때마다 그 객체가 들어간다."""
    processes: list[subprocess.Popen] = []
    real = subprocess.Popen

    class Recording(real):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            processes.append(self)

    monkeypatch.setattr(ffmpeg_module.subprocess, "Popen", Recording)
    return processes


def end_all(processes: list[subprocess.Popen]) -> None:
    """아직 도는 프로세스를 그 객체로 끝낸다 — 테스트가 실패해도 10분짜리 프로세스가 남지 않게."""
    for process in processes:
        if process.poll() is None:
            process.kill()
            process.wait()
