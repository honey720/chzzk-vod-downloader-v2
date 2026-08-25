"""DownloadViewModelWeb 배선 검증 (#221, Phase B2).

tests/unit/test_failure_display.py(배치 체인 실배선)와 같은 의도를 웹
경로로 재현한다 — 완료/실패가 content 쪽 상태 전이·배치 체인까지 이어지는지,
i18n이 실제로 배선되는지를 본다. 진행률 계산·실패 사유 매핑 자체는
tests/unit/test_download_bridge.py가 이미 검증했으므로 여기서는 다시
안 본다.
"""

import itertools
import os

import pytest

import config.config as config_mod
from app.dispatcher import Dispatcher
from app.viewmodels.content_viewmodel_web import ContentViewModelWeb
from app.viewmodels.download_viewmodel_web import DownloadViewModelWeb
from content.data import ContentItem
from core.models.download_state import DownloadState


class SpyEvaluateJS:
    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, js: str):
        self.calls.append(js)


class FakeHandle:
    def __init__(self, data):
        self.data = data

    def elapsed_seconds(self) -> float:
        return 61.0

    def wait(self, timeout=None) -> bool:
        return True


class FakeService:
    def __init__(self):
        self.submissions: list[dict] = []

    def submit(self, content, **kwargs):
        self.submissions.append({"content": content, **kwargs})
        return FakeHandle(kwargs["data"])


def _drain(dispatcher: Dispatcher) -> None:
    while dispatcher.pump(timeout=0):
        pass


def _metadata(title="테스트 제목"):
    return {
        "title": title,
        "category": "게임",
        "channelName": "채널",
        "createdDate": "2026-07-29",
        "duration": 60,
    }


_item_id_seq = itertools.count(1)


def _make_item(download_path: str, title: str = "항목") -> ContentItem:
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
    # ASCII 전용 id — json.dumps(ensure_ascii=True) 이스케이프와 f-string 비교가
    # 어긋나는 걸 피한다 (test_content_viewmodel_web.py와 같은 이유)
    item.id = f"fixed-{next(_item_id_seq)}"
    item.output_path = f"{download_path}/{title}.mp4"
    item.downloadState = DownloadState.WAITING
    return item


def _fake_probe(directory: str):
    return (True, "") if os.path.isdir(directory) else (False, "missing")


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch):
    """실 config.json 무접촉 — language 키만 기본값으로 격리한다."""
    store = {"language": "en_US"}
    monkeypatch.setattr(config_mod, "load_config", lambda: dict(store))
    return store


@pytest.fixture
def spy():
    return SpyEvaluateJS()


@pytest.fixture
def dispatcher(spy):
    return Dispatcher(evaluate_js=spy)


@pytest.fixture
def content(dispatcher):
    return ContentViewModelWeb(
        dispatcher,
        worker_factory=lambda *a, **k: None,  # 이 테스트 파일은 fetchContent를 쓰지 않는다
        probe=_fake_probe,
        messages={
            "invalid_path": lambda: "Invalid file path",
            "save_failed": lambda: "Failed to save file",
        },
    )


class TestStartWiresContentAndBridge:
    def test_start_notifies_content_and_submits_to_service(self, dispatcher, spy, content, tmp_path):
        vm = DownloadViewModelWeb(dispatcher, content, service=FakeService())
        item = _make_item(str(tmp_path))
        content.items.append(item)

        vm.start(item)
        _drain(dispatcher)

        assert f'window.__cvdv2_onItemStarted(...["{item.id}"])' in spy.calls
        assert vm._bridge._service.submissions[0]["content"].url == item.vod_url
        assert vm.isDownloading()


class TestFinishedFailedWiring:
    """WebDownloadBridge의 on_finished/on_failed가 content.finish/.fail로 이어져
    downloadState 전이와 배치 체인(다음 항목 자동 시작)까지 간다."""

    def test_finish_advances_batch(self, dispatcher, spy, content, tmp_path):
        service = FakeService()
        vm = DownloadViewModelWeb(dispatcher, content, service=service)
        item1 = _make_item(str(tmp_path), "첫 항목")
        item2 = _make_item(str(tmp_path), "둘째 항목")
        content.items.extend([item1, item2])
        requested = []
        content.on_download_requested = requested.append

        vm.start(item1)
        # 엔진이 완료 전이를 이미 마쳤다는 전제 (WebDownloadBridge._on_engine_finished와 동일)
        item1.downloadState = DownloadState.FINISHED
        service.submissions[0]["on_finished"]()
        _drain(dispatcher)

        assert item1.download_time == "00:01:01"
        assert f'window.__cvdv2_onItemFinished(...["{item1.id}", true])' in spy.calls
        # 배치 체인(emitFinishedRequest → downloadItem)이 content.finish() 내부에서
        # 이미 진행됐다 — 다음 항목이 자동으로 요청됐는지만 확인한다
        assert requested == [item2]

    def test_fail_marks_failed_and_advances_batch(self, dispatcher, spy, content, tmp_path):
        service = FakeService()
        vm = DownloadViewModelWeb(dispatcher, content, service=service)
        item1 = _make_item(str(tmp_path), "첫 항목")
        item2 = _make_item(str(tmp_path), "둘째 항목")
        content.items.extend([item1, item2])
        requested = []
        content.on_download_requested = requested.append

        vm.start(item1)
        service.submissions[0]["on_failed"](OSError(28, "No space left"))
        _drain(dispatcher)

        assert item1.downloadState is DownloadState.FAILED
        # en_US 카탈로그의 실제 번역문 — 내부 키(Failed to save file) 그대로가 아니다
        assert item1.stateMessage == "Failed to save the file. Check the save path and free disk space."
        assert f'window.__cvdv2_onItemFinished(...["{item1.id}", false])' in spy.calls
        assert requested == [item2]  # 배치가 다음 항목으로 이어진다


class TestI18nWiring:
    def test_translate_uses_configured_language_catalog(self, dispatcher, content, tmp_path, monkeypatch):
        monkeypatch.setattr(config_mod, "load_config", lambda: {"language": "ko_KR"})
        service = FakeService()
        vm = DownloadViewModelWeb(dispatcher, content, service=service)
        item = _make_item(str(tmp_path))
        content.items.append(item)

        vm.start(item)
        service.submissions[0]["on_failed"](OSError(28, "No space left"))
        _drain(dispatcher)

        # en_US 번역문과 다르다 — 실제로 ko_KR 카탈로그를 탔다는 증거
        assert item.stateMessage != "Failed to save the file. Check the save path and free disk space."
        assert item.stateMessage != ""


class TestDelegation:
    def test_pause_resume_stop_remove_threads_delegate_to_bridge(
        self, dispatcher, content, tmp_path
    ):
        service = FakeService()
        vm = DownloadViewModelWeb(dispatcher, content, service=service)
        item = _make_item(str(tmp_path))
        content.items.append(item)
        vm.start(item)

        vm.pause()
        vm.resume()
        vm.stop()
        vm.removeThreads()

        assert vm._bridge.handle is None  # removeThreads가 정리했다
        assert not vm.isDownloading()
