"""값을 고르는 칸 — 펼침 목록만 헤더 드롭다운 메뉴(`ChoiceMenu`)로 바꾼 Qt 콤보박스 (#620).

닫힌 모양 · 키보드 · 값 시그널 · 모델은 `QComboBox` 그대로다. 펼칠 때 Qt 목록(`view()`) 대신
`ChoiceMenu`를 콤보박스 아래 왼쪽 끝에 맞춰 띄운다 — 헤더의 저장 위치 · 정렬 드롭다운과 같은 모양 ·
동작(호버 면 `menuHover`, 고른 항목 ✓ + 굵은 글)이다.
"""

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer
from PySide6.QtGui import QAction, QCursor, QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QComboBox, QStyle, QStyleOptionComboBox, QWidget

from app.widgets.header_controls import MENU_SEP, ChoiceMenu
from app.widgets.tip_bubble import close_floats_if_left

# 메뉴 줄 글을 닫힌 칸 글과 따로 줄 때 쓰는 항목 역할 — 값은 `이름\x1f흐린 글`(`ChoiceMenu` 항목 글과 같은 꼴).
# 없으면 메뉴 줄은 항목 글(DisplayRole) 하나다. UserRole은 항목 값(`addItem`의 data)이 쓴다
MENU_TEXT_ROLE = Qt.ItemDataRole.UserRole + 1


class _WindowLeaveWatch(QObject):
    """메뉴가 열린 동안 콤보박스 창의 활성 · 상태 변화를 보고 창을 떠났으면 메뉴를 닫는다.

    `close_floats_if_left`는 메인 창의 `changeEvent`만 부른다 — 설정 창 · 편집 창은 따로 뜬 최상위 창이라
    그 창이 활성을 잃어도 메인 창에는 알림이 오지 않는다. 메뉴가 열릴 때 그 창에 걸고 닫힐 때 뗀다.
    """

    def __init__(self, combo: "ChoiceCombo") -> None:
        super().__init__(combo)
        self._combo = combo

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() in (QEvent.Type.ActivationChange, QEvent.Type.WindowStateChange):
            close_floats_if_left(watched)
        return False


class ChoiceCombo(QComboBox):
    """펼침 목록이 `ChoiceMenu`인 콤보박스 (#620).

    고르면 Qt 콤보박스가 목록에서 고를 때와 같은 차례로 시그널을 낸다 — 다른 값이면
    `currentIndexChanged` → `currentTextChanged` → `activated` → `textActivated`, 같은 값이면
    `activated` → `textActivated`만(Qt 6.11 측정). 호버는 메뉴 강조만 옮기고 값을 바꾸지 않는다. Esc ·
    바깥 누르기로 닫으면 값이 그대로다.

    휠은 초점이 있을 때만 값을 바꾼다 — 초점 없는 칸 위의 휠은 부모(스크롤)에게 넘긴다(설계 §4). 초점은
    누르기 · Tab으로만 받는다(`StrongFocus`) — Qt 기본(`WheelFocus`)이면 휠이 초점을 주면서 값을 바꾼다.

    메뉴는 열 때마다 모델에서 다시 채운다. 열린 동안 항목 글이 바뀌면(해상도 크기 조회 도착) 메뉴 글도
    따라 고친다. 메뉴 줄을 이름 · 오른쪽 흐린 글 두 칸으로 그리려면 항목에 `MENU_TEXT_ROLE`을 준다
    (`setMenuText`) — 닫힌 칸 글은 항목 글 그대로다.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.menuWidget = ChoiceMenu(self)
        self.menuWidget.anchor = self  # 왼쪽 끝 맞춤 · menuOffset 아래(헤더 드롭다운과 같다)
        self.menuWidget.triggered.connect(self._choose)
        self.menuWidget.aboutToHide.connect(self._onMenuHidden)
        self._watch = _WindowLeaveWatch(self)
        self._watched: QWidget | None = None  # 메뉴가 열린 동안 필터를 건 창
        self._closedOver = False  # 포인터가 이 칸 위에 있을 때 메뉴가 닫혔다(한 턴만)
        self.model().dataChanged.connect(self._syncMenuTexts)

    # ---- 펼치기 · 닫기 ----

    def showPopup(self) -> None:
        """Qt 목록 대신 `ChoiceMenu`를 콤보박스 아래에 띄운다 — Alt+↓ · F4 · Space · 누르기가 여기로 온다."""
        if self.count() == 0 or self.menuWidget.isVisible():
            return
        menu = self.menuWidget
        menu.clear()
        current = self.currentIndex()
        active: QAction | None = None
        for row in range(self.count()):
            name, detail = self.menuText(row)
            action = menu.addChoice(name, row, detail)
            action.setEnabled(self._rowEnabled(row))
            if row == current:
                active = action
        menu.mark(current)
        menu.setMinimumWidth(self.width())
        if active is not None:
            menu.setActiveAction(active)  # 키보드가 고른 항목에서 시작한다(Qt 목록과 같다)
        self._watched = self.window()
        self._watched.installEventFilter(self._watch)
        # 콤보박스 바로 밑에 띄우면 ChoiceMenu.showEvent가 menuOffset만큼 내린다(화면 끝이면 Qt가 위로 뒤집는다)
        menu.popup(self.mapToGlobal(QPoint(0, self.height())))
        self.update()

    def hidePopup(self) -> None:
        """열린 메뉴를 닫는다."""
        self.menuWidget.close()

    def isPopupVisible(self) -> bool:
        """메뉴가 열려 있는지."""
        return self.menuWidget.isVisible()

    def _onMenuHidden(self) -> None:
        if self._watched is not None:
            self._watched.removeEventFilter(self._watch)
            self._watched = None
        # 열린 메뉴 밖(이 칸 위)을 눌러 닫은 누르기를 Qt가 이 칸에 다시 보내도 메뉴를 다시 열지 않는다
        self._closedOver = self.rect().contains(self.mapFromGlobal(QCursor.pos()))
        QTimer.singleShot(0, self, self._forgetClose)
        self.update()

    def _forgetClose(self) -> None:
        self._closedOver = False

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """메뉴를 닫은 바로 그 누르기면 다시 열지 않는다 — 열린 동안 칸을 누르면 닫히기만 한다."""
        if self._closedOver:
            self._closedOver = False
            event.accept()
            return
        super().mousePressEvent(event)

    def setMenuText(self, row: int, name: str, detail: str) -> None:
        """그 줄의 메뉴 글을 이름(왼쪽) · 흐린 글(오른쪽) 두 칸으로 준다 — 닫힌 칸 글은 바꾸지 않는다."""
        self.setItemData(row, f"{name}{MENU_SEP}{detail}" if detail else name, MENU_TEXT_ROLE)

    def menuText(self, row: int) -> tuple[str, str]:
        """그 줄의 메뉴 글 (이름, 흐린 글) — `MENU_TEXT_ROLE`이 없으면 (항목 글, 빈 글)."""
        text = self.itemData(row, MENU_TEXT_ROLE)
        if not isinstance(text, str):
            return self.itemText(row), ""
        name, _sep, detail = text.partition(MENU_SEP)
        return name, detail

    def _rowEnabled(self, row: int) -> bool:
        flags = self.model().flags(
            self.model().index(row, self.modelColumn(), self.rootModelIndex())
        )
        return bool(flags & Qt.ItemFlag.ItemIsEnabled)

    # ---- 고르기 ----

    def _choose(self, action: QAction) -> None:
        """메뉴에서 고른 항목을 값으로 — Qt 목록에서 고를 때와 같은 시그널 차례."""
        row = action.data()
        if not isinstance(row, int) or not 0 <= row < self.count():
            return
        if row != self.currentIndex():
            self.setCurrentIndex(row)  # currentIndexChanged · currentTextChanged
        self.activated.emit(row)
        self.textActivated.emit(self.itemText(row))

    def _syncMenuTexts(self) -> None:
        """열린 메뉴의 항목 글을 모델 글로 고친다(닫혀 있으면 다음에 열 때 다시 채운다)."""
        if not self.menuWidget.isVisible():
            return
        for action in self.menuWidget.choices():
            row = action.data()
            if isinstance(row, int) and 0 <= row < self.count():
                name, detail = self.menuText(row)
                action.setText(f"{name}{MENU_SEP}{detail}" if detail else name)

    # ---- 모양 · 입력 ----

    def initStyleOption(self, option: QStyleOptionComboBox) -> None:
        """메뉴가 열린 동안 펼친 상태(`State_On`)를 건다 — QSS `QComboBox:on`(강조 테두리)이 그대로 산다."""
        super().initStyleOption(option)
        if self.menuWidget.isVisible():
            option.state |= QStyle.StateFlag.State_On

    def wheelEvent(self, event: QWheelEvent) -> None:
        """초점이 있을 때만 휠로 값을 바꾼다 — 없으면 부모에게 넘긴다."""
        if not self.hasFocus():
            event.ignore()
            return
        super().wheelEvent(event)
