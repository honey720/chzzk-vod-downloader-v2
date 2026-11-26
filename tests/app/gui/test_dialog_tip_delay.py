"""편집 창 · 구간 창 툴팁 지연 테스트 — 글은 카드 글처럼 머문 뒤에, 버튼 · 손잡이 · ✕는 바로 (#629)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QWidget
from pytestqt.qtbot import QtBot

from app.views.card_edit_dialog import CardEditDialog
from app.views.section_dialog import MESSAGE_ERROR, SectionEditDialog
from app.widgets import tip_bubble
from tests.app.gui.card_helpers import bubble_text_for, enter, leave, send_enter
from tests.app.gui.test_card_edit_dialog import _open
from tests.app.gui.test_section_editor import (  # noqa: F401 — 픽스처는 이름으로 쓴다
    _apply_production_qss,
    _destroy_windows,
    _make_item,
    _no_network,
    _pump,
    basis,
    open_window,
    set_rows,
)

PRODUCT_TEXT_DELAY_MS = 600  # 오너 2026-11-18 — 대화상자 글 툴팁 지연은 카드 글과 같은 600ms
TEST_TEXT_DELAY_MS = 40  # 지연 장치를 짧게 재려고 테스트가 거는 글 지연(ms)
SAMPLE_TIP = "툴팁 본보기"  # 글 라벨에 테스트가 직접 다는 툴팁 — 잘림과 무관하게 지연 경로만 잰다

# (창, 위젯 이름) — 글 툴팁이 달리는 위젯. rows[1]은 「구간 추가」로 나중에 만든 행이다
EDIT_TEXT = ("titleLabel", "metaLabel", "pathLabel")
SECTION_TEXT = (
    "titleLabel",
    "totalLabel",
    "sumLabel",
    "rows[0].infoLabel",
    "rows[0].errorLabel",
    "rows[1].infoLabel",
    "rows[1].errorLabel",
)
TEXT_CASES = [("edit", name) for name in EDIT_TEXT] + [("section", name) for name in SECTION_TEXT]
# 바로 뜨는 위젯 — 구간 행의 손잡이 · ✕(처음 행과 나중에 만든 행)
BUTTON_CASES = (
    "rows[0].deleteButton",
    "rows[0].handle",
    "rows[1].deleteButton",
    "rows[1].handle",
)


@pytest.fixture
def _hide_bubble() -> Iterator[None]:
    """앞뒤로 말풍선을 닫고 기다리던 툴팁을 그만둔다."""
    tip_bubble.hide_tip()
    tip_bubble.tip_filter().cancelPending()
    yield
    tip_bubble.tip_filter().cancelPending()
    tip_bubble.hide_tip()


@pytest.fixture
def _short_text_delay(monkeypatch: pytest.MonkeyPatch, _hide_bubble: None) -> None:
    """글 툴팁 지연만 `TEST_TEXT_DELAY_MS`로 줄인다 — 버튼 지연은 제품 값(0) 그대로다."""
    monkeypatch.setattr(tip_bubble, "TEXT_TIP_DELAY_MS", TEST_TEXT_DELAY_MS)


def _bubble_shown() -> bool:
    bubble = tip_bubble.current_bubble()
    return bubble is not None and bubble.isVisible()


def _edit_dialog(tmp_path: Path) -> CardEditDialog:
    """대기 카드 하나의 편집 창을 ✎로 연다 — 제목 · 정보 줄에는 `SAMPLE_TIP`을 단다."""
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = _open(win, item)
    dialog._holdWindow = win  # 메인 창을 붙잡아 테스트 중에 지워지지 않게 한다
    for label in (dialog.titleLabel, dialog.metaLabel):
        label.setDetailTip(SAMPLE_TIP)
    return dialog


def _section_dialog(qtbot: QtBot, tmp_path: Path) -> SectionEditDialog:
    """편집 창에서 구간 창을 열어 행 둘을 만든다 — 첫 행은 처음~10분, 둘째 행은 「구간 추가」로 만든 20~30분.

    두 행의 안내 줄에 오류 글을 보이고, 제목 · 합 알림 · 오류 글에는 `SAMPLE_TIP`을 단다(길이 · 합계는
    제품이 다는 밀리초 툴팁 그대로).
    """
    dialog = _edit_dialog(tmp_path)
    dialog.sectionButton.click()
    _pump()
    sections = dialog.sectionDialog
    assert sections is not None, "전제: 구간 창이 열렸다"
    sections._holdOwner = dialog
    qtbot.waitUntil(lambda: sections.viewModel().state != "loading", timeout=3000)
    _pump()
    set_rows(sections, [("", "00:10:00:00"), ("00:20:00:00", "00:30:00:00")])
    _pump()
    for row in sections._rows:
        row.showMessage({MESSAGE_ERROR: "오류 본보기"})
        row.errorLabel.setDetailTip(SAMPLE_TIP)
    sections.titleLabel.setDetailTip(SAMPLE_TIP)
    sections.totalLabel.setDetailTip(SAMPLE_TIP)
    _pump()
    return sections


def _widget(dialog: QWidget, name: str) -> QWidget:
    """「rows[i].이름」 또는 「이름」으로 창의 위젯을 찾는다."""
    if name.startswith("rows["):
        index, attr = name[len("rows[") :].split("].")
        return getattr(dialog._rows[int(index)], attr)
    return getattr(dialog, name)


def _target(qtbot: QtBot, tmp_path: Path, where: str, name: str) -> QWidget:
    """그 창을 열고 툴팁이 있는 보이는 위젯 하나를 돌려준다."""
    dialog = _edit_dialog(tmp_path) if where == "edit" else _section_dialog(qtbot, tmp_path)
    widget = _widget(dialog, name)
    assert widget.isVisible() and widget.toolTip(), f"전제: {where} {name}이 보이고 툴팁이 있다"
    return widget


@pytest.mark.usefixtures("_short_text_delay")
class TestTextTipDelay:
    """두 창의 글 툴팁은 글 지연만큼 머문 뒤에 뜬다 — 나중에 만든 구간 행의 글도 같다."""

    @pytest.mark.parametrize(("where", "name"), TEXT_CASES)
    def test_text_tip_shows_only_after_the_text_delay(
        self,
        qtbot: QtBot,
        tmp_path: Path,
        basis: type,  # noqa: F811
        where: str,
        name: str,
    ) -> None:
        """글 위젯에 들어가면 말풍선이 바로 뜨지 않고 글 지연 뒤에 그 위젯의 툴팁으로 떠야 한다.

        글 지연 40ms · 편집 창 제목 / 정보 줄 / 저장 위치, 구간 창 제목 / 합 알림 / 합계 /
        첫 행 · 나중 행의 길이 · 오류 글 들어감
        -> 들어감 직후 말풍선 없음 · 기다리는 위젯 = 그 위젯, 지연 뒤 말풍선 글 = 그 위젯의 툴팁
        """
        widget = _target(qtbot, tmp_path, where, name)
        send_enter(widget)
        assert not _bubble_shown()
        assert tip_bubble.tip_filter().pending() is widget
        qtbot.waitUntil(lambda: bubble_text_for(widget) == widget.toolTip(), timeout=3000)

    @pytest.mark.parametrize(("where", "name"), TEXT_CASES)
    def test_leaving_before_the_text_delay_shows_nothing(
        self,
        qtbot: QtBot,
        tmp_path: Path,
        basis: type,  # noqa: F811
        where: str,
        name: str,
    ) -> None:
        """글 지연 전에 글 위젯에서 나가면 지연이 지나도 말풍선이 뜨지 않아야 한다.

        글 지연 40ms · 글 위젯 들어감 · 바로 나감 · 지연의 세 배를 기다림 -> 기다리는 위젯 없음, 말풍선 없음
        다시 들어감 -> 지연 뒤 말풍선 글 = 그 위젯의 툴팁(말풍선이 뜨는 상태임을 확인)
        """
        widget = _target(qtbot, tmp_path, where, name)
        send_enter(widget)
        leave(widget)
        assert tip_bubble.tip_filter().pending() is None
        qtbot.wait(3 * TEST_TEXT_DELAY_MS)
        assert not _bubble_shown()
        enter(widget)
        qtbot.waitUntil(lambda: bubble_text_for(widget) == widget.toolTip(), timeout=3000)

    @pytest.mark.parametrize(
        ("where", "name"), [("edit", "titleLabel"), ("section", "rows[0].infoLabel")]
    )
    def test_switching_window_before_the_text_delay_shows_nothing(
        self,
        qtbot: QtBot,
        tmp_path: Path,
        basis: type,  # noqa: F811
        where: str,
        name: str,
    ) -> None:
        """글 지연 전에 다른 창이 활성이 되면(나감 없이) 지연이 지나도 말풍선이 뜨지 않아야 한다.

        글 지연 40ms · 편집 창 / 구간 창 활성 · 편집 창 제목 / 구간 창 첫 행 길이 글 들어감
        -> 다른 창 활성 -> 기다리는 위젯 없음, 지연의 세 배를 기다림 -> 말풍선 없음
        """
        widget = _target(qtbot, tmp_path, where, name)
        dialog = widget.window()
        dialog.activateWindow()
        qtbot.waitUntil(dialog.isActiveWindow, timeout=3000)
        send_enter(widget)
        assert tip_bubble.tip_filter().pending() is widget, "전제: 글 위젯을 기다린다"
        other = QWidget()
        other.resize(120, 80)
        other.show()
        other.activateWindow()
        try:
            qtbot.waitUntil(lambda: QApplication.activeWindow() is other, timeout=3000)
            assert tip_bubble.tip_filter().pending() is None
            qtbot.wait(3 * TEST_TEXT_DELAY_MS)
            assert not _bubble_shown()
        finally:
            other.hide()
            other.deleteLater()


@pytest.mark.usefixtures("_hide_bubble")
class TestProductDelays:
    """제품 지연 값 — 글은 카드 글과 같은 600ms, 손잡이 · ✕는 글 지연과 무관하게 바로."""

    @pytest.mark.parametrize(("where", "name"), TEXT_CASES)
    def test_text_tip_waits_600ms(
        self,
        qtbot: QtBot,
        tmp_path: Path,
        basis: type,  # noqa: F811
        where: str,
        name: str,
    ) -> None:
        """제품 지연에서 글 위젯에 들어가면 600ms 타이머로 그 위젯을 기다려야 한다.

        제품 지연 · 글 위젯 들어감 -> 말풍선 없음 · 기다리는 위젯 = 그 위젯 · 타이머 간격 600ms
        """
        widget = _target(qtbot, tmp_path, where, name)
        send_enter(widget)
        assert not _bubble_shown()
        assert tip_bubble.tip_filter().pending() is widget
        assert tip_bubble.tip_filter()._timer.interval() == PRODUCT_TEXT_DELAY_MS

    @pytest.mark.parametrize("name", BUTTON_CASES)
    def test_row_button_tip_shows_at_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
        qtbot: QtBot,
        tmp_path: Path,
        basis: type,  # noqa: F811
        name: str,
    ) -> None:
        """구간 행의 손잡이 · ✕에 들어가면 글 지연과 무관하게 바로 그 툴팁이 말풍선으로 떠야 한다.

        글 지연 10000ms · 첫 행 · 나중 행의 ✕ / 손잡이 들어감
        -> 들어감 직후 말풍선 글 = 그 위젯의 툴팁, 기다리는 위젯 없음
        """
        monkeypatch.setattr(tip_bubble, "TEXT_TIP_DELAY_MS", 10_000)
        widget = _target(qtbot, tmp_path, "section", name)
        enter(widget)
        assert bubble_text_for(widget) == widget.toolTip()
        assert tip_bubble.tip_filter().pending() is None
