"""카드 편집 창 — 여는 길 · 한 번에 쓰는 저장 · 취소 · 구간 초안 · 해상도 (#545 · #550).

실제 메인 창 배선을 탄다(✎ 버튼 · 카드 더블 클릭 → 목록 → 메인 창 → 편집 창). 네트워크는 타지 않는다 —
카드의 해상도 크기 조회는 막혀 「확인 중...」으로 남고, 크기가 필요한 테스트는 조회 결과 신호를 직접 낸다.
구간 창의 기준값 조회는 대역(`basis`)이다.
"""

import os
from collections.abc import Callable, Iterator

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QEnterEvent, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QStyle

import app.views.card_edit_dialog as dialog_mod
from app.viewmodels.data import ContentItem
from app.views.card_edit_dialog import CardEditDialog
from app.views.mainWindow import VodDownloader
from core.models.download_state import DownloadState
from core.models.plan import TimeRange
from app.widgets import tip_bubble
from tests.app.gui.card_helpers import bubble_text_for, enter, shown, qt_tooltip_shown
from tests.app.gui.mouse_buttons import double_click
from tests.app.gui.test_section_editor import (  # noqa: F401 — 픽스처는 이름으로 쓴다
    _apply_production_qss,
    _destroy_windows,
    _make_item,
    _no_network,
    _pump,
    basis,
    open_window,
    press_ok,
    set_rows,
    started,
)

STATES = (
    DownloadState.WAITING,
    DownloadState.RUNNING,
    DownloadState.PAUSED,
    DownloadState.FINISHED,
    DownloadState.FAILED,
)
IDS = [s.name for s in STATES]


def _window(tmp_path, *items: ContentItem) -> VodDownloader:
    return open_window(tmp_path, *items)


def _hover(card) -> None:
    """카드에 들어감 이벤트를 보낸다 — 카드 버튼은 호버 때만 보인다(#550). 포인터는 옮기지 않는다."""
    center = QPointF(card.rect().center())
    QApplication.sendEvent(card, QEnterEvent(center, center, card.mapToGlobal(center)))
    _pump()


def _open(win: VodDownloader, item: ContentItem) -> CardEditDialog:
    """카드에 호버해 ✎를 실제로 눌러 편집 창을 연다."""
    card = win.listView.widgetFor(item)
    _hover(card)
    assert card.editButton.isVisible(), "전제: 호버한 대기 카드에 ✎가 보인다"
    QTest.mouseClick(card.editButton, Qt.MouseButton.LeftButton)
    _pump()
    dialog = win._editDialog
    assert dialog is not None and dialog.isVisible(), "✎를 눌렀는데 편집 창이 열리지 않았다"
    return dialog


def _pick(dialog: CardEditDialog, resolution: int) -> None:
    """해상도 드롭다운에서 그 해상도 줄을 고른다."""
    combo = dialog.resolutionCombo
    for row in range(combo.count()):
        if dialog._item.unique_reps[combo.itemData(row)][0] == resolution:
            combo.setCurrentIndex(row)
            return
    raise AssertionError(f"드롭다운에 {resolution}p가 없다")


def _snapshot(item: ContentItem) -> tuple:
    """편집 창이 쓰는 값 — 제목 · 저장 위치 · 해상도 · 주소 · 스트림 · 크기 · 원본 표시 · 구간."""
    return (
        item.title,
        item.download_path,
        item.resolution,
        item.base_url,
        item.stream,
        item.total_size,
        item.resolution_tag,
        tuple(item.selections),
    )


@pytest.fixture
def folder(monkeypatch, tmp_path) -> Callable[[str], None]:
    """폴더 선택 대화상자 대역 — 고를 폴더를 테스트가 정한다(편집 창 · 카드 둘 다)."""
    chosen = {"path": ""}

    def pick(*_a, **_k) -> str:
        return chosen["path"]

    monkeypatch.setattr(dialog_mod.QFileDialog, "getExistingDirectory", pick)

    def choose(path: str) -> None:
        chosen["path"] = path

    return choose


# ---- 여는 길 ----


class TestOpening:
    """✎ · 더블 클릭으로 대기 카드에서만 열린다."""

    @pytest.mark.parametrize("state", STATES, ids=IDS)
    def test_edit_button_shows_only_on_waiting_cards(
        self, qtbot, tmp_path, state: DownloadState
    ) -> None:
        """호버한 카드의 ✎는 대기 상태에서만 보이고 툴팁이 「Edit」이어야 한다.

        상태 다섯 · 호버 -> 대기만 ✎ 보임
        """
        item = _make_item(str(tmp_path))
        item.downloadState = state
        win = _window(tmp_path, item)
        card = win.listView.widgetFor(item)
        _hover(card)
        assert card.editButton.isVisible() == (state == DownloadState.WAITING)
        assert card.editButton.toolTip() == "Edit"

    def test_edit_button_opens_the_dialog(self, qtbot, tmp_path) -> None:
        """대기 카드의 ✎를 누르면 그 카드의 편집 창이 열려야 한다.

        ✎ 클릭 -> 편집 창 보임, 창의 파일명 칸 = 카드 제목
        """
        item = _make_item(str(tmp_path))
        win = _window(tmp_path, item)
        dialog = _open(win, item)
        assert dialog.nameEdit.text() == "제목"

    @pytest.mark.parametrize("state", STATES, ids=IDS)
    def test_double_click_opens_the_dialog_only_on_waiting_cards(
        self, qtbot, tmp_path, monkeypatch, state: DownloadState
    ) -> None:
        """카드 썸네일을 더블 클릭하면 대기 카드만 편집 창이 열려야 한다.

        상태 다섯 -> 대기만 편집 창(완료 카드의 폴더 열기 경고는 기록만 한다)
        """
        monkeypatch.setattr("app.widgets.widget.QMessageBox.warning", lambda *args: None)
        item = _make_item(str(tmp_path))
        item.downloadState = state
        win = _window(tmp_path, item)
        card = win.listView.widgetFor(item)
        double_click(card.thumbnailLabel)
        _pump()
        assert (win._editDialog is not None) == (state == DownloadState.WAITING)

    def test_dialog_does_not_open_over_the_section_dialog(self, qtbot, tmp_path, basis) -> None:  # noqa: F811
        """구간 창이 열린 동안에는 편집 창이 열리지 않아야 한다(반대도 같다).

        구간 창 열림 -> ✎ 무시, 편집 창 열림 -> 구간 요약 클릭 무시
        """
        item = _make_item(str(tmp_path))
        win = _window(tmp_path, item)
        win.onCardSectionEdit(item)
        _pump()
        assert win._sectionDialog is not None, "전제: 구간 창이 열렸다"
        win.onCardEdit(item)
        assert win._editDialog is None
        win._sectionDialog.reject()
        _pump()
        _open(win, item)
        win.onCardSectionEdit(item)
        assert win._sectionDialog is None


# ---- 저장 · 취소 ----


class TestSaveAndCancel:
    """저장은 네 값을 한 번에 쓰고, 취소는 아무것도 쓰지 않는다."""

    def _change_everything(self, qtbot, dialog: CardEditDialog, folder, other: str) -> None:
        """파일명 · 해상도 · 저장 위치 · 구간(초안)을 모두 바꾼다."""
        dialog.nameEdit.setText("NewName")
        _pick(dialog, 480)
        folder(other)
        dialog.browseButton.click()
        dialog.sectionButton.click()
        _pump()
        sections = dialog.sectionDialog
        assert sections is not None, "전제: 구간 창이 열렸다"
        qtbot.waitUntil(lambda: sections.viewModel().state != "loading", timeout=3000)
        set_rows(sections, [("00:01:00:00", "00:02:00:00")])
        press_ok(sections)
        assert dialog.sectionDialog is None, "전제: 구간 창이 닫혔다"

    def test_save_writes_all_four_values_at_once(self, qtbot, tmp_path, basis, folder) -> None:  # noqa: F811
        """저장을 누르면 파일명 · 해상도 · 저장 위치 · 구간이 한 번에 카드에 들어가야 한다.

        제목 → "NewName", 1080p → 480p, 저장 위치 → other, 구간 → 1:00 ~ 2:00 한 개
        """
        other = str(tmp_path / "other")
        os.makedirs(other)
        item = _make_item(str(tmp_path))
        win = _window(tmp_path, item)
        dialog = _open(win, item)
        self._change_everything(qtbot, dialog, folder, other)
        assert _snapshot(item)[0] == "제목", "전제: 저장 전에는 카드가 그대로다"
        dialog.saveButton.click()
        _pump()
        assert (item.title, item.resolution, item.download_path) == ("NewName", 480, other)
        assert item.selections == (TimeRange(60.0, 120.0),)
        assert shown(win.listView.widgetFor(item).titleLabel) == "NewName"

    def test_cancel_writes_nothing(self, qtbot, tmp_path, basis, folder) -> None:  # noqa: F811
        """취소를 누르면 바꾼 것(구간 초안 포함)이 하나도 카드에 들어가지 않아야 한다.

        네 값을 모두 바꾼 뒤 취소 -> 카드 값이 열기 전과 같다
        """
        other = str(tmp_path / "other")
        os.makedirs(other)
        item = _make_item(str(tmp_path))
        win = _window(tmp_path, item)
        before = _snapshot(item)
        dialog = _open(win, item)
        self._change_everything(qtbot, dialog, folder, other)
        dialog.cancelButton.click()
        _pump()
        assert _snapshot(item) == before

    def test_section_draft_is_dropped_on_cancel(self, qtbot, tmp_path, basis) -> None:  # noqa: F811
        """구간 창에서 확인한 구간이라도 편집 창을 취소하면 카드의 구간이 그대로여야 한다.

        구간 없음 → 구간 창에서 1:00 ~ 2:00 확인 → 편집 창 취소 -> 카드 구간 ()
        """
        item = _make_item(str(tmp_path))
        win = _window(tmp_path, item)
        dialog = _open(win, item)
        dialog.sectionButton.click()
        _pump()
        sections = dialog.sectionDialog
        qtbot.waitUntil(lambda: sections.viewModel().state != "loading", timeout=3000)
        set_rows(sections, [("00:01:00:00", "00:02:00:00")])
        press_ok(sections)
        assert "1:00 ~ 2:00" in dialog.sectionLinesLabel.text(), "전제: 창의 구간 요약에는 들어갔다"
        dialog.reject()
        _pump()
        assert item.selections == ()

    def test_empty_name_saves_the_original_title(self, qtbot, tmp_path) -> None:
        """파일명 칸을 비우거나 공백만 두고 저장하면 처음 제목이 저장되어야 한다(#543).

        제목을 "Old"로 둔 카드 → 칸 "   " 저장 -> item.title == 처음 제목 "제목"
        """
        item = _make_item(str(tmp_path))
        item.title = "Old"
        win = _window(tmp_path, item)
        dialog = _open(win, item)
        dialog.nameEdit.setText("   ")
        dialog.saveButton.click()
        _pump()
        assert item.title == "제목"

    @pytest.mark.parametrize("change", ["running", "removed"])
    def test_save_writes_nothing_when_the_card_changed_while_open(
        self, qtbot, tmp_path, change: str
    ) -> None:
        """창이 열린 사이 카드가 받기 시작했거나 지워졌으면 저장이 쓰지 않고 닫아야 한다.

        열린 사이 RUNNING / 목록에서 지움 → 이름 "NewName" 저장 -> 제목 그대로, 창 닫힘
        """
        item = _make_item(str(tmp_path))
        win = _window(tmp_path, item)
        dialog = _open(win, item)
        if change == "running":
            item.downloadState = DownloadState.RUNNING
        else:
            win.contentManager.model.removeRows(win.contentManager.model.getRow(item))
        dialog.nameEdit.setText("NewName")
        dialog.saveButton.click()
        _pump()
        assert item.title == "제목"
        assert not dialog.isVisible() and win._editDialog is None


# ---- 해상도 ----


class TestResolution:
    """해상도 목록 · 다시 맞춤 · 늦게 온 크기."""

    def test_list_shows_the_frame_rate_and_the_looked_up_size(self, qtbot, tmp_path) -> None:
        """해상도 목록이 카드의 pill과 같은 차례 · fps · 조회한 크기를 보여야 한다.

        1080p 60fps · 480p 30fps, 1080p 크기 "1.00 GB" 도착 -> ["1080p 60fps · 1.00 GB", "480p · Checking..."]
        """
        item = _make_item(str(tmp_path))
        win = _window(tmp_path, item)
        card = win.listView.widgetFor(item)
        card._repSizeFetched.emit(0, "1.00 GB")
        _pump()
        dialog = _open(win, item)
        texts = [
            dialog.resolutionCombo.itemText(row) for row in range(dialog.resolutionCombo.count())
        ]
        assert texts == ["1080p 60fps · 1.00 GB", "480p · Checking..."]

    def test_size_arriving_while_open_updates_the_list(self, qtbot, tmp_path) -> None:
        """창이 열린 사이 크기 조회가 도착하면 그 해상도 줄이 고쳐져야 한다.

        열린 뒤 480p 크기 "300.00 MB" 도착 -> 둘째 줄 "480p · 300.00 MB"
        """
        item = _make_item(str(tmp_path))
        win = _window(tmp_path, item)
        dialog = _open(win, item)
        win.listView.widgetFor(item)._repSizeFetched.emit(1, "300.00 MB")
        _pump()
        assert dialog.resolutionCombo.itemText(1) == "480p · 300.00 MB"

    def test_size_arriving_after_the_dialog_closed_is_safe(self, qtbot, tmp_path) -> None:
        """창이 닫혀 지워진 뒤 크기 조회가 도착해도 앱이 멈추지 않고 카드에만 들어가야 한다.

        창 취소 · 지움 → 480p 크기 "300.00 MB" 도착 -> 카드 해상도 항목 크기 = "300.00 MB"
        """
        item = _make_item(str(tmp_path))
        win = _window(tmp_path, item)
        dialog = _open(win, item)
        dialog.reject()
        _pump()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        win.listView.widgetFor(item)._repSizeFetched.emit(1, "300.00 MB")
        _pump()
        assert item.unique_reps[1][-1] == "300.00 MB"

    def test_changing_the_resolution_refits_the_sections(
        self, qtbot, tmp_path, monkeypatch
    ) -> None:
        """구간이 있는 카드의 해상도를 바꿔 저장하면 구간 다시 맞추기를 한 번 청해야 한다.

        구간 1개 · 1080p → 480p 저장 -> refitSections(item) 1번, 해상도 그대로 저장은 0번
        """
        item = _make_item(str(tmp_path))
        item.selections = (TimeRange(60.0, 120.0),)
        win = _window(tmp_path, item)
        calls: list = []
        monkeypatch.setattr(win.contentManager, "refitSections", calls.append)
        dialog = _open(win, item)
        dialog.saveButton.click()
        _pump()
        assert calls == [], "해상도를 바꾸지 않은 저장이 다시 맞추기를 청했다"
        dialog = _open(win, item)
        _pick(dialog, 480)
        dialog.saveButton.click()
        _pump()
        assert calls == [item]

    def test_file_tail_follows_the_resolution_and_the_sections(self, qtbot, tmp_path) -> None:
        """파일명 칸 뒤 꼬리 · 머리 줄 해상도가 고른 해상도와 구간 수를 따라야 한다.

        1080p 전체 -> " 1080p.mp4" · 머리 "채널 · 1080p", 480p로 바꿈 -> " 480p.mp4" · "채널 · 480p",
        구간 2개 카드 -> " 1080p_1.mp4 …"
        """
        item = _make_item(str(tmp_path))
        win = _window(tmp_path, item)
        dialog = _open(win, item)
        assert dialog.tailLabel.text() == " 1080p.mp4"
        assert dialog.metaLabel.text() == "채널 · 1080p"
        _pick(dialog, 480)
        assert dialog.tailLabel.text() == " 480p.mp4"
        assert dialog.metaLabel.text() == "채널 · 480p"
        dialog.reject()
        _pump()
        item.selections = (TimeRange(0.0, 60.0), TimeRange(120.0, 180.0))
        dialog = _open(win, item)
        assert dialog.tailLabel.text() == " 1080p_1.mp4 …"


# ---- 다운로드 차례 ----


class TestDownloadOrder:
    """창이 열린 동안 그 카드는 다운로드 차례에서 빠진다."""

    def test_open_card_is_skipped_until_the_dialog_closes(self, qtbot, tmp_path, started) -> None:  # noqa: F811
        """편집 창이 열린 카드는 다운로드를 눌러도 시작되지 않고, 창을 닫으면 차례가 이어져야 한다.

        카드 둘(A 편집 중 · B) → 다운로드 -> B만 시작, A 창 닫음 -> 배치가 A를 기다리지 않는다
        """
        first = _make_item(str(tmp_path), title="A")
        second = _make_item(str(tmp_path), title="B")
        win = _window(tmp_path, first, second)
        dialog = _open(win, first)
        win.downloadButton.click()
        _pump()
        assert started == [second]
        dialog.reject()
        _pump()
        assert win.contentManager._editingItem is None


# ---- 키 · 휠 · 버튼 ----


class TestKeysAndButtons:
    """Enter · Esc · 휠 · 버튼 차례."""

    def test_enter_in_a_field_does_not_close(self, qtbot, tmp_path) -> None:
        """파일명 칸에서 Enter를 치면 창이 닫히지 않아야 한다.

        칸에 초점 · Enter -> 창 보임, 카드 제목 그대로
        """
        item = _make_item(str(tmp_path))
        win = _window(tmp_path, item)
        dialog = _open(win, item)
        dialog.nameEdit.setFocus()
        dialog.nameEdit.setText("NewName")
        QTest.keyClick(dialog.nameEdit, Qt.Key.Key_Return)
        _pump()
        assert dialog.isVisible() and item.title == "제목"

    def test_enter_on_a_focused_button_presses_it(self, qtbot, tmp_path) -> None:
        """초점이 있는 저장 버튼에서 Enter를 치면 저장해야 한다.

        이름 "NewName" · 저장 버튼 초점 · Enter -> 창 닫힘, 제목 "NewName"
        """
        item = _make_item(str(tmp_path))
        win = _window(tmp_path, item)
        dialog = _open(win, item)
        dialog.nameEdit.setText("NewName")
        dialog.saveButton.setFocus()
        QTest.keyClick(dialog.saveButton, Qt.Key.Key_Return)
        _pump()
        assert not dialog.isVisible() and item.title == "NewName"

    def test_escape_cancels(self, qtbot, tmp_path) -> None:
        """Esc를 치면 아무것도 쓰지 않고 닫혀야 한다.

        이름 "NewName" · Esc -> 창 닫힘, 제목 그대로
        """
        item = _make_item(str(tmp_path))
        win = _window(tmp_path, item)
        dialog = _open(win, item)
        dialog.nameEdit.setText("NewName")
        QTest.keyClick(dialog.nameEdit, Qt.Key.Key_Escape)
        _pump()
        assert not dialog.isVisible() and item.title == "제목"

    @pytest.mark.parametrize("focused", [False, True], ids=["unfocused", "focused"])
    def test_wheel_changes_the_resolution_only_when_focused(
        self, qtbot, tmp_path, focused: bool
    ) -> None:
        """초점이 없는 해상도 드롭다운은 휠로 값이 바뀌지 않고, 초점이 있으면 바뀌어야 한다.

        첫 줄에서 아래로 휠 한 칸 -> 초점 없음: 그대로, 초점 있음: 둘째 줄
        """
        item = _make_item(str(tmp_path))
        win = _window(tmp_path, item)
        dialog = _open(win, item)
        combo = dialog.resolutionCombo
        if focused:
            combo.setFocus()
            _pump()
        else:
            dialog.nameEdit.setFocus()
            _pump()
        assert combo.hasFocus() == focused, "전제: 초점 조건"
        center = QPointF(combo.width() / 2, combo.height() / 2)
        wheel = QWheelEvent(
            center,
            combo.mapToGlobal(center),
            QPoint(0, 0),
            QPoint(0, -120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        QApplication.sendEvent(combo, wheel)
        _pump()
        assert combo.currentIndex() == (1 if focused else 0)

    def test_buttons_follow_the_platform_order(self, qtbot, tmp_path) -> None:
        """저장 · 취소 차례가 그 스타일의 대화상자 버튼 배치 힌트를 따라야 한다(설계 §12.1).

        힌트가 macOS 배치면 취소 → 저장, 그 밖(Windows · KDE · GNOME)이면 저장이 취소보다 앞(Windows) 또는
        뒤(KDE · GNOME) — Qt 버튼 상자가 정한 자리와 힌트가 맞는다
        """
        item = _make_item(str(tmp_path))
        win = _window(tmp_path, item)
        dialog = _open(win, item)
        layout = dialog.style().styleHint(QStyle.StyleHint.SH_DialogButtonLayout, None, dialog)
        save_x = dialog.saveButton.mapTo(dialog, QPoint(0, 0)).x()
        cancel_x = dialog.cancelButton.mapTo(dialog, QPoint(0, 0)).x()
        save_first = save_x < cancel_x
        expected = {
            QDialogButtonBox.ButtonLayout.WinLayout.value: True,
            QDialogButtonBox.ButtonLayout.MacLayout.value: False,
            QDialogButtonBox.ButtonLayout.KdeLayout.value: True,
            QDialogButtonBox.ButtonLayout.GnomeLayout.value: False,
        }
        assert save_first == expected[layout], (
            f"버튼 배치 힌트 {layout}에서 저장이 앞인가: {save_first}"
        )
        assert dialog.saveButton.property("role") == "primary"

    def test_save_button_is_filled_as_the_main_action(self, qtbot, tmp_path) -> None:
        """저장 버튼은 주 동작 채움 색이라 가운데 화소가 취소 버튼과 달라야 한다(설계 §12.1 채움 색 규칙).

        저장 · 취소 버튼 grab()의 왼쪽 4px · 세로 가운데 화소(글자를 피함) -> 색이 다르다
        """
        item = _make_item(str(tmp_path))
        win = _window(tmp_path, item)
        dialog = _open(win, item)

        def face(button) -> int:
            image = button.grab().toImage()
            return image.pixel(4, image.height() // 2)

        assert face(dialog.saveButton) != face(dialog.cancelButton)


def _with_sections(item: ContentItem, count: int) -> ContentItem:
    """구간 count개(1분씩, 2분 간격)를 넣는다."""
    item.selections = tuple(TimeRange(120.0 * n, 120.0 * n + 60.0) for n in range(count))
    return item


class TestSectionButtonPlace:
    """「구간 편집…」은 구간 요약 오른쪽 끝에 위 정렬로 선다(오너 2026-11-09)."""

    def _place(self, dialog: CardEditDialog) -> tuple[int, int, int]:
        """(버튼 왼쪽 − 요약 오른쪽, 버튼 위끝 − 요약 위끝, 버튼 오른쪽 − 기본 위치 오른쪽) — 창 좌표."""
        button = dialog.sectionButton.geometry().translated(
            dialog.sectionButton.parentWidget().mapTo(dialog, QPoint(0, 0))
        )
        summary = dialog.sectionSummaryLabel.geometry().translated(
            dialog.sectionSummaryLabel.parentWidget().mapTo(dialog, QPoint(0, 0))
        )
        rightmost = dialog.defaultPathButton.geometry().translated(
            dialog.defaultPathButton.parentWidget().mapTo(dialog, QPoint(0, 0))
        )
        return (
            button.left() - summary.right(),
            button.top() - summary.top(),
            button.right() - rightmost.right(),
        )

    @pytest.mark.parametrize("count", [1, 5, 20])
    def test_button_sits_right_of_the_summary_at_the_top(self, qtbot, tmp_path, count: int) -> None:
        """구간 수가 1 · 5 · 20이어도 버튼이 요약 오른쪽에, 위끝이 요약 첫 줄 위끝과 같게, 저장 위치 줄의 맨 오른쪽 버튼과 같은 오른쪽 열에 서야 한다.

        구간 count개 카드 -> 버튼 왼쪽 > 요약 오른쪽, 위끝 차이 0, 오른쪽 끝 차이 0
        """
        item = _with_sections(_make_item(str(tmp_path)), count)
        win = _window(tmp_path, item)
        dialog = _open(win, item)
        assert dialog.sectionButton.isVisible() and dialog.sectionSummaryLabel.isVisible(), (
            "전제: 구간 줄이 보인다"
        )
        gap, top, right = self._place(dialog)
        assert gap > 0 and top == 0 and right == 0, (
            f"구간 {count}개: 사이 {gap} · 위끝 {top} · 오른쪽 {right}"
        )

    def test_long_notice_wraps_without_clipping_at_the_minimum_width(self, qtbot, tmp_path) -> None:
        """요약 뒤 경고 안내가 길어도 창 최소폭에서 감겨 잘리지 않아야 한다.

        경고 다섯(fps · 끝 당김 · 끝 늘림 · 길이 밖 · 길이 확인 중) · 창 최소폭(≥ 520, 내용 최소폭 이상)
        -> 요약 칸 높이 ≥ 그 폭의 필요 높이 (#555)
        """
        from fractions import Fraction

        from app.viewmodels.data import SECTION_CHECK_PENDING

        item = _with_sections(_make_item(str(tmp_path)), 2)
        item.section_refit_fps = Fraction(30)
        item.section_end_pulled = item.section_end_extended = True
        item.section_unfit = frozenset({1})
        item.section_check = SECTION_CHECK_PENDING
        win = _window(tmp_path, item)
        dialog = _open(win, item)
        dialog.resize(dialog.minimumWidth(), dialog.height())
        _pump()
        assert dialog.width() == dialog.minimumWidth() >= dialog_mod.DIALOG_WIDTH, "전제: 창 최소폭"
        label = dialog.sectionSummaryLabel
        assert label.isVisible(), "전제: 요약이 보인다"
        assert "checking length" in label.text(), "전제: 긴 경고가 요약에 있다"
        assert label.height() >= label.heightForWidth(label.width())
        assert dialog.sectionButton.geometry().left() > label.geometry().right()

    def test_long_button_text_widens_the_minimum_width_without_squeezing(
        self, qtbot, tmp_path
    ) -> None:
        """버튼 글이 길어져 내용 최소폭이 520을 넘으면 창 최소폭이 따라 늘어 버튼이 글 폭보다 좁아지지 않아야 한다.

        「구간 편집…」 글을 원래 글 8번 반복으로 바꿈(긴 번역 대신) -> 창 최소폭 > 520,
        최소폭으로 줄인 창에서 그 버튼 폭 ≥ 그 버튼 sizeHint 폭 (#555)
        """
        item = _with_sections(_make_item(str(tmp_path)), 2)
        win = _window(tmp_path, item)
        dialog = _open(win, item)
        button = dialog.sectionButton
        button.setText(button.text() * 8)  # 글꼴과 상관없이 내용 최소폭이 520을 넘게 늘린다
        _pump()
        assert button.isVisible(), "전제: 구간 편집 버튼이 보인다"
        assert button.sizeHint().width() > dialog_mod.DIALOG_WIDTH, "전제: 버튼 하나가 520보다 넓다"
        assert dialog.minimumWidth() > dialog_mod.DIALOG_WIDTH
        dialog.resize(dialog.minimumWidth(), dialog.height())
        _pump()
        assert dialog.width() == dialog.minimumWidth(), "전제: 창 최소폭"
        assert button.width() >= button.sizeHint().width(), (
            f"버튼 {button.width()} < 글 폭 {button.sizeHint().width()}"
        )

    def test_long_channel_name_does_not_widen_the_minimum_width(self, qtbot, tmp_path) -> None:
        """채널 이름이 아주 길어도 창 최소폭이 머리 줄 글 폭만큼 늘지 않아야 한다.

        채널 이름 「채널」 200번 반복 -> 머리 줄 원문은 그대로, 창 최소폭 < 머리 줄 원문 글 폭 (#555)
        """
        item = _make_item(str(tmp_path))
        item.channel_name = "채널" * 200
        win = _window(tmp_path, item)
        dialog = _open(win, item)
        meta = dialog.metaLabel
        assert meta.isVisible(), "전제: 머리 줄이 보인다"
        assert meta.text().startswith(item.channel_name), "전제: 머리 줄 원문에 긴 채널 이름이 있다"
        full = meta.fontMetrics().horizontalAdvance(meta.text())
        assert dialog.minimumWidth() < full, f"창 최소폭 {dialog.minimumWidth()} ≥ 글 폭 {full}"


class TestWaitingHint:
    """다운로드 차례가 이 카드를 기다리면 창 아래에 구간 창과 같은 안내가 뜬다."""

    def test_hint_appears_while_the_batch_waits_and_hides_when_it_stops(
        self,
        qtbot,
        tmp_path,
        started,  # noqa: F811
    ) -> None:
        """배치가 편집 중인 카드를 기다리는 동안에만 안내가 보여야 한다.

        A 편집 중 · B 대기 → 받기(B 받는 중) -> 안내 숨김, B 끝나 남은 대상이 A뿐 -> 안내 보임(구간 창과 같은 문구),
        A를 지움 -> 안내 숨김
        """
        first, second = _make_item(str(tmp_path), title="A"), _make_item(str(tmp_path), title="B")
        win = _window(tmp_path, first, second)
        dialog = _open(win, first)
        assert not dialog.waitHintLabel.isVisible(), "전제: 처음에는 안내가 없다"
        win.contentManager.downloadItem()
        _pump()
        assert started == [second]
        assert not dialog.waitHintLabel.isVisible(), "다른 카드를 받는 중인데 안내가 보인다"
        second.downloadState = DownloadState.FINISHED
        win.contentManager.emitFinishedRequest(second)
        _pump()
        assert win.contentManager.isWaitingOnEdit(), "전제: 배치가 A를 기다린다"
        assert dialog.waitHintLabel.isVisible()
        assert dialog.waitHintLabel.text() == (
            "A download is waiting for this card — it continues when you close this window"
        )
        win.contentManager.removeItem(first)
        _pump()
        assert not dialog.waitHintLabel.isVisible()


class TestTipBubbles:
    """편집 창 · 구간 창의 툴팁은 말풍선으로 바로 뜨고 Qt 기본 툴팁은 뜨지 않는다 (#550 inbox 128)."""

    @pytest.fixture(autouse=True)
    def _hide_bubble(self) -> Iterator[None]:
        tip_bubble.hide_tip()
        yield
        tip_bubble.hide_tip()

    def test_edit_dialog_path_shows_the_full_path_in_the_bubble(self, qtbot, tmp_path) -> None:
        """편집 창의 저장 위치 글에 들어가면 전체 경로가 말풍선으로 뜨고 Qt 툴팁은 뜨지 않아야 한다.

        카드 편집 창 · 저장 위치 글 들어감 -> 말풍선 글 = 저장 위치 글의 툴팁(전체 경로), Qt 툴팁 안 뜸
        """
        item = _make_item(str(tmp_path))
        win = _window(tmp_path, item)
        dialog = _open(win, item)
        label = dialog.pathLabel
        assert label.toolTip(), "전제: 저장 위치 글에 툴팁이 있다"
        enter(label)
        assert bubble_text_for(label) == label.toolTip()
        assert not qt_tooltip_shown(label)

    def test_section_dialog_buttons_show_their_tooltips_in_the_bubble(
        self,
        qtbot,
        tmp_path,
        basis,  # noqa: F811
    ) -> None:
        """구간 창의 첫 행 ✕ · 나중에 만든 행의 손잡이에 들어가면 그 툴팁이 말풍선으로 떠야 한다.

        편집 창 → 「구간 편집…」 · 조회 끝 · 「구간 추가」로 둘째 행 -> 첫 행 ✕와 둘째 행 손잡이에 들어가면
        각자 툴팁이 말풍선, Qt 툴팁 안 뜸(#586 — 구간 창의 툴팁은 손잡이와 ✕에만 있다)
        """
        item = _make_item(str(tmp_path))
        win = _window(tmp_path, item)
        dialog = _open(win, item)
        dialog.sectionButton.click()
        _pump()
        sections = dialog.sectionDialog
        assert sections is not None, "전제: 구간 창이 열렸다"
        qtbot.waitUntil(lambda: sections.viewModel().state != "loading", timeout=3000)
        _pump()
        sections.addButton.click()
        _pump()
        for widget in (sections._rows[0].deleteButton, sections._rows[1].handle):
            assert widget.toolTip(), f"전제: {widget.objectName()}에 툴팁이 있다"
            enter(widget)
            assert bubble_text_for(widget) == widget.toolTip()
            assert not qt_tooltip_shown(widget)
