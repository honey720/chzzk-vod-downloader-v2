"""쓰기 속도 샘플링 — 임시 폴더를 다른 매체로 보낼지 판단하는 재료 (#192).

app 계층의 `content/manager.py::probe_writable`(#138)과 같은 원리(제물
스레드 + `join(timeout)`, 실제 파일을 만들어 썼다 지움)를 core에
독립적으로 둔다 — core는 app을 import할 수 없으므로(SPEC §3.1) 그
함수를 그대로 재사용하지 못하고, 같은 패턴만 복제했다.

**반드시 `os.fsync()`로 실제 매체에 반영시킨 뒤 시간을 잰다.** 일반
버퍼드 쓰기는 OS 페이지 캐시에 즉시 반환되어(#191 실기 확인 —
`f.write()` 8192바이트 2000회 평균 3.7μs) 매체 속도와 무관하게 항상
빠르게 보인다 — fsync 없이는 이 샘플링 자체가 무의미하다.
"""

import os
import tempfile
import threading
import time

# 4MB — 너무 작으면 노이즈(고정 오버헤드 비중이 커짐)에 흔들리고, 너무 크면
# 정말 느린 매체에서 프로브 자체가 오래 걸린다. 순차 쓰기라 대부분의 매체가
# 이 정도는 초 단위 안에 끝낸다.
SAMPLE_BYTES = 4 * 1024 * 1024
PROBE_TIMEOUT_S = 5.0

_speed_cache: dict[object, float | None] = {}
_cache_lock = threading.Lock()


def _volume_key(directory: str) -> object:
    """directory가 속한 볼륨을 식별하는 키. stat 실패 시 경로 자체로 폴백."""
    try:
        return os.stat(directory).st_dev
    except OSError:
        return os.path.abspath(directory)


def measure_write_speed(
    directory: str, sample_bytes: int = SAMPLE_BYTES, timeout_s: float = PROBE_TIMEOUT_S
) -> float | None:
    """directory에 샘플 파일을 써서 초당 바이트 수를 잰다. 실패·시간초과면 None.

    같은 볼륨은 프로세스 생존 동안 한 번만 잰다(볼륨 캐시) — 다운로드마다
    매번 재는 건 낭비고, 매체 속도는 그 사이 바뀌지 않는다고 가정한다.
    """
    key = _volume_key(directory)
    with _cache_lock:
        if key in _speed_cache:
            return _speed_cache[key]

    outcome: dict[str, float] = {}

    def probe() -> None:
        try:
            fd, probe_path = tempfile.mkstemp(prefix=".cvdv2_speed_", dir=directory)
        except OSError:
            return
        try:
            data = os.urandom(sample_bytes)
            start = time.perf_counter()
            os.write(fd, data)
            os.fsync(fd)  # 페이지 캐시가 아니라 실제 매체 반영 시간을 잰다 (#191)
            outcome["elapsed"] = time.perf_counter() - start
        except OSError:
            pass
        finally:
            os.close(fd)
            try:
                os.remove(probe_path)
            except OSError:
                pass

    worker = threading.Thread(target=probe, daemon=True, name="DiskSpeedProbe")
    worker.start()
    worker.join(timeout_s)

    speed = None
    elapsed = outcome.get("elapsed")
    if elapsed and elapsed > 0:
        speed = sample_bytes / elapsed

    with _cache_lock:
        _speed_cache[key] = speed
    return speed


def clear_speed_cache() -> None:
    """볼륨 속도 캐시를 비운다 — 테스트 전용."""
    with _cache_lock:
        _speed_cache.clear()
