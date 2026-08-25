"""ContentViewModelWeb 계약 검증 (#220, Phase B1).

tests/unit/test_content_manager.py(LOADING 자리표시·교체·삭제·에러 경로)와
tests/unit/test_write_probe.py(다운로드 게이트)·tests/unit/test_failure_display.py
(배치 체인)가 이미 고정해둔 시나리오를 **같은 의도로** 다시 짠다 — 관찰
지점만 "Signal emit 스파이"에서 "Dispatcher에 들어온 evaluate_js 호출
스파이"로 바꿨다. 새로 설계하는 시나리오가 아니라 이식이다.
"""

import concurrent.futures
import itertools
import os

import pytest

from app.dispatcher import Dispatcher
from app.viewmodels.content_viewmodel_web import ContentViewModelWeb
from content.data import ContentItem
from core.models.download_state import DownloadState


class FakeWorker:
    """ContentWorkerWeb과 같은 run(on_finished, on_error) 계약의 가짜 워커."""

    #: None이면 성공 결과를, 문자열이면 그 메시지로 on_error를 부른다
    fail_message = None

    def __init__(self, vod_url, cookies, downloadPath):
        self.vod_url = vod_url
        self.downloadPath = downloadPath

    def run(self, on_finished, on_error):
        if self.fail_message is not None:
            on_error(self.fail_message)
            return
        result = (
            self.vod_url,
            _metadata("제목:" + self.vod_url),
            [["1080", "http://example.invalid/1080"]],
            "1080",
            "http://example.invalid/1080",
            self.downloadPath,
            None,
        )
        on_finished(result, "video")


class SpyEvaluateJS:
    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, js: str):
        self.calls.append(js)


def _metadata(title="테스트 제목"):
    return {
        "title": title,
        "category": "게임",
        "channelName": "채널",
        "createdDate": "2026-07-29",
        "duration": 60,
    }


_item_id_seq = itertools.count(1)


def _make_item(state, title="완성 아이템", download_path=".") -> ContentItem:
    item = ContentItem(
        "https://chzzk.naver.com/video/1",
        _metadata(title),
        [["1080", "http://example.invalid/1080"]],
        "1080",
        "http://example.invalid/1080",
        download_path,
        "video",
        None,
    )
    # ASCII 전용 id — json.dumps(ensure_ascii=True)가 이스케이프하는 문자와
    # 테스트 f-string 리터럴 비교가 어긋나는 걸 피한다 (한글 title과는 별개)
    item.id = f"fixed-{next(_item_id_seq)}"
    item.downloadState = state
    return item


def _drain(vm: ContentViewModelWeb) -> None:
    """워커 스레드 제출이 끝나고 큐 전달이 백엔드 스레드에 도착할 때까지 처리한다."""
    if vm._pendingFutures:
        concurrent.futures.wait(vm._pendingFutures, timeout=3)
    while vm._dispatcher.pump(timeout=0):
        pass


@pytest.fixture
def spy():
    return SpyEvaluateJS()


@pytest.fixture
def dispatcher(spy):
    return Dispatcher(evaluate_js=spy)


def _fake_probe(directory: str) -> tuple[bool, str]:
    """실제 content.manager.probe_writable의 '없으면 missing' 판정만 흉내낸다.

    쓰기 가능 여부 자체(denied/timeout)는 이 테스트 파일이 검증할 대상이
    아니다 — tests/unit/test_write_probe.py가 이미 고정해뒀다. 여기서는
    "존재하는 폴더면 통과"만 있으면 배선 검증에 충분하다.
    """
    return (True, "") if os.path.isdir(directory) else (False, "missing")


@pytest.fixture
def vm(dispatcher):
    return ContentViewModelWeb(
        dispatcher,
        worker_factory=FakeWorker,
        probe=_fake_probe,
        messages={
            "invalid_path": lambda: "Invalid file path",
            "save_failed": lambda: "Failed to save file",
        },
    )


class TestFindItemGate:
    def test_loading_item_is_not_download_target(self, vm):
        vm.fetchContent("https://chzzk.naver.com/video/1", {}, ".")
        found, item, _ = vm.findItem()
        assert not found
        assert item is None
        _drain(vm)

    def test_waiting_item_is_still_found_when_another_is_loading(self, vm):
        waiting = _make_item(DownloadState.WAITING)
        vm.items.append(waiting)
        vm.fetchContent("https://chzzk.naver.com/video/2", {}, ".")
        found, item, _ = vm.findItem()
        assert found
        assert item is waiting
        _drain(vm)

    def test_finished_and_failed_are_still_skipped(self, vm):
        vm.items.append(_make_item(DownloadState.FINISHED, "완료"))
        vm.items.append(_make_item(DownloadState.FAILED, "실패"))
        found, _, _ = vm.findItem()
        assert not found

    def test_has_loading_items(self, vm):
        assert not vm.hasLoadingItems()
        vm.fetchContent("https://chzzk.naver.com/video/1", {}, ".")
        assert vm.hasLoadingItems()
        _drain(vm)
        assert not vm.hasLoadingItems()


class TestFetchPath:
    def test_placeholder_becomes_waiting_item_after_fetch(self, vm):
        vm.fetchContent("https://chzzk.naver.com/video/1", {}, ".")
        _drain(vm)

        assert len(vm.items) == 1
        item = vm.items[0]
        assert item.downloadState == DownloadState.WAITING
        assert item.title == "제목:https://chzzk.naver.com/video/1"
        assert not vm._pendingPlaceholders

    def test_placeholder_id_is_carried_over_to_completed_item(self, vm):
        """카드 정체성 이어받기 (#214) — 재발급되면 JS 쪽에서 다른 카드로 보인다."""
        vm.fetchContent("https://chzzk.naver.com/video/1", {}, ".")
        placeholder_id = vm.items[0].id
        _drain(vm)

        assert vm.items[0].id == placeholder_id

    def test_multiple_urls_all_complete_in_order(self, vm):
        urls = [f"https://chzzk.naver.com/video/{i}" for i in range(3)]
        for url in urls:
            vm.fetchContent(url, {}, ".")
        _drain(vm)

        assert len(vm.items) == 3
        for url, item in zip(urls, vm.items):
            assert item.downloadState == DownloadState.WAITING, url
            assert item.vod_url == url

    def test_replacement_keeps_row_position(self, vm):
        first = _make_item(DownloadState.WAITING, title="앞 아이템")
        vm.items.append(first)
        vm.fetchContent("https://chzzk.naver.com/video/2", {}, ".")
        last = _make_item(DownloadState.WAITING, title="뒤 아이템")
        vm.items.append(last)
        _drain(vm)

        assert len(vm.items) == 3
        assert vm.items[0] is first
        assert vm.items[1].downloadState == DownloadState.WAITING
        assert vm.items[1].vod_url == "https://chzzk.naver.com/video/2"
        assert vm.items[2] is last

    def test_result_is_discarded_when_placeholder_was_deleted(self, vm):
        vm.fetchContent("https://chzzk.naver.com/video/1", {}, ".")
        placeholder = vm.items[0]
        vm.removeItem(placeholder)
        _drain(vm)

        assert vm.items == []
        assert not vm._pendingPlaceholders

    def test_fetch_dispatches_insert_then_update_events(self, vm, spy):
        vm.fetchContent("https://chzzk.naver.com/video/1", {}, ".")
        placeholder_id = vm.items[0].id
        _drain(vm)

        assert spy.calls == [
            f'window.__cvdv2_onItemInserted(...["{placeholder_id}", 1])',
            f'window.__cvdv2_onItemUpdated(...["{placeholder_id}"])',
        ]


class TestFetchErrorPath:
    def test_placeholder_is_removed_and_error_dispatched(self, vm, spy, monkeypatch):
        # 클래스 속성으로 미리 실패를 고정한다 — fetchContent가 즉시 워커를
        # 스레드풀에 제출하므로, 인스턴스 생성 후에 세팅하면 이미 run()이
        # 시작돼버리는 레이스가 있다(test_content_manager.py와 같은 이유)
        monkeypatch.setattr(FakeWorker, "fail_message", "boom")
        vm.fetchContent("https://chzzk.naver.com/video/1", {}, ".")
        placeholder_id = vm.items[0].id
        _drain(vm)

        assert vm.items == []
        assert not vm._pendingPlaceholders
        assert spy.calls[-1] == 'window.__cvdv2_onContentError(...["boom"])'
        assert any(f'onItemDeleted(...["{placeholder_id}"' in c for c in spy.calls)

    def test_error_after_manual_delete_only_dispatches_message(self, vm, spy, monkeypatch):
        monkeypatch.setattr(FakeWorker, "fail_message", "boom")
        vm.fetchContent("https://chzzk.naver.com/video/1", {}, ".")
        vm.removeItem(vm.items[0])
        _drain(vm)

        assert vm.items == []
        assert spy.calls[-1] == 'window.__cvdv2_onContentError(...["boom"])'


class TestDownloadItemWriteGate:
    def test_unwritable_path_fails_before_start(self, dispatcher, spy, tmp_path):
        vm = ContentViewModelWeb(
            dispatcher,
            worker_factory=FakeWorker,
            probe=lambda d: (False, "denied"),
            messages={
                "invalid_path": lambda: "Invalid file path",
                "save_failed": lambda: "Failed to save file",
            },
        )
        requested = []
        vm.on_download_requested = requested.append
        item = _make_item(DownloadState.WAITING, "쓰기 불가 항목", str(tmp_path))
        vm.items.append(item)

        vm.downloadItem()
        while dispatcher.pump(timeout=0):
            pass

        assert requested == []
        assert item.downloadState is DownloadState.FAILED
        assert item.stateMessage == "Failed to save file"
        assert spy.calls[-1] == 'window.__cvdv2_onAllFinished(...[])'

    def test_missing_path_keeps_invalid_path_message(self, vm, tmp_path):
        item = _make_item(DownloadState.WAITING, "경로 없음", str(tmp_path / "없는폴더"))
        vm.items.append(item)

        vm.downloadItem()
        _drain(vm)

        assert item.downloadState is DownloadState.FAILED
        assert item.stateMessage == "Invalid file path"

    def test_writable_path_proceeds_to_download(self, vm, tmp_path):
        requested = []
        vm.on_download_requested = requested.append
        item = _make_item(DownloadState.WAITING, "정상 항목", str(tmp_path))

        vm.items.append(item)
        vm.downloadItem()

        assert requested == [item]
        assert item.downloadState is DownloadState.WAITING  # 시작 전이 — 상태 전이는 브리지 몫


class TestBatchChain:
    def test_finish_dispatches_and_advances_batch(self, vm, spy, tmp_path):
        item1 = _make_item(DownloadState.WAITING, "첫 항목", str(tmp_path))
        item2 = _make_item(DownloadState.WAITING, "둘째 항목", str(tmp_path))
        vm.items.extend([item1, item2])
        requested = []
        vm.on_download_requested = requested.append

        vm.downloadItem()
        assert requested == [item1]

        # finish()는 완료 전이를 하지 않는다(원본 content_viewmodel.py와 동일 —
        # 엔진이 이미 전이를 마쳤다는 전제) — 실제 엔진이 하는 일을 흉내낸다
        item1.downloadState = DownloadState.FINISHED
        vm.finish(item1, "00:01:00")
        _drain(vm)

        assert f'window.__cvdv2_onItemFinished(...["{item1.id}", true])' in spy.calls
        assert requested == [item1, item2]  # 배치가 다음 항목으로 이어진다

    def test_fail_marks_failed_and_advances_batch(self, vm, spy, tmp_path):
        item1 = _make_item(DownloadState.WAITING, "첫 항목", str(tmp_path))
        item2 = _make_item(DownloadState.WAITING, "둘째 항목", str(tmp_path))
        vm.items.extend([item1, item2])
        requested = []
        vm.on_download_requested = requested.append

        vm.downloadItem()
        vm.fail(item1, "Postprocessing failed")
        _drain(vm)

        assert item1.downloadState is DownloadState.FAILED
        assert f'window.__cvdv2_onItemFinished(...["{item1.id}", false])' in spy.calls
        assert requested == [item1, item2]

    def test_all_failed_batch_reaches_end(self, vm, spy, tmp_path):
        item = _make_item(DownloadState.WAITING, "단독 항목", str(tmp_path))
        vm.items.append(item)

        vm.downloadItem()
        vm.fail(item, "boom")
        _drain(vm)

        assert item.downloadState is DownloadState.FAILED
        assert spy.calls[-1] == 'window.__cvdv2_onAllFinished(...[])'
        assert vm.downloadResultCounts() == (0, 1)


class TestDownloadResultCounts:
    def test_counts_reflect_current_items(self, vm, tmp_path):
        finished = _make_item(DownloadState.FINISHED, "완료", str(tmp_path))
        failed = _make_item(DownloadState.FAILED, "실패", str(tmp_path))
        waiting = _make_item(DownloadState.WAITING, "대기", str(tmp_path))
        vm.items.extend([finished, failed, waiting])

        assert vm.downloadResultCounts() == (1, 1)
