"""구간 편집 창 — 시간 입력으로 받을 구간을 정한다 (#309).

카드 하나의 구간 목록을 편집하는 모달 창이다. 대기 상태 카드의 구간 요약(재생 시간 자리)을
누르면 열린다. 타임라인 · 미리보기는 이 창의 위쪽에 나중에 붙는다 — 시간 입력 영역은 그때도
항상 보인다.

창은 뷰모델(``app/viewmodels/section_edit.py``)만 본다. 해석 · 검증 · 카드에
쓰기는 뷰모델이 하고, 창은 글자를 넘기고 결과를 그린다.

화면은 조회 상태에 따라 셋이다.

| 상태 | 보이는 것 | 확인 버튼 |
|---|---|---|
| 조회 중 | "영상 정보를 읽는 중" 한 줄 | 꺼짐 |
| 조회 실패 | 실패 안내 | 꺼짐 |
| 준비됨 | 머리줄(구간 수 · 프레임률) · 구간 행 · 구간 추가 · 구간 길이의 합 · 입력법 안내 | 오류가 없을 때만 켜짐 |

구간의 순서는 행 왼쪽 손잡이(⋮⋮)를 끌어 바꾸고, 키보드로는 시각 칸에서 Alt+↑/↓(macOS Option)로
바꾼다(설계 §12.1 「카드 편집 창 · 구간 창」, #586). 순서가 구간 번호이고 파일 이름의 ``_N``이다.
툴팁은 손잡이와 ✕에만 둔다 — 시각 입력법 · 최대 개수는 목록 아래 흐린 글 한 줄이다.

오류는 팝업으로 띄우지 않는다 — 틀린 칸의 테두리와 그 행의 문구로 보인다. 값이 바뀔 때마다
검사하고, 치고 있는 행의 오류를 바로 띄울지 칸을 떠날 때 띄울지는 뷰모델의 표
(``ERROR_TIMING``)가 정한다. 확인 버튼은 띄우지 않은 오류가 있어도 꺼진다.

시각 하나는 칸 둘이다 — 시분초 칸과 프레임 칸(``app/widgets/timecode_edit.py``의
``TimePointEdit``). 숫자만 받고, 시분초 칸은 초 자리부터 채운다. 한 행은
[시작 시분초][시작 프레임] ~ [끝 시분초][끝 프레임]이다.

- Tab: 시작 시분초 → 시작 프레임 → 끝 시분초 → 끝 프레임 → 그 행의 버튼 → 다음 행
- Enter: 그 시각의 값을 확정하고 **다음 시각의 시분초 칸**으로 간다(프레임 칸을 건너뛴다) —
  시작 → 끝 → 다음 행의 시작, 마지막 행의 끝에서는 확인 버튼. ``NEXT_AFTER_ENTER``가 정한다
- ".": 시분초 칸에서 같은 시각의 프레임 칸으로 간다

**칸에서 친 Enter는 창을 닫지 않는다** — 초점이 있는 버튼에서 친 Enter만 그 버튼을 누른다(카드 편집
창과 같은 규칙). 그래서 창에 기본 버튼을 두지 않는다. Esc는 취소로 닫는다 — 끄는 중이면 끌기만 버린다.
버튼은 Qt 버튼 상자가 OS 관례 차례로 놓고, 확인이 주 동작(채움)이다(설계 §12.1).
"""

import sys

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QEnterEvent, QMouseEvent, QPainter, QPaintEvent, QShowEvent
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from shiboken6 import isValid

import app.theme as theme
from app.viewmodels.section_edit import (
    END,
    PART_CLOCK,
    PART_FRAME,
    START,
    STATE_FAILED,
    STATE_LOADING,
    STATE_READY,
    SectionEditViewModel,
)
from app.widgets.eliding_label import ElidingLabel, TwoLineLabel
from app.widgets.scroll_padding import ScrollBarPadding
from app.widgets.tip_bubble import install_tips
from app.widgets.timecode_edit import TimecodeEdit, TimePointEdit
from app.widgets import view as _pointer  # widget_under_pointer — 테스트가 그 모듈에서 갈아 끼운다

# 칸의 폭을 재는 본보기 글자 — 칸에 깔리는 글자 그대로다
_CLOCK_SAMPLE = "00:00:00"
_FRAME_SAMPLE = "00"
_LENGTH_SAMPLE = "00:00:00.000"  # 행의 길이 글 본보기 — 뷰모델 lengthText의 형식 그대로
# 말줄임 라벨이 글 폭 그대로면 잘린 것으로 재는 몫(px) — ElidingLabel.sizeHint의 +4와 같다
_LABEL_SLACK = 4
_EDIT_PADDING = 22  # 칸의 글자 양옆 몫(px) — QSS의 padding과 테두리, 커서 한 줄
# Enter를 친 뒤 포커스가 갈 곳 — "clock"이면 다음 시각의 시분초 칸(프레임 칸을 건너뛴다),
# "frame"이면 같은 시각의 프레임 칸을 먼저 지난다. 바꾸기 쉽게 한 곳에 둔다
NEXT_AFTER_ENTER = "clock"
# 행 아래 안내 줄에 보이는 글의 종류 — SectionRow.showMessage가 우선순위대로 하나만 보인다
MESSAGE_ERROR = "error"  # 오류 — 그 행을 확인할 수 없다
MESSAGE_NOTICE = "notice"  # 끝이 영상 끝을 넘는 중 · 영상 끝으로 맞췄다는 안내
MESSAGE_NOTE = "note"  # 무시 안내 — 완전히 빈 행
_INITIAL_SIZE = (560, 420)  # 창의 첫 크기(px) — 행 여섯 개쯤이 스크롤 없이 보인다
# 행 사이의 간격(px). 행마다 아래에 안내 줄 한 줄이 늘 서 있어 그 줄이 행 사이를 띄운다 —
# 간격을 따로 두면 20행의 높이가 안내 줄만큼 더 길어진다
_ROW_SPACING = 0
# 칸의 줄과 그 아래 안내 줄 사이(px) — 띄우지 않는다. 안내 줄이 늘 한 줄을 차지해 행 사이를
# 이미 띄우고, 2px씩이면 20행에서 40px이 더 든다
_MESSAGE_GAP = 0
DRAG_HANDLE_WIDTH = 16  # 손잡이 폭(px) — 아이콘 도형(16)과 같다(프로토 inbox 55)
DRAG_EDGE = 24  # 끄는 중 목록 위 · 아래 끝에서 이 거리(px) 안이면 저절로 스크롤한다
DRAG_SCROLL_STEP = 8  # 저절로 스크롤 한 번에 움직이는 거리(px) — DRAG_SCROLL_MS마다
DRAG_SCROLL_MS = 30  # 저절로 스크롤 간격(ms)
# 들어갈 자리 선 두께(px) — 칸의 focus ring(1)보다 한 단계 굵게, 행 사이 빈 줄에 묻히지 않게
DRAG_LINE = 2
# 끄는 행의 불투명도 — 자리는 비우지 않는다(끄는 동안 다른 행이 들썩이지 않게)
DRAG_OPACITY = 0.4
_DOT_RADIUS = 1.1  # 손잡이 점의 반지름(px) — 두 줄 × 세 점 = ⋮⋮
_DOT_STEP_X = 2.5  # 손잡이 가운데에서 두 줄까지 가로 거리(px)
_DOT_STEP_Y = 5.0  # 손잡이 점 사이 세로 거리(px)


def move_keys_text() -> str:
    """순서를 바꾸는 키 글 — macOS는 Option 기호(⌥)로 적는다."""
    return "⌥↑↓" if sys.platform == "darwin" else "Alt+↑↓"


def is_move_key(event: QEvent) -> bool:
    """시각 칸에서 행을 옮기는 키(Alt+↑/↓, macOS Option)인지.

    수정 키는 Alt 하나뿐이어야 한다 — 다른 수정 키가 함께 눌렸으면 칸에 맡긴다. macOS는 화살표에
    키패드 수정자를 붙여 보내므로 그것은 세지 않는다.
    """
    if event.type() != QEvent.Type.KeyPress:
        return False
    if event.key() not in (Qt.Key.Key_Up, Qt.Key.Key_Down):
        return False
    modifiers = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
    return modifiers == Qt.KeyboardModifier.AltModifier


class _MoveKeyFilter(QObject):
    """시각 칸에 거는 Alt+↑/↓ 필터 — 받은 키를 창의 함수(``handler``)에 넘긴다.

    창 자신을 필터로 걸지 않는다 — 창이 파괴되는 도중 자식 칸이 사라지며 보내는 이벤트가 반쯤 지워진
    창의 파이썬 함수로 가서 예외가 난다. 이 객체는 창의 첫 자식이라 칸보다 먼저 사라지고, 사라진
    필터는 Qt가 더 부르지 않는다.
    """

    def __init__(self, handler, parent: QObject) -> None:
        super().__init__(parent)
        self._handler = handler

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if is_move_key(event) and self._handler(watched, event):
            return True
        return super().eventFilter(watched, event)


class DragHandle(QWidget):
    """구간 행 왼쪽의 손잡이(⋮⋮) — 이것을 눌러 끌 때만 행이 옮겨진다 (#586).

    시각 칸 · 번호 · 길이를 끌어서는 옮겨지지 않는다(글 고르기 · 칸 누르기와 겹치지 않게). 누르고
    ``startDragDistance`` 넘게 움직이면 ``dragStarted``, 그 뒤 움직일 때마다 ``dragMoved``, 놓으면
    ``dropped``를 낸다(전역 위치). 누른 동안은 Qt가 이 위젯에 포인터를 붙잡아 두어 창 밖의 움직임 ·
    놓기도 여기로 온다. 초점은 받지 않는다 — 끌어도 칸의 초점이 그대로다.
    """

    dragStarted = Signal()
    dragMoved = Signal(QPoint)
    dropped = Signal(QPoint)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("sectionDragHandle")
        self.setFixedWidth(DRAG_HANDLE_WIDTH)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        # 행이 하나뿐이면 숨는다 — 숨어도 자리는 남긴다(칸이 옆으로 흔들리지 않게)
        policy = self.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.setSizePolicy(policy)
        self._press: QPoint | None = None  # 누른 전역 위치 — 누르지 않았으면 None
        self._dragging = False

    def isDragging(self) -> bool:
        """끄는 중인지 — 누르고 끌기 거리를 넘긴 뒤 놓기 전까지다."""
        return self._dragging

    def cancel(self) -> None:
        """끌기를 버린다(Esc) — 다음 움직임 · 놓기는 아무 일도 하지 않는다."""
        self._press = None
        self._dragging = False
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.update()

    def paintEvent(self, _event: QPaintEvent) -> None:
        """⋮⋮를 그린다 — 평소 흐린 글 색, 포인터가 위에 있거나 끄는 중이면 본문 글 색."""
        tokens = theme.current_tokens()
        strong = self.underMouse() or self._dragging
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(tokens["text" if strong else "textMuted"]))
        cx, cy = self.width() / 2, self.height() / 2
        for dx in (-_DOT_STEP_X, _DOT_STEP_X):
            for dy in (-_DOT_STEP_Y, 0.0, _DOT_STEP_Y):
                painter.drawEllipse(QPointF(cx + dx, cy + dy), _DOT_RADIUS, _DOT_RADIUS)
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self._press = event.globalPosition().toPoint()
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._press is None:
            return
        where = event.globalPosition().toPoint()
        if not self._dragging:
            if (where - self._press).manhattanLength() < QApplication.startDragDistance():
                return
            self._dragging = True
            self.update()
            self.dragStarted.emit()
        self.dragMoved.emit(where)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._press is None:
            return
        dragging = self._dragging
        self.cancel()
        if dragging:
            self.dropped.emit(event.globalPosition().toPoint())


class SectionRow(QWidget):
    """구간 한 행 — 손잡이 · 번호 · [시작 시분초][프레임] ~ [끝 시분초][프레임] · 길이 · 삭제,
    그 아래 안내 줄 한 줄.

    안내 줄(``messageSlot``)은 글이 없어도 **늘 한 줄의 높이를 차지한다** — 오류가 나고
    사라져도, 안내가 바뀌어도 행의 높이가 같아 아래 행들이 오르내리지 않는다. 오류 · 끝을
    맞춘 안내 · 무시 안내가 이 한 줄을 함께 쓰고 한 번에 하나만 보인다(``showMessage``). 줄바꿈하지 않는다 —
    넘치는 글은 말줄임하고 전문은 툴팁에 둔다(``ElidingLabel``).

    포인터가 있는 행은 띠를 칠해 번호 · 시각 칸 · 길이 · ✕가 한 줄임을 보인다(행 호버, #586). 들어감 ·
    나감은 ``hoverChanged``로 창에 알리고, 칠할지는 창이 정한다(``setHovered``) — 끄는 중 · 창이
    비활성이면 칠하지 않고, 행을 다시 만든 뒤에는 창이 포인터 밑 행을 다시 맞춘다. 자식(칸 · ✕ · 손잡이)
    위로 가도 Qt는 행에 나감을 보내지 않아 칠한 채 남는다.

    띠는 입력칸 줄 위아래로 ``sectionRowHoverPad``만큼이고, 안내 글이 보이면 그 글자 아래 끝 + 같은
    여백까지 내려간다(``hoverBand``). 그 여백을 두려고 행 위에 같은 높이의 빈 자리를 둔다 — 안내 글이 있는
    행의 띠는 다음 행의 이 빈 자리까지 걸치므로 행이 아니라 행 목록(``_RowList``)이 칠한다. 호버가 켜지는
    범위는 행 전체 그대로다 — 행 사이에 틈이 없어 지날 때 깜박이지 않는다.
    """

    hoverChanged = Signal(bool)  # 포인터가 이 행에 들어왔다(True) · 나갔다(False)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("sectionRow")
        self._hovered = False
        column = QVBoxLayout(self)
        # 위 빈 자리 = 호버 띠의 위 여백 — 띠 위 끝이 행 위 끝이라 첫 행에서도 잘리지 않는다
        column.setContentsMargins(0, theme.METRICS["sectionRowHoverPad"], 0, 0)
        column.setSpacing(_MESSAGE_GAP)
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.METRICS["sectionRowGap"])  # 앱 스타일의 기본 간격은 0이다
        column.addLayout(layout)
        self._fieldLine = layout  # 입력칸 줄 — 호버 띠의 기준(hoverBand)

        self.handle = DragHandle(self)
        layout.addWidget(self.handle)
        self.numberLabel = QLabel(self)
        self.numberLabel.setObjectName("sectionNumberLabel")
        self.numberLabel.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.numberLabel.ensurePolished()  # 폭은 QSS 글꼴이 입혀진 뒤에 잰다
        self.numberLabel.setMinimumWidth(self.numberLabel.fontMetrics().horizontalAdvance("00"))
        layout.addWidget(self.numberLabel)

        self.startEdit = self._timePointEdit("sectionStartEdit")
        self.rangeLabel = QLabel("~", self)  # 시작과 끝 사이 — 번역하지 않는 기호다
        self.rangeLabel.setObjectName("sectionRangeLabel")
        self.endEdit = self._timePointEdit("sectionEndEdit")
        layout.addWidget(self.startEdit)
        layout.addWidget(self.rangeLabel)
        layout.addWidget(self.endEdit)

        # 구간 길이(밀리초 표기) — 최소폭은 본보기 글이 다 들어가는 폭이다. 창의 최소폭이 행에서
        # 나오므로(_fitRowWidth) 가장 좁은 창에서도 길이가 잘리지 않는다(프로토 구간 행, #586).
        # 더 긴 글(100시간 이상)만 말줄임하고, 잘렸을 때만 전문을 툴팁에 둔다
        self.infoLabel = ElidingLabel(self, tip_when_elided=True)
        self.infoLabel.setObjectName("sectionInfoLabel")
        self.infoLabel.ensurePolished()  # 폭은 QSS 글꼴이 입혀진 뒤에 잰다
        sample = self.infoLabel.fontMetrics().horizontalAdvance(_LENGTH_SAMPLE)
        self.infoLabel.setMinimumWidth(sample + _LABEL_SLACK)
        layout.addWidget(self.infoLabel, 1)

        self.deleteButton = self._rowButton("✕", "sectionDeleteButton")
        layout.addWidget(self.deleteButton)

        # 안내 줄 — 글이 없어도 한 줄의 높이를 차지한다. 손잡이 · 번호 폭만큼 들여 칸의 왼쪽 끝에 맞춘다
        self.messageSlot = QWidget(self)
        self.messageSlot.setObjectName("sectionMessageSlot")
        slot = QHBoxLayout(self.messageSlot)
        indent = DRAG_HANDLE_WIDTH + self.numberLabel.minimumWidth() + 2 * layout.spacing()
        slot.setContentsMargins(indent, 0, 0, 0)
        slot.setSpacing(0)
        # 오류 문구 — 오류가 있을 때만 보인다
        self.errorLabel = self._messageLabel("sectionErrorLabel")
        # 끝이 영상 끝을 넘는 중 · 영상 끝으로 맞췄다는 안내 — 흐린 글이다. 오류가 아니다
        self.noticeLabel = self._messageLabel("sectionNoticeLabel")
        # 걸러 낼 행의 안내 — 완전히 비어 있어 구간에 넣지 않는 행에만 보인다. 오류가 아니다
        self.noteLabel = self._messageLabel("sectionNoteLabel")
        # 우선순위가 높은 것부터 — 한 번에 하나만 보인다
        self._messages = (
            (MESSAGE_ERROR, self.errorLabel),
            (MESSAGE_NOTICE, self.noticeLabel),
            (MESSAGE_NOTE, self.noteLabel),
        )
        for _kind, label in self._messages:
            slot.addWidget(label, 1)
        # 높이는 글자에서 유도한다 — 안내 줄의 글꼴(QSS)이 입혀진 뒤의 한 줄 높이
        self.errorLabel.ensurePolished()
        self.messageSlot.setFixedHeight(self.errorLabel.fontMetrics().height())
        column.addWidget(self.messageSlot)

    def _messageLabel(self, name: str) -> ElidingLabel:
        # 잘렸을 때만 전문을 툴팁에 둔다 — 보이는 글을 되풀이하는 툴팁은 두지 않는다(설계 §12.1)
        label = ElidingLabel(self.messageSlot, tip_when_elided=True)
        label.setObjectName(name)
        label.setWordWrap(False)  # 한 줄 — 줄바꿈하면 행의 높이가 글 길이에 따라 달라진다
        label.setVisible(False)
        return label

    def showMessage(self, texts: dict[str, str]) -> None:
        """안내 줄에 글 하나를 보인다 — 종류(``MESSAGE_*``) → 글. 빈 글은 없는 것이다.

        둘 이상이 걸리면 우선순위가 가장 높은 것만 보인다: 오류 > 끝을 맞춘 안내 > 무시 안내. 아무것도 없으면
        줄은 빈 채로 높이만 차지한다.
        """
        chosen = next((kind for kind, _label in self._messages if texts.get(kind)), None)
        for kind, label in self._messages:
            label.setText(texts.get(kind, "") if kind == chosen else "")
            label.setVisible(kind == chosen)

    def _timePointEdit(self, name: str) -> TimePointEdit:
        edit = TimePointEdit(self)
        edit.setObjectName(name)
        # 폭은 글자에서 유도한다
        for part, sample in ((edit.clockEdit, _CLOCK_SAMPLE), (edit.frameEdit, _FRAME_SAMPLE)):
            part.setFixedWidth(part.fontMetrics().horizontalAdvance(sample) + _EDIT_PADDING)
        return edit

    def _rowButton(self, text: str, name: str) -> QPushButton:
        # 면 · 테두리가 있는 상자 버튼 — 기본 버튼 규칙을 받고 크기만 #sectionDeleteButton이 줄인다
        # (프로토 구간 행, #586). 흐린 글자 버튼(role="subtle")이 아니다
        button = QPushButton(text, self)
        button.setObjectName(name)
        button.setAutoDefault(False)  # 칸에서 Enter를 눌러도 이 버튼이 눌리지 않는다
        button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        return button

    def edit(self, column: int) -> TimePointEdit:
        """칸 번호(START · END)의 시각 입력 — 시분초 칸과 프레임 칸의 묶음."""
        return self.startEdit if column == START else self.endEdit

    def enterEvent(self, event: QEnterEvent) -> None:
        super().enterEvent(event)
        self.hoverChanged.emit(True)

    def leaveEvent(self, event: QEvent) -> None:
        super().leaveEvent(event)
        self.hoverChanged.emit(False)

    def isHovered(self) -> bool:
        """행 호버 띠를 칠한 상태인지."""
        return self._hovered

    def setHovered(self, hovered: bool) -> None:
        """행 호버 띠를 칠한 상태로 적는다 — 칠하는 것은 행 목록이다(``_RowList``, #586)."""
        self._hovered = hovered

    def shownMessage(self) -> ElidingLabel | None:
        """안내 줄에 보이는 글의 라벨 — 없으면 None."""
        return next((label for _kind, label in self._messages if label.text()), None)

    def hoverBand(self) -> QRect:
        """호버 띠의 자리(행 좌표, 행 밖으로 걸칠 수 있다) — 폭은 행 전체다.

        위 끝은 입력칸 줄 위 끝 − 여백(= 행 위 끝), 아래 끝은 입력칸 줄 아래 끝 + 여백이다. 안내 글이
        보이면 아래 끝을 그 글자(잉크)의 아래 끝 + 여백까지 내린다 — 줄 상자가 아니라 글자에서 잰다. 줄
        상자의 아래는 내려가는 글자(g · y)의 자리라 한국어 글에서는 여백이 넓어 보인다. 내린 끝은 다음 행
        입력칸 줄(행 아래 끝 + 여백)에 닿지 않게 그 1px 위에서 자른다(#586).
        """
        pad = theme.METRICS["sectionRowHoverPad"]
        line = self._fieldLine.geometry()
        top = line.top() - pad
        bottom = line.bottom() + 1 + pad  # 띠 아래 끝 바로 밑 줄(배타)
        label = self.shownMessage()
        if label is not None:
            metrics = label.fontMetrics()
            shown = QLabel.text(label)  # 말줄임한 뒤 보이는 글
            ink = metrics.tightBoundingRect(shown)
            origin = label.mapTo(self, QPoint(0, 0)).y()
            baseline = origin + (label.height() - metrics.height()) // 2 + metrics.ascent()
            ink_bottom = baseline + ink.y() + ink.height()  # 글자 아래 끝 바로 밑 줄(배타)
            bottom = max(bottom, min(ink_bottom + pad, self.height() + pad - 1))
        return QRect(0, top, self.width(), bottom - top)


class _RowList(QWidget):
    """구간 행을 담는 목록 — 호버 행의 띠를 행보다 먼저(행 밑에) 칠한다 (#586).

    띠가 행 밖(다음 행의 위 빈 자리)까지 걸칠 수 있어 행이 아니라 여기서 칠한다. 칠할 행은 창이
    정한다(``setHoverRow``). 색은 ``rowHoverBg``, 모서리는 ``menuHoverRadius``다.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._hoverRow: SectionRow | None = None

    def setHoverRow(self, row: SectionRow | None) -> None:
        """띠를 칠할 행을 정한다 — None이면 칠하지 않는다."""
        self._hoverRow = row
        self.update()

    def paintEvent(self, _event: QPaintEvent) -> None:
        row = self._hoverRow
        if row is None or not isValid(row) or not row.isVisible():
            return
        band = row.hoverBand().translated(row.pos())
        radius = theme.METRICS["menuHoverRadius"]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.current_tokens()["rowHoverBg"]))
        painter.drawRoundedRect(QRectF(band), radius, radius)
        painter.end()


class SectionEditDialog(QDialog):
    """구간 편집 창. 뷰모델의 상태를 그리고 입력을 뷰모델에 넘긴다."""

    def __init__(self, viewmodel: SectionEditViewModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # 시각 칸의 Alt+↑/↓ 필터 — 칸보다 먼저 만들어 칸보다 먼저 사라지게 한다(_MoveKeyFilter)
        self._moveKeys = _MoveKeyFilter(self._onMoveKey, self)
        self._viewmodel = viewmodel
        self._rows: list[SectionRow] = []
        # 숫자를 치고 있는 (행, 칸) — 그 행의 오류 가운데 일부는 칸을 떠날 때 띄운다. 없으면 None
        self._typing: tuple[int, int] | None = None
        # 끝을 맞췄다는 안내를 다음 조작이 내려도 되는지. 맞추게 만든 클릭 · 키는 내리지 않는다 —
        # 그 조작의 처리가 끝난 뒤(타이머 0)에야 켜진다
        self._clampNoticeArmed = False
        self._armTimer = QTimer(self)
        self._armTimer.setSingleShot(True)
        self._armTimer.setInterval(0)
        self._armTimer.timeout.connect(self._armClampNotice)
        # 끄는 중인 행 — 끌기를 시작한 행 위젯, 들어갈 틈(0 = 맨 위 행 앞 ~ 행 수 = 맨 끝), 마지막
        # 전역 위치. 끌지 않으면 None
        self._drag: dict | None = None
        self._dragTimer = QTimer(self)
        self._dragTimer.setInterval(DRAG_SCROLL_MS)
        self._dragTimer.timeout.connect(self._dragAutoScroll)
        # 첫 초점을 아직 두지 않았는지 — 창이 보이고 행이 생긴 뒤 한 번 둔다(_placeFirstFocus)
        self._firstFocusPending = True
        # 호버 바탕을 칠한 행 — 늘 하나 이하다. 행을 다시 만든 뒤 · 굴린 뒤 · 창이 다시 활성일 때
        # 다음 턴에 포인터 밑 행으로 다시 맞춘다(_scheduleRehover)
        self._hoverRow: SectionRow | None = None
        self._rehoverPending = False
        self.setObjectName("sectionEditDialog")
        self.setModal(True)
        self.setupUi()
        self.retranslateUi()
        viewmodel.stateChanged.connect(self._onStateChanged)
        viewmodel.rowsReset.connect(self._rebuildRows)
        viewmodel.validated.connect(self._refresh)
        viewmodel.endClamped.connect(self._onEndClamped)
        self._onStateChanged()
        self._rebuildRows()
        # 툴팁은 모두 말풍선(0ms)으로 뜬다 — 창의 위젯마다 필터를 건다(설계 §12.1 「툴팁」, #550).
        # 나중에 만드는 행은 _rebuildRows가 건다
        install_tips(self)
        self.resize(*_INITIAL_SIZE)

    def viewModel(self) -> SectionEditViewModel:
        """이 창이 보는 뷰모델."""
        return self._viewmodel

    def setupUi(self) -> None:
        """위젯을 만든다 — 글자는 ``retranslateUi``가 넣는다."""
        layout = QVBoxLayout(self)

        # 제목 — 잘렸을 때만 전문을 툴팁에 둔다(보이는 글을 되풀이하지 않는다, 설계 §12.1)
        self.titleLabel = ElidingLabel(self, tip_when_elided=True)
        self.titleLabel.setObjectName("sectionTitleLabel")
        self.titleLabel.setText(self._viewmodel.item.title)
        layout.addWidget(self.titleLabel)

        # 조회 중 · 조회 실패 안내 — 준비되면 숨는다
        self.statusLabel = QLabel(self)
        self.statusLabel.setObjectName("sectionStatusLabel")
        self.statusLabel.setWordWrap(True)
        self.statusLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.statusLabel, 1)

        self.headerLabel = QLabel(self)
        self.headerLabel.setObjectName("sectionHeaderLabel")
        layout.addWidget(self.headerLabel)

        self.scrollArea = QScrollArea(self)
        self.scrollArea.setObjectName("sectionScrollArea")
        self.scrollArea.setWidgetResizable(True)
        self.scrollArea.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._rowContainer = _RowList(self.scrollArea)
        self._rowContainer.setObjectName("sectionRowContainer")
        self._rowLayout = QVBoxLayout(self._rowContainer)
        self._rowLayout.setSpacing(_ROW_SPACING)
        self._rowLayout.addStretch(1)
        self.scrollArea.setWidget(self._rowContainer)
        # 세로 스크롤바는 필요할 때만 보이고, 보이는 동안은 행 컨테이너의 오른쪽 여백 안에
        # 들어간다 — 바가 생기고 사라져도 행의 폭과 칸의 자리가 같다(메인 카드 목록과 같은
        # 방식). 여백이 바보다 좁으면 모자란 만큼 행이 좁아지므로 바의 폭 이상으로 둔다
        self.scrollArea.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        margins = self._rowLayout.contentsMargins()
        bar = self.scrollArea.verticalScrollBar()
        bar.ensurePolished()
        right = max(margins.right(), bar.sizeHint().width())
        # 아래 여백은 호버 띠 여백 이상 — 마지막 행에 안내 글이 있으면 띠가 행 아래로 그만큼 걸친다
        bottom = max(margins.bottom(), theme.METRICS["sectionRowHoverPad"])
        self._rowLayout.setContentsMargins(margins.left(), margins.top(), right, bottom)
        self._scrollPadding = ScrollBarPadding(self.scrollArea, self._rowLayout, right)
        # 행의 양옆에 드는 폭(px) — 왼쪽 여백 + 오른쪽 여백(스크롤바가 보이면 바가 그 자리에 든다)
        self._rowSideWidth = margins.left() + right
        # 굴리면 포인터 밑의 행이 바뀐다 — 행 호버를 다시 맞춘다
        bar.valueChanged.connect(self._scheduleRehover)
        layout.addWidget(self.scrollArea, 1)
        # 끄는 동안 들어갈 자리에 긋는 선 — 행 컨테이너 위에 겹쳐 놓는다(레이아웃에 들지 않는다).
        # 색은 전역 QSS가 칠한다(강조색)
        self.dropLine = QFrame(self._rowContainer)
        self.dropLine.setObjectName("sectionDropLine")
        self.dropLine.setFixedHeight(DRAG_LINE)
        self.dropLine.hide()

        under = QHBoxLayout()
        # 「+ 구간 추가」 — 목록 아래 왼쪽(설계 §12.1)
        self.addButton = QPushButton(self)
        self.addButton.setObjectName("sectionAddButton")
        self.addButton.setAutoDefault(False)
        self.addButton.clicked.connect(self._onAdd)
        under.addWidget(self.addButton)
        # 구간 길이의 합이 영상보다 길다는 알림 — 「+ 구간 추가」 오른쪽의 남는 자리를 쓴다. 글이
        # 없어도 그 자리를 차지해, 알림이 생기고 사라져도 행 영역과 버튼이 움직이지 않는다. 알림은
        # 두 문장이다(무슨 일인지 · 숫자) — 들어가면 한 줄로 잇고, 좁으면 두 줄로 나눠 숫자
        # 쪽만 줄인다. 전체 글은 늘 툴팁에 있다(TwoLineLabel)
        self.totalLabel = TwoLineLabel(self)
        self.totalLabel.setObjectName("sectionTotalLabel")
        under.addWidget(self.totalLabel, 1)
        # 구간 길이의 합("합계 11:20:16.000") — 「+ 구간 추가」 줄의 오른쪽 끝. 행의 길이 표기와
        # 같은 형식이다. 합이 영상보다 길면 왼쪽의 알림이 그 합을 함께 적으므로 이 글은 숨는다(같은
        # 값을 두 번 적지 않는다)
        self.sumLabel = QLabel(self)
        self.sumLabel.setObjectName("sectionSumLabel")
        under.addWidget(self.sumLabel)
        layout.addLayout(under)
        # 시각 입력법 · 최대 개수 — 목록 아래 흐린 글 한 줄. 칸마다 걸던 툴팁을 여기 한 번으로
        # 모았다(설계 §12.1). 좁으면 말줄임하고 잘렸을 때만 전문을 툴팁에 둔다
        self.hintLabel = ElidingLabel(self, tip_when_elided=True)
        self.hintLabel.setObjectName("sectionHintLabel")
        layout.addWidget(self.hintLabel)

        # 받는 중인 배치가 이 카드를 기다릴 때만 보인다 — 창을 닫아야 이어 간다는 것을 알린다
        self.waitHintLabel = QLabel(self)
        self.waitHintLabel.setObjectName("sectionWaitHintLabel")
        self.waitHintLabel.setWordWrap(True)
        self.waitHintLabel.setVisible(False)
        layout.addWidget(self.waitHintLabel)

        # 버튼 차례는 Qt 버튼 상자가 OS 관례로 정한다(설계 §12.1). 확인이 주 동작(채움 색 규칙)
        self.buttonBox = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        self.okButton = self.buttonBox.button(QDialogButtonBox.StandardButton.Ok)
        self.okButton.setObjectName("sectionOkButton")
        self.okButton.setProperty("role", "primary")
        # 버튼 상자가 만들며 이미 스타일을 입혔다 — 속성을 바꾼 뒤 다시 입힌다
        theme.repolish(self.okButton)
        self.cancelButton = self.buttonBox.button(QDialogButtonBox.StandardButton.Cancel)
        self.cancelButton.setObjectName("sectionCancelButton")
        # 기본 버튼으로 두지 않는다 — 두면 창 어디서든 Enter가 확인을 누른다. autoDefault도
        # 끈다(QDialog는 autoDefault 버튼을 스스로 기본 버튼으로 올린다). 초점이 있는 버튼의
        # Enter는 keyPressEvent가 받는다
        for button in (self.okButton, self.cancelButton):
            button.setAutoDefault(False)
            button.setDefault(False)
        self.buttonBox.accepted.connect(self.accept)
        self.buttonBox.rejected.connect(self.reject)
        layout.addWidget(self.buttonBox)

    def retranslateUi(self) -> None:
        """고정 글자를 넣는다. 확인 · 취소는 Qt 자체 카탈로그가 번역한다(표준 버튼)."""
        self.setWindowTitle(self.tr("Sections"))
        self.addButton.setText(self.tr("+ Add section"))
        self.waitHintLabel.setText(
            self.tr("A download is waiting for this card — it continues when you close this window")
        )

    def setWaitingHint(self, waiting: bool) -> None:
        """받는 중인 배치가 이 카드를 기다리는지에 맞춰 아래쪽 안내를 켜고 끈다."""
        self.waitHintLabel.setVisible(waiting)

    # ---- 그리기 ----

    def _onStateChanged(self) -> None:
        """조회 상태에 맞춰 안내와 입력 영역을 바꾼다."""
        state = self._viewmodel.state
        ready = state == STATE_READY
        if state == STATE_LOADING:
            self.statusLabel.setText(self._viewmodel.loadingText())
        elif state == STATE_FAILED:
            self.statusLabel.setText(self._viewmodel.failureText())
        status = {STATE_LOADING: "info", STATE_FAILED: "error"}.get(state, "")
        if self.statusLabel.property("status") != status:
            self.statusLabel.setProperty("status", status)
            theme.repolish(self.statusLabel)
        self.statusLabel.setVisible(not ready)
        for widget in (self.headerLabel, self.addButton, self.scrollArea, self.hintLabel):
            widget.setVisible(ready)
        self._refresh()
        self._placeFirstFocus()

    # ---- 끝을 맞췄다는 안내 ----

    def _onEndClamped(self, _row: int) -> None:
        """뷰모델이 값을 맞췄다(영상 끝 · 칸의 최대값) — 이 조작이 끝난 뒤의 다음 조작부터 안내를 내릴 수 있다."""
        self._clampNoticeArmed = False
        self._armTimer.start()

    def _armClampNotice(self) -> None:
        self._clampNoticeArmed = True

    def clampNoticeArmed(self) -> bool:
        """끝을 맞췄다는 안내가 떠 있고, 다음 조작이 그것을 내리는지."""
        return self._clampNoticeArmed and self._viewmodel.clampedRow() is not None

    def _onInput(self) -> None:
        """창 안에 클릭 · 키 입력 하나가 왔다 — 끝을 맞췄다는 안내를 내린다. 그 조작은 그대로 처리된다.

        입력 칸(누르기 · 키)과 창의 버튼, 창의 빈 자리(누르기 · 칸이 받지 않은 키)가 부른다. 입력
        칸은 그 입력을 처리하기 전에 알린다. 맞추게 만든 조작(칸을 떠나게 한 클릭 · Tab · Enter ·
        붙여넣기)으로는 내리지 않는다 — 그 조작의 처리가 끝난 뒤에야 내릴 수 있게 된다
        (``_armClampNotice``).

        앱 전체의 이벤트를 지켜보지 않는다 — 파이썬으로 받는 앱 이벤트 필터는 사라지는 중인
        위젯의 이벤트까지 받아 프로세스를 죽일 수 있다.
        """
        if not self._clampNoticeArmed or not isValid(self._viewmodel):
            return
        self._clampNoticeArmed = False
        self._viewmodel.clearClampNotice()

    def mousePressEvent(self, event) -> None:
        """창의 빈 자리 · 글자를 눌렀다 — 입력 하나로 센다."""
        self._onInput()
        super().mousePressEvent(event)

    def _commitFocused(self) -> None:
        """치고 있는 칸의 편집을 끝낸다 — 포커스를 옮기지 않는 버튼(확인 · 구간 추가 · 행의 버튼)이 부른다."""
        focused = self.focusWidget()
        if isinstance(focused, TimecodeEdit):
            focused.commit()

    def _rowAction(self, action, row: int, *args) -> None:
        """행의 버튼 — 치고 있는 칸의 편집을 끝낸 뒤 뷰모델의 행 조작을 부른다."""
        self._onInput()
        self._commitFocused()
        action(row, *args)

    def _onDeleteOrClear(self, row: int) -> None:
        """행의 ✕ — 행이 둘 이상이면 그 행을 지우고, 하나뿐이면 그 행의 값을 비운다(행은 남는다).

        값을 비운 뒤에는 그 행의 시작 시분초 칸으로 간다 — 다시 넣기 시작할 자리다.
        """
        if self._viewmodel.canRemove():
            self._rowAction(self._viewmodel.removeRow, row)
            return
        self._onInput()
        self._commitFocused()
        if not self._viewmodel.canClear():
            return
        self._viewmodel.clearRow(row)
        if self._rows:
            self._rows[0].startEdit.clockEdit.setFocus(Qt.FocusReason.TabFocusReason)

    def _onAdd(self) -> None:
        """구간 추가 — 빈 행을 끝에 넣고 그 행의 시작 시분초 칸으로 간다."""
        self._onInput()
        self._commitFocused()
        before = len(self._viewmodel.rows)
        self._viewmodel.addRow()
        if len(self._viewmodel.rows) > before and self._rows:
            self._rows[-1].startEdit.clockEdit.setFocus(Qt.FocusReason.TabFocusReason)

    def _rebuildRows(self) -> None:
        """행 위젯을 뷰모델의 행 수 · 순서대로 다시 만든다."""
        self._endDrag()  # 끌던 행이 사라진다 — 끌기를 버린다
        old, self._rows = self._rows, []  # 먼저 비운다 — 사라지는 칸의 알림을 받지 않는다
        self._typing = None
        self._hoverRow = None  # 칠한 행도 사라진다 — 새 행 가운데 포인터 밑 행을 다음 턴에 맞춘다
        self._rowContainer.setHoverRow(None)
        for row in old:
            self._rowLayout.removeWidget(row)
            row.hide()  # 부모를 떼지 않는다 — 떼면 파괴될 때까지 최상위 창이 된다
            row.deleteLater()
        # 빈 끝 칸에 흐리게 깔 값 — 영상의 끝 타임코드(머리줄의 것과 같다). 빈 시작 칸은 00:00:00:00
        end_text = self._viewmodel.endTimecodeText()
        for index in range(len(self._viewmodel.rows)):
            row = SectionRow(self._rowContainer)
            if end_text:
                row.endEdit.setEmptyText(end_text)
            for column in (START, END):
                edit = row.edit(column)
                edit.edited.connect(
                    lambda text, r=index, c=column, e=edit: self._onEdited(r, c, text, e)
                )
                edit.committed.connect(lambda r=index, c=column, e=edit: self._onCommitted(r, c, e))
                edit.entered.connect(lambda r=index, c=column: self._onEntered(r, c))
                edit.pasted.connect(lambda r=index, c=column, e=edit: self._onPasted(r, c, e))
                edit.touched.connect(self._onInput)
                # Alt+↑/↓ — 칸마다 거는 필터다(앱 전체 필터가 아니다)
                for part in (edit.clockEdit, edit.frameEdit):
                    part.installEventFilter(self._moveKeys)
            row.handle.dragStarted.connect(lambda r=row: self._beginDrag(r))
            row.handle.dragMoved.connect(self._dragMove)
            row.handle.dropped.connect(self._dropAt)
            row.deleteButton.clicked.connect(lambda _=False, r=index: self._onDeleteOrClear(r))
            row.hoverChanged.connect(lambda on, r=row: self._onRowHover(r, on))
            self._rowLayout.insertWidget(index, row)
            # 바로 보이게 한다 — 레이아웃은 아직 안 보인 행을 건너뛰어, 그대로면 옮긴 행의 자리를
            # 이번 차례에 잴 수 없다(_revealRow). 창이 숨어 있으면 창이 보일 때 함께 보인다
            row.show()
            self._rows.append(row)
            install_tips(row)  # 새 행의 손잡이 · ✕ 툴팁도 말풍선으로(#550)
        # 목록의 값은 _refresh가 칸에 넣는다 — 빈 시각(빈 글)이 아닌 값은 새 칸의 값과 달라
        # 언제나 들어가고, 넣은 값은 0이어도 전부 밝게 보인다
        self._refresh()
        self._fitRowWidth()
        self._placeFirstFocus()
        self._scheduleRehover()

    # ---- 행 호버(#586) ----
    # Qt는 포인터가 움직일 때만 들어감 · 나감을 보낸다. 행을 다시 만들거나(놓기 · Alt+↑/↓ · 추가 · 삭제)
    # 목록을 굴리면 포인터 밑의 행이 바뀌어도 알림이 없다 — 그 뒤 포인터 밑 행을 다시 맞춘다

    def _hoverAllowed(self) -> bool:
        """지금 행 호버를 칠해도 되는지 — 끄는 중이 아니고 창이 보이며 활성이어야 한다."""
        return self._drag is None and self.isVisible() and self.isActiveWindow()

    def _setHoverRow(self, row: SectionRow | None) -> None:
        """호버 띠를 그 행 하나에만 칠한다 — None이면 모두 끈다."""
        if row is self._hoverRow:
            return
        previous, self._hoverRow = self._hoverRow, row
        if previous is not None and isValid(previous):
            previous.setHovered(False)
        if row is not None:
            row.setHovered(True)
        self._rowContainer.setHoverRow(row)

    def hoverRow(self) -> SectionRow | None:
        """호버 바탕을 칠한 행 — 없으면 None."""
        return self._hoverRow

    def _onRowHover(self, row: SectionRow, entered: bool) -> None:
        """행의 들어감 · 나감 — 끄는 중 · 창이 비활성이면 칠하지 않는다."""
        if row not in self._rows:
            return  # 다시 만들며 사라지는 행이다
        if entered:
            if self._hoverAllowed():
                self._setHoverRow(row)
        elif self._hoverRow is row:
            self._setHoverRow(None)

    def _scheduleRehover(self) -> None:
        """호버 다시 맞추기를 다음 이벤트 루프 턴에 한 번 돌린다 — 한 조작에서 여러 번 불려도 한 번."""
        if not self._rehoverPending:
            self._rehoverPending = True
            QTimer.singleShot(0, self, self._rehover)

    def _rehover(self) -> None:
        """포인터 밑의 행을 칠하고 그 밖의 행은 끈다 — 끄는 중 · 창이 비활성이면 모두 끈다."""
        self._rehoverPending = False
        target = None
        if self._hoverAllowed():
            under = _pointer.widget_under_pointer()
            while under is not None and not isinstance(under, SectionRow):
                under = under.parentWidget()
            if under in self._rows:
                target = under
        self._setHoverRow(target)

    def changeEvent(self, event: QEvent) -> None:
        """창이 비활성이 되면 행 호버를 끄고, 다시 활성이 되면 포인터 밑 행으로 맞춘다."""
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange:
            if self.isActiveWindow():
                self._scheduleRehover()
            else:
                self._setHoverRow(None)

    def _placeFirstFocus(self) -> None:
        """창을 연 뒤 첫 초점을 한 번 둔다 — 열자마자 친 Enter가 창을 닫지 않게(#586).

        행이 있으면(조회가 끝남) 첫 행의 시작 시분초 칸이다. Tab으로 들어간 것과 같다 — 칸은 초점을 받으면
        글 전체를 고른 상태가 된다. 조회 중 · 조회 실패면 창 자신에 둔다 — 버튼에 두면 Enter가 그 버튼을
        누른다(취소면 창이 닫힌다). 창 자신의 Enter는 아무 일도 하지 않고(``keyPressEvent``) Esc는 취소다.
        조회 중이었으면 조회가 끝나 행이 생길 때 다시 불려 첫 행 칸으로 옮긴다.

        창이 보일 때(``showEvent``) · 조회 상태가 바뀔 때 · 행을 다시 만들 때 부른다. 첫 행 칸에 두면
        그 뒤로는 아무것도 하지 않는다.
        """
        if not self._firstFocusPending or not self.isVisible():
            return
        if self._viewmodel.state == STATE_READY and self._rows:
            self._firstFocusPending = False
            self._rows[0].startEdit.clockEdit.setFocus(Qt.FocusReason.TabFocusReason)
        else:
            self.setFocus(Qt.FocusReason.OtherFocusReason)

    def _revealRow(self, index: int) -> None:
        """그 행이 목록의 보이는 범위에 다 들어오게 스크롤한다 — 이미 다 보이면 움직이지 않는다(#586).

        행을 옮긴 뒤(끌어 놓기 · Alt+↑/↓) 부른다. 행 위젯은 방금 다시 만들어졌으므로 레이아웃을 먼저
        돌려 자리를 정한다.
        """
        if not 0 <= index < len(self._rows):
            return
        self._rowLayout.activate()
        self.scrollArea.ensureWidgetVisible(self._rows[index], 0, 0)

    def _fitRowWidth(self) -> None:
        """스크롤 영역의 최소 폭을 행 하나가 다 들어가는 폭으로 둔다 — 창이 그보다 좁아지지 않는다.

        스크롤 영역은 안에 든 위젯의 최소 폭을 밖으로 알리지 않는다. 가로 스크롤이 없어 그대로
        두면 창을 가장 좁게 줄였을 때 행의 오른쪽 끝(✕ 버튼)이 잘린다 (#309). 행 컨테이너가
        아니라 행에 묻는다 — 방금 만든 행은 아직 보이기 전이라 컨테이너의 최소 폭에 들지 않는다.
        """
        if not self._rows:
            return
        frame = 2 * self.scrollArea.frameWidth()
        row = self._rows[0].minimumSizeHint().width()
        self.scrollArea.setMinimumWidth(row + self._rowSideWidth + frame)

    def _isCurrent(self, row: int, column: int, edit: TimePointEdit) -> bool:
        """그 시각 입력이 지금 그 (행, 칸)에 놓인 것인지.

        행을 다시 만들면(추가 · 삭제 · 순서 바꾸기) 앞의 행 위젯은 숨겨져 사라진다. 그 가운데
        포커스가 있던 칸은 숨겨지며 포커스를 잃어 편집 끝을 알리는데, 그 칸이 알던 행 번호에는
        이제 다른 구간이 있다 — 그 알림을 받아 쓰면 옮겨 간 행의 값이 덮인다.
        """
        return row < len(self._rows) and self._rows[row].edit(column) is edit

    def _onEdited(self, row: int, column: int, text: str, edit: TimePointEdit) -> None:
        """칸의 숫자가 바뀌었다 — 값을 넘겨 다시 검증한다. 이 칸을 치는 중이라고 적어 둔다."""
        if not isValid(self._viewmodel) or not self._isCurrent(row, column, edit):
            return
        self._typing = (row, column)
        self._viewmodel.setText(row, column, text, False)

    def _onCommitted(self, row: int, column: int, edit: TimePointEdit) -> None:
        """그 시각의 편집이 끝났다(두 칸을 모두 떠남 · Enter) — 미뤄 둔 오류를 띄운다."""
        if not isValid(self._viewmodel):
            return  # 창이 닫히며 칸이 포커스를 잃었다 — 뷰모델은 이미 사라졌다
        if not self._isCurrent(row, column, edit):
            return  # 행을 다시 만들며 사라지는 칸이다 — 그 번호의 행은 이제 다른 구간이다
        if self._typing == (row, column):
            self._typing = None
        self._viewmodel.setText(row, column, edit.text())
        self._showSettled(row, column, edit)

    def _showSettled(self, row: int, column: int, edit: TimePointEdit) -> None:
        """편집을 끝낸 칸의 값이 뷰모델에서 바뀌었으면(영상 끝 · 칸의 최대값으로 맞춤) 칸에 그 값을 넣는다.

        표시를 맞추는 쪽(``_refresh``)은 포커스가 있는 칸을 건드리지 않는다 — Enter · 붙여넣기는
        포커스가 그 칸에 있는 채로 편집을 끝내므로 여기서 넣는다. 넣은 값은 전부 밝게 보인다.
        """
        settled = self._viewmodel.rows[row][column]
        if edit.text() != settled:
            edit.setText(settled)

    def _onPasted(self, row: int, column: int, edit: TimePointEdit) -> None:
        """칸에 글을 붙여넣었다 — 넘는 값(칸의 최대값 · 영상의 끝)을 떠나기를 기다리지 않고 바로 맞춘다."""
        if not isValid(self._viewmodel) or not self._isCurrent(row, column, edit):
            return
        if self._viewmodel.settle(row, column):
            self._showSettled(row, column, edit)

    def _onEntered(self, row: int, column: int) -> None:
        """칸에서 Enter를 쳤다 — 다음 칸으로 포커스를 옮긴다. 창을 닫지 않는다."""
        self.focusTargetAfter(row, column).setFocus(Qt.FocusReason.TabFocusReason)

    def focusTargetAfter(self, row: int, column: int) -> QWidget:
        """그 시각에서 Enter를 친 뒤 포커스를 받을 위젯.

        시작 → 끝 → 다음 행의 시작, 마지막 행의 끝 → 확인 버튼. 시각 안에서는
        ``NEXT_AFTER_ENTER``가 정한 칸으로 간다 — 기본은 시분초 칸이다(프레임 칸을 건너뛴다).
        시분초 칸에서 친 Enter가 프레임 칸을 지나게 하려면 그 값을 "frame"으로 바꾼다.
        """
        current = self._rows[row].edit(column)
        if NEXT_AFTER_ENTER == PART_FRAME and current.clockEdit.hasFocus():
            return current.frameEdit
        if column == START:
            return self._rows[row].endEdit.clockEdit
        if row + 1 < len(self._rows):
            return self._rows[row + 1].startEdit.clockEdit
        return self.okButton

    def _refresh(self) -> None:
        """행의 글자 · 오류 표시와 머리줄 · 버튼 상태를 뷰모델에 맞춘다."""
        viewmodel = self._viewmodel
        count = len(self._rows)
        for index, row in enumerate(self._rows):
            if index >= len(viewmodel.rows):
                break
            # 걸러 낸 행은 번호가 없다 — 남은 행끼리 1부터 잇는다(파일 이름의 번호)
            number = viewmodel.rowNumber(index)
            row.numberLabel.setText("" if number is None else str(number))
            # 치고 있는 행의 오류는 표(ERROR_TIMING)가 정한 때에 띄운다
            error = viewmodel.shownErrorText(index, self._typing)
            flagged = viewmodel.shownErrorParts(index, self._typing)
            for column in (START, END):
                edit = row.edit(column)
                text = viewmodel.rows[index][column]
                # 입력 중인 시각은 건드리지 않는다 — 고쳐 쓴 표기는 편집을 끝낸 뒤에 넣는다
                if not edit.hasEditFocus() and edit.text() != text:
                    edit.setText(text)
                # 칸에는 툴팁을 두지 않는다 — 입력법은 목록 아래 안내 한 줄이다(설계 §12.1)
                for part, widget in ((PART_CLOCK, edit.clockEdit), (PART_FRAME, edit.frameEdit)):
                    # 오류가 난 칸만 붉게 — 초 · 분 넘침은 시분초 칸, 프레임 넘침은 프레임 칸,
                    # 시각 전체의 오류는 그 시각의 두 칸
                    self._setFlag(widget, "invalid", (column, part) in flagged)
                # 묶음에도 적어 둔다 — 그 시각의 어느 칸이든 칠해졌는지(스타일에는 쓰지 않는다)
                edit.setProperty("invalid", any(key[0] == column for key in flagged))
            row.infoLabel.setText(viewmodel.lengthText(index))
            ignored = viewmodel.isIgnored(index)
            row.showMessage(
                {
                    MESSAGE_ERROR: error,
                    MESSAGE_NOTICE: viewmodel.noticeText(
                        index, self._typing, self._fullParts(index)
                    ),
                    MESSAGE_NOTE: viewmodel.ignoredText() if ignored else "",
                }
            )
            # 하나뿐인 행은 지울 수 없다 — 값이 있으면 ✕가 그 값을 비운다(비어 있으면 꺼 둔다)
            row.deleteButton.setEnabled(viewmodel.canRemove() or viewmodel.canClear())
            row.deleteButton.setToolTip(
                self.tr("Delete section") if count > 1 else self.tr("Clear values")
            )
            # 행이 하나뿐이면 옮길 곳이 없다 — 손잡이를 숨긴다(자리는 남는다)
            row.handle.setVisible(count > 1)
            row.handle.setToolTip(self.tr("Reorder · {0}").format(move_keys_text()))
        if self._hoverRow is not None:
            self._rowContainer.update()  # 안내 글이 바뀌면 호버 띠의 아래 끝도 바뀐다
        self.headerLabel.setText(viewmodel.headerText())
        ready = viewmodel.state == STATE_READY
        self.hintLabel.setText(
            self.tr(
                "Digits fill from the right · '.' goes to the frame field · empty = start/end of the video · up to {0} sections"
            ).format(viewmodel.maxSections())
            if ready
            else ""
        )
        self.addButton.setEnabled(viewmodel.canAdd())
        notice = viewmodel.totalNoticeText()
        self.totalLabel.setLines(*viewmodel.totalNoticeLines())
        total = "" if notice else viewmodel.totalText()
        self.sumLabel.setText(total)
        self.sumLabel.setVisible(bool(total))
        # 끝 칸을 치는 중 끝만 영상 끝을 넘은 것은 확인을 막지 않는다 — 확인이 그 칸을 맞춘다
        self.okButton.setEnabled(viewmodel.canCommit(self._typing))

    def _fullParts(self, index: int) -> frozenset[str]:
        """치고 있는 시각이 그 행의 것이면, 그 시각에서 자리를 다 채운 칸. 아니면 빈 집합."""
        if self._typing is None or self._typing[0] != index:
            return frozenset()
        typed = self._rows[index].edit(self._typing[1])
        parts = ((PART_CLOCK, typed.clockEdit), (PART_FRAME, typed.frameEdit))
        return frozenset(name for name, part in parts if part.isFull())

    @staticmethod
    def _setFlag(widget: QWidget, name: str, on: bool) -> None:
        """동적 속성을 바꾸고 스타일을 다시 입힌다 — 색은 전역 QSS가 고른다."""
        if widget.property(name) != on:
            widget.setProperty(name, on)
            theme.repolish(widget)

    # ---- 순서 바꾸기(#586) ----

    def moveRowTo(self, row: int, target: int) -> None:
        """row 행을 target 자리로 옮긴다 — 손잡이 끌기와 Alt+↑/↓가 부른다.

        치고 있는 칸의 편집을 먼저 끝낸다(그 값도 행과 함께 옮겨 간다). 초점이 시각 칸에 있었으면
        옮긴 뒤에도 **같은 구간의 같은 칸**에 둔다 — 그 칸이 든 행이 옮겨 간 자리를 따라간다. 행 위젯은
        뷰모델의 ``rowsReset``으로 다시 만들어진다.
        """
        count = len(self._rows)
        if row == target or not (0 <= row < count and 0 <= target < count):
            return
        self._onInput()
        self._commitFocused()
        focused = self._partAt(self.focusWidget())
        if focused is not None:
            # 초점을 창에 잠시 맡긴다 — 맡기지 않으면 행을 다시 만들며 숨는 칸이 초점을 Tab 차례의 다음
            # 칸으로 넘기고, 스크롤 영역이 그 칸 쪽으로 스크롤한다(이미 보이던 행인데 목록이 움직인다)
            self.setFocus(Qt.FocusReason.OtherFocusReason)
        self._viewmodel.moveRow(row, target)
        self._revealRow(target)  # 옮겨 간 행이 보이게 — 초점이 그 행의 칸으로 간다
        if focused is None:
            return
        at, column, part = focused
        if at == row:
            at = target
        elif row < at <= target:
            at -= 1
        elif target <= at < row:
            at += 1
        edit = self._rows[at].edit(column)
        widget = edit.clockEdit if part == PART_CLOCK else edit.frameEdit
        widget.setFocus(Qt.FocusReason.OtherFocusReason)

    def _partAt(self, widget: QWidget | None) -> tuple[int, int, str] | None:
        """그 위젯이 시각 칸이면 (행, 칸 번호, 시분초 · 프레임). 아니면 None."""
        if widget is None:
            return None
        for index, row in enumerate(self._rows):
            for column in (START, END):
                edit = row.edit(column)
                if widget is edit.clockEdit:
                    return index, column, PART_CLOCK
                if widget is edit.frameEdit:
                    return index, column, PART_FRAME
        return None

    def _onMoveKey(self, watched: QObject, event: QEvent) -> bool:
        """시각 칸의 Alt+↑/↓ — 그 행을 한 칸 위 · 아래로 옮긴다. 키를 받았으면 True.

        맨 위 · 맨 아래에서는 아무 일도 없다(키는 받는다 — 칸이 커서를 옮기지 않게). 다른 수정 키가
        함께 눌렸으면 필터가 여기로 넘기지 않는다(``is_move_key``). 끄는 중에는 옮기지 않는다.
        """
        where = self._partAt(watched)
        if where is None:
            return False
        row = where[0]
        target = row + (-1 if event.key() == Qt.Key.Key_Up else 1)
        if self._drag is None and 0 <= target < len(self._rows):
            self.moveRowTo(row, target)
        return True

    def isDragging(self) -> bool:
        """손잡이로 행을 끄는 중인지."""
        return self._drag is not None

    def _beginDrag(self, row: SectionRow) -> None:
        """손잡이를 끌기 시작했다 — 끄는 행을 흐리게 하고 저절로 스크롤을 켠다.

        흐리게 하는 효과 객체는 끄는 행 하나에만 붙였다가 끌기가 끝나면 뗀다(행마다 붙이지 않는다).
        """
        if row not in self._rows or self._drag is not None:
            return
        self._onInput()
        self._commitFocused()
        # 끄는 동안 행 호버를 끈다 — 들어갈 자리 선과 흐려진 행만 보인다(_hoverAllowed가 다시 켜지 않게 막는다)
        self._setHoverRow(None)
        effect = QGraphicsOpacityEffect(row)
        effect.setOpacity(DRAG_OPACITY)
        row.setGraphicsEffect(effect)
        index = self._rows.index(row)
        self._drag = {"row": row, "from": index, "gap": index, "pos": None}
        self.dropLine.setFixedWidth(self._rowContainer.width())
        self.dropLine.raise_()
        self._dragTimer.start()

    def dropGap(self, global_pos: QPoint) -> int:
        """전역 위치가 가리키는 틈 — 0은 맨 위 행 앞, 행 수는 맨 끝. 행 가운데보다 위면 그 행 앞이다."""
        y = self._rowContainer.mapFromGlobal(global_pos).y()
        for index, row in enumerate(self._rows):
            if y < row.y() + row.height() / 2:
                return index
        return len(self._rows)

    def _dragMove(self, global_pos: QPoint) -> None:
        """끄는 중 포인터가 움직였다 — 들어갈 틈을 다시 정하고 그 자리에 선을 긋는다.

        옮겨도 그대로인 틈(끄는 행의 바로 위 · 아래)에는 선을 긋지 않는다.
        """
        drag = self._drag
        if drag is None or not self._rows:
            return
        drag["pos"] = global_pos
        gap = self.dropGap(global_pos)
        drag["gap"] = gap
        last = self._rows[-1]
        y = self._rows[gap].y() if gap < len(self._rows) else last.y() + last.height()
        # 선은 행 목록 안에 다 들어오게 — 맨 끝 틈에서 바닥에 반쯤 걸치면 잘린다
        top = min(max(0, y - DRAG_LINE // 2), self._rowContainer.height() - DRAG_LINE)
        self.dropLine.move(0, top)
        self.dropLine.setVisible(gap not in (drag["from"], drag["from"] + 1))

    def _dragAutoScroll(self) -> None:
        """끄는 중 포인터가 목록 위 · 아래 끝(``DRAG_EDGE`` 안)에 있으면 그쪽으로 조금 스크롤한다."""
        drag = self._drag
        if drag is None or drag["pos"] is None:
            return
        viewport = self.scrollArea.viewport()
        y = viewport.mapFromGlobal(drag["pos"]).y()
        bar = self.scrollArea.verticalScrollBar()
        if y < DRAG_EDGE:
            bar.setValue(bar.value() - DRAG_SCROLL_STEP)
        elif y > viewport.height() - DRAG_EDGE:
            bar.setValue(bar.value() + DRAG_SCROLL_STEP)
        else:
            return
        self._dragMove(drag["pos"])

    def _endDrag(self) -> dict | None:
        """끌기를 끝낸다 — 선 · 흐림 · 저절로 스크롤을 거둔다. 끌던 것이 없으면 None."""
        drag, self._drag = self._drag, None
        self._dragTimer.stop()
        self.dropLine.hide()
        if drag is not None and isValid(drag["row"]):
            drag["row"].setGraphicsEffect(None)
        if drag is not None:
            self._scheduleRehover()  # 놓거나 버린 뒤 포인터 밑 행을 다시 칠한다
        return drag

    def _dropAt(self, global_pos: QPoint) -> None:
        """놓았다 — 창 밖이거나 같은 자리면 그대로, 아니면 그 틈으로 옮기고 번호를 다시 매긴다."""
        if self._drag is None:
            return
        self._dragMove(global_pos)
        drag = self._endDrag()
        if not self.frameGeometry().contains(global_pos):
            return
        source, gap = drag["from"], drag["gap"]
        target = gap if gap <= source else gap - 1
        if target != source:
            self.moveRowTo(source, target)

    def _cancelDrag(self) -> None:
        """끄는 중 Esc — 끌기만 버린다. 행은 그대로이고 창은 닫지 않는다."""
        drag = self._endDrag()
        if drag is not None and isValid(drag["row"]):
            drag["row"].handle.cancel()

    # ---- 닫기 ----

    def showEvent(self, event: QShowEvent) -> None:
        """보일 때 버튼 상자가 확인을 기본 버튼으로 올린 것을 되돌린다.

        ``QDialogButtonBox``는 처음 보일 때 기본 버튼이 없으면 첫 AcceptRole 버튼(확인)을 기본 버튼으로
        올린다 — 기본 버튼이면 창 어디서든 Enter가 확인을 누를 수 있고, 기본 버튼 모양이 된다. 자식이 먼저
        보이므로 여기서 되돌린다.
        """
        super().showEvent(event)
        for button in (self.okButton, self.cancelButton):
            button.setDefault(False)
        self._placeFirstFocus()

    def keyPressEvent(self, event) -> None:
        """Enter는 초점이 있는 버튼만 누른다. 그 밖의 Enter는 아무것도 하지 않는다.

        QDialog는 Enter를 받으면 기본 버튼을 누른다 — 창 어디서든 Enter가 창을 닫게 된다. 그 길을
        막는다(카드 편집 창과 같은 규칙). 끄는 중의 Esc는 끌기만 버린다. 그 밖의 Esc(취소)를 비롯한
        다른 키는 QDialog에 맡긴다.
        """
        if event.key() == Qt.Key.Key_Escape and self._drag is not None:
            self._cancelDrag()
            event.accept()
            return
        self._onInput()  # 칸이 받지 않은 키 — 입력 하나로 센다
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            focused = self.focusWidget()
            if isinstance(focused, QAbstractButton) and focused.isEnabled():
                focused.click()
            event.accept()
            return
        super().keyPressEvent(event)

    def accept(self) -> None:
        """구간을 카드에 쓰고 닫는다. 오류가 남아 있으면 닫지 않는다.

        카드가 그사이 대기 상태가 아니게 됐으면 쓰지 않고 닫는다(뷰모델의 ``commit``이 판정한다).
        """
        self._commitFocused()  # Enter 없이 확인을 누른 칸의 편집을 끝낸다
        if not self._viewmodel.canCommit():
            return
        if self._viewmodel.commit():
            super().accept()
        else:
            super().reject()
