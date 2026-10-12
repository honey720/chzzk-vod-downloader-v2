"""대기열의 디스크 왕복, 손상 복구와 화면의 명시적 재개 동작."""

import pytest
from PySide6.QtCore import QObject, Signal, Qt

from app.queue_store import QueueStore, encode_item
from app.viewmodels.data import ContentItem
from app.views.mainWindow import VodDownloader
from core.models.download_state import DownloadState
from core.utils.persistence import StorageBusyError


def item_at(directory, number=1, kind="m3u8"):
    item = ContentItem(
        f"https://chzzk.naver.com/video/{number}",
        {
            "title": f"영상 {number}",
            "channelName": "채널",
            "duration": 60,
        },
        [[1080, ""]],
        1080,
        "",
        str(directory),
        kind,
        None,
    )
    return item


@pytest.mark.parametrize("state", list(DownloadState))
def test_queue_roundtrip_restores_waiting_without_secrets(tmp_path, state):
    item = item_at(tmp_path)
    item.downloadState = state
    item.base_url = "https://cdn.invalid/video?token=SECRET"
    item.stateMessage = "URL https://cdn.invalid/video?token=SECRET"
    item.title = "편집한 제목"
    item.output_path = str(tmp_path / "chosen.mp4")
    store = QueueStore(tmp_path / "settings")
    store.load()
    store.save([encode_item(item)])
    store.close()
    saved = store.path.read_text()
    assert "SECRET" not in saved and "base_url" not in saved
    restored = QueueStore(tmp_path / "settings")
    try:
        items = restored.load()
        if state == DownloadState.FINISHED:
            assert items == []
        else:
            assert len(items) == 1
            got = items[0]
            assert got.downloadState == DownloadState.WAITING
            assert got.needs_refresh and got.base_url == ""
            assert got.queue_id == item.queue_id
            assert (got.title, got.resolution, got.output_path) == (
                item.title,
                1080,
                item.output_path,
            )
    finally:
        restored.close()


def test_order_duplicates_removal_backup_and_single_writer(tmp_path):
    directory = tmp_path / "settings"
    first, duplicate, other = (item_at(tmp_path, n) for n in (1, 1, 2))
    store = QueueStore(directory)
    store.load()
    store.save([encode_item(i) for i in (other, first, duplicate)])
    contender = QueueStore(directory)
    with pytest.raises(StorageBusyError):
        contender.load()
    store.save([encode_item(i) for i in (other, duplicate)])
    store.close()
    loaded = QueueStore(directory)
    assert [i.queue_id for i in loaded.load()] == [other.queue_id, duplicate.queue_id]
    loaded.close()
    store.path.write_text("broken", encoding="utf-8")
    recovered = QueueStore(directory)
    assert [i.queue_id for i in recovered.load()] == [
        other.queue_id,
        first.queue_id,
        duplicate.queue_id,
    ]
    assert recovered.warning and list(directory.glob("*.broken-*"))
    recovered.close()


def test_partly_invalid_queue_keeps_valid_items(tmp_path):
    store = QueueStore(tmp_path / "settings")
    store.load()
    store.save([None, {"id": 99}, encode_item(item_at(tmp_path))])
    store.close()
    restored = QueueStore(tmp_path / "settings")
    assert len(restored.load()) == 1
    assert restored.warning
    restored.close()


def test_queue_preserves_section_selection_and_original_folder(tmp_path):
    """구간 카드가 복원되면 구간·프레임률·저장 폴더를 보존하고 재검증 대기여야 한다."""
    from fractions import Fraction
    from core.models.plan import TimeRange
    from app.queue_store import decode_item

    original = item_at(tmp_path)
    original.selections = (TimeRange(1.25, 12.5), TimeRange(30, 45))
    original.section_frame_rate = Fraction(30000, 1001)
    original.section_check = "verified"
    got = decode_item(encode_item(original))
    assert got.selections == original.selections
    assert got.section_frame_rate == Fraction(30000, 1001)
    assert got.download_path == str(tmp_path)
    assert got.section_check == "unverified"
    assert got.downloadState == DownloadState.WAITING
    assert got.needs_refresh


@pytest.mark.parametrize(
    "change",
    [
        {"selections": [{"start": -1, "end": 2}]},
        {"selections": [{"start": 3, "end": 2}]},
        {"selections": [{"start": 1, "end": float("inf")}]},
        {"section_frame_rate": "0/1"},
        {"section_frame_rate": "1/0"},
    ],
)
def test_invalid_section_record_does_not_drop_other_queue_items(tmp_path, change):
    """구간 기록이 잘못되면 해당 항목만 제외하고 다른 카드를 복원해야 한다."""
    store = QueueStore(tmp_path / "settings")
    store.load()
    invalid = encode_item(item_at(tmp_path)) | change
    valid = item_at(tmp_path, 2)
    store.save([invalid, encode_item(valid)])
    store.close()
    restored = QueueStore(tmp_path / "settings")
    try:
        assert [i.queue_id for i in restored.load()] == [valid.queue_id]
        assert restored.warning
    finally:
        restored.close()


def test_window_restart_preserves_order_and_never_starts_download(tmp_path, qtbot, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.viewmodels.download_viewmodel.DownloadViewModel.start",
        lambda *args: calls.append(args),
    )
    win = VodDownloader()
    qtbot.addWidget(win)
    first, second = item_at(tmp_path, 1), item_at(tmp_path, 2)
    win.contentManager.model.addItem(second)
    win.contentManager.model.addItem(first)
    first.downloadState = DownloadState.FAILED
    first.stateMessage = "연결 오류"
    win.contentManager.model.notifyChanged(first)
    win.close()
    restored = VodDownloader()
    qtbot.addWidget(restored)
    assert [i.vod_url for i in restored.contentManager.model.items] == [
        second.vod_url,
        first.vod_url,
    ]
    assert all(
        i.downloadState == DownloadState.WAITING for i in restored.contentManager.model.items
    )
    assert calls == []
    restored.close()


class FakeFetch(QObject):
    finished = Signal(object, str)
    error = Signal(str)

    def __init__(self, url, cookies, path):
        super().__init__()
        self.url, self.path = url, path

    def run(self):
        self.finished.emit(
            (self.url, {"title": "새 제목"}, [[1080, "fresh"]], 1080, "fresh", self.path, None),
            "m3u8",
        )


def test_actual_resume_button_refreshes_and_starts_once(tmp_path, qtbot, monkeypatch):
    calls = []
    monkeypatch.setattr("app.viewmodels.content_viewmodel.FetchJob", FakeFetch)
    monkeypatch.setattr(
        "app.viewmodels.download_viewmodel.DownloadViewModel.start",
        lambda self, item: calls.append(item),
    )
    win = VodDownloader()
    qtbot.addWidget(win)
    item = item_at(tmp_path)
    item.downloadState = DownloadState.FAILED
    win.contentManager.model.addItem(item)
    widget = win.listView.widgetFor(item)
    assert not widget.resumeButton.isHidden()
    assert widget.retryButton.isHidden()
    qtbot.mouseClick(widget.resumeButton, Qt.LeftButton)
    win.onCardResume(item)  # 빠른 중복 클릭도 같은 요청을 만들지 않는다.
    qtbot.waitUntil(lambda: len(calls) == 1)
    assert calls == [item] and item.base_url == "fresh" and item.title == "영상 1"
    win.close()


def test_other_running_video_keeps_retry_queued(tmp_path, qtbot, monkeypatch):
    win = VodDownloader()
    qtbot.addWidget(win)
    item = item_at(tmp_path)
    item.downloadState = DownloadState.FAILED
    win.contentManager.model.addItem(item)
    monkeypatch.setattr(win.downloadViewModel, "isDownloading", lambda: True)
    calls = []
    monkeypatch.setattr(win.contentManager, "downloadItem", lambda *args: calls.append(args))
    win.onCardResume(item)
    win.onCardResume(item)
    assert item.retry_pending and item.downloadState == DownloadState.WAITING and calls == []
    monkeypatch.setattr(win.downloadViewModel, "isDownloading", lambda: False)
    win.close()


@pytest.mark.parametrize("scheme", ["dark", "light"])
@pytest.mark.parametrize("width", [640, 900, 1400])
def test_resume_button_fits_card_with_visible_delete(tmp_path, qtbot, qapp, scheme, width):
    import app.theme as theme
    import main
    from app.widgets.widget import ContentItemWidget
    from PySide6.QtCore import QTranslator

    translator = QTranslator()
    assert translator.load(main.resource_path("translations/ko_KR.qm"))
    qapp.installTranslator(translator)
    previous = theme._current_scheme
    try:
        theme.apply_color_scheme(qapp, scheme, main.resource_path(theme.QSS_RELATIVE_PATH))
        item = item_at(tmp_path)
        item.downloadState = DownloadState.FAILED
        item.resume_available = item.merge_only = True
        widget = ContentItemWidget(item, 0)
        qtbot.addWidget(widget)
        widget.setData(item, 0)
        widget.resize(width, widget.sizeHint().height())
        widget.show()
        qtbot.waitUntil(lambda: widget.resumeButton.isVisible())
        assert widget.resumeButton.text() == "병합 다시 시도"
        button_rect = widget.resumeButton.geometry()
        assert not button_rect.intersects(widget.deleteButton.geometry())
        assert widget.contentFrame.rect().contains(button_rect)
        if width == 900:
            widget.grab().save(str(tmp_path / f"resume-{scheme}.png"))
    finally:
        qapp.removeTranslator(translator)
        theme.apply_color_scheme(qapp, previous, main.resource_path(theme.QSS_RELATIVE_PATH))


def test_resume_keeps_selected_lower_quality_after_metadata_refresh(tmp_path, qtbot, monkeypatch):
    class MultiQuality(FakeFetch):
        def run(self):
            self.finished.emit(
                (
                    self.url,
                    {"title": "새 제목"},
                    [[720, "fresh720"], [1080, "fresh1080"]],
                    1080,
                    "fresh1080",
                    self.path,
                    None,
                ),
                "m3u8",
            )

    calls = []
    monkeypatch.setattr("app.viewmodels.content_viewmodel.FetchJob", MultiQuality)
    monkeypatch.setattr(
        "app.viewmodels.download_viewmodel.DownloadViewModel.start",
        lambda self, item: calls.append((item.resolution, item.base_url)),
    )
    win = VodDownloader()
    qtbot.addWidget(win)
    item = item_at(tmp_path)
    item.resolution = 720
    item.unique_reps = [[720, "old720"], [1080, "old1080"]]
    item.downloadState = DownloadState.FAILED
    win.contentManager.model.addItem(item)
    win.onCardResume(item)
    qtbot.waitUntil(lambda: bool(calls))
    assert calls == [(720, "fresh720")]
    assert item.resolution == 720
    win.close()
