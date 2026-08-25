"""Phase B 다운로드 배관의 피크 메모리를 잰다 (#221 완료 조건).

`#208`은 유휴 메모리만 쟀다(다운로드 경로가 없었으므로). 이 스크립트는
Phase A+B가 새로 만든 실제 코드 경로 — `app.dispatcher.Dispatcher` →
`app.viewmodels.content_viewmodel_web.ContentViewModelWeb` →
`app.viewmodels.download_viewmodel_web.DownloadViewModelWeb` →
`app.download_bridge.WebDownloadBridge` — 를 그대로 태워 실제 VOD 한 건을
받으며 그 과정에서 이 프로세스가 찍은 피크 RSS(OS가 추적하는 최댓값,
폴링이 아니라 커널 카운터를 그대로 읽는다)를 출력한다.

**측정 범위의 한계 — 실행 전 반드시 읽을 것**: 이 스크립트는 pywebview
창을 띄우지 않는다(`evaluate_js`를 print로 대체한 스텁 Dispatcher를 쓴다).
`main_web.py`에는 아직 다운로드를 실제로 트리거할 UI/JS 배선이 없다
(Phase C가 아직 없음) — 그래서 "Phase B의 새 배관 자체가 얼마나 무거운가"
만 잰다. `#208`이 실측한 유휴 메모리(WebView2 멀티프로세스 등 셸
오버헤드)는 이 숫자에 포함되지 않는다 — pywebview 창 없이 순수 Python
프로세스로 돈 값이다. 실제 셸(main_web.py, Nuitka 빌드) 위에서의 종단
간(창+다운로드) 피크는 Phase C가 실제 UI 트리거를 만든 뒤에 별도로
재야 한다.

사용법 (오너가 직접 실행 — 실제 로그인 쿠키·네트워크 필요):
    $env:CVDV2_NID_AUT = "..."          # PowerShell. bash는 export CVDV2_NID_AUT=...
    $env:CVDV2_NID_SES = "..."
    uv run python scripts/measure_download_peak_memory.py <VOD 또는 클립 URL> [--resolution 720] [--output 경로]

쿠키는 커맨드라인 인자로 받지 않는다 — 셸 히스토리에 남는 것을 피한다
(비공개 함수 쿠키 없이도 공개 VOD는 빈 쿠키로 동작한다).
"""

import argparse
import ctypes
import logging
import platform
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os  # noqa: E402

from app.dispatcher import Dispatcher  # noqa: E402
from app.i18n import resolve_language, translate  # noqa: E402
from app.viewmodels.content_viewmodel_web import ContentViewModelWeb  # noqa: E402
from app.viewmodels.content_worker_web import ContentWorkerWeb  # noqa: E402
from app.viewmodels.download_viewmodel_web import DownloadViewModelWeb  # noqa: E402
from config.log_setup import setup_logging  # noqa: E402
import config.config as config  # noqa: E402

logger = logging.getLogger("measure_download_peak_memory")


def _peak_rss_bytes() -> int:
    """이 프로세스가 지금까지 찍은 피크 RSS(바이트) — OS 커널 카운터를 그대로 읽는다.

    폴링이 아니다: Windows는 GetProcessMemoryInfo의 PeakWorkingSetSize,
    POSIX는 getrusage(RUSAGE_SELF).ru_maxrss를 쓴다 — 둘 다 프로세스
    시작부터 지금까지의 최댓값을 커널이 계속 추적해온 값이라, 폴링
    간격 사이의 스파이크를 놓치지 않는다.
    """
    if platform.system() == "Windows":
        class _ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = _ProcessMemoryCounters()
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(counters), ctypes.sizeof(counters)
        )
        if not ok:
            raise OSError("GetProcessMemoryInfo 실패")
        return counters.PeakWorkingSetSize

    import resource

    ru_maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux는 KB, macOS(Darwin)는 바이트 단위로 반환한다 (getrusage(2) 매뉴얼)
    return ru_maxrss * 1024 if platform.system() == "Linux" else ru_maxrss


class _StubDispatcher(Dispatcher):
    """evaluate_js를 print로 대체 — pywebview 창 없이 배관만 실행하기 위함."""

    def __init__(self):
        super().__init__(evaluate_js=lambda js: logger.debug("JS 호출(생략됨): %s", js[:120]))


def _load_cookies() -> dict:
    return {
        "NID_AUT": os.environ.get("CVDV2_NID_AUT", ""),
        "NID_SES": os.environ.get("CVDV2_NID_SES", ""),
    }


def _fake_probe(directory: str) -> tuple[bool, str]:
    return (True, "") if os.path.isdir(directory) else (False, "missing")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("url", help="치지직 VOD 또는 클립 URL")
    parser.add_argument("--resolution", type=int, default=None, help="원하는 해상도(생략 시 최고 화질)")
    parser.add_argument("--output", default=None, help="저장 폴더(생략 시 현재 디렉토리)")
    parser.add_argument("--timeout", type=int, default=600, help="다운로드 제한 시간(초)")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    setup_logging(logging.INFO)
    download_path = args.output or os.getcwd()
    if not os.path.isdir(download_path):
        logger.error("저장 폴더가 존재하지 않습니다: %s", download_path)
        return 2

    dispatcher = _StubDispatcher()
    stop_event = threading.Event()
    backend_thread = threading.Thread(target=dispatcher.run_forever, args=(stop_event,), daemon=True)
    backend_thread.start()

    language = resolve_language(config.load_config().get("language"))
    done = threading.Event()
    result_code = {"code": 1}

    content = ContentViewModelWeb(
        dispatcher,
        worker_factory=lambda vod_url, cookies, dp: ContentWorkerWeb(
            vod_url, cookies, dp, translate=lambda key: translate(key, language)
        ),
        probe=_fake_probe,
        messages={
            "invalid_path": lambda: translate("Invalid file path", language),
            "save_failed": lambda: translate("Failed to save file", language),
        },
    )
    download_vm = DownloadViewModelWeb(dispatcher, content)
    content.on_download_requested = download_vm.start

    # findItem()이 찾을 다음 항목이 완료/실패했는지 폴링해 종료를 감지한다
    def _watch():
        import time

        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            if content.items and content.items[0].downloadState.name in ("FINISHED", "FAILED"):
                result_code["code"] = 0 if content.items[0].downloadState.name == "FINISHED" else 1
                done.set()
                return
            time.sleep(0.5)
        logger.error("제한 시간(%d초) 초과", args.timeout)
        done.set()

    watcher = threading.Thread(target=_watch, daemon=True)
    watcher.start()

    logger.info("조회 시작: %s", args.url)
    content.fetchContent(args.url, _load_cookies(), download_path)

    # 조회 완료(LOADING → WAITING/실패) 대기 후 다운로드 트리거
    import time

    fetch_failed = False
    for _ in range(int(args.timeout / 0.5)):
        if not content.items:
            # 조회 실패 시 자리표시가 제거된다(_workerError) — 빈 리스트가 곧 실패 신호
            fetch_failed = True
            break
        if content.items[0].downloadState.name != "LOADING":
            break
        time.sleep(0.5)
    else:
        logger.error("조회가 시간 안에 끝나지 않았습니다.")
        stop_event.set()
        return 1

    if fetch_failed or content.items[0].downloadState.name == "FAILED":
        logger.error("조회 실패 — 다운로드를 시작할 수 없습니다.")
        stop_event.set()
        return 2

    logger.info("다운로드 시작 트리거")
    content.downloadItem()

    done.wait(args.timeout + 5)
    stop_event.set()
    backend_thread.join(timeout=2)

    peak = _peak_rss_bytes()
    logger.info("피크 RSS: %.1f MB (%d bytes)", peak / (1024 * 1024), peak)
    logger.info("이 값은 pywebview 창 없이(순수 Python 프로세스) 측정됐다 — 위 모듈 docstring의 한계 참고")

    return result_code["code"]


if __name__ == "__main__":
    sys.exit(main())
