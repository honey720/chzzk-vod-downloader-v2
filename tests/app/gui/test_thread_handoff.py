"""워커 스레드가 Qt 객체를 잡지 않음 — 카드의 조회 스레드 · 풀 일 · 여유 공간 측정 (#564).

워커가 QWidget의 마지막 참조를 놓으면 그 위젯이 워커 스레드에서 지워져 프로세스가 죽는다.
테스트는 프로세스를 죽이지 않는다 — 워커를 멈춰 둔 채 메인 스레드가 참조를 놓고, 그때 객체가
남아 있으면(워커가 잡고 있다) 메인 스레드가 참조를 되찾은 뒤에 워커를 풀어 준다. 지워진 스레드는
`destroyed`를 그 자리에서 받아 적는다.
"""

import gc
import threading
import weakref
from collections.abc import Callable, Iterator

import pytest
from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QApplication, QWidget

from app.viewmodels.data import ContentItem
from app.widgets.widget import ContentItemWidget

MAIN = threading.get_ident()  # 테스트가 도는 스레드 = Qt 메인 스레드
URL = "https://v.invalid/720.mp4"  # 크기를 조회하는 해상도 주소
THUMB = "https://img.invalid/thumb.jpg"  # 받는 썸네일 주소


class _Parked:
    """네트워크 대역 — 호출한 스레드를 적고 `go`가 켜질 때까지 멈춰 있다가 응답을 돌려준다.

    `size`가 None이면 응답 대신 예외를 던진다(조회 실패).
    """

    def __init__(self, size: int | None = None) -> None:
        self.size = size
        self.started = threading.Event()  # 첫 호출이 들어왔다
        self.go = threading.Event()  # 켜지면 응답한다
        self.workers: list[threading.Thread] = []

    def _answer(self) -> object:
        self.workers.append(threading.current_thread())
        self.started.set()
        assert self.go.wait(5), "대역의 문이 열리지 않았다"
        if self.size is None:
            raise RuntimeError("network disabled in tests")
        return _Response(self.size)

    def head(self, *args: object, **kwargs: object) -> object:
        return self._answer()

    def get(self, *args: object, **kwargs: object) -> object:
        return self._answer()

    def finish(self) -> None:
        """문을 열고 멈춰 있던 워커가 다 끝나기를 기다린다 — Qt 이벤트는 처리하지 않는다."""
        self.go.set()
        for worker in self.workers:
            worker.join(5)
            assert not worker.is_alive(), "전제: 워커 스레드가 끝났다"


class _Response:
    """크기 조회 응답 대역 — content-length와 그림 바이트."""

    def __init__(self, size: int) -> None:
        self.headers = {"content-length": str(size)}
        self.content = b""

    def raise_for_status(self) -> None:
        return None

    def close(self) -> None:
        return None


@pytest.fixture
def parked(monkeypatch) -> _Parked:
    fake = _Parked()
    monkeypatch.setattr("app.widgets.widget.get_thread_session", lambda: fake)
    monkeypatch.setattr("app.widgets.widget._global_download_path", "C:/dl")
    return fake


@pytest.fixture(autouse=True)
def _drain(qapp) -> Iterator[None]:
    """테스트가 남긴 미룬 지우기 · 큐 이벤트를 처리한다 — 다음 테스트가 그것을 받지 않게."""
    yield
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()


def _item(reps: list, thumbnail: str = "") -> ContentItem:
    """대기 카드의 아이템 — mp4(파일 기반)라 해상도마다 크기를 조회한다."""
    return ContentItem(
        "https://chzzk.naver.com/video/1",
        {
            "title": "제목",
            "category": "",
            "channelName": "채널",
            "createdDate": "",
            "duration": 60,
            "thumbnailImageUrl": thumbnail,
        },
        reps,
        reps[0][0] if reps else None,
        reps[0][1] if reps else "",
        "C:/dl",
        "video",
        None,
    )


def _record_deletion(obj: QObject) -> list[int]:
    """obj가 지워지는 스레드의 id를 적을 목록 — 지워지는 그 스레드에서 적는다."""
    where: list[int] = []
    obj.destroyed.connect(
        lambda *_: where.append(threading.get_ident()), Qt.ConnectionType.DirectConnection
    )
    return where


def _drop_while_worker_parked(
    make: Callable[[], ContentItemWidget], parked: _Parked
) -> tuple[bool, list[int]]:
    """카드를 만들어 워커를 띄우고, 워커를 멈춰 둔 채 메인이 카드의 참조를 놓는다.

    그 순간 카드가 남았는지와 지워진 스레드를 돌려준다. 카드의 참조는 이 함수만 든다. 카드가
    남았으면(워커가 잡고 있다) 메인이 참조를 되찾은 뒤에 워커를 풀고, 워커가 끝난 뒤 메인에서
    놓는다 — 워커 스레드에서 지워져 프로세스가 죽지 않게 한다.
    """
    card = make()
    assert parked.started.wait(5), "전제: 워커가 대역에 들어왔다"
    where = _record_deletion(card)
    ref = weakref.ref(card)
    del card
    gc.collect()
    rescued = ref()
    survived = rescued is not None
    parked.finish()
    del rescued  # 워커가 끝난 뒤라 마지막 참조는 메인에 있다
    gc.collect()
    return survived, where


# ================================================================ 카드의 조회 스레드


def test_size_thread_does_not_keep_the_card(qtbot, parked) -> None:
    """크기 조회가 도는 동안 메인이 부모 없는 카드를 놓으면 카드가 그 자리(메인 스레드)에서 지워져야 한다.

    해상도 하나(mp4) · 크기 조회 대역이 멈춰 있음 -> del card 직후 카드 없음,
    지워진 스레드 == [메인]
    """

    def make() -> ContentItemWidget:
        card = ContentItemWidget(_item([[720, URL]]))
        card.loadRepresentations()
        return card

    survived, where = _drop_while_worker_parked(make, parked)

    assert not survived, "워커 스레드가 카드를 잡고 있다"
    assert where == [MAIN]


def test_image_thread_does_not_keep_the_card(qtbot, parked) -> None:
    """썸네일을 받는 동안 메인이 부모 없는 카드를 놓으면 카드가 그 자리(메인 스레드)에서 지워져야 한다.

    해상도 없음 · 썸네일 주소 있음 · 받기 대역이 멈춰 있음 -> del card 직후 카드 없음,
    지워진 스레드 == [메인]
    """
    survived, where = _drop_while_worker_parked(
        lambda: ContentItemWidget(_item([], thumbnail=THUMB)), parked
    )

    assert not survived, "워커 스레드가 카드를 잡고 있다"
    assert where == [MAIN]


def test_size_result_reaches_a_living_card(qtbot, parked) -> None:
    """크기 조회가 끝나면 살아 있는 카드의 해상도 목록과 아이템 크기에 결과가 들어가야 한다.

    해상도 하나(mp4) · 대역의 content-length 1073741824(1 GiB) -> unique_reps[0]의 크기 글
    "1.00 GB", 아이템의 total_size "1.00 GB"
    """
    parked.size = 1073741824  # 1 GiB
    card = ContentItemWidget(_item([[720, URL]]))
    card.loadRepresentations()
    assert parked.started.wait(5)

    parked.finish()
    qtbot.waitUntil(lambda: card.item.unique_reps[0][-1] == "1.00 GB", timeout=3000)

    assert card.item.total_size == "1.00 GB"


def test_size_result_after_qt_deleted_the_card_is_dropped(qtbot, parked) -> None:
    """Qt가 카드를 지운 뒤 크기 조회가 끝나면 결과를 버려야 한다 — 지워진 카드에 쓰지 않는다.

    부모가 있는 카드(목록과 같음) · 조회 중에 deleteLater로 지움 · 테스트는 카드의 파이썬 객체를
    쥐고 있음, 대역의 content-length 1073741824 -> 결과가 메인 스레드에 닿아도 예외 없음,
    아이템의 크기 글은 "Checking..." 그대로
    """
    parked.size = 1073741824  # 1 GiB
    parent = QWidget()
    card = ContentItemWidget(_item([[720, URL]]), 0, parent)
    card.loadRepresentations()
    item = card.item
    assert parked.started.wait(5)
    where = _record_deletion(card)
    card.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert where == [MAIN], "전제: Qt가 카드를 메인 스레드에서 지웠다"

    parked.finish()
    for _ in range(3):
        QApplication.processEvents()  # 결과가 메인 스레드에 닿는다

    assert item.unique_reps[0][-1] == "Checking..."
    assert card is not None  # 결과가 닿는 동안 카드의 파이썬 객체가 살아 있었다
