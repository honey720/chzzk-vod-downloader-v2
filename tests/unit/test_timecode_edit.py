"""타임코드 입력 칸 — 숫자만 쳐서 오른쪽부터 채운다 (#309).

`app.widgets.timecode_edit.TimecodeEdit`에 실제 키 이벤트를 보내, 밝게 보이는 부분과 칸이
내놓는 값을 잰다. 기대값은 손으로 적은 글자다.
"""

import random
from fractions import Fraction

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QFocusEvent, QMouseEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

import app.widgets.timecode_edit as timecode_edit
from app.widgets.timecode_edit import (
    CLOCK_FIELDS,
    TimecodeEdit,
    TimePointEdit,
    digits_from_text,
)


@pytest.fixture
def edit(qtbot):
    """보이는 입력 칸 하나."""
    widget = TimecodeEdit()
    qtbot.addWidget(widget)
    widget.show()
    QTest.qWaitForWindowExposed(widget)
    return widget


@pytest.fixture
def clipboard(monkeypatch):
    """클립보드 대역 — 칸이 읽는 글을 정한다. 실제(OS) 클립보드를 읽거나 쓰지 않는다."""

    class _Clipboard:
        content = ""

        def text(self) -> str:
            return self.content

        def setText(self, text: str) -> None:
            self.content = text

    fake = _Clipboard()
    application = type("_Application", (), {"clipboard": staticmethod(lambda: fake)})
    monkeypatch.setattr(timecode_edit, "QGuiApplication", application)
    return fake


def type_digits(widget, digits: str) -> None:
    QTest.keyClicks(widget, digits)
    QApplication.processEvents()


def test_empty_field_is_all_dim_and_reads_as_zero(edit):
    """아무것도 치지 않은 칸은 전부 흐리고 값은 00:00:00:00이어야 한다.

    숫자 0개 -> 흐린 부분 "00:00:00:00", 밝은 부분 "", 값 "00:00:00:00"
    """
    assert (edit.dimText(), edit.brightText()) == ("00:00:00:00", "")
    assert edit.text() == "00:00:00:00"


def test_digits_fill_from_the_right_and_only_the_typed_part_is_bright(edit):
    """숫자는 맨 오른쪽부터 채워지고 친 부분만 밝게 보여야 한다 — 오너가 준 여섯 단계.

    0 · 0 · 1 · 0 · 0 · 3을 차례로 침
    -> 밝은 부분: "0" → "00" → "0:01" → "00:10" → "0:01:00" → "00:10:03"
    -> 값: 00:00:00:00 → 00:00:00:00 → 00:00:00:01 → 00:00:00:10 → 00:00:01:00 → 00:00:10:03
    """
    seen = []
    for digit in "001003":
        type_digits(edit, digit)
        seen.append((edit.brightText(), edit.text()))

    assert seen == [
        ("0", "00:00:00:00"),
        ("00", "00:00:00:00"),
        ("0:01", "00:00:00:01"),
        ("00:10", "00:00:00:10"),
        ("0:01:00", "00:00:01:00"),
        ("00:10:03", "00:00:10:03"),
    ]
    assert edit.dimText() + edit.brightText() == "00:00:10:03"
    assert edit.dimText() == "00:"


def test_the_ninth_digit_is_ignored(edit):
    """여덟 자리가 찬 칸은 숫자를 더 받지 않아야 한다.

    1~8을 친 뒤 9를 침 -> 값 "12:34:56:78" 그대로, 흐린 부분 없음
    """
    type_digits(edit, "12345678")
    assert edit.text() == "12:34:56:78"

    type_digits(edit, "9")

    assert edit.text() == "12:34:56:78" and edit.digits() == "12345678"
    assert edit.dimText() == ""


def test_backspace_removes_the_rightmost_digit_and_delete_clears(edit):
    """Backspace는 맨 오른쪽 숫자부터 하나씩 지우고 나머지를 오른쪽으로 당기며, Delete는 전부 지워야 한다.

    001003을 침(00:00:10:03) → Backspace -> 밝은 부분 "0:01:00", 값 "00:00:01:00"
    → Backspace -> "00:10" · "00:00:00:10" → Delete -> 밝은 부분 "", 값 "00:00:00:00"
    """
    type_digits(edit, "001003")

    QTest.keyClick(edit, Qt.Key.Key_Backspace)
    assert (edit.brightText(), edit.text()) == ("0:01:00", "00:00:01:00")
    QTest.keyClick(edit, Qt.Key.Key_Backspace)
    assert (edit.brightText(), edit.text()) == ("00:10", "00:00:00:10")
    QTest.keyClick(edit, Qt.Key.Key_Delete)
    assert (edit.brightText(), edit.text()) == ("", "00:00:00:00")


def test_typing_into_a_field_that_has_a_value_starts_over(edit):
    """값이 있는 칸에 들어가 숫자를 치면 기존 값을 지우고 새로 시작해야 한다.

    값 "01:02:03:04"인 칸에 포커스가 들어온 뒤 5를 침 -> 값 "00:00:00:05", 밝은 부분 "5"
    이어서 6을 침 -> 값 "00:00:00:56"(이어 붙는다)
    """
    edit.setText("01:02:03:04")
    assert edit.brightText() == "01:02:03:04", "넣은 값은 전부 밝게 보여야 한다"
    QApplication.sendEvent(edit, _focus_in())

    type_digits(edit, "5")
    assert (edit.text(), edit.brightText()) == ("00:00:00:05", "5")
    type_digits(edit, "6")
    assert edit.text() == "00:00:00:56"


def test_backspace_on_a_field_just_entered_edits_the_existing_value(edit):
    """값이 있는 칸에 들어가 Backspace를 누르면 기존 값의 맨 오른쪽 숫자를 지워야 한다.

    값 "01:02:03:04"인 칸에 포커스가 들어온 뒤 Backspace -> 값 "00:10:20:30"
    """
    edit.setText("01:02:03:04")
    QApplication.sendEvent(edit, _focus_in())

    QTest.keyClick(edit, Qt.Key.Key_Backspace)

    assert edit.text() == "00:10:20:30"


def test_only_digits_are_taken_and_the_keypad_counts(edit):
    """숫자가 아닌 글자는 들어가지 않고, 숫자 키패드의 숫자는 들어가야 한다.

    "1a:b.2-" 를 치고, 키패드 수식키와 함께 3을 침 -> 숫자 "123", 값 "00:00:01:23"
    """
    type_digits(edit, "1a:b.2-")
    QTest.keyClick(edit, Qt.Key.Key_3, Qt.KeyboardModifier.KeypadModifier)

    assert edit.digits() == "123"
    assert edit.text() == "00:00:01:23"


def test_out_of_range_digits_are_kept_as_typed(edit):
    """칸은 초 75 같은 값을 올림하거나 고치지 않고 친 그대로 내놓아야 한다.

    7500을 침 -> 값 "00:00:75:00"
    """
    type_digits(edit, "7500")

    assert edit.text() == "00:00:75:00"


def test_signals_tell_edits_from_leaving_the_field(edit):
    """숫자가 바뀌면 edited가, 칸을 떠나거나 Enter를 치면 committed가 나와야 한다.

    12를 침 -> edited 2번(마지막 값 "00:00:00:12"), committed 0번
    Enter -> committed 1번. 포커스가 나감 -> committed 2번
    """
    edited, committed = QSignalSpy(edit.edited), QSignalSpy(edit.committed)

    type_digits(edit, "12")
    assert edited.count() == 2 and edited.at(1)[0] == "00:00:00:12"
    assert committed.count() == 0

    QTest.keyClick(edit, Qt.Key.Key_Return)
    assert committed.count() == 1
    QApplication.sendEvent(edit, _focus_out())
    assert committed.count() == 2


@pytest.mark.parametrize(
    "pasted, value, bright",
    [
        ("1:02:03:04", "01:02:03:04", "1:02:03:04"),  # 네 칸 — 그대로
        ("03:04", "00:00:03:04", "03:04"),  # 두 칸 — 오른쪽부터(초 · 프레임)
        ("5:03:04", "00:05:03:04", "5:03:04"),  # 세 칸
        ("001003", "00:00:10:03", "00:10:03"),  # 숫자만 — 치는 것과 같다
        ("  12:34:56:78\n", "12:34:56:78", "12:34:56:78"),  # 앞뒤 공백
    ],
)
def test_paste_aligns_fields_from_the_right(edit, clipboard, pasted, value, bright):
    """붙여넣은 글은 칸을 오른쪽부터 맞춰야 한다.

    위 표의 글을 클립보드에 두고 Ctrl+V -> 값과 밝은 부분이 표와 같다
    """
    clipboard.content = pasted

    QTest.keyClick(edit, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)

    assert (edit.text(), edit.brightText()) == (value, bright)


@pytest.mark.parametrize(
    "pasted",
    ["12:34:56.789", "abc", "1:2:3:4:5", "123:04", "1::2", "123456789", "", "12 34"],
)
def test_paste_of_anything_else_changes_nothing(edit, clipboard, pasted):
    """숫자 · 콜론 밖의 글자가 있거나 칸에 맞지 않는 글은 붙여넣지 않아야 한다.

    값 "00:00:10:03"인 칸에 위 글(밀리초 · 글자 · 다섯 칸 · 세 자리 칸 · 빈 칸 · 아홉 자리 · 빈 글 · 공백)을 붙여넣음
    -> 값 그대로, edited 0번
    """
    type_digits(edit, "001003")
    edited = QSignalSpy(edit.edited)
    clipboard.content = pasted

    QTest.keyClick(edit, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)

    assert edit.text() == "00:00:10:03" and edited.count() == 0
    assert digits_from_text(pasted) is None


def test_the_value_is_always_four_fields(edit):
    """칸이 내놓는 값은 숫자를 몇 개 쳤든 네 칸 HH:MM:SS:FF여야 한다.

    숫자를 0개부터 8개까지 하나씩 침 -> 매번 값이 "dd:dd:dd:dd" 모양(길이 11, 콜론 셋)
    """
    values = [edit.text()]
    for digit in "12345678":
        type_digits(edit, digit)
        values.append(edit.text())

    for value in values:
        fields = value.split(":")
        assert len(value) == 11 and len(fields) == 4
        assert all(len(field) == 2 and field.isascii() and field.isdigit() for field in fields)
    assert values[-1] == "12:34:56:78"


def test_copy_puts_the_whole_four_field_value_on_the_clipboard(edit, clipboard):
    """복사는 흐린 자리까지 포함한 네 칸 값 전체를 클립보드에 넣어야 한다.

    0010을 친 칸(밝은 부분 "00:10")에서 Ctrl+C -> 클립보드 "00:00:00:10"
    """
    type_digits(edit, "0010")

    QTest.keyClick(edit, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)

    assert clipboard.content == "00:00:00:10"


def test_select_all_selects_the_whole_field_and_a_digit_then_starts_over(edit):
    """Ctrl+A는 칸 전체를 선택하고, 선택된 칸에 숫자를 치면 새로 시작하며 선택이 풀려야 한다.

    001003을 친 칸에서 Ctrl+A -> 선택됨. 5를 침 -> 값 "00:00:00:05", 선택 풀림
    """
    type_digits(edit, "001003")
    assert not edit.isAllSelected()

    QTest.keyClick(edit, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    assert edit.isAllSelected()
    type_digits(edit, "5")

    assert edit.text() == "00:00:00:05"
    assert not edit.isAllSelected()


def test_backspace_on_a_selected_field_still_removes_one_digit(edit):
    """선택된 칸에서도 Backspace는 맨 오른쪽 숫자 하나를, Delete는 전부를 지워야 한다.

    001003을 치고 Ctrl+A → Backspace -> "00:00:01:00". 다시 Ctrl+A → Delete -> "00:00:00:00"
    """
    type_digits(edit, "001003")
    edit.selectAll()

    QTest.keyClick(edit, Qt.Key.Key_Backspace)
    assert edit.text() == "00:00:01:00" and not edit.isAllSelected()
    edit.selectAll()
    QTest.keyClick(edit, Qt.Key.Key_Delete)
    assert edit.text() == "00:00:00:00"


def test_double_click_and_drag_select_the_whole_field_and_a_click_clears_it(edit):
    """더블클릭과 드래그는 칸 전체를 선택하고, 한 번 누르면 선택이 풀려야 한다.

    칸을 더블클릭 -> 선택됨. 한 번 누름 -> 풀림. 누른 채 끌어 옮김 -> 선택됨
    """
    center = edit.rect().center()

    QTest.mouseDClick(edit, Qt.MouseButton.LeftButton, pos=center)
    assert edit.isAllSelected()
    QTest.mouseClick(edit, Qt.MouseButton.LeftButton, pos=center)
    assert not edit.isAllSelected()

    far = QPoint(center.x() + QApplication.startDragDistance() * 3, center.y())
    QTest.mousePress(edit, Qt.MouseButton.LeftButton, pos=center)
    QApplication.sendEvent(edit, _drag_to(far))
    QTest.mouseRelease(edit, Qt.MouseButton.LeftButton, pos=far)
    assert edit.isAllSelected()


def test_a_selected_field_is_painted_differently(edit):
    """선택된 칸은 선택되지 않은 칸과 다르게 그려져야 한다.

    001003을 친 칸의 그림을 선택 전 · 후로 견줌 -> 다르다
    """
    type_digits(edit, "001003")
    plain = edit.grab().toImage()

    edit.selectAll()
    QApplication.processEvents()

    assert edit.grab().toImage() != plain


def test_context_menu_has_only_copy_and_paste(edit, clipboard):
    """우클릭 메뉴에는 복사 · 붙여넣기 두 항목만 있어야 하고, 붙여넣기는 Ctrl+V와 같은 규칙이어야 한다.

    클립보드 "03:04"인 채 메뉴를 만듦 -> 항목 == ["Copy", "Paste"], 붙여넣기 켜짐
    붙여넣기를 누름 -> 값 "00:00:03:04". 복사를 누름 -> 클립보드 "00:00:03:04"
    클립보드 "abc"인 채 메뉴를 만듦 -> 붙여넣기 꺼짐
    """
    clipboard.content = "03:04"
    menu = edit.buildContextMenu()
    copy, paste = menu.actions()

    assert [action.text() for action in menu.actions()] == ["Copy", "Paste"]
    assert paste.isEnabled()
    paste.trigger()
    assert edit.text() == "00:00:03:04"
    copy.trigger()
    assert clipboard.content == "00:00:03:04"

    clipboard.content = "abc"
    assert not edit.buildContextMenu().actions()[1].isEnabled()


def _drag_to(point: QPoint) -> QMouseEvent:
    """왼쪽 버튼을 누른 채 point로 옮기는 이벤트."""
    return QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(point),
        QPointF(point),  # 전역 위치 — 칸은 보지 않는다
        Qt.MouseButton.NoButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )


def _seconds(value: str, fps: Fraction) -> Fraction:
    """네 칸 값의 명목 시각 — 시 × 3600 + 분 × 60 + 초 + 프레임 ÷ 프레임률. 범위를 따지지 않는다."""
    hours, minutes, seconds, frames = (int(field) for field in value.split(":"))
    return hours * 3600 + minutes * 60 + seconds + Fraction(frames) / fps


@pytest.mark.parametrize("fps", [Fraction(2997, 100), Fraction(30), Fraction(60)])
def test_typing_one_more_digit_never_makes_the_value_smaller(edit, fps):
    """숫자를 하나 더 치면 값(명목 시각)이 줄지 않아야 한다 — 치는 도중의 "너무 크다"는 더 쳐도 풀리지 않는다.

    씨앗을 고정한 무작위 여덟 자리 숫자열 200개를 한 글자씩 침(29.97 · 30 · 60fps)
    -> 글자를 칠 때마다 값이 직전 값 이상이다
    """
    generator = random.Random(309)
    for _ in range(200):
        QTest.keyClick(edit, Qt.Key.Key_Delete)
        previous = Fraction(0)
        for digit in (str(generator.randrange(10)) for _ in range(8)):
            type_digits(edit, digit)
            value = _seconds(edit.text(), fps)
            assert value >= previous, f"{edit.digits()!r}: {previous} → {value}"
            previous = value


def _focus_in() -> QFocusEvent:
    """포커스가 칸에 들어오는 이벤트 — offscreen에서는 창이 활성이 아닐 수 있어 직접 보낸다."""
    return QFocusEvent(QEvent.Type.FocusIn, Qt.FocusReason.TabFocusReason)


def _focus_out() -> QFocusEvent:
    """포커스가 칸에서 나가는 이벤트."""
    return QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.TabFocusReason)


# ================================================================ 시분초 칸 · 프레임 칸 · 시각 하나 (#309)


@pytest.fixture
def clock(qtbot):
    """보이는 시분초 칸 하나(묶음 셋 — 숫자 여섯 자리)."""
    widget = TimecodeEdit(fields=CLOCK_FIELDS)
    qtbot.addWidget(widget)
    widget.show()
    QTest.qWaitForWindowExposed(widget)
    return widget


@pytest.fixture
def point(qtbot):
    """보이는 시각 입력 하나 — 시분초 칸과 프레임 칸의 묶음."""
    widget = TimePointEdit()
    qtbot.addWidget(widget)
    widget.show()
    QTest.qWaitForWindowExposed(widget)
    return widget


@pytest.mark.parametrize(
    ("digits", "value", "bright"),
    [
        ("0100", "00:01:00", "01:00"),  # 1분
        ("012345", "01:23:45", "01:23:45"),
        ("5", "00:00:05", "5"),
        ("130", "00:01:30", "1:30"),
    ],
)
def test_clock_field_fills_from_the_seconds(clock, digits, value, bright):
    """시분초 칸에 친 숫자는 초 자리부터 채워져야 한다 — 숫자의 기본 단위가 초다.

    0100 -> 00:01:00(1분) / 012345 -> 01:23:45 / 5 -> 00:00:05 / 130 -> 00:01:30
    """
    type_digits(clock, digits)

    assert (clock.text(), clock.brightText()) == (value, bright)


def test_clock_field_shows_the_six_steps_with_seconds_as_the_base(clock):
    """오너의 여섯 단계는 보이는 모양이 그대로이고 값은 초 기준이어야 한다.

    0 · 0 · 1 · 0 · 0 · 3을 차례로 침
    -> 밝은 부분: "0" → "00" → "0:01" → "00:10" → "0:01:00" → "00:10:03"
    -> 값: 00:00:00 → 00:00:00 → 00:00:01 → 00:00:10 → 00:01:00 → 00:10:03
    """
    seen = []
    for digit in "001003":
        type_digits(clock, digit)
        seen.append((clock.brightText(), clock.text()))

    assert seen == [
        ("0", "00:00:00"),
        ("00", "00:00:00"),
        ("0:01", "00:00:01"),
        ("00:10", "00:00:10"),
        ("0:01:00", "00:01:00"),
        ("00:10:03", "00:10:03"),
    ]


def test_clock_field_ignores_the_seventh_digit(clock):
    """시분초 칸은 일곱째 숫자를 받지 않아야 한다.

    123456을 치고 7을 더 침 -> 값 "12:34:56" 그대로
    """
    type_digits(clock, "1234567")

    assert clock.text() == "12:34:56"
    assert clock.digits() == "123456"


@pytest.mark.parametrize("seed", range(20))
def test_typing_one_more_digit_into_the_clock_field_never_makes_it_smaller(clock, seed):
    """시분초 칸에 숫자를 하나 더 치면 값(시 · 분 · 초를 이어 읽은 수)이 줄지 않아야 한다.

    씨앗마다 무작위 숫자 여섯 개를 차례로 침 -> 단계마다 int(값의 숫자)가 앞 단계 이상
    """
    generator = random.Random(seed)
    previous = 0
    for _ in range(6):
        type_digits(clock, str(generator.randrange(10)))
        current = int(clock.text().replace(":", ""))
        assert current >= previous, clock.text()
        previous = current


def test_frame_field_takes_two_digits_and_ignores_the_third(point):
    """프레임 칸은 오른쪽부터 두 자리를 받고 세 자리째를 받지 않아야 한다. 치지 않으면 00이다.

    아무것도 안 침 -> "00" / 3 -> "03" / 30 -> "30" / 5를 더 침 -> "30" 그대로
    """
    frame = point.frameEdit
    seen = [frame.text()]
    for digit in "305":
        type_digits(frame, digit)
        seen.append(frame.text())

    assert seen == ["00", "03", "30", "30"]
    assert (frame.dimText(), frame.brightText()) == ("", "30")


def test_time_point_joins_the_two_fields_into_four_fields(point):
    """시각의 값은 시분초 칸과 프레임 칸을 합친 네 칸 HH:MM:SS:FF여야 한다.

    시분초 칸에 0100, 프레임 칸에 30 -> "00:01:00:30". 아무것도 치지 않은 시각은 "00:00:00:00"
    """
    assert point.text() == "00:00:00:00"

    type_digits(point.clockEdit, "0100")
    type_digits(point.frameEdit, "30")

    assert point.text() == "00:01:00:30"


def test_set_text_splits_a_timecode_into_both_fields_and_shows_it_bright(point):
    """setText는 네 칸 타임코드를 두 칸에 나눠 넣고 전부 밝게 보여야 한다. 읽을 수 없으면 비운다.

    "01:02:03:04" -> 시분초 "01:02:03" · 프레임 "04", 흐린 부분 없음
    "abc" -> 두 칸 모두 비어 값 "00:00:00:00", 밝은 부분 없음
    """
    point.setText("01:02:03:04")
    assert (point.clockEdit.text(), point.frameEdit.text()) == ("01:02:03", "04")
    assert (point.clockEdit.dimText(), point.frameEdit.dimText()) == ("", "")

    point.setText("abc")
    assert point.text() == "00:00:00:00"
    assert (point.clockEdit.brightText(), point.frameEdit.brightText()) == ("", "")


@pytest.mark.parametrize(
    "modifier", [Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.KeypadModifier]
)
def test_period_in_the_clock_field_moves_to_the_frame_field(point, modifier):
    """시분초 칸에서 "."을 누르면 같은 시각의 프레임 칸으로 가야 하고 칸에는 들어가지 않아야 한다.

    시분초 칸에 12를 치고 "."(자판 · 숫자 키패드) -> 포커스가 프레임 칸, 시분초 값 "00:00:12" 그대로
    """
    point.clockEdit.setFocus()
    type_digits(point.clockEdit, "12")

    QTest.keyClick(point.clockEdit, Qt.Key.Key_Period, modifier)
    QApplication.processEvents()

    assert point.frameEdit.hasFocus()
    assert point.clockEdit.text() == "00:00:12"


@pytest.mark.parametrize("part", ["clockEdit", "frameEdit"])
def test_copy_from_either_field_puts_the_whole_time_point_on_the_clipboard(point, clipboard, part):
    """어느 칸에서 복사하든 그 시각 전체(HH:MM:SS:FF)가 클립보드에 들어가야 한다.

    시분초 01:02:03 · 프레임 04인 시각의 시분초 칸 / 프레임 칸에서 Ctrl+C -> "01:02:03:04"
    """
    point.setText("01:02:03:04")

    QTest.keyClick(getattr(point, part), Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)

    assert clipboard.content == "01:02:03:04"


@pytest.mark.parametrize(
    ("pasted", "part", "expected"),
    [
        ("1:02:03:04", "clockEdit", "01:02:03:04"),  # 네 묶음 — 두 칸 모두
        ("1:02:03:04", "frameEdit", "01:02:03:04"),
        ("1:02:03", "clockEdit", "01:02:03:00"),  # 세 묶음 — 시분초 + 프레임 00
        ("1:02:03", "frameEdit", "01:02:03:00"),
        ("12:34", "clockEdit", "00:12:34:00"),  # 두 묶음 — 분:초 + 프레임 00
        ("0130", "clockEdit", "00:01:30:15"),  # 숫자만 — 붙여넣은 칸의 규칙(시분초 칸)
        ("30", "frameEdit", "00:00:07:30"),  # 숫자만 — 붙여넣은 칸의 규칙(프레임 칸)
    ],
)
def test_paste_follows_the_shape_of_the_text(point, clipboard, pasted, part, expected):
    """붙여넣기는 글의 모양에 따라 두 칸에 나눠 넣거나 붙여넣은 칸에만 넣어야 한다.

    시분초 00:00:07 · 프레임 15인 시각에 위 표의 글을 그 칸에서 Ctrl+V -> 기대한 시각
    """
    point.setText("00:00:07:15")
    clipboard.content = pasted

    QTest.keyClick(getattr(point, part), Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)

    assert point.text() == expected


@pytest.mark.parametrize(
    ("pasted", "part"),
    [
        ("1:02:03:04:05", "clockEdit"),  # 다섯 묶음
        ("ab:cd", "clockEdit"),  # 숫자 · 콜론 밖의 글자
        ("12a", "clockEdit"),
        ("1::2", "clockEdit"),  # 빈 묶음
        ("123:45", "clockEdit"),  # 세 자리 묶음
        ("1234567", "clockEdit"),  # 시분초 칸에 일곱 자리
        ("123", "frameEdit"),  # 프레임 칸에 세 자리
        ("", "clockEdit"),
    ],
)
def test_paste_of_a_text_that_does_not_fit_changes_nothing(point, clipboard, pasted, part):
    """받을 수 없는 글을 붙여넣으면 시각이 그대로여야 하고 edited도 나오지 않아야 한다.

    시분초 00:00:07 · 프레임 15인 시각에 위 표의 글을 붙여넣음 -> "00:00:07:15" 그대로, edited 0회
    """
    point.setText("00:00:07:15")
    clipboard.content = pasted
    edited = QSignalSpy(point.edited)

    QTest.keyClick(getattr(point, part), Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)

    assert point.text() == "00:00:07:15"
    assert edited.count() == 0


def test_moving_between_the_two_fields_is_not_leaving_the_time_point(point, qtbot):
    """시분초 칸에서 프레임 칸으로 가는 것은 그 시각을 떠난 것이 아니어야 하고, 둘 다 떠나면 committed가 한 번 나와야 한다.

    시분초 칸에 포커스 → "."으로 프레임 칸 -> committed 0회
    다른 위젯으로 포커스를 옮김 -> committed 1회
    """
    other = TimecodeEdit()
    qtbot.addWidget(other)
    other.show()
    QTest.qWaitForWindowExposed(other)
    point.activateWindow()
    point.clockEdit.setFocus()
    QApplication.processEvents()
    committed = QSignalSpy(point.committed)

    QTest.keyClick(point.clockEdit, Qt.Key.Key_Period)
    QApplication.processEvents()
    assert point.frameEdit.hasFocus() and committed.count() == 0

    point.frameEdit.clearFocus()
    QApplication.processEvents()

    assert committed.count() == 1


@pytest.mark.parametrize("part", ["clockEdit", "frameEdit"])
def test_enter_in_either_field_commits_and_reports_entered_once(point, part):
    """어느 칸에서 Enter를 치든 그 시각의 committed와 entered가 한 번씩, 그 순서로 나와야 한다."""
    order = []
    point.committed.connect(lambda: order.append("committed"))
    point.entered.connect(lambda: order.append("entered"))

    QTest.keyClick(getattr(point, part), Qt.Key.Key_Return)

    assert order == ["committed", "entered"]


def test_time_point_tells_when_the_frame_field_is_being_typed_and_full(point):
    """프레임 칸에 두 자리를 다 쳤는지, 지금 치는 칸이 프레임 칸인지를 알려야 한다.

    시분초 칸에 5 -> 프레임 칸을 치는 중 아님. 프레임 칸에 6 -> 치는 중 · 다 차지 않음. 0 -> 다 참
    """
    type_digits(point.clockEdit, "5")
    assert (point.typingFrame(), point.frameIsFull()) == (False, False)

    type_digits(point.frameEdit, "6")
    assert (point.typingFrame(), point.frameIsFull()) == (True, False)

    type_digits(point.frameEdit, "0")
    assert (point.typingFrame(), point.frameIsFull()) == (True, True)
