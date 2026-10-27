"""출력 파일 OSError의 봉쇄 검증 (#147 — #146 감사 E1·E2).

감사 실측(#146)에서 확인된 두 결함을 고정한다:
- E1: run 스레드에서 난 OSError(이어받기 스캔·수신 준비)가 file 다운로더의
  실패 처리를 통과해 스레드 밖으로 탈출 — 통지·로그 없이 카드가 영구
  '다운로드 중'으로 남는다
- E2: 워커에서 난 OSError는 통지는 되지만 future 슬롯이 정리되지 않아
  실행 루프가 영원히 끝나지 않는다

주입 지점은 감사와 동일하게 출력 파일의 open이다. builtins.open 전역 패치
대신 file_downloader 모듈 전역에 open을 얹는다(모듈 스코프 섀도잉) — 주입이
엔진 코드에만 미치고 pytest 내부 파일 I/O에는 영향이 없다. 첫 open
(_prepare_output의 'wb')은 정상으로 두고 'r+b'(스캔·워커 쓰기)만 센다.

서비스 계층(_run_handle)의 최후 방어선은 별도로 검증한다 — 엔진이 어떤
예외를 흘리더라도(다운로더 구현 실수 포함) 통지·로그·슬롯 반환이 보장돼야
한다는 부류 전체의 계약이다.
"""

import os
import threading
import time

from core.models.download_state import DownloadState
from core.services.download_service import DownloadService

import core.downloaders.file_downloader as fd_module
from core.downloaders.file_downloader import FileDownloader
from core.models.download_data import DownloadData

MB = 1024 * 1024
TOTAL_SIZE = 4 * MB  # 해상도 144 → 파트 1MB → 4파트


def _content_bytes(total: int) -> bytes:
    pattern = bytes(range(251))
    return (pattern * (total // len(pattern) + 1))[:total]


CONTENT = _content_bytes(TOTAL_SIZE)


class RecordingLogger:
    """엔진·서비스 로거 인터페이스의 기록용 최소 구현 — 그 외 호출은 무시한다."""

    def __init__(self):
        self.errors: list[str] = []
        self.closed = 0

    def log_error(self, message, exception=None):
        self.errors.append(message)

    def log_exception(self, message, exception=None):
        self.errors.append(message)

    def save_and_close(self):
        self.closed += 1

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class RangeResponse:
    """Range 요청 구간을 청크로 흘리는 가짜 응답 (run 하네스와 동일 최소 인터페이스)."""

    def __init__(self, body: bytes):
        self._body = body

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=8192):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]


class RangeSession:
    """head/get(Range)을 지원하는 가짜 세션 — 실네트워크 없음."""

    def __init__(self, content: bytes):
        self._content = content
        self.headers = {"content-length": str(len(content))}

    def head(self, url, **kwargs):
        return self

    def raise_for_status(self):
        pass

    def get(self, url, headers=None, **kwargs):
        start, end = map(int, (headers or {})["Range"].removeprefix("bytes=").split("-"))
        return RangeResponse(self._content[start : end + 1])


def _make_engine(tmp_path, monkeypatch):
    """가짜 세션이 연결된 엔진과 (data, logger, output, failures)를 준비한다."""
    output = str(tmp_path / "out.mp4")
    data = DownloadData(
        base_url="https://example.invalid/video.mp4",
        vod_url="https://chzzk.naver.com/video/1",
        output_path=output,
        resolution=144,
        content_type="video",
    )
    logger = RecordingLogger()
    monkeypatch.setattr(fd_module, "get_thread_session", lambda: RangeSession(CONTENT))
    failures: list[BaseException] = []
    engine = FileDownloader(data, logger, on_failed=failures.append)
    return engine, data, logger, output, failures


def _inject_output_oserror(monkeypatch, output: str, fail_from: int) -> None:
    """출력 파일의 'r+b' open을 fail_from번째 호출부터 OSError(ENOSPC)로 만든다.

    1회차 = _get_remaining_ranges의 이어받기 스캔(run 스레드), 2회차부터 =
    워커의 파트 쓰기 — fail_from으로 E1/E2 지점을 선택한다.
    """
    calls = {"n": 0}
    real_open = open

    def guarded_open(file, mode="r", *args, **kwargs):
        if file == output and mode == "r+b":
            calls["n"] += 1
            if calls["n"] >= fail_from:
                raise OSError(28, "No space left on device (테스트 주입)")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(fd_module, "open", guarded_open, raising=False)


# ================================================================ E1 — run 스레드


def test_run_thread_oserror_is_contained_as_failure(tmp_path, monkeypatch):
    """run 스레드의 OSError는 실패 콜백·로그·부분 산출물 정리로 귀결된다 (E1).

    고장 커밋에서는 OSError가 run() 밖으로 그대로 전파되어 이 테스트가
    예외로 실패한다 — 실제 앱에서는 통지 없는 스레드 사망이었다.
    """
    engine, data, logger, output, failures = _make_engine(tmp_path, monkeypatch)
    _inject_output_oserror(monkeypatch, output, fail_from=1)

    data.model.start()
    engine.run()

    assert len(failures) >= 1 and isinstance(failures[0], OSError)
    assert any("Download failed" in m for m in logger.errors)
    assert not os.path.exists(output)  # 부분 산출물 정리(_cleanup_partial)


# ================================================================ E2 — 워커


def test_worker_oserror_stops_loop_and_notifies(tmp_path, monkeypatch):
    """워커의 OSError는 전체 실패로 종결되고 실행 루프가 끝난다 (E2).

    고장 커밋에서는 future 슬롯이 정리되지 않아 루프가 영원히 돌았다 —
    join 타임아웃 단언이 그 회귀를 잡는다. 파일시스템 오류는 재큐(#131)에
    태우지 않고 즉시 실패시킨다 — 디스크 부족·마운트 해제는 재시도해도
    같은 결과라, 상한(10회)까지 도는 것은 유저가 보는 멈춤 시간일 뿐이다.
    """
    engine, data, logger, output, failures = _make_engine(tmp_path, monkeypatch)
    _inject_output_oserror(monkeypatch, output, fail_from=2)  # 스캔 1회는 허용

    data.model.start()
    thread = threading.Thread(target=engine.run, daemon=True)
    thread.start()
    thread.join(timeout=15)

    assert not thread.is_alive()  # 루프 종료 — 고장 커밋에서는 여기서 실패
    assert len(failures) >= 1 and isinstance(failures[0], OSError)
    assert engine.future_dict == {}  # 슬롯 잔류 없음 (finally 정리)
    assert engine.state is DownloadState.WAITING  # _fail_fatally의 중단 경로
    assert not os.path.exists(output)  # 부분 산출물 정리


# ================================================================ 서비스 최후 방어선


class _ExplodingEngine:
    """어떤 이유로든 run()이 예외를 흘리는 엔진 — 부류 전체의 대역."""

    run_thread_name = "DownloadThread"
    requires_base_url_resolution = False
    requires_key_resolution = False

    def __init__(self, **kwargs):
        pass

    def run(self):
        raise RuntimeError("엔진이 처리하지 못한 예외 (테스트 주입)")


def test_service_relays_unexpected_engine_exception(tmp_path, monkeypatch):
    """엔진이 흘린 예외도 서비스가 실패로 통지하고 로그·슬롯을 정리한다 (E1 근본).

    다운로더별 _failure_exceptions가 어떻게 설정돼 있든, 실행 스레드가
    통지 없이 죽는 부류 전체를 _run_handle의 방어선이 막아야 한다.
    고장 커밋에서는 on_failed가 오지 않아 단언이 실패한다.
    """
    data = DownloadData(
        base_url="https://example.invalid/video.mp4",
        vod_url="https://chzzk.naver.com/video/1",
        output_path=str(tmp_path / "out.mp4"),
        resolution=144,
        content_type="video",
    )
    service = DownloadService()
    monkeypatch.setattr(
        DownloadService, "_select_downloader", lambda self, content: _ExplodingEngine
    )
    logger = RecordingLogger()
    failures: list[BaseException] = []

    handle = service.submit(data.content, data=data, task_logger=logger, on_failed=failures.append)

    assert handle.wait(10)
    assert len(failures) == 1 and isinstance(failures[0], RuntimeError)
    assert any("Download failed" in m for m in logger.errors)
    assert logger.closed >= 1
    # 슬롯 반환은 _done.set() 직후 같은 finally에서 일어난다 — wait 반환과
    # 미세한 시차가 있어 짧게 기다린 뒤 단언한다
    deadline = time.time() + 2
    while service.active_count and time.time() < deadline:
        time.sleep(0.01)
    assert service.active_count == 0  # 슬롯 반환
