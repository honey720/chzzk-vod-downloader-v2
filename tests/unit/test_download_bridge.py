"""WebDownloadBridge 배선 검증 (#210) — download/qt_bridge.py의 웹 버전.

`tests/unit/test_qt_bridge.py`와 같은 시나리오를 같은 페이크(FakeHandle/
FakeService/FakeLogger)로 검증한다 — Signal.connect 대신 Dispatcher에
스파이 evaluate_js를 꽂고, 큐를 pump()로 비워 JS 호출 여부를 확인한다.
워커 스레드 콜백(on_finished/on_failed)은 dispatch()로 큐에만 들어가고
실제 JS 호출은 pump 이후에만 일어난다는 것 자체도 검증 대상이다.
"""

import pytest
import requests

from app.dispatcher import Dispatcher
from app.download_bridge import WebDownloadBridge, _failure_message_key
from content.data import ContentItem
from core.downloaders.base import PostprocessError
from core.downloaders.hls_aes_downloader import DecryptionError
from core.models.download_state import DownloadState
from core.models.events import ProgressEvent
from core.utils.ffmpeg import FFmpegNotFoundError, RemuxError


class FakeHandle:
    """DownloadHandle 대역 — test_qt_bridge.py와 동일."""

    def __init__(self, data):
        self.data = data
        self.wait_calls = 0

    def elapsed_seconds(self) -> float:
        return 61.0

    def wait(self, timeout=None) -> bool:
        self.wait_calls += 1
        return True


class FakeService:
    """DownloadService 대역 — test_qt_bridge.py와 동일."""

    def __init__(self):
        self.submissions: list[dict] = []
        self.abandoned: list = []

    def submit(self, content, **kwargs):
        self.submissions.append({"content": content, **kwargs})
        return FakeHandle(kwargs["data"])

    def abandon(self, handle):
        self.abandoned.append(handle)


class FakeLogger:
    """DownloadLogger 대역 — 파일 생성 없이 호출 이름만 기록한다."""

    def __init__(self, *args, **kwargs):
        self.calls: list[str] = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append(name)

        return record


class SpyEvaluateJS:
    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, js: str):
        self.calls.append(js)


def _make_item(content_type: str = "video") -> ContentItem:
    item = ContentItem(
        "https://chzzk.naver.com/video/1",
        {"title": "t"},
        [(720, "base")],
        720,
        "https://cdn.example/video.mp4",
        "downloads",
        content_type,
        None,
    )
    item.output_path = "downloads/t 720p.mp4"
    return item


def _drain(dispatcher: Dispatcher) -> None:
    """큐가 빌 때까지 pump한다 — 큐 안 액션이 또 다른 액션을 큐에 넣는 경우
    (예: _on_engine_finished가 dispatch_js를 호출)를 전부 처리하기 위함."""
    while dispatcher.pump(timeout=0):
        pass


@pytest.fixture
def spy():
    return SpyEvaluateJS()


@pytest.fixture
def dispatcher(spy):
    return Dispatcher(evaluate_js=spy)


@pytest.fixture
def bridge(dispatcher, monkeypatch):
    monkeypatch.setattr("app.download_bridge.DownloadLogger", FakeLogger)
    return WebDownloadBridge(dispatcher, service=FakeService())


def _submission(bridge: WebDownloadBridge) -> dict:
    return bridge._service.submissions[0]


class TestStart:
    def test_start_submits_content_with_shared_data(self, bridge):
        item = _make_item()
        bridge.start("item-1", item)

        sub = _submission(bridge)
        assert sub["content"] is sub["data"].content
        assert sub["content"].url == item.vod_url
        assert sub["content"].output_path == item.output_path
        assert bridge.task.state is DownloadState.RUNNING
        assert item.downloadState is DownloadState.RUNNING
        assert bridge.item_id == "item-1"


class TestProgressRelay:
    """진행 콜백은 워커 스레드에서 오지만, dispatch_js는 즉시 큐에 넣을 뿐이다
    (evaluate_js 자체는 pump 전까지 안 불린다 — 이것도 검증 대상)."""

    def test_file_progress_conversion_dispatches_but_does_not_call_evaluate_js_immediately(
        self, bridge, spy
    ):
        item = _make_item()
        bridge.start("item-1", item)

        on_progress = _submission(bridge)["on_progress"]
        on_progress(ProgressEvent(downloaded_size=50, total_size=100, speed=1.0))

        assert spy.calls == []  # pump 전에는 evaluate_js가 안 불린다

        _drain(bridge._dispatcher)

        assert spy.calls == [
            'window.__cvdv2_onProgress(...["item-1", "00:00:00", "50", "1.0 MB/s", 50])'
        ]

    def test_m3u8_progress_conversion_and_merge_flag(self, bridge, spy):
        item = _make_item("m3u8")
        bridge.start("item-1", item)

        sub = _submission(bridge)
        data = sub["data"]
        data.max_threads = 10
        data.completed_threads = 5
        sub["on_progress"](ProgressEvent(downloaded_size=500, speed=0.0))
        _drain(bridge._dispatcher)
        assert spy.calls[-1] == 'window.__cvdv2_onProgress(...["item-1", "N/A", "500", "0.0 MB/s", 50])'

        # 병합 시작은 큐를 거치지 않고 즉시 플래그를 기록한다 (qt_bridge.py와 동일)
        sub["on_merge_start"]()
        assert item.post_process is True
        data.merged_segments = 11
        sub["on_progress"](ProgressEvent(downloaded_size=500, speed=0.0))
        _drain(bridge._dispatcher)
        assert spy.calls[-1] == 'window.__cvdv2_onProgress(...["item-1", "N/A", "500", "0.0 MB/s", 100])'


class TestCompletion:
    def test_finished_only_reaches_js_after_pump(self, bridge, spy):
        """on_finished는 워커 스레드 콜백 — dispatch()로 큐에만 들어가고,
        실제 후처리(_on_engine_finished)와 JS 호출은 pump가 실행할 때 일어난다."""
        item = _make_item()
        bridge.start("item-1", item)

        _submission(bridge)["on_finished"]()

        assert spy.calls == []  # 아직 아무것도 안 불렸다 — 워커 스레드는 큐에 넣기만 했다
        assert bridge.handle is not None  # 후처리(참조 정리)도 아직 안 일어났다

        _drain(bridge._dispatcher)

        assert spy.calls == ['window.__cvdv2_onFinished(...["item-1", "00:01:01"])']
        assert bridge.handle is None
        assert bridge.task is None

    def test_failed_stops_engine_and_dispatches_reason_without_waiting(self, bridge, spy):
        """실패 (#134): 엔진 종료 신호(stop→WAITING) 후 실패 사유를 dispatch한다.

        test_qt_bridge.py의 test_failed_stops_engine_and_emits_reason_without_waiting과
        같은 시나리오 — #180 조사 근거(PostprocessError 원인 체인에 따른 문구 분기),
        PR #135 코멘트 근거(handle.wait() 금지, 죽은 마운트 I/O 프리즈 회귀 방지)도 동일.
        """
        item = _make_item("m3u8")
        bridge.start("item-1", item)
        item.post_process = True
        handle = bridge.handle
        model = _submission(bridge)["data"].model

        raw = "후처리(remux) 실패: ffmpeg stderr tail... [C:\\tools\\ffmpeg.exe]"
        pp_error = PostprocessError(raw)
        pp_error.__cause__ = RemuxError("ffmpeg remux 실패 (exit 183): Invalid data found")
        _submission(bridge)["on_failed"](pp_error)

        # 병합 표시 해제는 즉시(워커 스레드에서 바로) — qt_bridge.py와 동일
        assert item.post_process is False
        assert spy.calls == []  # JS 통지는 아직

        _drain(bridge._dispatcher)

        assert model.state is DownloadState.WAITING
        assert handle.wait_calls == 0  # 프리즈 회귀 방지 — 실패 경로에서 엔진을 기다리지 않는다
        assert spy.calls == [
            'window.__cvdv2_onFailed(...["item-1", "Postprocessing failed - invalid segments"])'
        ]
        assert bridge.handle is None
        assert bridge.task is None

    def test_unmapped_failure_dispatches_empty_reason(self, bridge, spy):
        item = _make_item()
        bridge.start("item-1", item)
        model = _submission(bridge)["data"].model

        _submission(bridge)["on_failed"](RuntimeError("raw internal detail"))
        _drain(bridge._dispatcher)

        assert spy.calls == ['window.__cvdv2_onFailed(...["item-1", ""])']
        assert model.state is DownloadState.WAITING

    def test_failure_after_user_cleanup_is_ignored(self, bridge, spy):
        """중지·정리 후 늦게 도착한 실패는 무시한다 (완료 경로의 가드와 동일)."""
        item = _make_item()
        bridge.start("item-1", item)
        bridge.stop()
        bridge.removeThreads()
        _drain(bridge._dispatcher)
        spy.calls.clear()  # stop()이 이미 dispatch_js한 onStopped 콜은 이 테스트 관심사가 아님

        # 워커 스레드가 늦게 도착시킨 실패 콜백 — handle이 이미 None이다
        bridge._relay_failed(RuntimeError("late"))
        _drain(bridge._dispatcher)

        assert spy.calls == []  # _on_engine_failed의 handle is None 가드가 무시했다
        assert item.downloadState is DownloadState.WAITING

    def test_user_stop_dispatches_js_immediately(self, bridge, spy):
        item = _make_item()
        bridge.start("item-1", item)

        bridge.stop()
        _drain(bridge._dispatcher)

        assert spy.calls == ['window.__cvdv2_onStopped(...["item-1"])']
        assert bridge.task.state is DownloadState.WAITING

    def test_pause_resume_dispatch_js_with_item_id(self, bridge, spy):
        item = _make_item()
        bridge.start("item-1", item)

        bridge.pause()
        bridge.resume()
        _drain(bridge._dispatcher)

        assert spy.calls == [
            'window.__cvdv2_onPaused(...["item-1"])',
            'window.__cvdv2_onResumed(...["item-1"])',
        ]


class StuckHandle(FakeHandle):
    """파일 I/O에 갇힌 워커 흉내 — wait가 절대 끝나지 않는다 (#136·#137)."""

    def __init__(self, data):
        super().__init__(data)
        self.wait_timeouts: list = []

    def wait(self, timeout=None) -> bool:
        self.wait_timeouts.append(timeout)
        return False


class TestRemoveThreads:
    def test_stuck_worker_gives_up_and_abandons_slot(self, bridge):
        item = _make_item()
        bridge.start("item-1", item)
        stuck = StuckHandle(_submission(bridge)["data"])
        bridge.handle = stuck

        bridge.removeThreads()

        assert stuck.wait_timeouts and all(t is not None and t > 0 for t in stuck.wait_timeouts)
        assert bridge._service.abandoned == [stuck]
        assert bridge.handle is None
        assert bridge.task is None

    def test_normal_exit_is_not_abandoned(self, bridge):
        item = _make_item()
        bridge.start("item-1", item)
        handle = bridge.handle

        bridge.removeThreads()

        assert handle.wait_calls == 1
        assert bridge._service.abandoned == []


class TestTranslateInjection:
    """i18n(#212)이 아직 없으므로 translate는 기본 항등 함수 — 주입하면 그걸 쓴다."""

    def test_default_translate_returns_key_unchanged(self, bridge, spy):
        item = _make_item()
        bridge.start("item-1", item)

        _submission(bridge)["on_failed"](OSError(28, "No space left"))
        _drain(bridge._dispatcher)

        assert spy.calls == ['window.__cvdv2_onFailed(...["item-1", "Failed to save file"])']

    def test_injected_translate_is_used(self, dispatcher, spy, monkeypatch):
        monkeypatch.setattr("app.download_bridge.DownloadLogger", FakeLogger)
        translated = {"Failed to save file": "파일 저장에 실패했습니다"}
        bridge = WebDownloadBridge(
            dispatcher, service=FakeService(), translate=lambda key: translated.get(key, key)
        )
        item = _make_item()
        bridge.start("item-1", item)

        _submission(bridge)["on_failed"](OSError(28, "No space left"))
        _drain(dispatcher)

        assert spy.calls == [
            'window.__cvdv2_onFailed(...["item-1", "\\ud30c\\uc77c \\uc800\\uc7a5\\uc5d0 \\uc2e4\\ud328\\ud588\\uc2b5\\ub2c8\\ub2e4"])'
        ]


class TestFinishedFailedCallbackHooks:
    """on_finished/on_failed 훅 (#221) — 미주입 시 하위 호환, 주입 시 JS 통지보다 먼저 호출된다."""

    def test_not_injected_is_backward_compatible(self, bridge, spy):
        """기본값(None)이면 지금처럼 JS 통지만 하고 끝난다 — #210 계약 무변화."""
        item = _make_item()
        bridge.start("item-1", item)

        _submission(bridge)["on_finished"]()
        _drain(bridge._dispatcher)

        assert spy.calls == ['window.__cvdv2_onFinished(...["item-1", "00:01:01"])']

    def test_on_finished_receives_item_and_download_time_before_js_dispatch(
        self, dispatcher, spy, monkeypatch
    ):
        monkeypatch.setattr("app.download_bridge.DownloadLogger", FakeLogger)
        calls = []

        def on_finished(item, download_time):
            calls.append((item, download_time))
            # 콜백 시점엔 아직 JS 통지 전이어야 한다(순서 계약)
            assert spy.calls == []

        bridge = WebDownloadBridge(dispatcher, service=FakeService(), on_finished=on_finished)
        item = _make_item()
        bridge.start("item-1", item)

        _submission(bridge)["on_finished"]()
        _drain(dispatcher)

        assert calls == [(item, "00:01:01")]
        assert spy.calls == ['window.__cvdv2_onFinished(...["item-1", "00:01:01"])']

    def test_on_failed_receives_item_and_translated_message_before_js_dispatch(
        self, dispatcher, spy, monkeypatch
    ):
        monkeypatch.setattr("app.download_bridge.DownloadLogger", FakeLogger)
        calls = []

        def on_failed(item, message):
            calls.append((item, message))
            assert spy.calls == []

        bridge = WebDownloadBridge(dispatcher, service=FakeService(), on_failed=on_failed)
        item = _make_item()
        bridge.start("item-1", item)

        _submission(bridge)["on_failed"](OSError(28, "No space left"))
        _drain(dispatcher)

        assert calls == [(item, "Failed to save file")]
        assert spy.calls == ['window.__cvdv2_onFailed(...["item-1", "Failed to save file"])']

    def test_on_finished_not_called_after_user_cleanup(self, dispatcher, spy, monkeypatch):
        """handle이 None(유저가 먼저 정리)이면 훅도 호출되지 않는다 — 기존 가드 무변화."""
        monkeypatch.setattr("app.download_bridge.DownloadLogger", FakeLogger)
        calls = []
        bridge = WebDownloadBridge(dispatcher, service=FakeService(), on_finished=calls.append)
        item = _make_item()
        bridge.start("item-1", item)
        on_finished_cb = _submission(bridge)["on_finished"]
        bridge.handle = None  # 완료 직후 사용자가 중지·정리를 마친 경우 흉내

        on_finished_cb()
        _drain(dispatcher)

        assert calls == []
        assert spy.calls == []


class _FakeHttpResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


def _http_error(status: int) -> requests.HTTPError:
    return requests.HTTPError(f"HTTP {status}", response=_FakeHttpResponse(status))


class TestFailureMessageKey:
    """실패 예외 → 안내 키 매핑 — qt_bridge.py와 완전히 동일한 로직(그대로 이식)."""

    def test_postprocess_maps_by_cause_type(self):
        not_found = PostprocessError("ffmpeg 실행 파일을 찾지 못했다")
        not_found.__cause__ = FFmpegNotFoundError("imageio-ffmpeg 패키지 미설치")
        assert _failure_message_key(not_found) == "Postprocessing failed - ffmpeg not found"

        invalid_input = PostprocessError("ffmpeg stderr...")
        invalid_input.__cause__ = RemuxError("exit 183: Invalid data found")
        assert _failure_message_key(invalid_input) == "Postprocessing failed - invalid segments"

        no_cause = PostprocessError("cause 없음")
        assert _failure_message_key(no_cause) == "Postprocessing failed - invalid segments"

        assert _failure_message_key(DecryptionError("키·IV 불일치")) == "Decryption failed"

    def test_http_statuses_map_like_metadata_path(self):
        assert _failure_message_key(_http_error(403)) == "Viewing permission required"
        assert _failure_message_key(_http_error(401)) == "Viewing permission required"
        assert _failure_message_key(_http_error(404)) == "Video not found"
        assert _failure_message_key(_http_error(500)) == "Network connection error"

    def test_transport_and_os_errors(self):
        assert _failure_message_key(requests.ConnectionError("boom")) == "Network connection error"
        assert _failure_message_key(requests.Timeout("slow")) == "Network connection error"
        assert _failure_message_key(OSError(28, "No space left", "C:\\full\\path.mp4")) == (
            "Failed to save file"
        )

    def test_unknown_exception_has_no_key(self):
        assert _failure_message_key(RuntimeError("anything")) is None
