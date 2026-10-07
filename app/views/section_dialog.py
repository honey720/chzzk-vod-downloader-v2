"""구간 편집 창 — 시간 입력으로 받을 구간을 정한다 (#309).

카드 하나의 구간 목록을 편집하는 모달 창이다. 대기 상태 카드의 구간 요약(재생 시간 자리)을
누르면 열린다. 타임라인 · 미리보기는 이 창의 위쪽에 나중에 붙는다 — 시간 입력 영역은 그때도
항상 보인다.

창은 뷰모델(``app/viewmodels/section_edit_viewmodel.py``)만 본다. 해석 · 검증 · 카드에
쓰기는 뷰모델이 하고, 창은 글자를 넘기고 결과를 그린다.

화면은 조회 상태에 따라 셋이다.

| 상태 | 보이는 것 | 확인 버튼 |
|---|---|---|
| 조회 중 | "영상 정보를 읽는 중" 한 줄 | 꺼짐 |
| 조회 실패 | 실패 안내 | 꺼짐 |
| 준비됨 | 머리줄(구간 수 · 프레임률) · 구간 행 · 구간 추가 | 오류가 없을 때만 켜짐 |

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

**Enter는 창을 닫지 않는다** — 창은 확인 버튼으로만 닫는다(확인 버튼에 포커스가 있을 때의
Enter · Space 포함). 그래서 창에 기본 버튼을 두지 않는다. Esc는 취소로 닫는다.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from shiboken6 import isValid

import app.theme as theme
from app.viewmodels.section_edit_viewmodel import (
    END,
    PART_CLOCK,
    PART_FRAME,
    START,
    STATE_FAILED,
    STATE_LOADING,
    STATE_READY,
    SectionEditViewModel,
)
from app.widgets.eliding_label import ElidingLabel
from app.widgets.scroll_padding import ScrollBarPadding
from app.widgets.timecode_edit import TimecodeEdit, TimePointEdit

# 칸의 폭을 재는 본보기 글자 — 칸에 깔리는 글자 그대로다
_CLOCK_SAMPLE = "00:00:00"
_FRAME_SAMPLE = "00"
_EDIT_PADDING = 22  # 칸의 글자 양옆 몫(px) — QSS의 padding과 테두리, 커서 한 줄
# Enter를 친 뒤 포커스가 갈 곳 — "clock"이면 다음 시각의 시분초 칸(프레임 칸을 건너뛴다),
# "frame"이면 같은 시각의 프레임 칸을 먼저 지난다. 바꾸기 쉽게 한 곳에 둔다
NEXT_AFTER_ENTER = "clock"
# 행 아래 안내 줄에 보이는 글의 종류 — SectionRow.showMessage가 우선순위대로 하나만 보인다
MESSAGE_ERROR = "error"  # 오류 — 그 행을 확인할 수 없다
MESSAGE_NOTE = "note"  # 무시 안내 — 완전히 빈 행
_INITIAL_SIZE = (560, 420)  # 창의 첫 크기(px) — 행 일곱 개쯤이 스크롤 없이 보인다
# 행 사이의 간격(px). 행마다 아래에 안내 줄 한 줄이 늘 서 있어 그 줄이 행 사이를 띄운다 —
# 간격을 따로 두면 20행의 높이가 안내 줄만큼 더 길어진다
_ROW_SPACING = 0
_MESSAGE_GAP = 2  # 칸의 줄과 그 아래 안내 줄 사이(px)


class SectionRow(QWidget):
    """구간 한 행 — 번호 · [시작 시분초][프레임] ~ [끝 시분초][프레임] · 길이 · 위 · 아래 · 삭제,
    그 아래 안내 줄 한 줄.

    안내 줄(``messageSlot``)은 글이 없어도 **늘 한 줄의 높이를 차지한다** — 오류가 나고
    사라져도, 안내가 바뀌어도 행의 높이가 같아 아래 행들이 오르내리지 않는다. 오류 · 무시
    안내가 이 한 줄을 함께 쓰고 한 번에 하나만 보인다(``showMessage``). 줄바꿈하지 않는다 —
    넘치는 글은 말줄임하고 전문은 툴팁에 둔다(``ElidingLabel``).
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(_MESSAGE_GAP)
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        column.addLayout(layout)

        self.numberLabel = QLabel(self)
        self.numberLabel.setObjectName("sectionNumberLabel")
        self.numberLabel.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.numberLabel.setMinimumWidth(self.numberLabel.fontMetrics().horizontalAdvance("00"))
        layout.addWidget(self.numberLabel)

        self.startEdit = self._timePointEdit("sectionStartEdit")
        self.rangeLabel = QLabel("~", self)  # 시작과 끝 사이 — 번역하지 않는 기호다
        self.rangeLabel.setObjectName("sectionRangeLabel")
        self.endEdit = self._timePointEdit("sectionEndEdit")
        layout.addWidget(self.startEdit)
        layout.addWidget(self.rangeLabel)
        layout.addWidget(self.endEdit)

        # 구간 길이(밀리초 표기) — 좁으면 말줄임하고 전문은 툴팁이다(ElidingLabel)
        self.infoLabel = ElidingLabel(self)
        self.infoLabel.setObjectName("sectionInfoLabel")
        layout.addWidget(self.infoLabel, 1)

        self.upButton = self._rowButton("▲", "sectionUpButton")
        self.downButton = self._rowButton("▼", "sectionDownButton")
        self.deleteButton = self._rowButton("✕", "sectionDeleteButton")
        for button in (self.upButton, self.downButton, self.deleteButton):
            layout.addWidget(button)

        # 안내 줄 — 글이 없어도 한 줄의 높이를 차지한다. 번호 폭만큼 들여 칸의 왼쪽 끝에 맞춘다
        self.messageSlot = QWidget(self)
        self.messageSlot.setObjectName("sectionMessageSlot")
        slot = QHBoxLayout(self.messageSlot)
        slot.setContentsMargins(self.numberLabel.minimumWidth() + layout.spacing(), 0, 0, 0)
        slot.setSpacing(0)
        # 오류 문구 — 오류가 있을 때만 보인다
        self.errorLabel = self._messageLabel("sectionErrorLabel")
        # 걸러 낼 행의 안내 — 완전히 비어 있어 구간에 넣지 않는 행에만 보인다. 오류가 아니다
        self.noteLabel = self._messageLabel("sectionNoteLabel")
        # 우선순위가 높은 것부터 — 한 번에 하나만 보인다
        self._messages = ((MESSAGE_ERROR, self.errorLabel), (MESSAGE_NOTE, self.noteLabel))
        for _kind, label in self._messages:
            slot.addWidget(label, 1)
        # 높이는 글자에서 유도한다 — 안내 줄의 글꼴(QSS)이 입혀진 뒤의 한 줄 높이
        self.errorLabel.ensurePolished()
        self.messageSlot.setFixedHeight(self.errorLabel.fontMetrics().height())
        column.addWidget(self.messageSlot)

    def _messageLabel(self, name: str) -> ElidingLabel:
        label = ElidingLabel(self.messageSlot)
        label.setObjectName(name)
        label.setWordWrap(False)  # 한 줄 — 줄바꿈하면 행의 높이가 글 길이에 따라 달라진다
        label.setVisible(False)
        return label

    def showMessage(self, texts: dict[str, str]) -> None:
        """안내 줄에 글 하나를 보인다 — 종류(``MESSAGE_*``) → 글. 빈 글은 없는 것이다.

        둘 이상이 걸리면 우선순위가 가장 높은 것만 보인다: 오류 > 무시 안내. 아무것도 없으면
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
        button = QPushButton(text, self)
        button.setObjectName(name)
        button.setProperty("role", "subtle")
        button.setAutoDefault(False)  # 칸에서 Enter를 눌러도 이 버튼이 눌리지 않는다
        button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        return button

    def edit(self, column: int) -> TimePointEdit:
        """칸 번호(START · END)의 시각 입력 — 시분초 칸과 프레임 칸의 묶음."""
        return self.startEdit if column == START else self.endEdit


class SectionEditDialog(QDialog):
    """구간 편집 창. 뷰모델의 상태를 그리고 입력을 뷰모델에 넘긴다."""

    def __init__(self, viewmodel: SectionEditViewModel, parent: QWidget | None = None):
        super().__init__(parent)
        self._viewmodel = viewmodel
        self._rows: list[SectionRow] = []
        # 숫자를 치고 있는 (행, 칸) — 그 행의 오류 가운데 일부는 칸을 떠날 때 띄운다. 없으면 None
        self._typing: tuple[int, int] | None = None
        self.setObjectName("sectionEditDialog")
        self.setModal(True)
        self.setupUi()
        self.retranslateUi()
        viewmodel.stateChanged.connect(self._onStateChanged)
        viewmodel.rowsReset.connect(self._rebuildRows)
        viewmodel.validated.connect(self._refresh)
        self._onStateChanged()
        self._rebuildRows()
        self.resize(*_INITIAL_SIZE)

    def viewModel(self) -> SectionEditViewModel:
        """이 창이 보는 뷰모델."""
        return self._viewmodel

    def setupUi(self) -> None:
        """위젯을 만든다 — 글자는 ``retranslateUi``가 넣는다."""
        layout = QVBoxLayout(self)

        self.titleLabel = ElidingLabel(self)
        self.titleLabel.setObjectName("sectionTitleLabel")
        self.titleLabel.setText(self._viewmodel.item.title)
        layout.addWidget(self.titleLabel)

        # 조회 중 · 조회 실패 안내 — 준비되면 숨는다
        self.statusLabel = QLabel(self)
        self.statusLabel.setObjectName("sectionStatusLabel")
        self.statusLabel.setWordWrap(True)
        self.statusLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.statusLabel, 1)

        header = QHBoxLayout()
        self.headerLabel = QLabel(self)
        self.headerLabel.setObjectName("sectionHeaderLabel")
        header.addWidget(self.headerLabel, 1)
        self.addButton = QPushButton(self)
        self.addButton.setObjectName("sectionAddButton")
        self.addButton.setAutoDefault(False)
        self.addButton.clicked.connect(self._onAdd)
        header.addWidget(self.addButton)
        layout.addLayout(header)

        self.scrollArea = QScrollArea(self)
        self.scrollArea.setObjectName("sectionScrollArea")
        self.scrollArea.setWidgetResizable(True)
        self.scrollArea.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._rowContainer = QWidget(self.scrollArea)
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
        self._rowLayout.setContentsMargins(margins.left(), margins.top(), right, margins.bottom())
        self._scrollPadding = ScrollBarPadding(self.scrollArea, self._rowLayout, right)
        layout.addWidget(self.scrollArea, 1)

        # 받는 중인 배치가 이 카드를 기다릴 때만 보인다 — 창을 닫아야 이어 간다는 것을 알린다
        self.waitHintLabel = QLabel(self)
        self.waitHintLabel.setObjectName("sectionWaitHintLabel")
        self.waitHintLabel.setWordWrap(True)
        self.waitHintLabel.setVisible(False)
        layout.addWidget(self.waitHintLabel)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancelButton = QPushButton(self)
        self.cancelButton.setObjectName("sectionCancelButton")
        self.cancelButton.setAutoDefault(False)
        self.cancelButton.clicked.connect(self.reject)
        self.okButton = QPushButton(self)
        self.okButton.setObjectName("sectionOkButton")
        # 기본 버튼으로 두지 않는다 — 두면 창 어디서든 Enter가 확인을 누른다. autoDefault도
        # 끈다(QDialog는 autoDefault 버튼을 스스로 기본 버튼으로 올린다). 포커스가 이 버튼에
        # 있을 때의 Enter는 keyPressEvent가 받는다
        self.okButton.setAutoDefault(False)
        self.okButton.clicked.connect(self.accept)
        buttons.addWidget(self.cancelButton)
        buttons.addWidget(self.okButton)
        layout.addLayout(buttons)

    def retranslateUi(self) -> None:
        """고정 글자를 넣는다."""
        self.setWindowTitle(self.tr("Edit sections"))
        self.addButton.setText(self.tr("+ Add section"))
        self.cancelButton.setText(self.tr("Cancel"))
        self.okButton.setText(self.tr("OK"))
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
        for widget in (self.headerLabel, self.addButton, self.scrollArea):
            widget.setVisible(ready)
        self._refresh()

    def _onAdd(self) -> None:
        """구간 추가 — 빈 행을 끝에 넣고 그 행의 시작 시분초 칸으로 간다."""
        before = len(self._viewmodel.rows)
        self._viewmodel.addRow()
        if len(self._viewmodel.rows) > before and self._rows:
            self._rows[-1].startEdit.clockEdit.setFocus(Qt.FocusReason.TabFocusReason)

    def _rebuildRows(self) -> None:
        """행 위젯을 뷰모델의 행 수 · 순서대로 다시 만든다."""
        old, self._rows = self._rows, []  # 먼저 비운다 — 사라지는 칸의 알림을 받지 않는다
        self._typing = None
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
            row.upButton.clicked.connect(lambda _=False, r=index: self._viewmodel.moveRow(r, -1))
            row.downButton.clicked.connect(lambda _=False, r=index: self._viewmodel.moveRow(r, 1))
            row.deleteButton.clicked.connect(lambda _=False, r=index: self._viewmodel.removeRow(r))
            self._rowLayout.insertWidget(index, row)
            self._rows.append(row)
        # 목록의 값은 _refresh가 칸에 넣는다 — 빈 시각(빈 글)이 아닌 값은 새 칸의 값과 달라
        # 언제나 들어가고, 넣은 값은 0이어도 전부 밝게 보인다
        self._refresh()

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
            # 치고 있는 행의 오류는 표(ERROR_TIMING)가 정한 때에 띄운다. 프레임 칸에 두 자리를
            # 다 쳤으면 프레임 넘침은 바로 띄운다
            frame_full = False
            if self._typing is not None and self._typing[0] == index:
                typed = row.edit(self._typing[1])
                frame_full = typed.typingFrame() and typed.frameIsFull()
            error = viewmodel.shownErrorText(index, self._typing, frame_full)
            flagged = viewmodel.shownErrorParts(index, self._typing, frame_full)
            for column in (START, END):
                edit = row.edit(column)
                text = viewmodel.rows[index][column]
                # 입력 중인 시각은 건드리지 않는다 — 고쳐 쓴 표기는 편집을 끝낸 뒤에 넣는다
                if not edit.hasEditFocus() and edit.text() != text:
                    edit.setText(text)
                tooltip = viewmodel.millisecondsText(index, column)
                for part, widget in ((PART_CLOCK, edit.clockEdit), (PART_FRAME, edit.frameEdit)):
                    widget.setToolTip(tooltip)
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
                    MESSAGE_NOTE: viewmodel.ignoredText() if ignored else "",
                }
            )
            row.deleteButton.setEnabled(viewmodel.canRemove())  # 하나뿐인 행은 지울 수 없다
            row.upButton.setEnabled(index > 0)
            row.downButton.setEnabled(index < count - 1)
            row.upButton.setToolTip(self.tr("Move up"))
            row.downButton.setToolTip(self.tr("Move down"))
            row.deleteButton.setToolTip(self.tr("Delete section"))
        self.headerLabel.setText(viewmodel.headerText())
        self.headerLabel.setToolTip(viewmodel.endMillisecondsText())  # 영상 끝의 밀리초 표기
        self.addButton.setEnabled(viewmodel.canAdd())
        self.okButton.setEnabled(viewmodel.canCommit())

    @staticmethod
    def _setFlag(widget: QWidget, name: str, on: bool) -> None:
        """동적 속성을 바꾸고 스타일을 다시 입힌다 — 색은 전역 QSS가 고른다."""
        if widget.property(name) != on:
            widget.setProperty(name, on)
            theme.repolish(widget)

    # ---- 닫기 ----

    def keyPressEvent(self, event) -> None:
        """Enter는 확인 버튼에 포커스가 있을 때만 확인을 누른다. 그 밖의 Enter는 아무것도 하지 않는다.

        QDialog는 Enter를 받으면 기본 버튼을 누른다 — 창 어디서든 Enter가 창을 닫게 된다. 그 길을
        막는다. Esc(취소)를 비롯한 다른 키는 QDialog에 맡긴다.
        """
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.focusWidget() is self.okButton and self.okButton.isEnabled():
                self.okButton.click()
            event.accept()
            return
        super().keyPressEvent(event)

    def accept(self) -> None:
        """구간을 카드에 쓰고 닫는다. 오류가 남아 있으면 닫지 않는다.

        카드가 그사이 대기 상태가 아니게 됐으면 쓰지 않고 닫는다(뷰모델의 ``commit``이 판정한다).
        """
        focused = self.focusWidget()
        if isinstance(focused, TimecodeEdit):
            focused.commit()  # Enter 없이 확인을 누른 칸의 편집을 끝낸다
        if not self._viewmodel.canCommit():
            return
        if self._viewmodel.commit():
            super().accept()
        else:
            super().reject()
