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


def test_the_list_has_focus_when_the_window_opens(qapp: QApplication) -> None:
    """창을 처음 띄우면 초점이 목록에 있고 메뉴바 「VOD 다운」 테두리는 강조색이 아니어야 한다(#536 — 설계 §12.1)."""
    win = VodDownloader()
    win.monitorTimer.stop()
    win.show()
    win.activateWindow()
    _pump()
    assert QApplication.focusWidget() is win.listView
    assert not _close(_top_border(win.menuDownloadButton), theme.current_tokens()["accent"])


@pytest.mark.parametrize("name", ["menuDownloadButton", "menuSettingsButton"])
def test_clicking_a_bar_control_draws_no_accent_border(
    window: VodDownloader, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """바 컨트롤을 마우스로 누르면 초점이 옮겨 가지 않고 테두리가 강조색이 아니어야 한다(#536 — §11.5).

    누름 = QTest.mouseClick(실제 마우스 이벤트 경로 — 누름 초점 처리를 탄다), 「설정」 창 열기는 막는다
    """
    from PySide6.QtTest import QTest

    monkeypatch.setattr(window, "onSetting", lambda: None)
    window.menuSettingsButton.clicked.disconnect()
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
    """시작 초점(목록)에서 Shift+Tab을 누르면 앞 바 컨트롤(완료 지우기)로 초점이 가고 테두리가 강조색이어야 한다.

    탭 순서: … 정렬 → 완료 지우기 → 목록 → ▶ (#536)
    """
    from PySide6.QtTest import QTest

    window.activateWindow()
    window.listView.setFocus(Qt.FocusReason.OtherFocusReason)
    _pump()
    assert QApplication.focusWidget() is window.listView, "전제: 목록에 초점"
    QTest.keyClick(window.listView, Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)
    _pump()
    assert QApplication.focusWidget() is window.clearFinishedButton
    assert _close(_top_border(window.clearFinishedButton), theme.current_tokens()["accent"])


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
    """메뉴바는 높이 = 안쪽 여백 위아래 + 바 컨트롤 + 경계선 1, 빈 자리는 바 면, 맨 아래 줄은 테두리 색이어야 한다.

    barPadY 8 · barControl 32 -> 높이 49, 오른쪽 빈 자리 = surface, 맨 아래 줄 = border
    """
    bar = window.appMenuBar
    tokens = theme.current_tokens()
    assert bar.height() == 2 * theme.METRICS["barPadY"] + theme.METRICS["barControl"] + 1
    assert _close(_pixel(bar, bar.width() - 4, bar.height() // 2), tokens["surface"])
    assert _close(_pixel(bar, bar.width() // 2, bar.height() - 1), tokens["border"])


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
    """고르지 않은 메뉴(「설정」)는 면 · 테두리 없이 바 면 위 글만이어야 한다 — 위 테두리 · 왼쪽 안 = 바 면."""
    button = window.menuSettingsButton
    surface = theme.current_tokens()["surface"]
    assert not button.isChecked()
    assert _close(_pixel(button, button.width() // 2, 0), surface)
    assert _close(_pixel(button, 3, button.height() // 2), surface)


def test_the_selected_menu_keeps_its_face_and_bold_text(window: VodDownloader) -> None:
    """고른 메뉴(「VOD 다운」)는 버튼 면(surfaceAlt) 위 굵은 글이어야 한다."""
    button = window.menuDownloadButton
    assert button.isChecked() and button.font().bold()
    assert _close(_pixel(button, 3, button.height() // 2), theme.current_tokens()["surfaceAlt"])


def test_the_chosen_chip_has_the_bar_face_and_a_border_and_no_accent_fill(
    window: VodDownloader,
) -> None:
    """고른 칩(전체)은 바 면 + 테두리, 고르지 않은 칩(대기)은 면 · 테두리 없이 바 면이고 칩 줄에 강조색이 없어야 한다."""
    tokens = theme.current_tokens()
    window.resize(window.minimumWidth() + 900, 600)  # 칩 모양이 보이는 폭
    _pump()
    chosen, other = window.filterChips["all"], window.filterChips["waiting"]
    assert chosen.isVisible() and other.isVisible(), "전제: 칩 모양"
    assert chosen.isChecked() and not other.isChecked()
    assert _close(_pixel(chosen, 3, chosen.height() // 2), tokens["surface"])
    assert _close(_pixel(chosen, chosen.width() // 2, 0), tokens["border"])
    assert _close(_pixel(other, other.width() // 2, 0), tokens["surface"])
    assert _close(_pixel(other, 3, other.height() // 2), tokens["surface"])
    bar = window.filterChipBar
    assert _inked(bar, bar.rect(), tokens["accent"]) == 0


def test_the_add_button_is_a_square_plus_with_a_shortcut_tip(window: VodDownloader) -> None:
    """등록은 글 없는 정사각(바 컨트롤 높이) 버튼이고, 들어가면 말풍선에 「Add · Enter」가 떠야 한다."""
    button = window.fetchButton
    side = theme.METRICS["barControl"]
    assert button.text() == ""
    assert (button.width(), button.height()) == (side, side)
    assert button.toolTip() == tip_bubble.shortcut_tip("Add", "Enter")
    assert _inked(button, button.glyphRect(), theme.current_tokens()["text"]) > 0, "「+」를 그린다"
    _enter(button)
    assert _bubble().isVisible() and _bubble().shownText() == "Add · Enter"


def test_the_url_hint_says_enter_adds(window: VodDownloader) -> None:
    """URL 칸 안내 글은 「Enter Chzzk URL · Enter to add」여야 한다."""
    assert window.urlInput.placeholderText() == "Enter Chzzk URL · Enter to add"


def test_the_save_location_folder_is_yellow_and_the_caret_is_a_chevron(
    window: VodDownloader,
) -> None:
    """저장 위치 버튼 글 모양의 폴더는 folderIcon 노랑이고, 펼침 표시는 ⌄ 칸(10px)에 흐린 색으로 그려야 한다."""
    from app.widgets.header_controls import BUTTON_PADDING, CARET_WIDTH

    window.resize(window.minimumWidth() + 900, 600)
    _pump()
    button = window.saveLocationButton
    assert not button.isCompact(), "전제: 글 모양"
    tokens = theme.current_tokens()
    size = theme.METRICS["actionGlyph"]
    glyph = QRect(1 + BUTTON_PADDING, (button.height() - size) // 2, size, size)
    assert _inked(button, glyph, tokens["folderIcon"]) > 0
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
