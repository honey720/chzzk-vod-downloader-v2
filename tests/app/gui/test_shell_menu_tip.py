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
from PySide6.QtTest import QTest
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
    """실제 QSS(다크). 테스트가 띄운 창은 끝에 파괴하고, 테스트가 테마를 바꿨으면 다크로 되돌린다."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))
    before = snapshot_top_levels()
    yield
    tip_bubble.hide_tip()
    drop_new_top_levels(before)
    theme.set_color_scheme("dark")
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))


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

    최근 = 짧은 이름 폴더 · 아주 긴 경로(창 폭 − 24 안에 다 안 들어감)
    """
    short = tmp_path / "짧은"
    short.mkdir()
    # 긴 이름을 두 조각으로 나눈다 — 리눅스는 이름 한 조각이 255바이트까지(한글 한 자 3바이트)
    piece = ("마지막 폴더 이름이 아주 아주 김 " * 3).strip()
    long = tmp_path / "중간" / piece / piece
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


def _global_rect(widget: QWidget) -> QRect:
    return QRect(widget.mapToGlobal(QPoint(0, 0)), widget.size())


def _move_in_window(window: VodDownloader, point: QPoint) -> None:
    """창(QWindow)에 합성 포인터 이동을 보낸다 — 실기 커서를 옮기지 않는다."""
    QTest.mouseMove(window.windowHandle(), point)
    _pump()


@pytest.mark.parametrize("animated", [True, False])
def test_moving_from_the_add_tip_onto_the_select_button_shows_the_select_tip(
    window: VodDownloader, qtbot, animated: bool
) -> None:
    """「+」 툴팁이 덮은 선택 버튼 자리로 바로 옮기면 선택 버튼 툴팁이 떠서 다 보여야 한다(#611 176h).

    애니메이션 켬 · 끔, 창 폭 최소폭 + 100, URL 칸에 글(「+」 켜짐), 창 밖 → 「+」 가운데 합성 이동 -> 「+」 툴팁(아래로)이 선택 버튼과
    겹친다, 겹친 자리 가운데로 합성 이동 -> 말풍선 대상 = 선택 버튼 · 닫히는 중 아님 · 불투명도 1까지 간다
    """
    import app.viewmodels.card_display as card_display

    card_display.use(card_display.CardDisplay(animations=animated))
    try:
        # 화면 안에 들어오는 폭 — offscreen 화면은 배율 1.5면 논리 533px라 넓은 창의 말풍선은 화면 안으로 밀린다
        window.resize(window.minimumWidth() + 100, 500)
        window.urlInput.setText("https://chzzk.naver.com/video/1")
        window.activateWindow()
        qtbot.waitUntil(window.isActiveWindow, timeout=3000)
        _move_in_window(window, QPoint(1, 1))
        add, select = window.fetchButton, window.selectButton
        _move_in_window(window, add.mapTo(window, add.rect().center()))
        bubble = _bubble()
        assert bubble.target is add and bubble.side == "below", "전제: 「+」 툴팁이 아래로 떴다"
        overlap = bubble.globalRect().intersected(_global_rect(select))
        assert not overlap.isEmpty(), "전제: 「+」 툴팁이 선택 버튼을 덮는다"
        _move_in_window(window, window.mapFromGlobal(overlap.center()))
        assert select.underMouse(), "전제: 포인터가 선택 버튼 위다"
        assert bubble.target is select
        assert bubble.isVisible() and not bubble.isFadingOut()
        qtbot.waitUntil(lambda: bubble.opacity() == 1.0, timeout=2000)
    finally:
        card_display.use(None)


def test_the_tip_bubble_never_takes_the_pointer() -> None:
    """툴팁 말풍선은 창 · 위젯 모두 포인터 입력을 받지 않아야 한다 — 밑의 대상이 들어감 · 누름을 받는다(#611).

    공용 말풍선 -> 창 플래그 WindowTransparentForInput · WA_TransparentForMouseEvents 둘 다 있음
    """
    bubble = tip_bubble.tip_bubble()
    assert bubble.windowFlags() & Qt.WindowType.WindowTransparentForInput
    assert bubble.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)


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


def _activate_other_window() -> QWidget:
    """다른 최상위 창을 띄워 활성으로 만든다 — 본 창이 비활성이 된다(Alt+Tab 흉내)."""
    other = QWidget()
    other.resize(120, 80)
    other.show()
    other.activateWindow()
    _pump()
    return other


def test_the_tip_closes_when_the_window_becomes_inactive(window: VodDownloader) -> None:
    """포인터를 대상에 둔 채(나감 없이) 다른 창이 활성이 되면 말풍선이 닫혀야 한다."""
    window.activateWindow()
    _pump()
    _enter(window.stopButton)
    assert _bubble().isVisible(), "전제: 말풍선이 떴다"
    other = _activate_other_window()
    assert QApplication.activeWindow() is other, "전제: 다른 창이 활성이다"
    assert not _bubble().isVisible()


def test_the_tip_closes_when_the_window_is_minimized(window: VodDownloader) -> None:
    """포인터를 대상에 둔 채 본 창을 최소화하면 말풍선이 닫혀야 한다."""
    _enter(window.stopButton)
    assert _bubble().isVisible(), "전제: 말풍선이 떴다"
    window.showMinimized()
    _pump()
    assert window.isMinimized(), "전제: 창이 최소화됐다"
    assert not _bubble().isVisible()


def test_an_open_menu_closes_when_the_window_becomes_inactive(window: VodDownloader) -> None:
    """정렬 메뉴가 열린 채 다른 창이 활성이 되면 메뉴가 닫혀야 한다."""
    window.activateWindow()
    _pump()
    menu = _open(window.sortButton)
    assert menu.isVisible(), "전제: 메뉴가 열렸다"
    other = _activate_other_window()
    assert QApplication.activeWindow() is other, "전제: 다른 창이 활성이다"
    assert not menu.isVisible()


def test_an_open_menu_closes_when_the_window_is_minimized(window: VodDownloader) -> None:
    """정렬 메뉴가 열린 채 본 창을 최소화하면 메뉴가 닫혀야 한다."""
    menu = _open(window.sortButton)
    assert menu.isVisible(), "전제: 메뉴가 열렸다"
    window.showMinimized()
    _pump()
    assert window.isMinimized(), "전제: 창이 최소화됐다"
    assert not menu.isVisible()


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


def test_pressable_bar_controls_show_a_hand_and_take_focus_by_the_bar_rule(
    window: VodDownloader,
) -> None:
    """누르는 바 컨트롤은 손 모양 커서이고, 메뉴바는 키보드로만 · 헤더바 · 하단바는 누를 때도 초점을 받아야 한다.

    메뉴바(「VOD 다운」 · 톱니바퀴) -> TabFocus, 그 밖(입력칸 제외) -> StrongFocus, 입력칸 -> StrongFocus(#611)
    """
    menu_bar = (window.menuDownloadButton, window.settingsButton)
    for widget in window.barControls():
        if widget is window.urlInput:
            assert widget.focusPolicy() == Qt.FocusPolicy.StrongFocus
            continue
        assert widget.cursor().shape() == Qt.CursorShape.PointingHandCursor, widget.objectName()
        wanted = Qt.FocusPolicy.TabFocus if widget in menu_bar else Qt.FocusPolicy.StrongFocus
        assert widget.focusPolicy() == wanted, widget.objectName()


def _focus_url(window: VodDownloader) -> None:
    """창을 활성으로 하고 URL 칸에 Tab 초점을 준다."""
    window.activateWindow()
    window.urlInput.setFocus(Qt.FocusReason.TabFocusReason)
    _pump()
    assert window.urlInput.hasFocus(), "전제: URL 칸에 초점"


def _click_in_window(window: VodDownloader, widget: QWidget) -> None:
    """그 위젯 가운데를 창(QWindow)에 합성 클릭한다 — 실기 커서를 옮기지 않는다."""
    point = widget.mapTo(window, widget.rect().center())
    QTest.mouseClick(
        window.windowHandle(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point
    )
    _pump()


POPUP_TRIES = 50  # 메뉴 닫기를 다시 해 보는 횟수(10ms 간격 — 0.5초)


def _close_popup_soon() -> None:
    """열릴 메뉴를 이벤트 루프가 돌자마자 닫는다 — 드롭다운 메뉴는 열린 동안 호출이 돌아오지 않는다.

    메뉴가 끝내 열리지 않으면 `POPUP_TRIES`번(10ms 간격) 뒤 그만둔다 — 다음 테스트의 팝업을 닫지 않게.
    """
    from PySide6.QtCore import QTimer

    def close(left: int) -> None:
        popup = QApplication.activePopupWidget()
        if popup is not None:
            popup.close()
        elif left > 0:
            QTimer.singleShot(10, lambda: close(left - 1))

    QTimer.singleShot(0, lambda: close(POPUP_TRIES))


@pytest.mark.parametrize("name", ["saveLocationButton", "sortButton"])
def test_pressing_a_drop_field_takes_the_focus_and_keeps_it_after_the_menu(
    window: VodDownloader, name: str
) -> None:
    """URL 칸에 초점이 있을 때 드롭다운을 누르면 초점이 드롭다운으로 와서 메뉴가 닫힌 뒤에도 남아야 한다(#611).

    URL 칸 Tab 초점 → 드롭다운 가운데 합성 클릭(메뉴는 열리자마자 닫음) -> 초점 = 드롭다운 · URL 칸 초점 없음,
    keyFocus 거짓, 위 테두리 가운데 ≈ fieldEdge · ≉ accent
    """
    _focus_url(window)
    widget = getattr(window, name)
    _close_popup_soon()
    _click_in_window(window, widget)
    assert window.focusWidget() is widget
    window.activateWindow()  # 메뉴가 닫힌 뒤 창 활성은 offscreen이 돌려주지 않는다 — 초점 위젯은 창이 기억한다
    _pump()
    assert widget.hasFocus(), "전제: 창이 다시 활성이고 초점이 드롭다운에 있다"
    assert not window.urlInput.hasFocus()
    assert widget.property("keyFocus") is False
    tokens = theme.current_tokens()
    assert _close(_top_border(widget), tokens["fieldEdge"])
    assert not _close(_top_border(widget), tokens["accent"])


@pytest.mark.parametrize("name", ["selectButton", "fetchButton", "chip"])
def test_pressing_a_bar_button_takes_the_focus_without_an_accent_border(
    window: VodDownloader, name: str
) -> None:
    """URL 칸에 초점이 있을 때 헤더바 버튼을 누르면 초점이 버튼으로 오고 테두리는 강조색이 아니어야 한다(#611).

    창 폭 1200, URL 칸에 글을 넣고(「+」가 켜짐) Tab 초점 → 버튼(선택 · 「+」 · 둘째 상태 칩 — 접혔으면 상태
    드롭다운) 가운데 합성 클릭(누름 동작은 끊음) -> 초점 = 버튼 · keyFocus 거짓 · 위 테두리 ≉ accent
    """
    window.resize(1200, 600)
    window.urlInput.setText("https://chzzk.naver.com/video/1")
    _focus_url(window)
    drop = window.filterChipBar.dropButton
    if name != "chip":
        widget = getattr(window, name)
    elif window.filterChipBar.isCompact():
        widget = drop  # 글꼴이 넓으면 칩이 상태 드롭다운으로 접힌다 — 그 드롭다운을 잰다
        _close_popup_soon()
    else:
        widget = list(window.filterChips.values())[1]
    assert widget.isVisible(), "전제: 버튼이 보인다"
    if widget is not drop:  # 드롭다운은 누름 연결이 없고 메뉴만 연다
        widget.clicked.disconnect()  # 등록 · 선택 모드 · 칩 고르기는 이 테스트가 재는 것이 아니다
    _click_in_window(window, widget)
    if widget is drop:
        window.activateWindow()  # 메뉴가 닫힌 뒤 창 활성은 offscreen이 돌려주지 않는다
        _pump()
    assert QApplication.focusWidget() is widget
    assert widget.property("keyFocus") is False
    assert not _close(_top_border(widget), theme.current_tokens()["accent"])


def test_keyboard_focus_after_a_mouse_focus_draws_the_accent_border(window: VodDownloader) -> None:
    """마우스로 초점을 받은 버튼도 키보드(Tab)로 다시 받으면 테두리가 강조색이어야 한다(#611).

    선택 버튼 합성 클릭(누름 동작은 끊음) → 목록에 초점 → 선택 버튼에 Tab 초점 -> keyFocus 참 · 위 테두리 ≈ accent
    """
    window.activateWindow()
    _pump()
    widget = window.selectButton
    widget.clicked.disconnect()
    _click_in_window(window, widget)
    assert widget.property("keyFocus") is False, "전제: 마우스 초점"
    window.listView.setFocus(Qt.FocusReason.OtherFocusReason)
    widget.setFocus(Qt.FocusReason.TabFocusReason)
    _pump()
    assert widget.property("keyFocus") is True
    assert _close(_top_border(widget), theme.current_tokens()["accent"])


@pytest.mark.parametrize("keyboard", [True, False])
def test_a_menu_closing_keeps_the_focus_kind(window: VodDownloader, keyboard: bool) -> None:
    """펼친 메뉴가 닫혀 초점이 돌아와도(PopupFocusReason) 받기 전 초점 종류가 그대로여야 한다(#611).

    정렬 칸에 Tab · 마우스 초점 → FocusOut · FocusIn(PopupFocusReason) 보냄 -> keyFocus 그대로(참 · 거짓)
    """
    from PySide6.QtGui import QFocusEvent

    window.activateWindow()
    widget = window.sortButton
    reason = Qt.FocusReason.TabFocusReason if keyboard else Qt.FocusReason.MouseFocusReason
    widget.setFocus(reason)
    _pump()
    assert widget.property("keyFocus") is keyboard, "전제: 받은 초점 종류"
    for kind in (QEvent.Type.FocusOut, QEvent.Type.FocusIn):
        QApplication.sendEvent(widget, QFocusEvent(kind, Qt.FocusReason.PopupFocusReason))
    assert widget.property("keyFocus") is keyboard


def _top_border(widget: QWidget) -> QColor:
    """위 테두리 가운데 화소 — grab 이미지는 장치 화소라 배율을 곱해 읽는다."""
    image = widget.grab().toImage()
    ratio = image.devicePixelRatio()
    return image.pixelColor(int(widget.width() / 2 * ratio), 0)


def _close(a: QColor, b: str) -> bool:
    c = QColor(b)
    return abs(a.red() - c.red()) + abs(a.green() - c.green()) + abs(a.blue() - c.blue()) <= 24


@pytest.mark.parametrize("name", ["fetchButton", "settingsButton", "selectButton"])
def test_keyboard_focus_draws_an_accent_border(window: VodDownloader, name: str) -> None:
    """키보드 초점을 받은 바 컨트롤의 테두리는 강조색이어야 한다(초점 전에는 강조색이 아니다).

    URL 칸에 글을 넣어 둔다 — 「+」는 빈 칸이면 꺼져 초점을 받지 않는다
    """
    window.urlInput.setText("https://chzzk.naver.com/video/1")
    _pump()
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


def test_keyboard_focus_draws_an_accent_bottom_on_a_drop_field(window: VodDownloader) -> None:
    """키보드 초점을 받은 펼침 칸(입력 칸)은 아래 테두리가 강조색이어야 한다(초점 전에는 강조색이 아니다).

    입력 칸 초점은 네 변 강조색 1px다(#611 176h) — 정렬 칸에 Tab 초점 -> 아래 테두리 가운데 ≈ accent
    """
    widget = window.sortButton
    tokens = theme.current_tokens()

    def bottom() -> QColor:
        image = widget.grab().toImage()
        ratio = image.devicePixelRatio()
        return image.pixelColor(int(widget.width() / 2 * ratio), int((widget.height() - 1) * ratio))

    assert not _close(bottom(), tokens["accent"]), "전제: 초점 전에는 강조색 테두리가 아니다"
    window.activateWindow()
    widget.setFocus(Qt.FocusReason.TabFocusReason)
    _pump()
    assert widget.hasFocus(), "전제: 초점을 받았다"
    assert _close(bottom(), tokens["accent"])


def test_the_list_has_focus_when_the_window_opens(qapp: QApplication) -> None:
    """창을 처음 띄우면 초점이 목록에 있고 메뉴바 「VOD 다운」 테두리는 강조색이 아니어야 한다(#536 — 설계 §12.1)."""
    win = VodDownloader()
    win.monitorTimer.stop()
    win.show()
    win.activateWindow()
    _pump()
    assert QApplication.focusWidget() is win.listView
    assert not _close(_top_border(win.menuDownloadButton), theme.current_tokens()["accent"])


@pytest.mark.parametrize("name", ["menuDownloadButton", "settingsButton"])
def test_clicking_a_bar_control_draws_no_accent_border(
    window: VodDownloader, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """바 컨트롤을 마우스로 누르면 초점이 옮겨 가지 않고 테두리가 강조색이 아니어야 한다(#536 — §11.5).

    누름 = QTest.mouseClick(실제 마우스 이벤트 경로 — 누름 초점 처리를 탄다), 「설정」 창 열기는 막는다
    """
    from PySide6.QtTest import QTest

    monkeypatch.setattr(window, "onSetting", lambda: None)
    window.settingsButton.clicked.disconnect()
    widget = getattr(window, name)
    window.activateWindow()
    _pump()
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton)
    _pump()
    assert QApplication.focusWidget() is not widget
    assert not _close(_top_border(widget), theme.current_tokens()["accent"])


def test_tab_from_the_list_moves_focus_to_a_bar_control_with_an_accent_border(
    window: VodDownloader,
) -> None:
    """시작 초점(목록)에서 Shift+Tab을 누르면 앞 바 컨트롤(선택 버튼)으로 초점이 가고 테두리가 강조색이어야 한다.

    탭 순서: … 정렬 → 선택 버튼 → 목록 → ▶ (#536 · #561)
    """
    from PySide6.QtTest import QTest

    window.activateWindow()
    window.listView.setFocus(Qt.FocusReason.OtherFocusReason)
    _pump()
    assert QApplication.focusWidget() is window.listView, "전제: 목록에 초점"
    QTest.keyClick(window.listView, Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)
    _pump()
    assert QApplication.focusWidget() is window.selectButton
    assert _close(_top_border(window.selectButton), theme.current_tokens()["accent"])


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


# ---------------------------------------------------------------------------
# ④ 셸 모양 프로토 맞춤 — 메뉴바 · 칩 · 등록 · 펼침 버튼 (#536 — 설계 §12.1 「셸 모양은 프로토대로」)
# ---------------------------------------------------------------------------


def _seen(widget: QWidget) -> "QImage":  # noqa: F821
    """창에 보이는 그대로의 위젯 자리 — 바탕이 투명한 위젯은 제 grab에 부모 면이 없어 창에서 잘라 찍는다."""
    window = widget.window()
    return window.grab(QRect(widget.mapTo(window, QPoint(0, 0)), widget.size())).toImage()


def _pixel(widget: QWidget, x: int, y: int) -> QColor:
    """위젯 좌표 (x, y)의 보이는 화소 — 이미지는 장치 화소라 배율을 곱해 읽는다."""
    image = _seen(widget)
    ratio = image.devicePixelRatio()
    return image.pixelColor(int(x * ratio), int(y * ratio))


def _inked(widget: QWidget, rect: QRect, color: str) -> int:
    """그 칸 안에서 `color`에 가까운 보이는 화소 수."""
    image = _seen(widget)
    ratio = image.devicePixelRatio()
    return sum(
        1
        for y in range(int(rect.top() * ratio), int((rect.bottom() + 1) * ratio))
        for x in range(int(rect.left() * ratio), int((rect.right() + 1) * ratio))
        if _close(image.pixelColor(x, y), color)
    )


def test_the_menu_bar_has_the_bar_face_and_a_bottom_line(window: VodDownloader) -> None:
    """메뉴바는 높이 = 안쪽 여백 위아래 + 바 컨트롤이고 맨 아래 줄까지 바 면이어야 한다 — 헤더바와 한 덩어리(#611).

    barPadY 8 · barControl 32 -> 높이 48, 가운데 빈 자리 = surface, 맨 아래 줄 = surface(경계선 없음)
    """
    bar = window.appMenuBar
    tokens = theme.current_tokens()
    assert bar.height() == 2 * theme.METRICS["barPadY"] + theme.METRICS["barControl"]
    assert _close(_pixel(bar, bar.width() // 2, bar.height() // 2), tokens["surface"])
    assert _close(_pixel(bar, bar.width() // 2, bar.height() - 1), tokens["surface"])


def test_the_header_bar_is_two_rows_high_with_a_bottom_line(window: VodDownloader) -> None:
    """헤더바는 높이 = 안쪽 여백 위아래 + 두 줄 + 줄 사이 + 경계선 1이고 맨 아래 줄이 테두리 색이어야 한다(오류 줄 자리 없음).

    barPadY 8 · barControl 32 · 줄 사이 8 -> 89, 맨 아래 줄 = border (#536 — 프로토 헤더바 89)
    """
    frame = window.headerFrame
    m = theme.METRICS
    assert (
        frame.height()
        == 2 * m["barPadY"] + 2 * m["barControl"] + window.headerFrameLayout.spacing() + 1
    )
    assert _close(
        _pixel(frame, frame.width() // 2, frame.height() - 1), theme.current_tokens()["border"]
    )


def test_an_unselected_menu_is_muted_text_without_a_face_or_border(window: VodDownloader) -> None:
    """메뉴바 오른쪽 끝 설정 톱니바퀴는 면 · 테두리 없이 바 면 위 그림만이어야 한다 — 위 테두리 · 왼쪽 안 = 바 면(#611)."""
    button = window.settingsButton
    surface = theme.current_tokens()["surface"]
    assert not button.isChecked()
    assert _close(_pixel(button, button.width() // 2, 0), surface)
    assert _close(_pixel(button, 3, button.height() // 2), surface)


def test_the_selected_menu_has_bold_text_and_no_face(window: VodDownloader) -> None:
    """고른 메뉴(「VOD 다운」)는 면 없이 바 면(surface) 위 굵은 글이어야 한다(밑줄 탭, #611)."""
    button = window.menuDownloadButton
    assert button.isChecked() and button.font().bold()
    assert _close(_pixel(button, 3, button.height() // 2), theme.current_tokens()["surface"])


def test_the_chosen_chip_has_the_bar_face_and_a_border_and_no_accent_fill(
    window: VodDownloader,
) -> None:
    """고른 칩(전체)은 면 surfaceAlt만(테두리 없음), 고르지 않은 칩(대기)은 면 · 테두리 없이 바 면이고 칩 줄에 강조색이 없어야 한다(#611)."""
    tokens = theme.current_tokens()
    window.resize(window.minimumWidth() + 900, 600)  # 칩 모양이 보이는 폭
    _pump()
    chosen, other = window.filterChips["all"], window.filterChips["waiting"]
    assert chosen.isVisible() and other.isVisible(), "전제: 칩 모양"
    assert chosen.isChecked() and not other.isChecked()
    assert _close(_pixel(chosen, 3, chosen.height() // 2), tokens["surfaceAlt"])
    assert _close(_pixel(chosen, chosen.width() // 2, 0), tokens["surfaceAlt"])
    assert _close(_pixel(other, other.width() // 2, 0), tokens["surface"])
    assert _close(_pixel(other, 3, other.height() // 2), tokens["surface"])
    bar = window.filterChipBar
    assert _inked(bar, bar.rect(), tokens["accent"]) == 0


def test_the_add_button_is_a_square_plus_with_a_shortcut_tip(window: VodDownloader) -> None:
    """등록은 글 없는 정사각(바 컨트롤 높이) 버튼이고, 들어가면 말풍선에 「Add · Enter」가 떠야 한다.

    URL 칸에 글이 있어 켠 상태에서 잰다(빈 칸이면 꺼진 색 — 아래 테스트)
    """
    window.urlInput.setText("https://chzzk.naver.com/video/1")
    _pump()
    button = window.fetchButton
    side = theme.METRICS["barControl"]
    assert button.text() == ""
    assert (button.width(), button.height()) == (side, side)
    assert button.toolTip() == tip_bubble.shortcut_tip("Add", "Enter")
    assert _inked(button, button.glyphRect(), theme.current_tokens()["text"]) > 0, "「+」를 그린다"
    _enter(button)
    assert _bubble().isVisible() and _bubble().shownText() == "Add · Enter"


@pytest.mark.parametrize("text", ["", "   "])
def test_the_add_button_is_off_while_the_url_input_is_blank(
    window: VodDownloader, text: str
) -> None:
    """URL 칸이 비었거나 공백뿐이면 「+」가 꺼지고 꺼진 글 색으로 그려져야 한다."""
    window.urlInput.setText("x")
    window.urlInput.setText(text)
    _pump()
    button = window.fetchButton
    tokens = theme.current_tokens()
    assert not button.isEnabled()
    assert _inked(button, button.glyphRect(), tokens["textDisabled"]) > 0
    assert _inked(button, button.glyphRect(), tokens["text"]) == 0


def test_the_add_button_turns_on_once_the_url_input_has_text(window: VodDownloader) -> None:
    """빈 URL 칸에 글을 치면 「+」가 켜지고 다시 지우면 꺼져야 한다."""
    assert not window.fetchButton.isEnabled(), "전제: 창을 연 직후 URL 칸은 비어 있다"
    QTest.keyClicks(window.urlInput, "h")
    _pump()
    assert window.fetchButton.isEnabled()
    window.urlInput.clear()
    _pump()
    assert not window.fetchButton.isEnabled()


@pytest.mark.parametrize("text", ["", "https://chzzk.naver.com/video/1"])
def test_the_add_button_has_no_accent_fill(window: VodDownloader, text: str) -> None:
    """「+」는 켜졌든 꺼졌든 강조색으로 채우지 않아야 한다(채운 강조 버튼은 하단 다운로드 하나 — 설계 §11.6)."""
    window.urlInput.setText(text)
    _pump()
    button = window.fetchButton
    assert _inked(button, button.rect(), theme.current_tokens()["accent"]) == 0


def test_the_url_hint_says_enter_adds(window: VodDownloader) -> None:
    """URL 칸 안내 글은 「Enter Chzzk URL · Enter to add」여야 한다."""
    assert window.urlInput.placeholderText() == "Enter Chzzk URL · Enter to add"


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_the_save_location_folder_is_muted_and_the_caret_is_a_chevron(
    qapp: QApplication, window: VodDownloader, scheme: str
) -> None:
    """저장 위치 버튼 글 모양의 폴더와 펼침 표시(⌄ 칸 10px)는 두 테마 모두 흐린 글 색 단색이어야 한다(#536)."""
    from app.widgets.header_controls import BUTTON_PADDING, CARET_WIDTH

    theme.set_color_scheme(scheme)
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))
    window.resize(window.minimumWidth() + 900, 600)
    _pump()
    button = window.saveLocationButton
    assert not button.isCompact(), "전제: 글 모양"
    tokens = theme.current_tokens()
    size = theme.METRICS["actionGlyph"]
    glyph = QRect(1 + BUTTON_PADDING, (button.height() - size) // 2, size, size)
    assert _inked(button, glyph, tokens["textMuted"]) > 0
    caret = button.caretRect()
    assert caret.width() == CARET_WIDTH
    assert _inked(button, caret, tokens["textMuted"]) > 0


# ---------------------------------------------------------------------------
# ⑤ 하단 아이콘 칸 · 말풍선 면 · 메뉴 아이콘(#536)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "tip"),
    [("conn", "Connections · 4"), ("mem", "Memory · 64 MB"), ("free", "Free space · 12.0 GB")],
)
def test_footer_cells_show_an_icon_and_the_value_with_the_name_in_the_tip(
    window: VodDownloader, key: str, tip: str
) -> None:
    """연결 · 메모리 · 여유 칸은 왼쪽 16px에 흐린 색 아이콘, 글은 값만, 이름은 툴팁이어야 한다.

    받는 중 · 연결 4 · 메모리 64 MiB · 여유 12 GiB -> 글 "4" · "64 MB" · "12.0 GB", 툴팁 「이름 · 값」
    """
    from app.viewmodels.footer_monitor import MonitorSnapshot

    monitor = window.footerMonitor
    monitor.setSnapshot(MonitorSnapshot(True, 2_000_000.0, None, 4, 64 * 1024 * 1024, 12 * 1024**3))
    _pump()
    label = monitor.labels[key]
    assert label.isVisible(), "전제: 칸이 보인다"
    assert label.text() == tip.split(" · ")[1]
    assert label.toolTip() == tip
    side = theme.METRICS["narrowIcon"]
    icon = QRect(0, (label.height() - side) // 2, side, side)
    assert _inked(label, icon, theme.current_tokens()["textMuted"]) > 0


def test_the_tip_bubble_floats_on_its_own_face(window: VodDownloader) -> None:
    """말풍선 몸통 면은 tipBg(바 면 아님)이어야 한다 — 하단 중지 버튼 말풍선의 몸통 가운데 화소."""
    _enter(window.stopButton)
    bubble = _bubble()
    assert bubble.isVisible() and bubble.tone == "tip"
    body = bubble.bodyRect()
    image = bubble.grab().toImage()
    ratio = image.devicePixelRatio()
    corner = image.pixelColor(int((body.left() + 3) * ratio), int(body.center().y() * ratio))
    tokens = theme.current_tokens()
    assert not _close(QColor(tokens["tipBg"]), tokens["surface"]), (
        "전제(다크): 떠 있는 면은 바 면과 다르다"
    )
    assert _close(corner, tokens["tipBg"])


def test_find_folder_uses_the_outline_folder(window: VodDownloader) -> None:
    """저장 위치 메뉴 「폴더 찾기…」 줄의 아이콘은 테두리 폴더(folder_outline)여야 한다."""
    menu = _open(window.saveLocationButton)
    find = next(a for a in menu.actions() if a.objectName() == "findFolderAction")
    assert find.property("glyph") == "folder_outline"
