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
from fractions import Fraction
from types import SimpleNamespace
from typing import Never

import pytest
from PySide6.QtCore import QEvent, QObject, Qt, QThreadPool
from PySide6.QtWidgets import QApplication, QWidget

import app.system.free_space as free_space_module
import app.viewmodels.section_edit as sev
from app.system.free_space import FreeSpaceProbe
from app.viewmodels.data import ContentItem
from app.widgets.widget import ContentItemWidget
from core.models.download_state import DownloadState
from core.models.plan import TimeRange

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
    """응답 대역 — 크기 조회의 content-length · 그림 바이트 · 여유 공간 측정의 free."""

    def __init__(self, size: int) -> None:
        self.headers = {"content-length": str(size)}
        self.content = b""
        self.free = size

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
    make: Callable[[], QObject], parked: _Parked
) -> tuple[bool, list[int]]:
    """객체(카드 · 측정기)를 만들어 워커를 띄우고, 워커를 멈춰 둔 채 메인이 객체의 참조를 놓는다.

    그 순간 객체가 남았는지와 지워진 스레드를 돌려준다. 객체의 참조는 이 함수만 든다. 객체가
    남았으면(워커가 잡고 있다) 메인이 참조를 되찾은 뒤에 워커를 풀고, 워커가 끝난 뒤 메인에서
    놓는다 — 워커 스레드에서 지워져 프로세스가 죽지 않게 한다.
    """
    obj = make()
    assert parked.started.wait(5), "전제: 워커가 대역에 들어왔다"
    where = _record_deletion(obj)
    ref = weakref.ref(obj)
    del obj
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


# ================================================================ 스레드 풀의 일


def _parked_after_run(base: type) -> type:
    """`run()`을 마친 뒤 `go`가 켜질 때까지 풀 스레드에 머무는 일 — 지워진 스레드를 적는다.

    결과 시그널은 `run()` 안에서 이미 났다. 머무는 동안 메인이 결과를 받아 참조를 놓게 한 뒤
    `go`를 켜면, 풀 스레드가 일을 놓는 차례가 메인보다 늦어진다.
    """

    class Parked(base):
        go = threading.Event()  # 켜지면 풀 스레드가 run에서 나온다
        deleted: list[int] = []  # 지워진 스레드의 id

        def __init__(self, *args: object) -> None:
            super().__init__(*args)
            self.destroyed.connect(
                lambda *_: Parked.deleted.append(threading.get_ident()),
                Qt.ConnectionType.DirectConnection,
            )

        def run(self) -> None:
            super().run()
            assert Parked.go.wait(5), "대역의 문이 열리지 않았다"

    return Parked


def _stop(*args: object) -> Never:
    # 실패(예외)는 로그의 traceback이 일을 붙잡아 지워지지 않는다 — 그만둠으로 끝낸다
    raise sev.StopRequested()


def _basis(item: object) -> SimpleNamespace:
    """조회 대역 — 30fps · 1초, moov 없음."""
    return SimpleNamespace(fps=Fraction(30), duration=1.0, mp4_head=None, mp4_pending=None)


class _Item(SimpleNamespace):
    """해시가 되는 카드 아이템 대역 — 결과를 받는 쪽이 카드를 사전의 키로 쓴다."""

    __hash__ = object.__hash__


class _NoRowModel:
    """목록 모델 대역 — 카드가 이미 지워졌다(`getRow`가 None)."""

    def getRow(self, item: object) -> None:
        return None

    def notifyChanged(self, item: object) -> None:
        return None


def _let_go_and_wait(qtbot, parked: type, pool: QThreadPool) -> None:
    """풀 스레드를 풀고 일이 끝나기를 기다린 뒤, 일이 지워질 때까지 메인 이벤트를 돌린다."""
    parked.go.set()
    assert pool.waitForDone(5000), "전제: 풀의 일이 끝났다"
    qtbot.waitUntil(lambda: bool(parked.deleted), timeout=3000)


def test_basis_job_is_deleted_on_the_main_thread(qtbot, monkeypatch) -> None:
    """편집 창의 기준값 조회가 끝나 뷰모델이 일을 놓은 뒤 풀 스레드가 나오면 일이 메인 스레드에서 지워져야 한다.

    조회 대역이 30fps · 1초를 돌려줌 · 뷰모델이 결과를 받아 _job을 비운 뒤 풀 스레드가 run에서
    나옴 -> 지워진 스레드 == [메인]
    """
    monkeypatch.setattr(sev.section_basis, "probe_section_basis", _basis)
    parked = _parked_after_run(sev.SectionBasisJob)
    monkeypatch.setattr(sev, "SectionBasisJob", parked)
    pool = QThreadPool()
    viewmodel = sev.SectionEditViewModel(
        SimpleNamespace(vod_url="https://v.invalid/1", selections=())
    )

    viewmodel.start(pool)
    qtbot.waitUntil(lambda: viewmodel.state == sev.STATE_READY, timeout=3000)
    assert viewmodel._job is None, "전제: 뷰모델이 일을 놓았다"
    _let_go_and_wait(qtbot, parked, pool)

    assert parked.deleted == [MAIN]


def test_refit_job_is_deleted_on_the_main_thread(qtbot, monkeypatch) -> None:
    """해상도를 바꾼 뒤의 조회가 끝나 결과를 받은 쪽이 일을 놓은 뒤 풀 스레드가 나오면 일이 메인 스레드에서 지워져야 한다.

    대기 카드 · 확인된 구간 하나(30fps) · 조회 대역이 그만둠 · 결과를 받아 대기 수 0이 된 뒤
    풀 스레드가 run에서 나옴 -> 지워진 스레드 == [메인]
    """
    monkeypatch.setattr(sev.section_basis, "probe_section_basis", _stop)
    parked = _parked_after_run(sev.SectionRefitJob)
    monkeypatch.setattr(sev, "SectionRefitJob", parked)
    pool = QThreadPool()
    refitter = sev.SectionRefitter(_NoRowModel(), pool)
    selections = (TimeRange(0.0, 1.0),)
    item = _Item(
        downloadState=DownloadState.WAITING,
        selections=selections,
        section_verified=(selections, SimpleNamespace(fps=Fraction(30))),
        selected_frame_rate=None,
        content_type="video",
        base_url="https://v.invalid/720.mp4",
        vod_url="https://v.invalid/1",
        resolution=720,
        stream=None,
    )

    refitter.request(item)
    qtbot.waitUntil(lambda: refitter.pendingCount() == 0, timeout=3000)
    _let_go_and_wait(qtbot, parked, pool)

    assert parked.deleted == [MAIN]


def test_size_job_is_deleted_on_the_main_thread(qtbot, monkeypatch) -> None:
    """구간 크기 세기가 끝나 결과를 받은 쪽이 일을 놓은 뒤 풀 스레드가 나오면 일이 메인 스레드에서 지워져야 한다.

    대기 카드 · 구간 하나 · 쥔 moov의 주소가 카드의 주소와 같음 · 세기 대역이 그만둠 · 결과를 받아
    대기 수 0이 된 뒤 풀 스레드가 run에서 나옴 -> 지워진 스레드 == [메인]
    """
    monkeypatch.setattr(sev, "section_file_bytes_of", _stop)
    parked = _parked_after_run(sev.SectionSizeJob)
    monkeypatch.setattr(sev, "SectionSizeJob", parked)
    pool = QThreadPool()
    sizer = sev.SectionSizer(_NoRowModel(), pool)
    item = _Item(
        downloadState=DownloadState.WAITING,
        selections=(TimeRange(0.0, 1.0),),
        is_segment_based=False,
        section_bytes=None,
        section_head=("https://v.invalid/720.mp4", object()),  # (받은 주소, moov 대역)
        base_url="https://v.invalid/720.mp4",
    )

    sizer.request(item)
    qtbot.waitUntil(lambda: sizer.pendingCount() == 0, timeout=3000)
    _let_go_and_wait(qtbot, parked, pool)

    assert parked.deleted == [MAIN]


# ================================================================ 여유 공간 측정


@pytest.fixture
def parked_disk(monkeypatch) -> _Parked:
    fake = _Parked(size=1024)  # 여유 1024바이트
    monkeypatch.setattr(free_space_module.shutil, "disk_usage", lambda path: fake.head())
    return fake


def test_measure_thread_does_not_keep_the_probe(qtbot, parked_disk) -> None:
    """측정이 도는 동안 메인이 부모 없는 측정기를 놓으면 측정기가 그 자리(메인 스레드)에서 지워져야 한다.

    측정 대역이 멈춰 있음 -> del probe 직후 측정기 없음, 지워진 스레드 == [메인]
    """

    def make() -> FreeSpaceProbe:
        probe = FreeSpaceProbe()
        assert probe.request("C:/")
        return probe

    survived, where = _drop_while_worker_parked(make, parked_disk)

    assert not survived, "워커 스레드가 측정기를 잡고 있다"
    assert where == [MAIN]


def test_measured_value_reaches_a_living_probe(qtbot, parked_disk) -> None:
    """측정이 끝나면 살아 있는 측정기가 값을 들고 measured를 내야 한다.

    측정 대역이 여유 1024바이트를 돌려줌 -> measured("C:/", 1024), value("C:/") == 1024
    """
    probe = FreeSpaceProbe()
    with qtbot.waitSignal(probe.measured, timeout=3000) as blocker:
        assert probe.request("C:/")
        assert parked_disk.started.wait(5)
        parked_disk.finish()

    assert blocker.args == ["C:/", 1024]
    assert probe.value("C:/") == 1024
