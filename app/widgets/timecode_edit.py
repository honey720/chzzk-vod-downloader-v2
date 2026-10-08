"""타임코드 입력 칸 — 숫자만 쳐서 시각 하나(``HH:MM:SS:FF``)를 넣는다 (#309).

시각 하나는 칸 둘이다(``TimePointEdit``) — **시분초 칸**(``HH:MM:SS``, 숫자 여섯 자리)과
**프레임 칸**(``FF``, 두 자리). 숫자를 치는 기본 단위가 초다: 시분초 칸에 친 숫자는 맨 오른쪽
(초 자리)부터 채워지며 앞서 친 숫자를 왼쪽으로 민다. 콜론은 치지 않는다 — 자리가 정해져 있다.
칸에는 ``00:00:00`` · ``00``이 흐리게 깔려 있고 친 부분만 밝게 보인다. 숫자를 하나라도 친 칸은
떠나거나 Enter를 치면 앞쪽의 0까지 전부 밝아진다(확정) — 표시만 바뀌고 값은 그대로다.

두 칸 모두 숫자를 하나도 치지 않은 시각은 **빈 시각**이다 — 값(``TimePointEdit.text()``)이 빈
글이고, 그것이 무엇을 뜻하는지는 받는 쪽이 정한다(구간의 시작이면 영상 맨 처음, 끝이면 맨 끝).
빈 시각의 칸에는 그 뜻하는 값이 흐리게 깔린다(``setEmptyText``).

    시분초 칸에 친 숫자   밝게 보이는 부분   값
    0                     0                  00:00:00
    0100                  01:00              00:01:00   (1분)
    001003                00:10:03           00:10:03
    012345                01:23:45           01:23:45

두 칸을 합친 값(``TimePointEdit.text()``)은 빈 시각이 아니면 **언제나 네 칸** ``HH:MM:SS:FF``다. 그 값이
타임코드로 맞는지(분 · 초가 60 미만인지, 프레임이 프레임률 미만인지)는 이 칸들이 판정하지
않는다 — 올림하거나 고치지 않고 친 그대로 내놓고, 해석과 검증은 받는 쪽
(``core/utils/timecode.py``)이 한다.

칸 하나(``TimecodeEdit``)는 두 자리 묶음 몇 개를 오른쪽부터 채우는 숫자 칸이다 — 묶음 수가
3이면 시분초 칸, 1이면 프레임 칸이다.

QLineEdit을 잇는다 — 테두리 · 포커스 강조 · 오류 강조(``invalid`` 속성) · 툴팁 · 탭 순서를 전역
QSS와 Qt가 그대로 준다. 글자만 이 클래스가 그린다(흐린 부분과 밝은 부분의 색이 달라 QLineEdit의
한 가지 글자색으로는 그릴 수 없다). QLineEdit 자신의 글자는 늘 비워 두고 읽기 전용으로 둔다 —
자체 커서 · 선택 · 붙여넣기 · 입력기 조합이 끼어들지 않는다. 키는 이 클래스가 받는다.

선택은 **칸 전체**뿐이다(드래그 · 더블클릭 · Ctrl+A). 부분 선택은 없다 — 숫자가 오른쪽부터
채워져 가운데를 골라 고칠 일이 없다. 복사(Ctrl+C)는 선택과 무관하게 네 칸 값 전체를 넣는다.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QColor, QGuiApplication, QKeySequence, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLineEdit,
    QMenu,
    QStyle,
    QStyleOptionFrame,
    QWidget,
)

from shiboken6 import isValid

import app.theme as theme

MAX_DIGITS = 8  # HHMMSSFF — 묶음 넷짜리 칸이 받는 숫자의 수
_DIGITS = "0123456789"  # 받는 글자 — 아스키 숫자만(str.isdigit은 다른 문자 체계의 숫자도 참이다)
_FIELD_DIGITS = 2  # 한 묶음의 자릿수
_FIELDS = MAX_DIGITS // _FIELD_DIGITS  # 시각 하나의 묶음 수 — 시 · 분 · 초 · 프레임
CLOCK_FIELDS = 3  # 시분초 칸의 묶음 수 — 시 · 분 · 초(숫자 여섯 자리)
FRAME_FIELDS = 1  # 프레임 칸의 묶음 수(숫자 두 자리)
_PART_SPACING = 3  # 시분초 칸과 프레임 칸 사이(px) — 한 시각으로 읽히게 붙여 둔다
_EMPTY_TIME_POINT = "00:00:00:00"  # 빈 시각의 칸에 깔리는 기본 글 — 받는 쪽이 바꾼다(setEmptyText)
_DOT = "."  # 시분초 칸에서 프레임 칸으로 넘어가는 키 — 숫자 키패드의 소수점도 이 글자로 온다


def digits_from_text(text: str, fields: int = _FIELDS) -> str | None:
    """붙여넣은 글을 친 숫자로 바꾼다. 받을 수 없는 글이면 None.

    - 숫자만 있으면 그대로다 — 치는 것과 같다(오른쪽부터 채운다)
    - 콜론이 있으면 묶음을 **오른쪽부터** 맞춘다. 맨 앞 묶음만 한 자리일 수 있고 나머지는
      두 자리로 채운다
    - 숫자 · 콜론 밖의 글자가 있거나, 묶음이 fields개를 넘거나, 한 묶음이 두 자리를 넘거나,
      빈 묶음이 있거나, 전체가 그 칸의 자릿수를 넘으면 받지 않는다

    Args:
        fields: 칸의 묶음 수 — 기본은 시각 하나(시 · 분 · 초 · 프레임)의 4
    """
    text = text.strip()
    if not text or any(ch not in _DIGITS + ":" for ch in text):
        return None
    if ":" not in text:
        return text if len(text) <= fields * _FIELD_DIGITS else None
    parts = text.split(":")
    if len(parts) > fields or any(not 1 <= len(part) <= _FIELD_DIGITS for part in parts):
        return None
    return parts[0] + "".join(part.zfill(_FIELD_DIGITS) for part in parts[1:])


def split_time_point(text: str) -> tuple[str, str] | None:
    """붙여넣은 콜론 있는 글을 (시분초 칸의 글, 프레임 칸의 글)로 나눈다. 받을 수 없으면 None.

    - 네 묶음 ``H:MM:SS:FF`` → 시분초 + 프레임
    - 세 묶음 ``H:MM:SS`` → 시분초 + 프레임 00
    - 두 묶음 ``MM:SS`` → 분:초 + 프레임 00
    숫자 · 콜론 밖의 글자가 있거나 묶음의 모양이 틀리면(빈 묶음 · 세 자리 묶음 · 다섯 묶음
    이상) 받지 않는다. 콜론이 없는 글은 이 함수의 몫이 아니다 — 붙여넣은 칸의 규칙으로 읽는다.
    """
    text = text.strip()
    if ":" not in text or any(ch not in _DIGITS + ":" for ch in text):
        return None
    parts = text.split(":")
    if not 2 <= len(parts) <= _FIELDS or any(not 1 <= len(p) <= _FIELD_DIGITS for p in parts):
        return None
    if len(parts) == _FIELDS:
        return ":".join(parts[:CLOCK_FIELDS]), parts[CLOCK_FIELDS]
    return ":".join(parts), "0" * _FIELD_DIGITS


class TimecodeEdit(QLineEdit):
    """숫자만 받아 오른쪽부터 채우는 입력 칸 — 두 자리 묶음 fields개(시분초 칸은 3, 프레임 칸은 1).

    - 숫자: 맨 오른쪽에 붙는다. 자리가 다 차면 받지 않는다. 값이 있는 칸에 들어와 처음 친
      숫자는 기존 값을 지우고 새로 시작한다
    - Backspace: 맨 오른쪽 숫자를 지운다 · Delete: 전부 지운다
    - ".": ``dotPressed``를 낸다 — 묶은 쪽(``TimePointEdit``)이 프레임 칸으로 넘긴다
    - 붙여넣기(Ctrl+V): 묶은 쪽이 정한 규칙(``setClipboardHandlers``), 없으면 ``digits_from_text``
    - 복사(Ctrl+C): 묶은 쪽이 정한 글(시각 전체), 없으면 이 칸의 값 전체(흐린 자리 포함)
    - 드래그 · 더블클릭 · Ctrl+A: 칸 전체를 선택한다. 선택된 칸에 숫자를 치면 새로 시작한다
    - 우클릭: 복사 · 붙여넣기 두 항목만 있는 메뉴
    - 칸을 떠나거나 Enter를 치면 ``committed``를 낸다. Enter는 이어서 ``entered``를 낸다 —
      받는 쪽이 다음 칸으로 넘긴다. Enter는 창으로 올라가지 않는다(창을 닫지 않는다)
    """

    edited = Signal(str)  # 친 숫자가 바뀌었다 — 이 칸의 표기(text())를 싣는다
    committed = Signal()  # 편집을 끝냈다 — 칸을 떠났거나 Enter를 쳤다
    entered = Signal()  # Enter를 쳤다 — committed 뒤에 나온다
    dotPressed = Signal()  # "."을 쳤다 — 칸에는 들어가지 않는다
    touched = Signal()  # 키를 누르거나 마우스로 눌렀다 — 그 입력을 처리하기 **전에** 나온다

    def __init__(self, parent: QWidget | None = None, fields: int = _FIELDS):
        """
        Args:
            fields: 두 자리 묶음의 수 — 4는 시각 하나, ``CLOCK_FIELDS``는 시분초, ``FRAME_FIELDS``는 프레임
        """
        super().__init__(parent)
        self._fields = fields
        self._max_digits = fields * _FIELD_DIGITS
        self._copy_source = None  # 복사할 글을 내는 함수 — 없으면 이 칸의 값
        self._paste_handler = None  # 붙여넣기를 받는 함수(글 → 받았는지) — 없으면 이 칸의 규칙
        self._paste_check = None  # 그 글을 붙여넣을 수 있는지 — 우클릭 메뉴가 묻는다
        self._digits = ""  # 친 숫자 — 오른쪽 끝이 맨 오른쪽 묶음의 일의 자리다
        # 포커스를 받은 뒤 아직 아무것도 치지 않았다 — 다음 숫자가 기존 값을 지운다
        self._fresh = False
        # 편집을 끝낸 값이다(칸을 떠났거나 Enter를 쳤다) — 앞쪽의 0까지 전부 밝게 그린다.
        # 숫자를 하나도 치지 않은 칸은 확정되지 않는다
        self._confirmed = False
        # 숫자를 하나도 치지 않았을 때 흐리게 깔 글(이 칸의 묶음 수만큼) — 없으면 0으로 깐다
        self._empty_text: str | None = None
        self._selected = False  # 칸 전체가 선택됐다
        self._leaving = False  # 포커스를 잃어 편집을 끝내는 중이다
        self._pressed_at = None  # 마우스를 누른 자리 — 거기서 끌면 칸 전체를 선택한다
        self.setProperty("role", "timecode")
        self.setReadOnly(True)  # QLineEdit 자신의 커서 · 선택 · 입력기 조합을 끈다
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, False)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self.setDragEnabled(False)
        self.setAcceptDrops(False)
        self.setCursor(Qt.CursorShape.IBeamCursor)

    # ---- 값 ----

    def digits(self) -> str:
        """친 숫자 — 아직 치지 않은 자리는 들어 있지 않다."""
        return self._digits

    def maxDigits(self) -> int:
        """이 칸이 받는 숫자의 수 — 시분초 칸은 6, 프레임 칸은 2."""
        return self._max_digits

    def isFull(self) -> bool:
        """자리를 다 채웠는지(시분초 칸은 6자리, 프레임 칸은 2자리) — 더 쳐도 값이 바뀌지 않는다."""
        return len(self._digits) == self._max_digits

    def text(self) -> str:
        """칸의 값 — 언제나 묶음 수만큼(``HH:MM:SS:FF`` · ``HH:MM:SS`` · ``FF``). 치지 않은 자리는 0이다."""
        padded = self._digits.zfill(self._max_digits)
        return ":".join(
            padded[at : at + _FIELD_DIGITS] for at in range(0, self._max_digits, _FIELD_DIGITS)
        )

    def setText(self, text: str) -> None:
        """값을 넣는다 — 넣은 값은 전부 밝게 보인다(친 것과 같다).

        이 칸의 묶음 수만큼의 글(``HH:MM:SS:FF`` 등)을 받는다. 그 밖의 글은
        ``digits_from_text``의 규칙으로 읽고, 읽을 수 없으면 칸을 비운다.
        """
        digits = digits_from_text(text, self._fields)
        parts = text.strip().split(":")
        if digits is not None and len(parts) == self._fields:
            digits = "".join(part.zfill(_FIELD_DIGITS) for part in parts)  # 전부 밝게
        self._digits = (digits or "")[-self._max_digits :]
        self._confirmed = bool(self._digits)
        self._fresh = self.hasFocus()
        self.update()

    def setEmptyText(self, text: str | None) -> None:
        """숫자를 하나도 치지 않았을 때 흐리게 깔 글을 정한다 — 값(``text()``)은 바뀌지 않는다.

        Args:
            text: 이 칸의 묶음 수만큼의 글(``HH:MM:SS`` · ``FF``). None이면 0으로 깐다
        """
        if text != self._empty_text:
            self._empty_text = text
            self.update()

    def setClipboardHandlers(self, copy_source, paste_handler, paste_check) -> None:
        """복사 · 붙여넣기를 묶은 쪽에 맡긴다 — 시각 하나의 두 칸이 함께 움직인다.

        Args:
            copy_source: 복사할 글을 돌려주는 함수
            paste_handler: 붙여넣은 글을 받아 넣고, 받았는지를 돌려주는 함수
            paste_check: 그 글을 붙여넣을 수 있는지를 돌려주는 함수
        """
        self._copy_source = copy_source
        self._paste_handler = paste_handler
        self._paste_check = paste_check

    def brightText(self) -> str:
        """밝게 보이는 부분 — 친 숫자와 그 사이의 콜론."""
        if self._confirmed:
            return self.text()  # 편집을 끝낸 값 — 앞쪽의 0까지 밝다
        count = len(self._digits)
        if not count:
            return ""
        return self.text()[-(count + (count - 1) // _FIELD_DIGITS) :]

    def dimText(self) -> str:
        """흐리게 보이는 부분 — 아직 치지 않은 자리. 빈 칸이면 깔아 둔 글(``setEmptyText``)이다."""
        if not self._digits and self._empty_text is not None:
            return self._empty_text
        shown = self.text()
        return shown[: len(shown) - len(self.brightText())]

    def pasteText(self, text: str) -> bool:
        """글을 이 칸의 규칙으로 붙여넣는다. 받을 수 없는 글이면 아무것도 바꾸지 않고 False."""
        digits = digits_from_text(text, self._fields)
        if digits is None:
            return False
        self._setDigits(digits)
        return True

    def commit(self) -> None:
        """편집을 끝낸다 — 친 칸은 앞쪽의 0까지 밝게 채우고 ``committed``를 낸다."""
        self._fresh = False
        self._confirmed = bool(self._digits)  # 빈 칸은 떠나도 흐린 채로 둔다
        self.update()
        self.committed.emit()

    def copy(self) -> None:
        """값 전체를 클립보드에 넣는다 — 흐린 자리도 값이다(0). 묶인 칸이면 시각 전체다."""
        source = self._copy_source
        QGuiApplication.clipboard().setText(source() if source is not None else self.text())

    def paste(self) -> None:
        """클립보드의 글을 붙여넣는다 — 묶인 칸이면 묶은 쪽의 규칙, 아니면 ``pasteText``."""
        text = QGuiApplication.clipboard().text()
        if self._paste_handler is not None:
            self._paste_handler(text)
        else:
            self.pasteText(text)

    def selectAll(self) -> None:
        """칸 전체를 선택한다."""
        self._setSelected(True)

    def deselect(self) -> None:
        """선택을 푼다."""
        self._setSelected(False)

    def isAllSelected(self) -> bool:
        """칸 전체가 선택됐는지."""
        return self._selected

    def _setSelected(self, selected: bool) -> None:
        if selected != self._selected:
            self._selected = selected
            self.update()

    def buildContextMenu(self) -> QMenu:
        """우클릭 메뉴를 만든다 — 복사 · 붙여넣기 두 항목뿐이다.

        붙여넣기는 클립보드의 글을 받을 수 있을 때만 켜진다.
        """
        menu = QMenu(self)
        copy = QAction(self.tr("Copy"), menu)
        copy.triggered.connect(self.copy)
        paste = QAction(self.tr("Paste"), menu)
        clip = QGuiApplication.clipboard().text()
        paste.setEnabled(
            self._paste_check(clip)
            if self._paste_check is not None
            else digits_from_text(clip, self._fields) is not None
        )
        paste.triggered.connect(self.paste)
        menu.addAction(copy)
        menu.addAction(paste)
        return menu

    def contextMenuEvent(self, event) -> None:
        menu = self.buildContextMenu()
        menu.exec(event.globalPos())
        menu.deleteLater()

    def _setDigits(self, digits: str) -> None:
        self._fresh = False
        self._setSelected(False)
        if self._confirmed:
            self._confirmed = False  # 다시 치기 시작했다 — 친 자리만 밝게 그린다
            self.update()
        if digits == self._digits:
            return
        self._digits = digits
        self.update()
        self.edited.emit(self.text())

    # ---- 입력 ----

    def keyPressEvent(self, event) -> None:
        self.touched.emit()
        if event.matches(QKeySequence.StandardKey.Paste):
            self.paste()
            return
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy()
            return
        if event.matches(QKeySequence.StandardKey.SelectAll):
            self.selectAll()
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
        if typed == _DOT:
            self.dotPressed.emit()  # 칸에는 들어가지 않는다 — 숫자 키패드의 소수점도 여기로 온다
            return
        blocked = (
            Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.AltModifier
            | Qt.KeyboardModifier.MetaModifier
        )
        # 숫자 키패드의 숫자도 글자로 온다(KeypadModifier는 막지 않는다)
        if len(typed) == 1 and typed in _DIGITS and not event.modifiers() & blocked:
            current = "" if (self._fresh or self._selected) else self._digits
            if len(current) < self._max_digits:
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
        if (
            event.reason() != Qt.FocusReason.PopupFocusReason
        ):  # 우클릭 메뉴가 뜬 것은 떠난 것이 아니다
            self._setSelected(False)
            self._leaving = True
            try:
                self.commit()
            finally:
                self._leaving = False
        self.update()

    def leavingByFocus(self) -> bool:
        """지금의 ``committed``가 포커스를 잃어서 나온 것인지 — Enter로 나온 것과 가린다."""
        return self._leaving

    def mousePressEvent(self, event) -> None:
        self.touched.emit()
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed_at = event.position().toPoint()
            self._setSelected(False)  # 한 번 누르면 선택이 풀린다

    def mouseMoveEvent(self, event) -> None:
        # 누른 채 끌면 칸 전체를 선택한다 — 어디서 어디까지 끌었는지는 보지 않는다
        if self._pressed_at is not None and event.buttons() & Qt.MouseButton.LeftButton:
            moved = (event.position().toPoint() - self._pressed_at).manhattanLength()
            if moved >= QApplication.startDragDistance():
                self._setSelected(True)

    def mouseReleaseEvent(self, event) -> None:
        self._pressed_at = None

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._setSelected(True)

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
        if self._selected:
            # 선택된 칸 — 글자 전체에 선택 바탕을 깔고 글자는 그 위의 색으로 그린다. 입력창의
            # 선택 색(QSS의 selection-background-color · selection-color)과 같은 토큰이다
            width = dim_width + metrics.horizontalAdvance(bright)
            top = baseline - metrics.ascent() - 1
            painter.fillRect(
                left - 1, top, width + 2, metrics.height() + 2, QColor(tokens["accent"])
            )
            painter.setPen(QColor(tokens["onAccent"]))
            painter.drawText(left, baseline, dim + bright)
            painter.end()
            return
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


class TimePointEdit(QWidget):
    """시각 하나의 입력 — 시분초 칸과 프레임 칸을 나란히 묶는다 (#309).

    값(``text()``)은 두 칸을 합친 ``HH:MM:SS:FF``다 — 받는 쪽(뷰모델)이 보는 것은 지금도 네 칸
    타임코드 하나다. 두 칸은 각자 숫자를 받고(시분초 여섯 자리 · 프레임 두 자리), 아래는 함께 한다.

    - "."(시분초 칸): 같은 시각의 프레임 칸으로 간다
    - 복사: 어느 칸에서든 시각 전체(``HH:MM:SS:FF``). 빈 시각이면 그 시각이 뜻하는 값
    - 빈 시각: 두 칸 모두 숫자를 하나도 치지 않았다 — ``text()``가 빈 글이다. 시분초 칸이 빈 채
      프레임 칸만 쳤으면 빈 시각이 아니고 시분초는 ``00:00:00``이다
    - 붙여넣기: 콜론이 있으면 두 칸에 나눠 넣는다(``split_time_point``). 숫자만 있으면
      붙여넣은 칸의 입력 규칙이다. 숫자 · 콜론 밖의 글자가 있으면 받지 않는다
    - ``committed``: 이 시각을 떠났거나 Enter를 쳤을 때 한 번. 두 칸 사이를 오가는 것은 떠난 것이 아니다
    - ``entered``: 어느 칸에서든 Enter — 받는 쪽이 다음 시각으로 넘긴다
    """

    edited = Signal(str)  # 두 칸 가운데 하나가 바뀌었다 — 네 칸 표기(text())를 싣는다
    committed = Signal()  # 이 시각의 편집을 끝냈다
    entered = Signal()  # Enter를 쳤다 — committed 뒤에 나온다
    pasted = Signal()  # 붙여넣은 글을 받았다 — edited 뒤에 나온다(값이 그대로여도 나온다)
    touched = Signal()  # 두 칸 가운데 하나에 키 · 마우스 입력이 왔다 — 처리하기 전에 나온다

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(_PART_SPACING)
        self.clockEdit = TimecodeEdit(self, CLOCK_FIELDS)
        self.frameEdit = TimecodeEdit(self, FRAME_FIELDS)
        self._empty_text = _EMPTY_TIME_POINT  # 빈 시각이 뜻하는 값 — 칸에 흐리게 깔린다
        for part in (self.clockEdit, self.frameEdit):
            layout.addWidget(part)
            part.setClipboardHandlers(self.meaningText, self._handler(part), self.canPaste)
            part.edited.connect(self._edited(part))
            part.committed.connect(self._committed(part))
            part.entered.connect(self.entered)
            part.touched.connect(self.touched)
        self.clockEdit.dotPressed.connect(self._toFrame)
        self.setFocusProxy(self.clockEdit)

    # ---- 값 ----

    def text(self) -> str:
        """이 시각의 값 — 네 칸 ``HH:MM:SS:FF``. 빈 시각이면 빈 글이다."""
        if self.isEmpty():
            return ""
        return f"{self.clockEdit.text()}:{self.frameEdit.text()}"

    def isEmpty(self) -> bool:
        """빈 시각인지 — 두 칸 모두 숫자를 하나도 치지 않았다."""
        return not (self.clockEdit.digits() or self.frameEdit.digits())

    def meaningText(self) -> str:
        """이 시각이 뜻하는 값 — 빈 시각이면 깔아 둔 값(``setEmptyText``), 아니면 ``text()``."""
        return self.text() or self._empty_text

    def setEmptyText(self, text: str) -> None:
        """빈 시각이 뜻하는 값을 정한다 — 빈 시각의 두 칸에 흐리게 깔리고, 복사하면 이 값이 나간다.

        Args:
            text: 네 칸 타임코드 ``HH:MM:SS:FF``. 읽을 수 없으면 ``00:00:00:00``으로 둔다
        """
        readable = text.count(":") == _FIELDS - 1 and split_time_point(text) is not None
        self._empty_text = text if readable else _EMPTY_TIME_POINT
        self._syncEmptyText()

    def _syncEmptyText(self) -> None:
        """빈 시각이면 두 칸에 뜻하는 값을 깔고, 아니면 걷는다(치지 않은 칸은 0으로 깔린다)."""
        clock = frame = None
        if self.isEmpty():
            clock, frame = split_time_point(self._empty_text)
        self.clockEdit.setEmptyText(clock)
        self.frameEdit.setEmptyText(frame)

    def setText(self, text: str) -> None:
        """네 칸 타임코드를 두 칸에 나눠 넣는다 — 넣은 값은 전부 밝게 보인다. 읽을 수 없으면 비운다."""
        parts = split_time_point(text) if text.count(":") == _FIELDS - 1 else None
        if parts is None:
            self.clockEdit.setText("")
            self.frameEdit.setText("")
            self._syncEmptyText()
            return
        clock, frame = parts
        # 묶음을 두 자리로 채워 넣어야 전부 밝게 보인다(값이 0인 자리도 넣은 값이다)
        self.clockEdit.setText(":".join(part.zfill(_FIELD_DIGITS) for part in clock.split(":")))
        self.frameEdit.setText(frame.zfill(_FIELD_DIGITS))
        self._syncEmptyText()

    def hasEditFocus(self) -> bool:
        """두 칸 가운데 하나에 포커스가 있는지."""
        return self.clockEdit.hasFocus() or self.frameEdit.hasFocus()

    def commit(self) -> None:
        """이 시각의 편집을 끝낸다 — ``committed``를 낸다."""
        self.committed.emit()

    # ---- 복사 · 붙여넣기 ----

    def canPaste(self, text: str) -> bool:
        """그 글을 이 시각에 붙여넣을 수 있는지 — 콜론이 없는 글은 칸마다 다르므로 넉넉히 본다."""
        text = text.strip()
        if ":" in text:
            return split_time_point(text) is not None
        return digits_from_text(text, CLOCK_FIELDS) is not None

    def pasteInto(self, part: TimecodeEdit, text: str) -> bool:
        """part에 글을 붙여넣는다. 받을 수 없는 글이면 아무것도 바꾸지 않고 False."""
        if ":" not in text.strip():
            return part.pasteText(
                text
            )  # 숫자만 — 붙여넣은 칸의 입력 규칙(pasteText가 edited를 낸다)
        parts = split_time_point(text)
        if parts is None:
            return False
        before = self.text()
        clock, frame = parts
        self.clockEdit.setText(clock)
        self.frameEdit.setText(frame.zfill(_FIELD_DIGITS))
        self._syncEmptyText()
        if self.text() != before:
            self.edited.emit(self.text())
        return True

    def _handler(self, part: TimecodeEdit):
        def handle(text: str) -> bool:
            accepted = self.pasteInto(part, text)
            if accepted:
                self.pasted.emit()  # 받는 쪽이 붙여넣은 값을 한 번에 들어온 값으로 다룬다
            return accepted

        return handle

    # ---- 두 칸의 신호 ----

    def _edited(self, part: TimecodeEdit):
        def relay(_text: str) -> None:
            if not isValid(self):
                return
            self._syncEmptyText()
            self.edited.emit(self.text())

        return relay

    def _committed(self, part: TimecodeEdit):
        def relay() -> None:
            if not isValid(self):
                return  # 창이 닫히며 칸이 포커스를 잃었다 — 이 묶음은 이미 사라졌다
            siblings = (self.clockEdit, self.frameEdit)
            if part.leavingByFocus() and QApplication.focusWidget() in siblings:
                return  # 같은 시각의 다른 칸으로 갔다 — 이 시각을 떠난 것이 아니다
            self.commit()

        return relay

    def _toFrame(self) -> None:
        self.frameEdit.setFocus(Qt.FocusReason.TabFocusReason)
