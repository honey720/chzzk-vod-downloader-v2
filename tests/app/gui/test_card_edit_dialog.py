"""카드 편집 창 — 여는 길 · 한 번에 쓰는 저장 · 취소 · 구간 초안 · 해상도 · 옛 길과 같은 결과 (#545).

실제 메인 창 배선을 탄다(✎ 버튼 · 카드 더블 클릭 → 목록 → 메인 창 → 편집 창). 네트워크는 타지 않는다 —
카드의 해상도 크기 조회는 막혀 「확인 중...」으로 남고, 크기가 필요한 테스트는 조회 결과 신호를 직접 낸다.
구간 창의 기준값 조회는 대역(`basis`)이다.
"""

import os
from collections.abc import Callable

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QStyle

import app.views.card_edit_dialog as dialog_mod
from app.viewmodels.data import ContentItem
from app.views.card_edit_dialog import CardEditDialog
from app.views.mainWindow import VodDownloader
from core.models.download_state import DownloadState
from core.models.plan import TimeRange
from tests.app.gui.card_helpers import shown
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


def _open(win: VodDownloader, item: ContentItem) -> CardEditDialog:
    """카드의 ✎를 실제로 눌러 편집 창을 연다."""
    card = win.listView.widgetFor(item)
    assert card.editButton.isVisible(), "전제: 대기 카드에 ✎가 보인다"
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
        """카드의 ✎는 대기 상태에서만 보이고 툴팁이 「Edit」이어야 한다.

        상태 다섯 -> 대기만 ✎ 보임
        """
        item = _make_item(str(tmp_path))
        item.downloadState = state
        win = _window(tmp_path, item)
        card = win.listView.widgetFor(item)
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
        self, qtbot, tmp_path, state: DownloadState
    ) -> None:
        """카드 썸네일을 더블 클릭하면 대기 카드만 편집 창이 열려야 한다.

        상태 다섯 -> 대기만 편집 창
        """
        item = _make_item(str(tmp_path))
        item.downloadState = state
        win = _window(tmp_path, item)
        card = win.listView.widgetFor(item)
        QTest.mouseDClick(card.thumbnailLabel, Qt.MouseButton.LeftButton)
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


# ---- 옛 길과 같은 결과 ----


class TestSameResultAsTheOldPaths:
    """같은 값을 옛 길(카드에서 바로)과 새 창으로 고치면 결과가 같다."""

    def test_title_matches_the_inline_edit(self, qtbot, tmp_path) -> None:
        """제목을 그 자리 고치기와 편집 창으로 같은 글로 바꾸면 item.title이 같아야 한다.

        카드 A: 제목 클릭 · "NewName" · Enter / 카드 B: 편집 창 "NewName" 저장 -> 둘 다 "NewName"
        """
        old = _make_item(str(tmp_path), title="A")
        new = _make_item(str(tmp_path), title="B")
        win = _window(tmp_path, old, new)
        card = win.listView.widgetFor(old)
        shown(card.titleLabel)
        QTest.mouseClick(card.titleLabel, Qt.MouseButton.LeftButton)
        _pump()
        card.titleEdit.selectAll()
        QTest.keyClicks(card.titleEdit, "NewName")
        QTest.keyClick(card.titleEdit, Qt.Key.Key_Return)
        _pump()
        dialog = _open(win, new)
        dialog.nameEdit.setText("NewName")
        dialog.saveButton.click()
        _pump()
        assert old.title == new.title == "NewName"

    def test_save_location_matches_the_card_path(
        self, qtbot, tmp_path, monkeypatch, folder
    ) -> None:
        """저장 위치를 카드의 경로 클릭과 편집 창으로 같은 폴더로 바꾸면 download_path가 같아야 한다.

        같은 폴더 other -> 두 카드의 download_path == other
        """
        other = str(tmp_path / "other")
        os.makedirs(other)
        old = _make_item(str(tmp_path), title="A")
        new = _make_item(str(tmp_path), title="B")
        win = _window(tmp_path, old, new)
        monkeypatch.setattr(
            "app.widgets.widget.QFileDialog.getExistingDirectory", lambda *a, **k: other
        )
        win.listView.widgetFor(old).pathButton.click()
        _pump()
        folder(other)
        dialog = _open(win, new)
        dialog.browseButton.click()
        dialog.saveButton.click()
        _pump()
        assert old.download_path == new.download_path == other

    def test_resolution_matches_the_pill(self, qtbot, tmp_path, monkeypatch) -> None:
        """해상도를 pill과 편집 창으로 같은 항목으로 바꾸면 해상도 · 주소 · 스트림 · 크기 · 다시 맞추기가 같아야 한다.

        구간 1개 카드 둘, 480p로: 카드 A pill 클릭 / 카드 B 편집 창 저장
        -> (resolution, base_url, total_size) 같음, 두 카드 모두 다시 맞추기 1번, B의 pill도 480p 고름
        """
        old = _make_item(str(tmp_path), title="A")
        new = _make_item(str(tmp_path), title="B")
        for item in (old, new):
            item.selections = (TimeRange(60.0, 120.0),)
        win = _window(tmp_path, old, new)
        calls: list = []
        monkeypatch.setattr(win.contentManager, "refitSections", calls.append)
        old_card = win.listView.widgetFor(old)
        if old_card.pillMode() == "collapsed":
            old_card.setExpanded(True)
            _pump()
        old_card.buttons[1].click()
        _pump()
        dialog = _open(win, new)
        _pick(dialog, 480)
        dialog.saveButton.click()
        _pump()
        picked = lambda it: (it.resolution, it.base_url, it.total_size, it.resolution_tag)  # noqa: E731
        assert picked(old) == picked(new)
        assert calls == [old, new]
        new_card = win.listView.widgetFor(new)
        assert new_card.buttons[1].property("selected") is True
