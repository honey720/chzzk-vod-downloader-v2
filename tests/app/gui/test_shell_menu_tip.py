"""셸 S3-3 — 드롭다운 메뉴 D · 말풍선 · 바 컨트롤 높이 32 · 상태 모양 (#536, 설계 §2 · §11 · §12.1).

메뉴는 실제로 열어(`popup` → `aboutToShow`) 채우고 그린 뒤 잰다. 말풍선은 실제 위젯에 들어감 · 나감 · 누름
이벤트를 보내 띄우고 닫는다(위젯마다 건 필터를 탄다). 자리 계산(`tip_rect`)은 순수 함수라 화면 사각형을 주고
잰다. 색은 토큰에서 읽고, 상태 모양은 `grab()` 화소로 잰다(창 합성 경계는 재지 않는다 — 소유자 실기).
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QSize, Qt
from PySide6.QtGui import QColor, QEnterEvent, QHelpEvent, QMouseEvent
from PySide6.QtWidgets import QApplication, QWidget

import app.system.settings as settings
import app.theme as theme
import CVDv2 as main_module
from app.views.mainWindow import VodDownloader
from app.widgets import tip_bubble
from app.widgets.header_controls import ChoiceMenu
from tests.app.gui.card_helpers import drop_new_top_levels, hold_style, snapshot_top_levels


@pytest.fixture(autouse=True)
def _environment(qapp: QApplication) -> Iterator[None]:
    """실제 QSS(다크). 테스트가 띄운 창은 끝에 파괴한다."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))
    before = snapshot_top_levels()
    yield
    tip_bubble.hide_tip()
    drop_new_top_levels(before)


@pytest.fixture
def window(qapp: QApplication) -> VodDownloader:
    win = VodDownloader()
    win.monitorTimer.stop()
    win.resize(win.minimumWidth() + 300, 600)
    win.show()
    _pump()
    return win


def _pump() -> None:
    for _ in range(3):
        QApplication.processEvents()


def _open(button: QWidget) -> ChoiceMenu:
    """펼침 버튼의 메뉴를 실제로 열고 한 번 그린다(열 때 채운다 — aboutToShow)."""
    menu = button.menuWidget
    menu.popup(button.mapToGlobal(button.rect().bottomLeft()))
    _pump()
    menu.grab()  # 그리기 — 흐린 글이 잘렸는지(detailElided)는 그릴 때 정해진다
    return menu


def _enter(widget: QWidget) -> None:
    point = QPointF(widget.width() / 2, widget.height() / 2)
    event = QEnterEvent(point, widget.mapTo(widget.window(), point), widget.mapToGlobal(point))
    QApplication.sendEvent(widget, event)


def _leave(widget: QWidget) -> None:
    QApplication.sendEvent(widget, QEvent(QEvent.Type.Leave))


def _press(widget: QWidget) -> None:
    point = QPointF(widget.width() / 2, widget.height() / 2)
    event = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        point,
        widget.mapToGlobal(point),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(widget, event)


def _bubble() -> tip_bubble.TipBubble:
    bubble = tip_bubble.current_bubble()
    assert bubble is not None, "전제: 말풍선이 만들어졌다"
    return bubble


# ---------------------------------------------------------------------------
# ① 메뉴 D 구조
# ---------------------------------------------------------------------------


def test_the_sort_menu_has_two_headed_groups_with_a_separator(window: VodDownloader) -> None:
    """정렬 메뉴는 머리글 「Sort by」 · 기준 다섯 · 구분선 · 머리글 「Direction」 · 방향 둘 차례여야 한다."""
    menu = _open(window.sortButton)
    rows = []
    for action in menu.actions():
        if action.isSeparator():
            rows.append("-")
        elif action.property("menuHeader"):
            rows.append(("header", ChoiceMenu.nameOf(action)))
        else:
            rows.append(action.data()[0])
    assert rows == [
        ("header", "Sort by"),
        *["by"] * 5,
        "-",
        ("header", "Direction"),
        "direction",
        "direction",
    ]


@pytest.mark.parametrize(
    ("by", "names"),
    [
        ("added", ["Oldest added first", "Newest added first"]),
        ("title", ["A to Z", "Z to A"]),
        ("upload", ["Newest first", "Oldest first"]),
        ("state", ["Downloading first", "Finished first"]),
    ],
)
def test_direction_names_follow_the_sort_key(
    window: VodDownloader, by: str, names: list[str]
) -> None:
    """정렬 방향 이름은 고른 기준마다 달라야 한다(설계 §12.1)."""
    window.contentManager.order.setSort(by, False)
    menu = _open(window.sortButton)
    direction = [ChoiceMenu.nameOf(a) for a in menu.choices() if a.data()[0] == "direction"]
    assert direction == names


def test_every_row_shares_one_icon_column_and_marked_rows_are_bold(window: VodDownloader) -> None:
    """정렬 메뉴의 모든 항목은 같은 x의 16px 아이콘 칸을 쓰고, ✓ 항목(고른 기준 · 방향)만 굵게 표시돼야 한다."""
    menu = _open(window.sortButton)
    rects = [menu.iconRect(a) for a in menu.choices()]
    assert {r.left() for r in rects} == {rects[0].left()}
    assert {(r.width(), r.height()) for r in rects} == {(theme.METRICS["menuIcon"],) * 2}
    assert menu.markedKeys() == [("by", "added"), ("direction", "asc")]


def test_menu_windows_have_both_corner_flags(window: VodDownloader) -> None:
    """저장 위치 · 정렬 · 좁은 폭 상태 필터 메뉴는 투명 바탕 속성과 프레임 없음 플래그를 둘 다 가져야 한다."""
    menus = [
        window.saveLocationButton.menuWidget,
        window.sortButton.menuWidget,
        window.filterChipBar.dropMenu,
    ]
    for menu in menus:
        assert isinstance(menu, ChoiceMenu)
        assert menu.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        assert menu.windowFlags() & Qt.WindowType.FramelessWindowHint


def test_the_sort_menu_is_at_least_as_wide_as_its_button(window: VodDownloader) -> None:
    """정렬 메뉴의 폭은 버튼 폭 이상이어야 한다(폭은 버튼 폭 ~ 창 폭 − 24)."""
    menu = _open(window.sortButton)
    assert menu.width() >= window.sortButton.width()


def test_menu_tooltips_appear_only_for_elided_paths(window: VodDownloader, tmp_path: Path) -> None:
    """저장 위치 메뉴에서 흐린 경로가 잘린 줄만 그 전문을 툴팁으로 갖고, 잘리지 않은 줄은 툴팁이 없어야 한다.

    최근 = 짧은 이름 폴더 · 아주 긴 마지막 폴더(창 폭 − 24 안에 다 안 들어감)
    """
    short = tmp_path / "짧은"
    short.mkdir()
    long = tmp_path / "중간" / ("마지막 폴더 이름이 아주 아주 김 " * 6).strip()
    long.mkdir(parents=True)
    settings.update_settings({"recentPaths": [str(short), str(long)]})
    win = VodDownloader()
    win.monitorTimer.stop()
    win.resize(win.minimumWidth() + 100, 600)
    win.show()
    _pump()
    menu = _open(win.saveLocationButton)
    rows = {tuple(a.data())[1]: a for a in menu.choices()}
    assert ChoiceMenu.menuTip(rows[str(long)]) == str(long)
    assert ChoiceMenu.menuTip(rows[str(short)]) == ""
    for action, shown in ((rows[str(long)], True), (rows[str(short)], False)):
        rect = menu.actionGeometry(action)
        # 줄 왼쪽 — 폭 상한에 걸린 메뉴도 Qt는 줄 사각형을 잘리기 전 폭으로 두어 가운데가 메뉴 밖일 수 있다
        center = QPoint(rect.left() + 20, rect.center().y())
        QApplication.sendEvent(
            menu, QHelpEvent(QEvent.Type.ToolTip, center, menu.mapToGlobal(center))
        )
        bubble = tip_bubble.current_bubble()
        visible = bubble is not None and bubble.isVisible() and bubble.target is menu
        assert visible is shown, f"{ChoiceMenu.nameOf(action)}: 말풍선 {visible}"
        tip_bubble.hide_tip()
    menu.hide()


# ---------------------------------------------------------------------------
# ② 말풍선
# ---------------------------------------------------------------------------


def test_a_header_target_gets_its_bubble_below(window: VodDownloader) -> None:
    """헤더바(위쪽 영역)의 정렬 버튼에 들어가면 말풍선이 바로 아래에 떠야 한다(좁은 폭 아이콘 툴팁)."""
    window.sortButton.setCompact(True)
    _enter(window.sortButton)
    bubble = _bubble()
    assert bubble.isVisible() and bubble.text == window.sortButton.toolTip()
    assert bubble.side == "below"


def test_a_footer_target_gets_its_bubble_above(window: VodDownloader) -> None:
    """하단바의 중지 버튼에 들어가면 이벤트를 돌리지 않아도 말풍선이 바로(0ms) 위에 떠야 한다."""
    _enter(window.stopButton)
    bubble = _bubble()
    assert bubble.isVisible()
    assert bubble.side == "above"
    assert bubble.text == "Stop"


def test_the_bubble_keeps_the_gap_and_never_covers_its_target(window: VodDownloader) -> None:
    """말풍선 창은 대상과 겹치지 않고, 대상 쪽 끝이 대상에서 tipGap(4px) 떨어져야 한다."""
    _enter(window.stopButton)
    bubble = _bubble()
    target, geometry = bubble.targetRect, bubble.geometry()
    assert not geometry.intersects(target)
    assert target.top() - (geometry.bottom() + 1) == theme.METRICS["tipGap"]


def test_leaving_closes_the_bubble(window: VodDownloader) -> None:
    """대상에서 나가면 말풍선이 바로 닫혀야 한다."""
    _enter(window.stopButton)
    _leave(window.stopButton)
    assert not _bubble().isVisible()


def test_pressing_closes_the_bubble_until_the_pointer_leaves(window: VodDownloader) -> None:
    """대상을 누르면 말풍선이 닫히고, 나갔다 들어오기 전에는 다시 뜨지 않아야 한다."""
    target = window.stopButton
    _enter(target)
    _press(target)
    assert not _bubble().isVisible()
    _enter(target)
    assert not _bubble().isVisible()
    _leave(target)
    _enter(target)
    assert _bubble().isVisible()


def test_qt_tooltip_events_are_swallowed_for_bar_controls(window: VodDownloader) -> None:
    """툴팁 글이 있는 바 컨트롤의 Qt 툴팁 이벤트는 필터가 삼켜 기본 툴팁이 뜨지 않아야 한다."""
    target = window.stopButton
    event = QHelpEvent(QEvent.Type.ToolTip, QPoint(2, 2), target.mapToGlobal(QPoint(2, 2)))
    assert tip_bubble._FILTER["filter"].eventFilter(target, event) is True


@pytest.mark.parametrize(
    ("side", "expected"),
    [("below", "above"), ("above", "below")],
)
def test_tip_rect_flips_when_the_wanted_side_is_off_screen(side: str, expected: str) -> None:
    """바라는 쪽이 화면 밖이면 반대쪽에 놓고, 대상과 겹치지 않아야 한다.

    화면 0 ~ 600, 대상이 화면 아래 끝(below 실패) 또는 위 끝(above 실패)에 붙음
    """
    screen = QRect(0, 0, 800, 600)
    target = QRect(300, 570, 40, 30) if side == "below" else QRect(300, 0, 40, 30)
    rect, chosen = tip_bubble.tip_rect(target, QSize(80, 40), side, screen)
    assert chosen == expected
    assert screen.contains(rect)
    assert not rect.intersects(target)


def test_tip_rect_slides_into_the_screen_along_the_cross_axis() -> None:
    """화면 오른쪽 끝 대상의 말풍선은 가로로만 화면 안으로 밀리고 대상 아래 tipGap 자리는 그대로여야 한다."""
    screen = QRect(0, 0, 800, 600)
    target = QRect(780, 100, 20, 20)
    rect, chosen = tip_bubble.tip_rect(target, QSize(120, 40), "below", screen)
    assert chosen == "below"
    assert rect.right() == screen.right()
    assert rect.top() == target.bottom() + 1 + theme.METRICS["tipGap"]


# ---------------------------------------------------------------------------
# ③ 바 컨트롤 높이 · 상태 모양
# ---------------------------------------------------------------------------


def test_bar_controls_are_bar_control_high(window: VodDownloader) -> None:
    """메뉴바 · 헤더바 · 하단바의 컨트롤(버튼 · 드롭다운 · 정렬 · 칩 · 입력칸)은 모두 높이 32여야 한다."""
    window.filterChipBar.setCompact(True)  # 좁은 폭 드롭다운도 잰다
    for widget in window.barControls():
        assert widget.height() == theme.METRICS["barControl"], widget.objectName()


def test_pressable_bar_controls_show_a_hand_and_take_focus_by_keyboard_only(
    window: VodDownloader,
) -> None:
    """누르는 바 컨트롤은 손 모양 커서이고 초점은 키보드(Tab)로만 받아야 한다 — 입력칸은 그대로다."""
    for widget in window.barControls():
        if widget is window.urlInput:
            assert widget.focusPolicy() == Qt.FocusPolicy.StrongFocus
            continue
        assert widget.cursor().shape() == Qt.CursorShape.PointingHandCursor, widget.objectName()
        assert widget.focusPolicy() == Qt.FocusPolicy.TabFocus, widget.objectName()


def _top_border(widget: QWidget) -> QColor:
    """위 테두리 가운데 화소 — grab 이미지는 장치 화소라 배율을 곱해 읽는다."""
    image = widget.grab().toImage()
    ratio = image.devicePixelRatio()
    return image.pixelColor(int(widget.width() / 2 * ratio), 0)


def _close(a: QColor, b: str) -> bool:
    c = QColor(b)
    return abs(a.red() - c.red()) + abs(a.green() - c.green()) + abs(a.blue() - c.blue()) <= 24


@pytest.mark.parametrize(
    "name", ["fetchButton", "menuSettingsButton", "sortButton", "clearFinishedButton"]
)
def test_keyboard_focus_draws_an_accent_border(window: VodDownloader, name: str) -> None:
    """키보드 초점을 받은 바 컨트롤의 테두리는 강조색이어야 한다(초점 전에는 강조색이 아니다)."""
    widget = getattr(window, name)
    tokens = theme.current_tokens()
    assert not _close(_top_border(widget), tokens["accent"]), (
        "전제: 초점 전에는 강조색 테두리가 아니다"
    )
    window.activateWindow()
    widget.setFocus(Qt.FocusReason.TabFocusReason)
    _pump()
    assert widget.hasFocus(), "전제: 초점을 받았다"
    assert _close(_top_border(widget), tokens["accent"])


def test_keyboard_focus_on_the_accent_button_draws_the_focus_on_accent_border(
    window: VodDownloader,
) -> None:
    """강조색 채움 버튼(▶ 다운로드)에 키보드 초점이 오면 테두리는 focusOnAccent여야 한다."""
    button = window.downloadButton
    button.setEnabled(True)
    tokens = theme.current_tokens()
    window.activateWindow()
    button.setFocus(Qt.FocusReason.TabFocusReason)
    _pump()
    assert button.hasFocus(), "전제: 초점을 받았다"
    assert _close(_top_border(button), tokens["focusOnAccent"])
