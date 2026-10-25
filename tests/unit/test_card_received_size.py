"""카드의 받은 크기는 줄지 않는다 (#395).

엔진의 받은 양은 다시 요청하며 받다 만 세그먼트를 버리면 그만큼 준다. 카드는 전에 보인 값 아래로
내리지 않는다. 엔진 대신 제출된 진행 콜백을 부르고, content(카드 쪽)가 받은 크기를 잰다 — 실제
배선(서비스 콜백 → Signal → content)을 탄다.
"""

import pytest
from PySide6.QtCore import QObject, QTimer

from app.download_logger import DownloadLogger
from app.viewmodels.data import ContentItem
from app.viewmodels.download_viewmodel import DownloadViewModel
from core.models.events import ProgressEvent


class Content(QObject):
    """content 자리(제품에서는 ContentManager)의 대역 — 받은 진행 통지의 크기를 적는다."""

    def __init__(self):
        super().__init__()
        self.sizes: list[str] = []

    def update_progress(self, rem, size, spd, prog, item):
        self.sizes.append(size)

    def pause(self, item):
        pass

    def resume(self, item):
        pass

    def stop(self, item):
        pass

    def finish(self, item, download_time):
        pass

    def fail(self, item, message=""):
        pass


class Handle:
    """DownloadService가 돌려주는 실행 핸들의 대역."""

    def __init__(self, data):
        self.data = data

    def wait(self, timeout=None) -> bool:
        return True


class Service:
    """DownloadService 대역 — 제출된 진행 콜백을 남긴다."""

    def __init__(self):
        self.on_progress = None

    def submit(self, content, **kwargs):
        self.on_progress = kwargs["on_progress"]
        return Handle(kwargs["data"])

    def abandon(self, handle):
        pass


@pytest.fixture
def started(qapp, monkeypatch):
    """다운로드를 시작한 viewmodel과 content 대역 — 끝나면 viewmodel의 타이머를 세운다."""
    monkeypatch.setattr(DownloadLogger, "_setup_logging", lambda self: None)
    monkeypatch.setattr(DownloadLogger, "log_download_info", lambda self, item: None)
    made = []

    def start(content_type: str):
        content, service = Content(), Service()
        viewmodel = DownloadViewModel(content, service=service)
        made.append(viewmodel)
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
        viewmodel.start(item)
        return content, service

    yield start
    for viewmodel in made:
        for timer in viewmodel.findChildren(QTimer):
            timer.stop()


def _shown(qapp, content, service, sizes) -> list[str]:
    for size in sizes:
        service.on_progress(ProgressEvent(downloaded_size=size, total_size=1000, speed=1.0))
    qapp.processEvents()
    return content.sizes


@pytest.mark.parametrize("content_type", ["m3u8", "video"])
def test_card_size_does_not_go_below_what_it_showed(qapp, started, content_type):
    """받은 양이 줄어든 통지가 와도 카드의 받은 크기는 전에 보인 값 아래로 내려가지 않아야 한다 (#395).

    받은 양 100 → 300 → 200(받다 만 세그먼트를 버림) → 250 → 400
    -> 카드 "100", "300", "300", "300", "400"
    """
    content, service = started(content_type)

    shown = _shown(qapp, content, service, [100, 300, 200, 250, 400])

    assert shown == ["100", "300", "300", "300", "400"]


def test_card_size_follows_a_received_size_that_only_grows(qapp, started):
    """받은 양이 줄지 않으면 카드의 받은 크기는 받은 양 그대로여야 한다 (#395).

    받은 양 100 → 200 → 200 → 350
    -> 카드 "100", "200", "200", "350"
    """
    content, service = started("m3u8")

    shown = _shown(qapp, content, service, [100, 200, 200, 350])

    assert shown == ["100", "200", "200", "350"]
