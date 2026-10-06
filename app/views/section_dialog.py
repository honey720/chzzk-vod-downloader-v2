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

오류는 팝업으로 띄우지 않는다 — 틀린 칸의 테두리와 그 행의 문구로 보인다.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

import app.theme as theme
from app.viewmodels.section_edit_viewmodel import (
    END,
    START,
    STATE_FAILED,
    STATE_LOADING,
    STATE_READY,
    SectionEditViewModel,
)
from app.widgets.eliding_label import ElidingLabel

# 타임코드 칸의 폭을 재는 본보기 글자 — 시가 세 자리인 경우까지 들어간다
_TIMECODE_SAMPLE = "000:00:00:00"
_INITIAL_SIZE = (560, 420)  # 창의 첫 크기(px) — 행 일곱 개쯤이 스크롤 없이 보인다


class SectionRow(QWidget):
    """구간 한 행 — 번호 · 시작 · 끝 · 길이 · 위 · 아래 · 삭제, 오류가 있으면 그 아래 한 줄.

    오류 문구는 칸 옆이 아니라 아래 줄에 둔다 — 옆에 두면 좁은 창에서 말줄임되어, 무엇이
    틀렸는지 마우스를 올려야 보인다. 아래 줄은 줄바꿈하므로 어떤 폭에서도 전문이 보인다.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        column.addLayout(layout)

        self.numberLabel = QLabel(self)
        self.numberLabel.setObjectName("sectionNumberLabel")
        self.numberLabel.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.numberLabel.setMinimumWidth(self.numberLabel.fontMetrics().horizontalAdvance("00"))
        layout.addWidget(self.numberLabel)

        self.startEdit = self._timecodeEdit("sectionStartEdit")
        self.endEdit = self._timecodeEdit("sectionEndEdit")
        layout.addWidget(self.startEdit)
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

        # 오류 문구 — 오류가 있을 때만 보인다. 번호 폭만큼 들여 칸의 왼쪽 끝에 맞춘다
        self.errorLabel = QLabel(self)
        self.errorLabel.setObjectName("sectionErrorLabel")
        self.errorLabel.setWordWrap(True)
        self.errorLabel.setVisible(False)
        self.errorLabel.setIndent(self.numberLabel.minimumWidth() + layout.spacing())
        column.addWidget(self.errorLabel)

    def _timecodeEdit(self, name: str) -> QLineEdit:
        edit = QLineEdit(self)
        edit.setObjectName(name)
        edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # 폭은 글자에서 유도한다 — 여백은 QSS의 padding과 테두리 몫을 넉넉히 잡은 값이다
        edit.setFixedWidth(edit.fontMetrics().horizontalAdvance(_TIMECODE_SAMPLE) + 28)
        return edit

    def _rowButton(self, text: str, name: str) -> QPushButton:
        button = QPushButton(text, self)
        button.setObjectName(name)
        button.setProperty("role", "subtle")
        button.setAutoDefault(False)  # 칸에서 Enter를 눌러도 이 버튼이 눌리지 않는다
        button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        return button

    def edit(self, column: int) -> QLineEdit:
        """칸 번호(START · END)의 입력창."""
        return self.startEdit if column == START else self.endEdit


class SectionEditDialog(QDialog):
    """구간 편집 창. 뷰모델의 상태를 그리고 입력을 뷰모델에 넘긴다."""

    def __init__(self, viewmodel: SectionEditViewModel, parent: QWidget | None = None):
        super().__init__(parent)
        self._viewmodel = viewmodel
        self._rows: list[SectionRow] = []
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
        self.addButton.clicked.connect(self._viewmodel.addRow)
        header.addWidget(self.addButton)
        layout.addLayout(header)

        self.scrollArea = QScrollArea(self)
        self.scrollArea.setObjectName("sectionScrollArea")
        self.scrollArea.setWidgetResizable(True)
        self.scrollArea.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._rowContainer = QWidget(self.scrollArea)
        self._rowContainer.setObjectName("sectionRowContainer")
        self._rowLayout = QVBoxLayout(self._rowContainer)
        self._rowLayout.addStretch(1)
        self.scrollArea.setWidget(self._rowContainer)
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
        self.okButton.setDefault(True)
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

    def _rebuildRows(self) -> None:
        """행 위젯을 뷰모델의 행 수 · 순서대로 다시 만든다."""
        for row in self._rows:
            self._rowLayout.removeWidget(row)
            row.hide()  # 부모를 떼지 않는다 — 떼면 파괴될 때까지 최상위 창이 된다
            row.deleteLater()
        self._rows = []
        for index in range(len(self._viewmodel.rows)):
            row = SectionRow(self._rowContainer)
            for column in (START, END):
                edit = row.edit(column)
                edit.textEdited.connect(
                    lambda text, r=index, c=column: self._viewmodel.setText(r, c, text, False)
                )
                edit.editingFinished.connect(
                    lambda r=index, c=column, e=edit: self._viewmodel.setText(r, c, e.text())
                )
            row.upButton.clicked.connect(lambda _=False, r=index: self._viewmodel.moveRow(r, -1))
            row.downButton.clicked.connect(lambda _=False, r=index: self._viewmodel.moveRow(r, 1))
            row.deleteButton.clicked.connect(lambda _=False, r=index: self._viewmodel.removeRow(r))
            self._rowLayout.insertWidget(index, row)
            self._rows.append(row)
        self._refresh()

    def _refresh(self) -> None:
        """행의 글자 · 오류 표시와 머리줄 · 버튼 상태를 뷰모델에 맞춘다."""
        viewmodel = self._viewmodel
        count = len(self._rows)
        for index, row in enumerate(self._rows):
            if index >= len(viewmodel.rows):
                break
            row.numberLabel.setText(str(index + 1))
            error = viewmodel.errorText(index)
            for column in (START, END):
                edit = row.edit(column)
                text = viewmodel.rows[index][column]
                # 입력 중인 칸은 건드리지 않는다 — 고쳐 쓴 표기는 편집을 끝낸 뒤에 넣는다
                if not edit.hasFocus() and edit.text() != text:
                    edit.setText(text)
                edit.setToolTip(viewmodel.millisecondsText(index, column))
                self._setFlag(edit, "invalid", bool(error))
            row.infoLabel.setText(viewmodel.lengthText(index))
            row.errorLabel.setText(error)
            row.errorLabel.setVisible(bool(error))
            row.upButton.setEnabled(index > 0)
            row.downButton.setEnabled(index < count - 1)
            row.upButton.setToolTip(self.tr("Move up"))
            row.downButton.setToolTip(self.tr("Move down"))
            row.deleteButton.setToolTip(self.tr("Delete section"))
        self.headerLabel.setText(viewmodel.headerText())
        self.addButton.setEnabled(viewmodel.canAdd())
        self.okButton.setEnabled(viewmodel.canCommit())

    @staticmethod
    def _setFlag(widget: QWidget, name: str, on: bool) -> None:
        """동적 속성을 바꾸고 스타일을 다시 입힌다 — 색은 전역 QSS가 고른다."""
        if widget.property(name) != on:
            widget.setProperty(name, on)
            theme.repolish(widget)

    # ---- 닫기 ----

    def accept(self) -> None:
        """구간을 카드에 쓰고 닫는다. 오류가 남아 있으면 닫지 않는다.

        카드가 그사이 대기 상태가 아니게 됐으면 쓰지 않고 닫는다(뷰모델의 ``commit``이 판정한다).
        """
        focused = self.focusWidget()
        if isinstance(focused, QLineEdit):
            focused.editingFinished.emit()  # Enter 없이 확인을 누른 칸의 글자를 넘긴다
        if not self._viewmodel.canCommit():
            return
        if self._viewmodel.commit():
            super().accept()
        else:
            super().reject()
