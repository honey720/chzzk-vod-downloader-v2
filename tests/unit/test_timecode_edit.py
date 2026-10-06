"""타임코드 입력 칸 — 숫자만 쳐서 오른쪽부터 채운다 (#309).

`app.widgets.timecode_edit.TimecodeEdit`에 실제 키 이벤트를 보내, 밝게 보이는 부분과 칸이
내놓는 값을 잰다. 기대값은 손으로 적은 글자다.
"""

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QFocusEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

import app.widgets.timecode_edit as timecode_edit
from app.widgets.timecode_edit import TimecodeEdit, digits_from_text


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


def _focus_in() -> QFocusEvent:
    """포커스가 칸에 들어오는 이벤트 — offscreen에서는 창이 활성이 아닐 수 있어 직접 보낸다."""
    return QFocusEvent(QEvent.Type.FocusIn, Qt.FocusReason.TabFocusReason)


def _focus_out() -> QFocusEvent:
    """포커스가 칸에서 나가는 이벤트."""
    return QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.TabFocusReason)
