"""DownloadService 오케스트레이션 규칙 검증 (#75).

실제 다운로더 엔진 대신 페이크 엔진을 주입(monkeypatch)해 서비스의
오케스트레이션 — 다운로더 선택, 큐잉·동시 실행 수 제한, 상태 전이,
콜백 중계, resolver 주입 — 만 검증한다. 엔진 자체의 규칙은
test_file_downloader_*·test_m3u8_downloader_*가 박제한다.
"""

import threading

import pytest

import core.services.download_service as download_service_module
from core.models.content import Content, ContentType
from core.models.download_state import DownloadState
from core.services.download_service import DownloadService

WAIT = 5.0  # 스레드 동기화 대기 상한(초) — 초과는 테스트 실패


class FakeEngine:
    """엔진 계약(생성자 시그니처·run·emit_progress)만 흉내 내는 페이크.

    release Event가 set될 때까지 run()이 블로킹해 동시 실행 검증을 가능하게 한다.
    """

    def __init__(self, data, logger, on_progress=None, on_finished=None, on_failed=None, **_):
        self.data = data
        self.logger = logger
        self.on_progress = on_progress
        self.on_finished = on_finished
        self.on_failed = on_failed
        self.started = threading.Event()
        self.release = threading.Event()
        self.fail_with: BaseException | None = None
        self.final_progress_calls = 0

    def run(self):
        self.started.set()
        assert self.release.wait(WAIT), "테스트가 release를 set하지 않았다"
        if self.fail_with is not None:
            self.on_failed(self.fail_with)
        else:
            self.on_finished()

    def emit_progress(self):
        self.final_progress_calls += 1


class FakeEngineFactory:
    """서비스 모듈의 엔진 심볼을 대체해 생성된 페이크 엔진을 수집한다."""

    def __init__(self):
        self.instances: list[FakeEngine] = []
        self.created = threading.Event()

    def __call__(self, **kwargs):
        engine = FakeEngine(**kwargs)
        self.instances.append(engine)
        self.created.set()
        return engine


class RecordingLogger:
    """호출된 로그 메서드 이름을 기록하는 DownloadLogger 호환 페이크."""

    def __init__(self):
        self.calls: list[str] = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append(name)

        return record


def _make_content(content_type: ContentType = ContentType.CHZZK_VIDEO) -> Content:
    return Content(
        content_type=content_type,
        url="https://chzzk.naver.com/video/1",
        output_path="out.mp4",
        resolution=720,
        base_url="https://cdn.example/video.mp4",
    )


@pytest.fixture
def file_factory(monkeypatch):
    factory = FakeEngineFactory()
    monkeypatch.setattr(download_service_module, "FileDownloader", factory)
    return factory


@pytest.fixture
def m3u8_factory(monkeypatch):
    factory = FakeEngineFactory()
    monkeypatch.setattr(download_service_module, "M3U8Downloader", factory)
    return factory


def _finish(engine: FakeEngine):
    """페이크 엔진을 정상 완료 경로로 종료시킨다."""
    engine.release.set()


class TestDownloaderSelection:
    """컨텐츠 타입 → 다운로더 선택 규칙."""

    @pytest.mark.parametrize("content_type", [ContentType.CHZZK_VIDEO, ContentType.CHZZK_CLIP])
    def test_video_and_clip_use_file_downloader(self, file_factory, content_type):
        service = DownloadService()
        handle = service.submit(_make_content(content_type))
        assert file_factory.created.wait(WAIT)
        _finish(file_factory.instances[0])
        assert handle.wait(WAIT)

    def test_m3u8_uses_m3u8_downloader_and_resolver(self, m3u8_factory):
        """m3u8은 시작 시점에 resolver로 base_url을 해석한 뒤 엔진을 실행한다."""
        resolved: list[Content] = []

        def resolver(content: Content) -> str:
            resolved.append(content)
            return "https://cdn.example/playlist.m3u8"

        service = DownloadService(base_url_resolver=resolver)
        content = _make_content(ContentType.CHZZK_VIDEO_M3U8)
        content.base_url = None
        handle = service.submit(content)

        assert m3u8_factory.created.wait(WAIT)
        engine = m3u8_factory.instances[0]
        assert engine.started.wait(WAIT)
        # 해석 결과가 엔진 공유 데이터(base_url)에 반영된 뒤 run이 시작된다
        assert handle.data.base_url == "https://cdn.example/playlist.m3u8"
        assert resolved == [content]
        _finish(engine)
        assert handle.wait(WAIT)

    def test_m3u8_without_resolver_fails(self, m3u8_factory):
        """resolver 미주입 상태의 m3u8 제출은 실패 콜백으로 통지된다 (엔진 미실행)."""
        failures: list[BaseException] = []
        recording = RecordingLogger()
        service = DownloadService()
        content = _make_content(ContentType.CHZZK_VIDEO_M3U8)
        handle = service.submit(content, task_logger=recording, on_failed=failures.append)

        assert handle.wait(WAIT)
        assert len(failures) == 1
        assert not m3u8_factory.instances[0].started.is_set()
        # 구 어댑터와 동일한 실패 로깅·로그 마감
        assert "log_exception" in recording.calls
        assert "save_and_close" in recording.calls

    def test_resolver_failure_notifies_failed(self, m3u8_factory):
        """resolver가 던진 예외는 실패 콜백으로 그대로 전달된다."""
        boom = RuntimeError("resolve 실패")
        failures: list[BaseException] = []

        def resolver(content: Content) -> str:
            raise boom

        service = DownloadService(base_url_resolver=resolver)
        handle = service.submit(
            _make_content(ContentType.CHZZK_VIDEO_M3U8), on_failed=failures.append
        )
        assert handle.wait(WAIT)
        assert failures == [boom]

    def test_unsupported_type_raises(self, file_factory):
        with pytest.raises(ValueError):
            DownloadService().submit(_make_content(ContentType.CHZZK_LIVE))

    def test_mismatched_data_raises(self, file_factory):
        from core.models.download_data import DownloadData

        content = _make_content()
        other = DownloadData.from_content(_make_content())
        with pytest.raises(ValueError):
            DownloadService().submit(content, data=other)


class TestConcurrencyAndQueue:
    """동시 실행 수 제한과 큐잉."""

    def test_default_limit_is_one_and_queues(self, file_factory):
        """기본값(1)에서는 두 번째 제출이 첫 건 완료까지 대기한다 — 현행 GUI와 동일."""
        service = DownloadService()
        h1 = service.submit(_make_content())
        h2 = service.submit(_make_content())

        assert file_factory.created.wait(WAIT)
        assert file_factory.instances[0].started.wait(WAIT)
        # 첫 건이 실행 중인 동안 두 번째 엔진은 만들어지지 않는다
        assert len(file_factory.instances) == 1
        assert service.pending_count == 1

        _finish(file_factory.instances[0])
        assert h1.wait(WAIT)
        # 첫 건 종료 후 큐의 다음 건이 시작된다
        assert _wait_for_second(file_factory)
        assert file_factory.instances[1].started.wait(WAIT)
        _finish(file_factory.instances[1])
        assert h2.wait(WAIT)

    def test_limit_two_runs_concurrently(self, file_factory):
        service = DownloadService(max_concurrent=2)
        h1 = service.submit(_make_content())
        h2 = service.submit(_make_content())

        assert _wait_for_count(file_factory, 2)
        assert file_factory.instances[0].started.wait(WAIT)
        assert file_factory.instances[1].started.wait(WAIT)

        for engine in file_factory.instances:
            _finish(engine)
        assert h1.wait(WAIT) and h2.wait(WAIT)

    def test_invalid_limit_raises(self):
        with pytest.raises(ValueError):
            DownloadService(max_concurrent=0)

    def test_stop_before_start_skips_engine(self, file_factory):
        """큐 대기 중 중지된 건은 엔진을 실행하지 않고 종료 처리된다."""
        service = DownloadService()
        h1 = service.submit(_make_content())
        h2 = service.submit(_make_content())
        assert file_factory.instances[0].started.wait(WAIT)

        h2.stop()
        _finish(file_factory.instances[0])
        assert h1.wait(WAIT)
        assert h2.wait(WAIT)
        # 취소된 건의 엔진은 만들어지지 않는다
        assert len(file_factory.instances) == 1


class TestLifecycleAndCallbacks:
    """상태 전이와 콜백 중계."""

    def test_finish_flow(self, file_factory):
        """완료 시: FINISHED 전이 → 최종 진행 통지 → 완료 콜백 (구 manager.finish 순서)."""
        finished = threading.Event()
        states: list[DownloadState] = []
        service = DownloadService()
        handle = service.submit(
            _make_content(), on_state_change=states.append, on_finished=finished.set
        )

        engine = _first(file_factory)
        assert engine.started.wait(WAIT)
        assert handle.state is DownloadState.RUNNING
        _finish(engine)

        assert finished.wait(WAIT)
        assert handle.wait(WAIT)
        assert handle.state is DownloadState.FINISHED
        assert engine.final_progress_calls == 1
        assert states == [DownloadState.RUNNING, DownloadState.FINISHED]

    def test_pre_started_model_is_idempotent(self, file_factory):
        """호출자(브리지)가 먼저 RUNNING 전이를 마쳐도 실행에 지장이 없다."""
        from core.models.download_data import DownloadData

        content = _make_content()
        data = DownloadData.from_content(content)
        data.model.start()

        service = DownloadService()
        handle = service.submit(content, data=data)
        engine = _first(file_factory)
        assert engine.started.wait(WAIT)
        _finish(engine)
        assert handle.wait(WAIT)
        assert handle.state is DownloadState.FINISHED

    def test_failed_relays_exception_without_transition(self, file_factory):
        """실패는 예외 객체 그대로 콜백 통지만 한다 — 상태 정리는 어댑터 몫 (구 흐름 보존)."""
        boom = RuntimeError("다운로드 실패")
        failures: list[BaseException] = []
        service = DownloadService()
        handle = service.submit(_make_content(), on_failed=failures.append)

        engine = _first(file_factory)
        assert engine.started.wait(WAIT)
        engine.fail_with = boom
        engine.release.set()

        assert handle.wait(WAIT)
        assert failures == [boom]
        assert handle.state is DownloadState.RUNNING

    def test_progress_callback_relays_events(self, file_factory):
        """엔진 진행 콜백이 제출 시 등록한 콜백으로 전달된다."""
        from core.models.events import ProgressEvent

        events: list[ProgressEvent] = []
        service = DownloadService()
        handle = service.submit(_make_content(), on_progress=events.append)

        engine = _first(file_factory)
        assert engine.started.wait(WAIT)
        event = ProgressEvent(downloaded_size=10, total_size=100, speed=1.0, active_threads=2)
        engine.on_progress(event)
        _finish(engine)
        assert handle.wait(WAIT)
        assert events == [event]


def _first(factory: FakeEngineFactory) -> FakeEngine:
    assert factory.created.wait(WAIT)
    return factory.instances[0]


def _wait_for_count(factory: FakeEngineFactory, count: int, timeout: float = WAIT) -> bool:
    """팩토리가 count개 이상의 엔진을 만들 때까지 폴링 대기한다."""
    deadline = threading.Event()
    for _ in range(int(timeout / 0.05)):
        if len(factory.instances) >= count:
            return True
        deadline.wait(0.05)
    return len(factory.instances) >= count


def _wait_for_second(factory: FakeEngineFactory) -> bool:
    return _wait_for_count(factory, 2)
