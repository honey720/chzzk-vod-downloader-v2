"""엔진 ↔ 뷰모델 경계 (#484) — 뷰모델은 값 묶음과 안내 키만 읽고, 카드는 메인 스레드에서만 적는다.

배선은 viewmodel 계약 게이트(tests/app/gui/test_download_viewmodel_contract.py)의 대역을 그대로 쓴다.
서비스 대역의 콜백 입구는 실제 서비스 핸들의 중계라(tests/mocks/engine_side.py), 진행에는 그 순간의
공유 데이터가 값 묶음으로, 실패에는 예외에서 고른 키가 실린다.
"""

import threading

import pytest

from core.models.download_state import DownloadState
from core.models.events import ProgressEvent
from tests.app.gui.test_download_viewmodel_contract import (  # noqa: F401 — 픽스처를 이 모듈에 들인다
    MAIN_THREAD,
    Wired,
    _make_item,
    _stop_viewmodel_timers,
    log_calls,
)


@pytest.fixture
def wired(qapp, log_calls):  # noqa: F811
    """다운로드 한 건의 배선 — 계약 게이트의 대역 그대로."""
    return Wired(qapp, log_calls)


def _on_worker(call) -> None:
    """엔진처럼 워커 스레드에서 부르고 끝날 때까지 기다린다."""
    thread = threading.Thread(target=call)
    thread.start()
    thread.join()


def _watch_item_writes(item) -> list[tuple[str, int]]:
    """아이템의 post_process · downloadState 쓰기를 (이름, 쓴 스레드 id)로 남기게 한다."""
    writes: list[tuple[str, int]] = []
    base = type(item)

    class Watched(base):
        def __setattr__(self, name, value):
            if name in ("post_process", "downloadState"):
                writes.append((name, threading.get_ident()))
            super().__setattr__(name, value)

    item.__class__ = Watched
    return writes


def test_section_counts_come_from_the_counters_sent_with_the_progress(wired):
    """카드의 구간 수는 진행 통지에 실린 값 묶음의 것이어야 한다 — 그 뒤 바뀐 공유 데이터의 것이 아니다.

    공유 데이터 sections_total 3 · sections_done 1에서 워커가 진행을 알림 → 공유 데이터 sections_done
    3 · sections_failed 0으로 바꿈 → 이벤트 처리
    -> item.sections_done == 1
    """
    item = _make_item("m3u8")
    wired.start(item)
    data = wired.submission["data"]
    data.sections_total, data.sections_done = 3, 1

    _on_worker(lambda: wired.submission["on_progress"](ProgressEvent(downloaded_size=10)))
    data.sections_done = 3  # 통지 뒤에 엔진이 더 나아갔다 — 이 값은 아직 통지되지 않았다
    wired.pump()

    assert item.sections_done == 1


def test_failure_reaches_the_main_thread_as_a_key_not_an_exception(wired):
    """실패는 메인 스레드에 예외 객체가 아니라 엔진 쪽이 고른 안내 키로 닿아야 한다.

    워커가 OSError("디스크")로 실패를 알림 → 내부 실패 Signal의 인자를 기록
    -> 실린 값에 예외 객체가 없음 · 키 "Failed to save file"
    """
    item = _make_item("m3u8")
    wired.start(item)
    seen: list[tuple] = []
    wired.vm._engineFailed.connect(lambda *args: seen.append(args))

    _on_worker(lambda: wired.submission["on_failed"](OSError("디스크")))
    wired.pump()

    [args] = seen
    assert not any(isinstance(value, BaseException) for value in args)
    assert "Failed to save file" in args


def test_item_is_written_only_on_the_main_thread(wired):
    """엔진 통지(병합 시작 · 상태 전이 · 실패)가 워커에서 와도 아이템의 병합 표시 · 상태는 메인 스레드에서만 적혀야 한다.

    시작 → 워커가 병합 시작 · 완료 전이 · 실패를 차례로 알림 → 이벤트 처리
    -> post_process · downloadState 쓰기의 스레드가 모두 메인
    """
    item = _make_item("m3u8")
    wired.start(item)
    writes = _watch_item_writes(item)
    data = wired.submission["data"]

    _on_worker(wired.submission["on_merge_start"])
    _on_worker(data.model.finish)  # 서비스의 완료 전이는 워커 스레드에서 일어난다
    _on_worker(lambda: wired.submission["on_failed"](RuntimeError("실패")))
    wired.pump()

    names = {name for name, _ident in writes}
    assert names == {"post_process", "downloadState"}  # 대조군: 실제로 썼다
    assert {ident for _name, ident in writes} == {MAIN_THREAD}


def test_merge_start_then_failure_leaves_the_merge_mark_off(wired):
    """병합 시작과 실패가 서로 다른 워커에서 그 차례로 오면 병합 표시는 꺼진 채 끝나야 한다.

    워커 A가 병합 시작을 알림 → 워커 B가 실패를 알림 → 이벤트 처리
    -> post_process 거짓
    """
    item = _make_item("m3u8")
    wired.start(item)

    _on_worker(wired.submission["on_merge_start"])
    _on_worker(lambda: wired.submission["on_failed"](RuntimeError("실패")))
    wired.pump()

    assert item.post_process is False


def test_late_worker_state_does_not_overwrite_the_failed_card(wired):
    """실패 처리가 끝난 뒤 닿은 워커의 상태 전이는 카드 상태를 덮지 않아야 한다.

    워커가 실패를 알림 → 이벤트 처리(카드 실패 처리) → 카드를 FAILED로 적음 → 워커의 대기 전이 →
    이벤트 처리
    -> downloadState FAILED 그대로
    """
    item = _make_item("m3u8")
    wired.start(item)
    data = wired.submission["data"]
    _on_worker(lambda: wired.submission["on_failed"](RuntimeError("실패")))
    wired.pump()
    item.downloadState = DownloadState.FAILED  # content.fail이 하는 일 — 기록 대역은 적지 않는다

    def late_transition():
        """실패한 시도의 모델이 상태 전이를 알린다 — 실패한 엔진이 스스로 대기로 돌아가는 길."""
        data.model._on_state_change(DownloadState.WAITING)

    _on_worker(late_transition)
    wired.pump()

    assert item.downloadState is DownloadState.FAILED


def test_each_progress_counts_the_bar_with_its_own_paused_time(wired, monkeypatch):
    """두 스레드의 진행 통지가 겹쳐도 막대는 각 통지의 일시정지 누적으로 세어야 한다.

    구간 3개 m3u8. 스레드 A가 일시정지 누적 5초인 통지를 내고 막대를 세기 직전에 멈춤 → 스레드 B가
    누적 9초인 통지를 끝까지 냄 → A가 이어서 막대를 셈
    -> A가 막대에서 읽은 일시정지 누적 == 5
    """
    from app.viewmodels import download_viewmodel

    item = _make_item("m3u8")
    wired.start(item)
    data = wired.submission["data"]
    data.sections_total = 3
    read: dict[str, float] = {}
    a_waits, b_done = threading.Event(), threading.Event()
    original = download_viewmodel._SectionProgress.percent

    def percent(self, *args, **kwargs):
        if threading.current_thread().name == "A":
            a_waits.set()
            assert b_done.wait(5)  # B의 통지가 끝난 뒤에 센다
        read[threading.current_thread().name] = self._paused_seconds()
        return original(self, *args, **kwargs)

    monkeypatch.setattr(download_viewmodel._SectionProgress, "percent", percent)

    def notify(paused: float) -> None:
        monkeypatch.setattr(type(data.model), "paused_seconds", property(lambda _m: paused))
        wired.submission["on_progress"](ProgressEvent(downloaded_size=10))

    a = threading.Thread(target=notify, args=(5.0,), name="A")
    a.start()
    assert a_waits.wait(5)
    b = threading.Thread(target=notify, args=(9.0,), name="B")
    b.start()
    b.join()
    b_done.set()
    a.join()
    wired.pump()

    assert read == {"A": 5.0, "B": 9.0}
