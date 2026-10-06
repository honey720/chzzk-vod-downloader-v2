"""타임코드 입력 칸 — 숫자만 쳐서 ``HH:MM:SS:FF``를 넣는다 (#309).

칸에는 ``00:00:00:00``이 흐리게 깔려 있고, 친 숫자는 맨 오른쪽(프레임 자리)부터 채워지며
앞서 친 숫자를 왼쪽으로 민다. 콜론은 치지 않는다 — 자리가 정해져 있다. 친 부분만 밝게 보인다.

    친 숫자     밝게 보이는 부분     값
    0           0                    00:00:00:00
    001         0:01                 00:00:00:01
    001003      00:10:03             00:00:10:03

이 칸이 내놓는 값(``text()``)은 **언제나 네 칸**이다. 그 값이 타임코드로 맞는지(분 · 초가 60
미만인지, 프레임이 프레임률 미만인지)는 이 칸이 판정하지 않는다 — 올림하거나 고치지 않고
친 그대로 내놓고, 해석과 검증은 받는 쪽(``core/utils/timecode.py``)이 한다.

QLineEdit을 잇는다 — 테두리 · 포커스 강조 · 오류 강조(``invalid`` 속성) · 툴팁 · 탭 순서를 전역
QSS와 Qt가 그대로 준다. 글자만 이 클래스가 그린다(흐린 부분과 밝은 부분의 색이 달라 QLineEdit의
한 가지 글자색으로는 그릴 수 없다). QLineEdit 자신의 글자는 늘 비워 두고 읽기 전용으로 둔다 —
자체 커서 · 선택 · 붙여넣기 · 입력기 조합이 끼어들지 않는다. 키는 이 클래스가 받는다.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QKeySequence, QPainter
from PySide6.QtWidgets import QLineEdit, QStyle, QStyleOptionFrame, QWidget

import app.theme as theme

MAX_DIGITS = 8  # HHMMSSFF — 넘는 숫자는 받지 않는다
_DIGITS = "0123456789"  # 받는 글자 — 아스키 숫자만(str.isdigit은 다른 문자 체계의 숫자도 참이다)
_FIELD_DIGITS = 2  # 한 칸의 자릿수
_FIELDS = MAX_DIGITS // _FIELD_DIGITS  # 칸 수 — 시 · 분 · 초 · 프레임


def digits_from_text(text: str) -> str | None:
    """붙여넣은 글을 친 숫자로 바꾼다. 받을 수 없는 글이면 None.

    - 숫자만 있으면 그대로다 — 치는 것과 같다(오른쪽부터 프레임 · 초 · 분 · 시)
    - 콜론이 있으면 칸을 **오른쪽부터** 맞춘다. ``03:04``는 초 03 · 프레임 04이고
      ``1:02:03:04``는 그대로다. 맨 앞 칸만 한 자리일 수 있고 나머지는 두 자리로 채운다
    - 숫자 · 콜론 밖의 글자가 있거나, 칸이 넷을 넘거나, 한 칸이 두 자리를 넘거나, 빈 칸이
      있거나, 전체가 여덟 자리를 넘으면 받지 않는다
    """
    text = text.strip()
    if not text or any(ch not in _DIGITS + ":" for ch in text):
        return None
    if ":" not in text:
        return text if len(text) <= MAX_DIGITS else None
    fields = text.split(":")
    if len(fields) > _FIELDS or any(not 1 <= len(f) <= _FIELD_DIGITS for f in fields):
        return None
    return fields[0] + "".join(field.zfill(_FIELD_DIGITS) for field in fields[1:])


class TimecodeEdit(QLineEdit):
    """숫자만 받아 오른쪽부터 채우는 타임코드 입력 칸.

    - 숫자: 맨 오른쪽에 붙는다. 여덟 자리가 차면 받지 않는다. 값이 있는 칸에 들어와 처음 친
      숫자는 기존 값을 지우고 새로 시작한다
    - Backspace: 맨 오른쪽 숫자를 지운다 · Delete: 전부 지운다
    - 붙여넣기(Ctrl+V): ``digits_from_text``가 받는 글만
    - 칸을 떠나거나 Enter를 치면 ``committed``를 낸다. Enter는 이어서 ``entered``를 낸다 —
      받는 쪽이 다음 칸으로 넘긴다. Enter는 창으로 올라가지 않는다(창을 닫지 않는다)
    """

    edited = Signal(str)  # 친 숫자가 바뀌었다 — 네 칸 표기(text())를 싣는다
    committed = Signal()  # 편집을 끝냈다 — 칸을 떠났거나 Enter를 쳤다
    entered = Signal()  # Enter를 쳤다 — committed 뒤에 나온다

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._digits = ""  # 친 숫자 — 오른쪽 끝이 프레임의 일의 자리다
        # 포커스를 받은 뒤 아직 아무것도 치지 않았다 — 다음 숫자가 기존 값을 지운다
        self._fresh = False
        self.setProperty("role", "timecode")
        self.setReadOnly(True)  # QLineEdit 자신의 커서 · 선택 · 입력기 조합을 끈다
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, False)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.setDragEnabled(False)
        self.setAcceptDrops(False)
        self.setCursor(Qt.CursorShape.IBeamCursor)

    # ---- 값 ----

    def digits(self) -> str:
        """친 숫자 — 아직 치지 않은 자리는 들어 있지 않다."""
        return self._digits

    def text(self) -> str:
        """칸의 값 — 언제나 네 칸 ``HH:MM:SS:FF``. 치지 않은 자리는 0이다."""
        padded = self._digits.zfill(MAX_DIGITS)
        return ":".join(
            padded[at : at + _FIELD_DIGITS] for at in range(0, MAX_DIGITS, _FIELD_DIGITS)
        )

    def setText(self, text: str) -> None:
        """값을 넣는다 — 넣은 값은 전부 밝게 보인다(친 것과 같다).

        네 칸 타임코드(``HH:MM:SS:FF``)를 받는다. 그 밖의 글은 ``digits_from_text``의 규칙으로
        읽고, 읽을 수 없으면 칸을 비운다.
        """
        digits = digits_from_text(text)
        fields = text.strip().split(":")
        if digits is not None and len(fields) == _FIELDS:
            digits = "".join(field.zfill(_FIELD_DIGITS) for field in fields)  # 전부 밝게
        self._digits = (digits or "")[-MAX_DIGITS:]
        self._fresh = self.hasFocus()
        self.update()

    def brightText(self) -> str:
        """밝게 보이는 부분 — 친 숫자와 그 사이의 콜론."""
        count = len(self._digits)
        if not count:
            return ""
        return self.text()[-(count + (count - 1) // _FIELD_DIGITS) :]

    def dimText(self) -> str:
        """흐리게 보이는 부분 — 아직 치지 않은 자리."""
        shown = self.text()
        return shown[: len(shown) - len(self.brightText())]

    def pasteText(self, text: str) -> bool:
        """글을 붙여넣는다. 받을 수 없는 글이면 아무것도 바꾸지 않고 False."""
        digits = digits_from_text(text)
        if digits is None:
            return False
        self._setDigits(digits)
        return True

    def commit(self) -> None:
        """편집을 끝낸다 — ``committed``를 낸다."""
        self._fresh = False
        self.committed.emit()

    def _setDigits(self, digits: str) -> None:
        self._fresh = False
        if digits == self._digits:
            return
        self._digits = digits
        self.update()
        self.edited.emit(self.text())

    # ---- 입력 ----

    def keyPressEvent(self, event) -> None:
        if event.matches(QKeySequence.StandardKey.Paste):
            self.pasteText(QGuiApplication.clipboard().text())
            return
        key = event.key()
        if key == Qt.Key.Key_Backspace:
            self._setDigits(self._digits[:-1])
            return
        if key == Qt.Key.Key_Delete:
            self._setDigits("")
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.commit()
            self.entered.emit()
            event.accept()  # 창으로 올리지 않는다 — Enter가 창을 닫지 않는다
            return
        typed = event.text()
        blocked = (
            Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.AltModifier
            | Qt.KeyboardModifier.MetaModifier
        )
        # 숫자 키패드의 숫자도 글자로 온다(KeypadModifier는 막지 않는다)
        if len(typed) == 1 and typed in _DIGITS and not event.modifiers() & blocked:
            current = "" if self._fresh else self._digits
            if len(current) < MAX_DIGITS:
                self._setDigits(current + typed)
            else:
                self._fresh = False
            return
        event.ignore()  # 그 밖의 키(Esc 등)는 창이 받는다 — 글자는 칸에 들어가지 않는다

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self._fresh = True
        self.update()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.commit()
        self.update()

    def mousePressEvent(self, event) -> None:
        self.setFocus(Qt.FocusReason.MouseFocusReason)  # 글자를 선택하지 않는다

    def mouseMoveEvent(self, event) -> None:
        event.ignore()

    def mouseDoubleClickEvent(self, event) -> None:
        event.ignore()

    # ---- 그리기 ----

    def paintEvent(self, event) -> None:
        super().paintEvent(event)  # 바탕 · 테두리(포커스 · 오류 강조 포함) — 글자는 비어 있다
        option = QStyleOptionFrame()
        self.initStyleOption(option)
        rect = self.style().subElementRect(QStyle.SubElement.SE_LineEditContents, option, self)
        tokens = theme.current_tokens()
        dim, bright = self.dimText(), self.brightText()
        metrics = self.fontMetrics()
        dim_width = metrics.horizontalAdvance(dim)
        left = rect.center().x() - (dim_width + metrics.horizontalAdvance(bright)) // 2
        baseline = rect.center().y() + (metrics.ascent() - metrics.descent()) // 2 + 1
        painter = QPainter(self)
        painter.setFont(self.font())
        # 흐린 자리는 비활성 글자색, 친 자리는 본문 글자색 — 둘 다 theme.py의 토큰이다
        painter.setPen(QColor(tokens["textDisabled"]))
        painter.drawText(left, baseline, dim)
        painter.setPen(QColor(tokens["text" if self.isEnabled() else "textDisabled"]))
        painter.drawText(left + dim_width, baseline, bright)
        if self.hasFocus():
            # 커서는 늘 오른쪽 끝이다 — 숫자가 거기에 붙는다
            caret = left + dim_width + metrics.horizontalAdvance(bright) + 1
            painter.drawLine(
                caret, baseline - metrics.ascent(), caret, baseline + metrics.descent()
            )
        painter.end()
