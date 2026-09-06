"""메타데이터 조회의 백그라운드 도착 게이트 (#259 B2-0) — 조회는 풀 스레드에서, 반영은 메인 스레드에서.

조회(`fetchContent`)는 core의 조회 함수를 스레드 풀에서 돌리고, 결과는 Signal
큐 연결로 메인 스레드에 건너와 모델(자리표시 교체)과 카드를 갱신한다. 흡수(B2)
중 "풀에서 run()이 끝나면 슬롯을 바로 부르면 되잖아"로 단순화하면 모델과
위젯이 풀 스레드에서 갱신된다 — 조용히 깨지고 재현이 산발적이라 다른 어떤
테스트도 결정적으로 잡지 못한다. 이 파일이 그 자리를 잰다.

의존하는 이름은 둘뿐이다: 메인 창의 `contentManager` 속성(오너 확정 — 흡수
뒤에도 뷰모델 인스턴스를 가리킨다)과 core의 조회 함수
`core.services.metadata_service.fetch_content`(객체 패치). 조회를 수행하는
클래스가 무엇이든 알지 않는다. **B2 흡수 뒤 이 파일이 한 줄도 바뀌지 않은 채
통과하는 것이 B2의 성공 조건 중 하나다.**

판정은 스레드 식별자 기록·비교로만 한다. 관찰 지점의 슬롯을 **DirectConnection**
으로 붙여 "발신한 스레드"에서 기록하게 하고, 풀이 끝난(`waitForDone`) 뒤
`processEvents` 전에는 반영이 없어야 하며(큐에 있을 뿐), 그 뒤 도착한 반영의
스레드는 메인이어야 한다. 타이밍·sleep에 기대지 않는다.

조회 함수를 패치하는 이유: 실네트워크 금지이기도 하지만, 핵심은 **호출 스레드를
기록**하기 위해서다. 패치가 안 듣는 형태(이름을 복사해 import)로 바뀌면 조회가
차단된 세션으로 떨어져 실패 경로로 가고, 기록이 비어 시끄럽게 실패한다.
쿠키는 명백한 더미다.
"""

import threading

import pytest
from PySide6.QtCore import Qt

import app.views.mainWindow as mw_mod
from core.services import metadata_service
from core.services.metadata_service import MetadataError

MAIN_THREAD = threading.main_thread().ident
URL = "https://chzzk.naver.com/video/1"
DUMMY_COOKIES = {"NID_AUT": "dummy-aut", "NID_SES": "dummy-ses"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """위젯·조회의 HTTP 세션을 차단한다 — 조회 함수는 별도로 패치하므로 여기까지 오면 결함이다."""

    class _FailingSession:
        """requests 세션 대역 — 어떤 요청이든 즉시 예외. 카드의 썸네일·크기 조회 스레드까지 막는다."""

        def head(self, *a, **k):
            """HEAD 요청(파일 크기 조회) 차단."""
            raise RuntimeError("network disabled in tests")

        def get(self, *a, **k):
            """GET 요청(썸네일·API) 차단."""
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("app.widgets.widget.get_thread_session", lambda: _FailingSession())
    monkeypatch.setattr("content.network.get_thread_session", lambda: _FailingSession())
    monkeypatch.setattr("content.network._session", _FailingSession())
    monkeypatch.setattr("core.api.session._session", _FailingSession())


@pytest.fixture
def window(qapp, monkeypatch):
    """실물 메인 창 — 팝업은 기록으로 대체한다(실모달이 뜨면 이벤트 루프가 멈춘다).

    config는 conftest의 autouse 픽스처가 임시 폴더로 격리한다.
    """
    popups: list[str] = []
    monkeypatch.setattr(
        mw_mod.QMessageBox, "warning", lambda parent, title, text, *a, **k: popups.append(text)
    )
    monkeypatch.setattr(
        mw_mod.QMessageBox, "critical", lambda parent, title, text, *a, **k: popups.append(text)
    )
    win = mw_mod.VodDownloader()
    win.popups = popups
    yield win
    win.contentManager.threadpool.waitForDone(5000)
    qapp.processEvents()
    win.close()


class Recorder:
    """관찰 지점마다 (이름, 발신 스레드 id)를 순서대로 남기는 기록기.

    반영 지점(모델 삽입·제거, 뷰모델 통지)이 **어느 스레드에서** 일어났는지가
    이 파일의 유일한 판정 재료다. 슬롯을 DirectConnection으로 붙이면 큐를 거치지
    않고 발신한 스레드에서 즉시 돌므로, 슬롯 안의 `get_ident()`가 곧 발신
    스레드다. 순서도 함께 남겨 "배달 전 0건 → 배달 뒤 N건"을 리스트 등식으로
    잰다.
    """

    def __init__(self):
        self.events: list[tuple[str, int]] = []

    def hook(self, name: str):
        """`name` 지점에 붙일 슬롯을 만든다 — Signal 인자는 버리고 이름과 스레드 id만 남긴다."""

        def slot(*args):
            """DirectConnection으로 붙는 슬롯 — 발신 스레드에서 돈다."""
            self.events.append((name, threading.get_ident()))

        return slot

    def names(self) -> list[str]:
        """기록된 지점 이름을 순서대로 — 배달 전/후의 리스트 등식 단언용."""
        return [n for n, _ in self.events]

    def threads_of(self, name: str) -> set[int]:
        """`name` 지점이 발신된 스레드 id 집합 — {메인}과 같아야 한다."""
        return {t for n, t in self.events if n == name}


def _observe(win) -> Recorder:
    """모델·뷰모델의 반영 지점을 발신 스레드 기록용 슬롯으로 감시한다."""
    rec = Recorder()
    cm = win.contentManager
    direct = Qt.ConnectionType.DirectConnection
    cm.model.itemInserted.connect(rec.hook("itemInserted"), direct)
    cm.model.itemRemoved.connect(rec.hook("itemRemoved"), direct)
    cm.insertItemRequested.connect(rec.hook("insertItemRequested"), direct)
    cm.deleteItemRequested.connect(rec.hook("deleteItemRequested"), direct)
    cm.contentError.connect(rec.hook("contentError"), direct)
    return rec


def _result(download_path: str) -> tuple:
    """조회 함수가 돌려주는 페이로드 — (vod_url, metadata, unique_reps, resolution, base_url, download_path, live_rewind)."""
    metadata = {
        "title": "조회 결과",
        "category": "게임",
        "channelName": "채널",
        "createdDate": "2026-08-01",
        "duration": 60,
    }
    return (
        URL,
        metadata,
        [["1080", "http://example.invalid/1080"]],
        "1080",
        "http://example.invalid/1080",
        download_path,
        None,
    )


@pytest.fixture
def fetch_spy(monkeypatch):
    """core 조회 함수를 대역으로 — 호출 스레드 id와 받은 인자를 기록하고, 지정한 결과/예외를 낸다."""
    calls: list[dict] = []
    outcome = {"raise": None, "download_path": ""}

    def fake_fetch(vod_url, cookies, download_path, api):
        """core 조회 함수 자리 — 풀 스레드에서 불리므로 여기서 잡은 id가 "조회 스레드"다."""
        calls.append(
            {
                "thread": threading.get_ident(),
                "vod_url": vod_url,
                "cookies": cookies,
                "download_path": download_path,
            }
        )
        if outcome["raise"] is not None:
            raise outcome["raise"]
        return _result(download_path), "video"

    monkeypatch.setattr(metadata_service, "fetch_content", fake_fetch)
    return calls, outcome


def _pool_done(win) -> None:
    """풀 스레드의 조회가 끝나기를 기다린다 — 결과 통지는 이 시점에 큐에 있을 뿐 아직 배달되지 않았다."""
    assert win.contentManager.threadpool.waitForDone(5000), "조회 풀 스레드가 끝나지 않았다"


class TestFetchRunsOffTheMainThread:
    """출발 쪽 — 조회는 메인 스레드를 떠나 풀에서 돈다(도착 테스트의 전제)."""

    def test_the_core_fetch_is_called_on_a_pool_thread_with_the_given_arguments(
        self, window, fetch_spy, tmp_path
    ):
        calls, _ = fetch_spy
        window.contentManager.fetchContent(URL, DUMMY_COOKIES, str(tmp_path))
        _pool_done(window)

        [call] = calls
        assert call["thread"] != MAIN_THREAD, "조회가 메인 스레드에서 돌았다 — 풀 경로가 사라졌다"
        assert call["vod_url"] == URL
        assert call["cookies"] == DUMMY_COOKIES
        assert call["download_path"] == str(tmp_path)


class TestArrivalOnTheMainThread:
    """도착 쪽 — 반영(모델 삽입·제거·뷰모델 통지)은 큐를 거쳐 전부 메인 스레드에서 일어난다.

    각 테스트가 같은 두 단계로 잰다: 풀이 끝난 직후(`_pool_done`)에는 반영이
    자리표시 삽입 2건뿐이어야 하고(통지는 큐에 있을 뿐), `processEvents` 뒤
    도착한 반영의 발신 스레드가 메인이어야 한다.
    """

    def test_success_replaces_the_placeholder_on_the_main_thread(
        self, window, fetch_spy, qapp, tmp_path
    ):
        rec = _observe(window)
        cm = window.contentManager

        cm.fetchContent(URL, DUMMY_COOKIES, str(tmp_path))
        # 자리표시 삽입은 호출 즉시, 메인 스레드에서
        assert rec.names() == ["itemInserted", "insertItemRequested"]
        assert cm.hasLoadingItems()

        _pool_done(window)
        # 풀은 끝났지만 배달 전 — 교체는 아직이다(큐에 있다)
        assert rec.names() == ["itemInserted", "insertItemRequested"], (
            "processEvents 전에 반영이 일어났다 — 풀 스레드 직접 호출"
        )
        assert cm.hasLoadingItems()

        qapp.processEvents()
        assert rec.names() == ["itemInserted", "insertItemRequested", "itemRemoved", "itemInserted"]
        assert not cm.hasLoadingItems()
        assert cm.model.rowCount() == 1
        assert cm.model.itemAt(0).title == "조회 결과"
        for name in ("itemInserted", "itemRemoved", "insertItemRequested"):
            assert rec.threads_of(name) == {MAIN_THREAD}, f"{name}이 메인 스레드 밖에서 발신됐다"
        assert window.popups == []

    def test_failure_removes_the_placeholder_and_reports_on_the_main_thread(
        self, window, fetch_spy, qapp, tmp_path
    ):
        _, outcome = fetch_spy
        outcome["raise"] = MetadataError("Video not found", URL)
        rec = _observe(window)
        cm = window.contentManager
        errors: list[tuple[str, int]] = []
        cm.contentError.connect(
            lambda message: errors.append((message, threading.get_ident())),
            Qt.ConnectionType.DirectConnection,
        )

        cm.fetchContent(URL, DUMMY_COOKIES, str(tmp_path))
        _pool_done(window)
        assert rec.names() == ["itemInserted", "insertItemRequested"], (
            "processEvents 전에 반영이 일어났다 — 풀 스레드 직접 호출"
        )
        assert errors == []

        qapp.processEvents()
        assert rec.names() == [
            "itemInserted",
            "insertItemRequested",
            "itemRemoved",
            "deleteItemRequested",
            "contentError",
        ]
        assert cm.model.rowCount() == 0
        for name in ("itemRemoved", "deleteItemRequested", "contentError"):
            assert rec.threads_of(name) == {MAIN_THREAD}, f"{name}이 메인 스레드 밖에서 발신됐다"
        [(message, thread)] = errors
        assert thread == MAIN_THREAD
        # 형식: "<url>\n<번역 메시지>" — 번역기 미설치 환경이라 키 원문이 온다
        assert message == f"{URL}\nVideo not found"
        # 창은 오류를 팝업으로 보여준다(기록 대역) — 쿠키 값은 어디에도 새지 않는다
        assert window.popups and "Video not found" in window.popups[-1]
        assert "dummy-aut" not in window.popups[-1] and "dummy-ses" not in window.popups[-1]

    def test_unexpected_exception_is_reported_without_raw_details(
        self, window, fetch_spy, qapp, tmp_path
    ):
        """MetadataError가 아닌 예외도 같은 경로로 메인 스레드에 도착하고, 원시 문자열·쿠키는 새지 않는다 (#126)."""
        _, outcome = fetch_spy
        outcome["raise"] = RuntimeError("raw internal detail dummy-ses")
        rec = _observe(window)
        errors: list[str] = []
        window.contentManager.contentError.connect(errors.append)

        cm = window.contentManager
        cm.fetchContent(URL, DUMMY_COOKIES, str(tmp_path))
        _pool_done(window)
        assert rec.names() == ["itemInserted", "insertItemRequested"], (
            "processEvents 전에 반영이 일어났다 — 풀 스레드 직접 호출"
        )

        qapp.processEvents()
        # 자리표시 제거와 통지는 MetadataError 경로와 같은 순서·같은 스레드다
        assert rec.names() == [
            "itemInserted",
            "insertItemRequested",
            "itemRemoved",
            "deleteItemRequested",
            "contentError",
        ]
        assert cm.model.rowCount() == 0
        assert not cm.hasLoadingItems()
        for name in ("itemRemoved", "deleteItemRequested", "contentError"):
            assert rec.threads_of(name) == {MAIN_THREAD}, f"{name}이 메인 스레드 밖에서 발신됐다"
        # 유저 표시(Signal 페이로드·팝업)에는 원시 예외 문자열도 쿠키 값도 없다.
        # 로그는 검사하지 않는다 — 상세를 로그에 남기는 것이 설계다(#126)
        [message] = errors
        assert message.startswith(f"{URL}\n")
        assert "raw internal detail" not in message and "dummy-ses" not in message
        assert window.popups, "오류 팝업(기록 대역)이 뜨지 않았다"
        assert "raw internal detail" not in window.popups[-1]
        assert "dummy-aut" not in window.popups[-1] and "dummy-ses" not in window.popups[-1]
