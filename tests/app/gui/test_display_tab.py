"""설정 창 화면 탭 — 저장하면 카드 목록에 바로 반영 · 앱이 다시 활성이 되면 OS 애니메이션을 다시 봄 (#645).

실제 배선을 탄다: 본 창의 설정 버튼 동작(`onSetting`)으로 연 설정 창에서 체크박스를 누르고 저장 버튼을
누른다. 씨앗은 설정 DB에 직접 쓴다(`tests.support.settings_db`). 기대값은 손으로 정한 값이다(썸네일 110×62 ·
68×38 — 설계 §12.1).
"""

import json
from collections.abc import Callable, Iterator
from typing import Never

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QPixmap
from PySide6.QtWidgets import QApplication, QDialogButtonBox
from pytestqt.qtbot import QtBot

import app.system.motion_preference as motion_preference
import app.theme as theme
import CVDv2 as main_module
from app.viewmodels import card_display
from app.viewmodels.data import ContentItem
from app.views.dialog import SettingDialog
from app.views.mainWindow import VodDownloader
from app.widgets.widget import ContentItemWidget
from core.models.download_state import DownloadState
from tests.app.gui.card_helpers import drop_new_top_levels, hold_style, snapshot_top_levels
from tests.support.settings_db import read_settings, write_rows

D = DownloadState
BIG = (110, 62)  # 큰 썸네일(px) — 설계 §12.1
SMALL = (68, 38)  # 줄인 썸네일(px) — 설계 §12.1
DIALOG_WAIT_MS = 3000  # 설정 창이 뜨기를 기다리는 한도(ms)
POLL_MS = 10  # 설정 창이 떴는지 다시 보는 간격(ms)


@pytest.fixture(autouse=True)
def _production_style(qapp: QApplication) -> Iterator[None]:
    """실제 전역 QSS · 다크 테마로 잰다. 끝에 빈 QSS로 되돌린다."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))
    yield
    qapp.setStyleSheet("")
    qapp.setStyle(hold_style(theme.build_style()))
    qapp.setPalette(theme.build_palette())


@pytest.fixture(autouse=True)
def _drop_windows(qapp: QApplication) -> Iterator[None]:
    """테스트가 만든 최상위 창을 끝에 파괴한다."""
    before = snapshot_top_levels()
    yield
    drop_new_top_levels(before)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """카드의 크기 조회 · 그림 받기가 네트워크로 나가지 않게 한다."""

    class _FailingSession:
        def head(self, *args: object, **kwargs: object) -> Never:
            raise RuntimeError("network disabled in tests")

        def get(self, *args: object, **kwargs: object) -> Never:
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("app.widgets.widget.get_thread_session", lambda: _FailingSession())


@pytest.fixture
def window(qapp: QApplication, tmp_path: object) -> Iterator[VodDownloader]:
    """본 창 — 900×700으로 보인다."""
    win = VodDownloader()
    assert win.saveLocations.choose(str(tmp_path))
    win.resize(900, 700)
    win.show()
    QApplication.processEvents()
    yield win
    win.hide()


def _item(title: str, state: DownloadState) -> ContentItem:
    item = ContentItem(
        "https://chzzk.naver.com/video/1",
        {"title": title, "category": "", "channelName": "채널", "createdDate": "", "duration": 60},
        [],
        None,
        "",
        "",
        "video",
        None,
    )
    item.downloadState = state
    return item


def _add(win: VodDownloader, *states: DownloadState) -> list[ContentItem]:
    items = [_item(f"카드 {n}", state) for n, state in enumerate(states)]
    for item in items:
        win.contentManager.model.addItem(item)
    QApplication.processEvents()
    return items


def _card(win: VodDownloader, item: ContentItem) -> ContentItemWidget:
    widget = win.listView.widgetFor(item)
    assert widget is not None
    return widget


def _save_in_settings(
    qtbot: QtBot, win: VodDownloader, change: Callable[[SettingDialog], None]
) -> None:
    """본 창의 설정 동작으로 설정 창을 열고 `change`한 뒤 저장 버튼을 누른다 — 설정 창이 닫힐 때까지 돈다."""
    seen: dict = {"done": False, "error": None}
    tries = DIALOG_WAIT_MS // POLL_MS

    def act() -> None:
        nonlocal tries
        dialog = getattr(win, "dialog", None)
        if not isinstance(dialog, SettingDialog) or not dialog.isVisible():
            tries -= 1
            if tries > 0:
                QTimer.singleShot(POLL_MS, act)
            return
        try:
            change(dialog)
        except Exception as error:  # noqa: BLE001 — 창을 닫아 테스트가 멈추지 않게 하고 아래에서 다시 올린다
            seen["error"] = error
            dialog.reject()
            return
        save = dialog.dialogButtonBox.button(QDialogButtonBox.StandardButton.Save)
        if not save.isEnabled():
            dialog.reject()  # 바뀐 값이 없다 — 창을 닫아 테스트가 멈추지 않게 하고 아래 단언이 실패한다
            return
        save.click()
        seen["done"] = True

    QTimer.singleShot(POLL_MS, act)
    win.onSetting()
    QApplication.processEvents()
    if seen["error"] is not None:
        raise seen["error"]
    assert seen["done"], "설정 창이 뜨지 않았거나 바뀐 값이 없었다"


def _toggle(key: str) -> Callable[[SettingDialog], None]:
    """화면 탭에서 그 키의 체크박스를 누르는 동작."""

    def change(dialog: SettingDialog) -> None:
        dialog.showTab(2)  # 화면 탭
        dialog.displayChecks[key].click()

    return change


def _thumb_size(card: ContentItemWidget) -> tuple[int, int]:
    label = card.thumbnailLabel
    return label.width(), label.height()


def _status_row_cell(card: ContentItemWidget) -> tuple[int, int, int, int]:
    """3줄이 격자에서 차지한 (행, 열, 행 수, 열 수)."""
    layout = card.bodyLayout
    return layout.getItemPosition(layout.indexOf(card.statusRow))


def _png(width: int, height: int) -> bytes:
    """한 색으로 채운 PNG 바이트."""
    pixmap = QPixmap(width, height)
    pixmap.fill(QColor(30, 120, 200))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    pixmap.save(buffer, "PNG")
    return bytes(data)


# ================================================================ 썸네일 줄이기


def test_saving_smaller_thumbnails_reshapes_every_card(qtbot: QtBot, window: VodDownloader) -> None:
    """화면 탭에서 썸네일 줄이기를 켜고 저장하면 목록의 모든 카드가 줄인 모양이어야 한다.

    카드 셋(대기 · 완료 · 실패), 썸네일 줄이기 끔 → 켬 저장 ->
    썸네일 68×38, 3줄은 (2, 0) 두 열, 체크 원 지름 cardPickMarkCompact, 행 cardCompactThumbnail = true
    """
    items = _add(window, D.WAITING, D.FINISHED, D.FAILED)
    assert {_thumb_size(_card(window, item)) for item in items} == {BIG}, "전제: 큰 썸네일"
    _save_in_settings(qtbot, window, _toggle(card_display.COMPACT_THUMBNAIL_KEY))
    for item in items:
        card = _card(window, item)
        assert _thumb_size(card) == SMALL
        assert _status_row_cell(card) == (2, 0, 1, 2)
        side = card.thumbnailLabel.pickMarkRect().width()
        assert side == theme.METRICS["cardPickMarkCompact"]
    assert read_settings()["cardCompactThumbnail"] is True


def test_turning_smaller_thumbnails_off_restores_the_big_shape(
    qtbot: QtBot, window: VodDownloader
) -> None:
    """썸네일 줄이기가 켜진 목록에서 끄고 저장하면 카드가 큰 모양으로 돌아가야 한다.

    행 cardCompactThumbnail = true, 카드 둘 → 끔 저장 -> 썸네일 110×62, 3줄은 (2, 1) 한 열
    """
    write_rows({"cardCompactThumbnail": "true"})
    card_display.use(None)
    items = _add(window, D.WAITING, D.FINISHED)
    assert {_thumb_size(_card(window, item)) for item in items} == {SMALL}, "전제: 줄인 썸네일"
    _save_in_settings(qtbot, window, _toggle(card_display.COMPACT_THUMBNAIL_KEY))
    for item in items:
        card = _card(window, item)
        assert _thumb_size(card) == BIG
        assert _status_row_cell(card) == (2, 1, 1, 1)


def test_a_card_added_after_saving_uses_the_new_shape(qtbot: QtBot, window: VodDownloader) -> None:
    """썸네일 줄이기를 켜고 저장한 뒤 더한 카드도 줄인 모양이어야 한다.

    줄이기 켬 저장 → 카드 하나 더함 -> 그 카드 썸네일 68×38
    """
    _add(window, D.WAITING)
    _save_in_settings(qtbot, window, _toggle(card_display.COMPACT_THUMBNAIL_KEY))
    (added,) = _add(window, D.WAITING)
    assert _thumb_size(_card(window, added)) == SMALL


def test_a_received_thumbnail_is_redrawn_for_the_new_box(
    qtbot: QtBot, window: VodDownloader
) -> None:
    """그림을 받은 카드는 썸네일 줄이기를 켜고 끌 때마다 새 칸 크기의 그림이어야 한다.

    320×180 PNG를 받은 카드 → 줄이기 켬 저장 -> 그림 68×38 → 끔 저장 -> 그림 110×62
    """
    (item,) = _add(window, D.WAITING)
    card = _card(window, item)
    # 그림이 도착한 때와 같은 자리 — 받기를 시작하며 적는 라벨을 적고 도착 처리를 부른다(네트워크 없음)
    card._imageLabels["thumbnail"] = card.thumbnailLabel
    card._onImageFetched("thumbnail", _png(320, 180), "https://example.invalid/t.png", 62)
    assert card.thumbnailLabel.pixmap().size().toTuple() == BIG, "전제: 큰 칸 그림"
    _save_in_settings(qtbot, window, _toggle(card_display.COMPACT_THUMBNAIL_KEY))
    assert card.thumbnailLabel.pixmap().size().toTuple() == SMALL
    _save_in_settings(qtbot, window, _toggle(card_display.COMPACT_THUMBNAIL_KEY))
    assert card.thumbnailLabel.pixmap().size().toTuple() == BIG


def test_the_window_minimum_width_matches_a_window_opened_with_the_setting(
    qtbot: QtBot, window: VodDownloader
) -> None:
    """썸네일 줄이기를 바꿔 저장한 본 창의 최소폭은 그 설정으로 새로 연 본 창의 최소폭과 같아야 한다.

    줄이기 켬 저장 -> 저장한 창 minimumWidth == 새 창 minimumWidth
    """
    _save_in_settings(qtbot, window, _toggle(card_display.COMPACT_THUMBNAIL_KEY))
    card_display.use(None)
    fresh = VodDownloader()
    assert window.minimumWidth() == fresh.minimumWidth()


# ================================================================ 완료 · 실패 색조


def _tints(win: VodDownloader, items: list[ContentItem]) -> list[str]:
    return [_card(win, item).contentFrame.property("tint") or "" for item in items]


def test_saving_tint_off_clears_finished_and_failed_tints(
    qtbot: QtBot, window: VodDownloader
) -> None:
    """화면 탭에서 색조를 끄고 저장하면 완료 · 실패 카드 면의 색조가 없어야 한다.

    카드 셋(대기 · 완료 · 실패), 색조 켬 → 끔 저장 -> tint 속성 전부 "", 행 cardStateTint = false
    """
    items = _add(window, D.WAITING, D.FINISHED, D.FAILED)
    assert _tints(window, items) == ["", "finished", "failed"], "전제: 색조 켬"
    _save_in_settings(qtbot, window, _toggle(card_display.STATE_TINT_KEY))
    assert _tints(window, items) == ["", "", ""]
    assert read_settings()["cardStateTint"] is False


def test_saving_tint_on_brings_the_tints_back(qtbot: QtBot, window: VodDownloader) -> None:
    """색조가 꺼진 목록에서 켜고 저장하면 완료 · 실패 카드 면에 색조가 다시 있어야 한다.

    행 cardStateTint = false, 카드 셋 → 켬 저장 -> tint ["", "finished", "failed"]
    """
    write_rows({"cardStateTint": "false"})
    card_display.use(None)
    items = _add(window, D.WAITING, D.FINISHED, D.FAILED)
    assert _tints(window, items) == ["", "", ""], "전제: 색조 끔"
    _save_in_settings(qtbot, window, _toggle(card_display.STATE_TINT_KEY))
    assert _tints(window, items) == ["", "finished", "failed"]


def test_a_card_finishing_with_tint_off_gets_no_tint(qtbot: QtBot, window: VodDownloader) -> None:
    """색조를 끈 뒤 완료 · 실패가 된 카드는 색조가 없어야 한다.

    색조 끔 저장 → 대기 카드 둘을 완료 · 실패로 -> tint 둘 다 ""
    """
    items = _add(window, D.WAITING, D.WAITING)
    _save_in_settings(qtbot, window, _toggle(card_display.STATE_TINT_KEY))
    for item, state in zip(items, (D.FINISHED, D.FAILED), strict=True):
        item.downloadState = state
        _card(window, item).applyStateStyle()
    QApplication.processEvents()
    assert _tints(window, items) == ["", ""]


# ================================================================ 애니메이션


def test_saving_animations_off_reaches_the_cards(qtbot: QtBot, window: VodDownloader) -> None:
    """화면 탭에서 애니메이션을 끄고 저장하면 카드가 읽는 애니메이션 값이 거짓이어야 한다.

    OS 켬(conftest), 행 없음 → 끔 저장 -> card_display.current().animations 거짓, 행 animations = false
    """
    _add(window, D.WAITING)
    assert card_display.current().animations is True, "전제: OS 켬을 따름"
    _save_in_settings(qtbot, window, _toggle(card_display.ANIMATIONS_KEY))
    assert card_display.current().animations is False
    assert read_settings()["animations"] is False


def _activate_app() -> None:
    """앱 상태 바뀜 시그널로 「앱이 다시 활성이 됨」을 보낸다."""
    QGuiApplication.instance().applicationStateChanged.emit(Qt.ApplicationState.ApplicationActive)
    QApplication.processEvents()


def test_reactivating_the_app_follows_a_changed_os_setting(
    window: VodDownloader, monkeypatch: pytest.MonkeyPatch
) -> None:
    """animations 행이 없을 때 OS 설정이 끔으로 바뀐 뒤 앱이 다시 활성이 되면 애니메이션이 거짓이어야 한다.

    행 없음, OS 켬 → 끔으로 바꿈 → 앱 활성 -> current().animations 거짓
    """
    assert card_display.current().animations is True, "전제: OS 켬을 따름"
    monkeypatch.setattr(motion_preference, "system_animations", lambda: False)
    _activate_app()
    assert card_display.current().animations is False


def test_reactivating_the_app_keeps_the_chosen_value(
    window: VodDownloader, monkeypatch: pytest.MonkeyPatch
) -> None:
    """animations 행이 참이면 OS 설정이 끔이어도 앱이 다시 활성이 될 때 애니메이션이 참이어야 한다.

    행 animations = true, OS 끔 → 앱 활성 -> current().animations 참
    """
    write_rows({"animations": json.dumps(True)})
    card_display.use(None)
    monkeypatch.setattr(motion_preference, "system_animations", lambda: False)
    _activate_app()
    assert card_display.current().animations is True


def test_the_animations_check_shows_the_os_value_without_a_row(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """animations 행이 없으면 화면 탭 애니메이션 체크박스의 처음 값은 OS 값이어야 한다.

    행 없음, OS 끔 -> 체크 안 됨, 손대지 않고 저장 칸 값에 바뀐 키 없음
    """
    monkeypatch.setattr(motion_preference, "system_animations", lambda: False)
    dialog = SettingDialog()
    qtbot.addWidget(dialog)
    assert dialog.displayChecks[card_display.ANIMATIONS_KEY].isChecked() is False
    assert dialog.changes() == {}


def test_reactivating_the_app_with_the_same_os_value_keeps_the_display(
    window: VodDownloader,
) -> None:
    """OS 애니메이션 값이 그대로면 앱이 다시 활성이 돼도 들고 있는 카드 화면 설정이 그대로여야 한다.

    OS 켬(conftest) 그대로, 들고 있는 값 = CardDisplay(animations=False) → 앱 활성 -> current()가 같은 객체
    """
    held = card_display.CardDisplay(animations=False)
    card_display.use(held)
    _activate_app()
    assert card_display.current() is held


def test_reactivating_the_app_does_not_read_the_os_when_the_user_chose(
    window: VodDownloader, monkeypatch: pytest.MonkeyPatch
) -> None:
    """animations 행이 참 · 거짓이면 앱이 다시 활성이 될 때 OS 설정을 읽지 않아야 한다.

    행 animations = false → 앱 활성 -> OS 읽음 0번
    """
    write_rows({"animations": json.dumps(False)})
    calls: list[int] = []

    def read() -> bool:
        calls.append(1)
        return True

    monkeypatch.setattr(motion_preference, "system_animations", read)
    _activate_app()
    assert calls == []
