"""DownloadViewModelWeb 배선 검증 (#221, Phase B2 — #224 순환 의존 리팩터 포함).

두 그룹으로 나뉜다:
- `TestConstructsWithoutContentViewModel`/`TestDelegation`: `DownloadViewModelWeb`
  **자체의** 계약만 본다 — 콜러블 주입(#224)이 실제로 `ContentViewModelWeb`
  없이 성립하는지가 이 리팩터의 성립 증거다. `ContentViewModelWeb`을 아예
  import·구성하지 않는다.
- `TestFinishedFailedWiring`/`TestI18nWiring`: 실제 `ContentViewModelWeb`을
  꽂았을 때 배선이 맞물리는지 보는 **통합** 테스트 — 바인더(Phase C)가 할
  일(`on_started=content.start` 등)을 테스트가 대신 한다. 완료/실패가 content
  쪽 상태 전이·배치 체인까지 이어지는지, i18n이 실제로 배선되는지를 본다.
  tests/unit/test_failure_display.py(Qt 배치 체인 실배선)와 같은 의도다.

진행률 계산·실패 사유 매핑 자체는 tests/unit/test_download_bridge.py가
이미 검증했으므로 여기서는 다시 안 본다.
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


class TestConstructsWithoutContentViewModel:
    """#224 리팩터의 성립 증거 — ContentViewModelWeb을 전혀 만들지 않고도
    DownloadViewModelWeb의 콜백 계약을 완전히 검증할 수 있다."""

    def test_start_calls_on_started_and_submits_to_service(self, dispatcher, spy, tmp_path):
        started = []
        vm = DownloadViewModelWeb(dispatcher, service=FakeService(), on_started=started.append)
        item = _make_item(str(tmp_path))

        vm.start(item)
        _drain(dispatcher)

        assert started == [item]
        assert vm._bridge._service.submissions[0]["content"].url == item.vod_url
        assert vm.isDownloading()

    def test_no_callbacks_injected_is_safe_noop(self, dispatcher, tmp_path):
        """콜백을 하나도 안 넘겨도(전부 기본값) 예외 없이 동작한다."""
        vm = DownloadViewModelWeb(dispatcher, service=FakeService())
        item = _make_item(str(tmp_path))

        vm.start(item)
        vm._bridge._service.submissions[0]["on_finished"]()
        _drain(dispatcher)  # 예외가 안 나면 통과

    def test_on_finished_and_on_failed_receive_item_and_message(self, dispatcher, tmp_path):
        finished_calls = []
        failed_calls = []
        vm = DownloadViewModelWeb(
            dispatcher,
            service=FakeService(),
            on_finished=lambda item, t: finished_calls.append((item, t)),
            on_failed=lambda item, m: failed_calls.append((item, m)),
        )
        item = _make_item(str(tmp_path))

        vm.start(item)
        vm._bridge._service.submissions[0]["on_finished"]()
        _drain(dispatcher)

        assert finished_calls == [(item, "00:01:01")]
        assert failed_calls == []


class TestFinishedFailedWiring:
    """실제 ContentViewModelWeb을 꽂았을 때(=바인더가 할 일) 배선이 맞물리는지 —
    downloadState 전이와 배치 체인(다음 항목 자동 시작)까지 간다."""

    def _wire(self, dispatcher, content, service):
        return DownloadViewModelWeb(
            dispatcher,
            service=service,
            on_started=content.start,
            on_finished=content.finish,
            on_failed=content.fail,
        )

    def test_finish_advances_batch(self, dispatcher, spy, content, tmp_path):
        service = FakeService()
        vm = self._wire(dispatcher, content, service)
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
        vm = self._wire(dispatcher, content, service)
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
        vm = DownloadViewModelWeb(dispatcher, service=service, on_failed=content.fail)
        item = _make_item(str(tmp_path))
        content.items.append(item)

        vm.start(item)
        service.submissions[0]["on_failed"](OSError(28, "No space left"))
        _drain(dispatcher)

        # en_US 번역문과 다르다 — 실제로 ko_KR 카탈로그를 탔다는 증거
        assert item.stateMessage != "Failed to save the file. Check the save path and free disk space."
        assert item.stateMessage != ""


class TestDelegation:
    def test_pause_resume_stop_remove_threads_delegate_to_bridge(self, dispatcher, tmp_path):
        """DownloadViewModelWeb 자체의 위임 계약 — ContentViewModelWeb 불필요."""
        service = FakeService()
        vm = DownloadViewModelWeb(dispatcher, service=service)
        item = _make_item(str(tmp_path))
        vm.start(item)

        vm.pause()
        vm.resume()
        vm.stop()
        vm.removeThreads()

        assert vm._bridge.handle is None  # removeThreads가 정리했다
        assert not vm.isDownloading()
