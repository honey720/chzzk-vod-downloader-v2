from PySide6.QtWidgets import QApplication, QMessageBox, QScrollArea, QWidget, QVBoxLayout
from PySide6.QtCore import QCoreApplication, Qt, QTimer, Signal
from PySide6.QtGui import (
    QCursor,
    QDragEnterEvent,
    QDragMoveEvent,
    QDragLeaveEvent,
    QDropEvent,
    QPainter,
    QColor,
)
import app.theme as theme
from app.widgets.narrow_steps import RETURN_SLACK
from app.widgets.scroll_padding import ScrollBarPadding
from app.widgets.widget import BUSY_STATES, ContentItemWidget
from app.viewmodels.data import ContentItem


def widget_under_pointer() -> QWidget | None:
    """포인터 밑의 위젯 — 목록이 바뀐 뒤 호버를 다시 맞출 때 쓴다(#550). 테스트가 갈아 끼운다."""
    return QApplication.widgetAt(QCursor.pos())


def confirm_stop_and_delete(parent: QWidget) -> bool:
    """받는 중 카드의 ✕ — 「멈추고 지우기」를 묻는다. 기본 단추 · Esc는 취소다 (#550 설계 §12.1).

    고른 것이 「멈추고 지우기」면 참. 테스트가 갈아 끼운다.
    """
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(QCoreApplication.translate("ContentListView", "Delete"))
    box.setText(
        QCoreApplication.translate(
            "ContentListView", "This VOD is downloading. Stop the download and delete it?"
        )
    )
    stop = box.addButton(
        QCoreApplication.translate("ContentListView", "Stop and delete"),
        QMessageBox.ButtonRole.DestructiveRole,
    )
    cancel = box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(cancel)
    box.setEscapeButton(cancel)
    box.exec()
    return box.clickedButton() is stop


class _Overlay(QWidget):
    """`ContentListView` 위에 얹는 오버레이 — 드래그 반투명·빈 리스트 안내 (#226).

    `QAbstractItemView`는 항목을 뷰포트에 직접 그려서 view의 `paintEvent`가
    끝까지 그 위에 덧그릴 수 있었지만, `QScrollArea`는 카드 컨테이너가
    뷰포트의 자식 위젯이라 뷰포트에 그린 건 컨테이너가 그 위를 덮어버린다
    (실측으로 확인 — 빈 리스트 안내가 안 보였다). 그래서 오버레이를
    `ContentListView`의 형제뻘 자식으로 얹어 항상 맨 위에 띄운다.
    """

    def __init__(self, owner: "ContentListView") -> None:
        super().__init__(owner)
        self._owner = owner
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)

    def paintEvent(self, event) -> None:
        self._owner._paintOverlay(self)


class ContentListView(QScrollArea):
    """✅ 카드 목록을 표시하는 View — `QVBoxLayout` 직접 삽입 (#226).

    `QListView`+`setIndexWidget()`을 버렸다 — 삽입 단가가 리스트 크기에
    비례해 커지는 게 실측으로 확인됐고(#208, 카드 1000개 34.5초), Qt 공식
    문서도 `setIndexWidget()`을 정적 콘텐츠 전용이라 명시한다. 카드를
    `QVBoxLayout`에 직접 `insertWidget()`하면 삽입 단가가 O(1)이 된다(같은
    조건 2.26초). 아이템↔위젯 매핑은 `dict`로 O(1) 조회한다 — 모델
    시그널이 넘기는 건 항상 아이템 하나뿐이라 전체 재스캔이 구조적으로
    불가능하다.
    """

    deleteRequest = Signal(object)
    fetchRequested = Signal(str)
    # 상태별 조작(#245) — 카드의 ⏸/↻ 를 아이템과 함께 상위로 중계한다.
    # deleteRequest와 같은 패턴: 위젯은 자기 아이템을 모르는 무인자 시그널만
    # 내고, 뷰가 아이템을 붙여 앱 계층(mainWindow)으로 올린다.
    pauseRequested = Signal(object)
    retryRequested = Signal(object)
    # 받는 중 카드의 ✕에서 「멈추고 지우기」를 골랐다 (#550) — 엔진을 멈추는 쪽(mainWindow)이 받는다
    stopAndDeleteRequested = Signal(object)
    editRequested = Signal(object)  # 대기 카드의 ✎ · 더블 클릭 — 카드 편집 창 (#545)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        # 카드는 창 폭에 맞춰야 한다 — 넘치는 제목·경로·상태 문구는 ElidingLabel이
        # 알아서 잘라 보여준다(PR #229 후속). 가로 스크롤은 그 자체가 버그였다.
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 세로 스크롤바는 필요할 때만(AsNeeded). 다만 스크롤바가 보이는 동안은 카드 컨테이너의
        # 오른쪽 패딩에서 스크롤바 폭을 빼서(`_fitPaddingAroundScrollBar`) 스크롤바가 카드
        # 오른쪽 여백(outerMargin) 안에 들어가게 한다 — 카드 폭·위치가 스크롤바 유무와
        # 무관해진다(v2.10.1). 항상 켜 두는 안(AlwaysOn)은 빈 목록에도 바가 보여 기각(오너).
        # transient(오버레이) 힌트는 QSS로 스타일된 스크롤바에 Qt가 적용하지 않는다(실측 —
        # 위젯 기준 styleHint가 0). 카드 높이는 폭과 무관하므로 되먹임 진동은 없다(실측).
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        # 좌우 여백은 창(contentColumn)이 아니라 여기 카드 컨테이너가 진다(v2.10.1).
        # 목록 자체는 창 좌우 끝까지 가서 스크롤바가 창 끝에 붙고, 카드 프레임은
        # 이 패딩만큼 안쪽에 놓여 상단·하단 바와 같은 좌측 정렬선(outerMargin)을
        # 지킨다 — 값이 바뀐 게 아니라 자리가 옮겨진 것이다(#244 정렬선 유지).
        # 위 여백: 헤더바와 목록 사이에 간격이 없어(바는 경계선 하나로만 나눈다, #536) 헤더바 ↔ 첫 카드
        # 사이를 이 여백이 진다 — 카드 사이와 같은 cardGap(설계 §11.2, 프로토 CARD_GAP)
        outer = theme.METRICS["outerMargin"]
        gap = theme.METRICS["cardGap"]
        self._layout.setContentsMargins(outer, gap, outer, gap)
        # 스크롤바의 Show/Hide에 맞춰 오른쪽 패딩을 고친다 — 편집 창의 구간 목록과 함께 쓴다
        self._scrollPadding = ScrollBarPadding(self, self._layout, outer)
        # 카드 사이 — 카드 자체는 바깥 여백이 없다(#550). 카드 폭에는 영향을 주지 않는 값이라
        # ElidingLabel 폭 계산과 무관하다
        self._layout.setSpacing(gap)
        self._layout.addStretch(1)  # 카드는 항상 이 스트레치 앞에 insertWidget된다
        self.setWidget(self._container)

        self.setAcceptDrops(True)

        self._model = None
        # 보이는 차례 · 필터(#495 — CardOrder). 없으면(모델만 건 대역) 목록 행 차례, 필터 없음
        self._order = None
        self._widgets: dict[ContentItem, ContentItemWidget] = {}
        self._dragActive = False  # 드래그 상태 플래그 TODO: 대체 가능한 메서드 사용
        self._renumberPending = False  # 삭제 뒤 번호 재계산 배치 대기 플래그 (#235)
        self._hoverCard: ContentItemWidget | None = None  # 호버라고 보는 카드 하나(#550)
        self._rehoverPending = False  # 호버 다시 맞추기를 다음 턴에 한 번만 (#550)
        self._steppingFocus = False  # 목록이 카드 초점을 옮기는 중 — 들어옴으로 보지 않는다
        self._showYear = True  # 카드 날짜에 연도를 적는지 — 목록 전체가 함께 정한다(#554)
        self._datesPending = False  # 날짜 연도 전체 판정을 다음 턴에 한 번만(#554)
        self._newCards: list[ContentItemWidget] = []  # 다음 턴에 연도를 볼 새 카드(#554)

        self._overlay = _Overlay(self)
        self._overlay.setGeometry(self.rect())
        self._overlay.raise_()
        self._overlay.show()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._overlay.setGeometry(self.rect())
        self._scheduleRehover()
        self._scheduleDates()

    # ---- 날짜 연도 — 목록 전체가 함께 뺀다 (#554) ----
    # 같은 폭에서 연도가 있는 카드와 없는 카드가 섞이지 않게 목록이 한 번에 정한다. 판정은 창 폭이 바뀔
    # 때(보이는 카드 전부)와 카드가 들어올 때(들어온 카드만)뿐이다 — 카드 상태가 바뀔 때는 하지 않는다

    def showsYear(self) -> bool:
        """카드 날짜에 연도를 적는지."""
        return self._showYear

    def _scheduleDates(self) -> None:
        """날짜 연도 전체 판정을 다음 이벤트 루프 턴에 한 번 돌린다."""
        if not self._datesPending:
            self._datesPending = True
            QTimer.singleShot(0, self, self._fitDates)

    def _fitDates(self) -> None:
        """보이는 카드의 1줄이 연도 있는 날짜로 들어가는지 보고 목록 전체의 연도를 정한다.

        연도를 적는 중이면 한 장이라도 안 들어갈 때 뺀다. 뺀 뒤에는 모든 카드에 +8px이 남을 때
        되돌린다(`narrow_steps.RETURN_SLACK` — 경계에서 오가지 않게).
        """
        self._datesPending = False
        self._newCards.clear()
        self._container.layout().activate()
        self._decideYear([w for w in self._widgets.values() if w.isVisible()], allow_show=True)

    def _fitNewDates(self) -> None:
        """들어온 카드만 본다 — 연도를 적는 중인데 들어가지 않는 카드가 있으면 목록 전체에서 뺀다."""
        cards = [w for w in self._newCards if w.isVisible()]
        self._newCards.clear()
        if cards and self._showYear:
            self._container.layout().activate()
            self._decideYear(cards, allow_show=False)

    def _decideYear(self, cards: list[ContentItemWidget], allow_show: bool) -> None:
        cards = [card for card in cards if card.row1Room() > 0]
        if not cards:
            return
        if self._showYear:
            show = all(card.row1Need(True) <= card.row1Room() for card in cards)
        elif allow_show:
            show = all(card.row1Need(True) + RETURN_SLACK <= card.row1Room() for card in cards)
        else:
            return
        if show != self._showYear:
            self._showYear = show
            for widget in self._widgets.values():
                widget.setShowYear(show)

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        super().scrollContentsBy(dx, dy)
        self._scheduleRehover()  # 굴리면 포인터 밑 카드가 바뀐다

    # ---- 호버 다시 맞추기 (#550) ----
    # Qt는 포인터가 움직일 때만 들어감 · 나감을 보낸다. 지우기 · 정렬 · 필터 · 굴림 · 창 크기로 카드가
    # 포인터 밑에서 바뀌면 옛 카드가 호버인 채 남는다 — 목록이 바뀐 뒤 포인터 밑 카드를 다시 맞춘다

    def _onCardHover(self, widget: ContentItemWidget, hovered: bool) -> None:
        """카드 하나의 호버가 바뀌었다 — 호버 카드는 늘 하나다."""
        if hovered:
            previous = self._hoverCard
            if previous is not None and previous is not widget:
                previous.setHovered(False)
            self._hoverCard = widget
        elif self._hoverCard is widget:
            self._hoverCard = None

    def hoverCard(self) -> ContentItemWidget | None:
        """호버라고 보는 카드 — 없으면 None."""
        return self._hoverCard

    def _scheduleRehover(self) -> None:
        """호버 다시 맞추기를 다음 이벤트 루프 턴에 한 번 돌린다 — 한 번의 조작에서 여러 번 불려도 한 번."""
        if not self._rehoverPending:
            self._rehoverPending = True
            QTimer.singleShot(0, self, self._rehover)

    def _rehover(self) -> None:
        """포인터 밑의 카드를 호버로, 그 밖의 카드를 호버 아님으로 맞춘다 — 카드 수와 무관하다."""
        self._rehoverPending = False
        under = widget_under_pointer() if self.isVisible() else None
        card = None
        if under is not None and (under is self._container or self._container.isAncestorOf(under)):
            while under is not None and not isinstance(under, ContentItemWidget):
                under = under.parentWidget()
            card = under
        if card is self._hoverCard:
            return
        if self._hoverCard is not None:
            self._hoverCard.setHovered(False)
        if card is not None:
            card.setHovered(True)

    # ---- 카드 초점 — 보이는 차례로 Tab (#550) ----

    def _visibleCards(self, forward: bool, start: int) -> ContentItemWidget | None:
        """레이아웃 자리 `start`부터 한 방향으로 첫 보이는 카드."""
        step = 1 if forward else -1
        index = start
        while 0 <= index < self._layout.count():
            candidate = self._layout.itemAt(index).widget()
            if isinstance(candidate, ContentItemWidget) and not candidate.isHidden():
                return candidate
            index += step
        return None

    def _focusCard(self, card: ContentItemWidget, reason: Qt.FocusReason) -> None:
        self._steppingFocus = True
        try:
            card.setFocus(reason)
        finally:
            self._steppingFocus = False
        self.ensureWidgetVisible(card, 0, 0)

    def stepCardFocus(self, widget: ContentItemWidget, forward: bool) -> bool:
        """초점을 보이는 차례(정렬 · 필터 뒤)의 다음(앞) 카드로 옮긴다 — 끝이면 목록 밖으로.

        카드가 부른다(`ContentItemWidget.focusNextPrevChild`). 옮겼으면 참. 카드는 만든 차례로 초점
        사슬에 들어 있어 Qt에 맡기면 정렬 · 필터와 다른 차례로 간다.
        """
        index = self._layout.indexOf(widget)
        if index < 0:
            return False
        reason = Qt.FocusReason.TabFocusReason if forward else Qt.FocusReason.BacktabFocusReason
        card = self._visibleCards(forward, index + (1 if forward else -1))
        if card is not None:
            self._focusCard(card, reason)
            return True
        # 목록 밖으로 — 초점 사슬에서 카드 · 목록이 아닌 다음(앞) 위젯
        current = widget.nextInFocusChain() if forward else widget.previousInFocusChain()
        while current is not None and current is not widget:
            inside = current is self or self.isAncestorOf(current)
            takes_tab = current.focusPolicy().value & Qt.FocusPolicy.TabFocus.value
            if (
                not inside
                and takes_tab
                and current.isVisible()
                and current.isEnabled()
                and current.window() is widget.window()
            ):
                current.setFocus(reason)
                return True
            current = current.nextInFocusChain() if forward else current.previousInFocusChain()
        return False

    def cardFocusEntered(self, widget: ContentItemWidget, reason: Qt.FocusReason) -> None:
        """카드가 초점을 받았다 — 목록 밖에서 Tab으로 들어왔으면 보이는 차례의 첫(끝) 카드로 옮긴다."""
        if self._steppingFocus:
            return
        if reason in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason):
            forward = reason == Qt.FocusReason.TabFocusReason
            start = 0 if forward else self._layout.count() - 1
            target = self._visibleCards(forward, start)
            if target is not None and target is not widget:
                self._focusCard(target, reason)
                return
        self.ensureWidgetVisible(widget, 0, 0)

    def _fitPaddingAroundScrollBar(self) -> None:
        """스크롤바가 보이면 컨테이너 오른쪽 패딩에서 그 폭을 빼고, 숨으면 되돌린다 (v2.10.1).

        카드 오른쪽 끝은 스크롤바 유무와 무관하게 항상 목록 오른쪽 끝 − outerMargin이다 —
        스크롤바는 그 여백 안에 들어간다. 스크롤바가 여백보다 넓으면 패딩은 0에서 멈춘다.
        하는 일은 ``ScrollBarPadding``에 있다.
        """
        self._scrollPadding.fit()

    def setModel(self, model) -> None:
        """✅ 모델을 설정하고 위젯을 자동으로 연결"""
        self._model = model
        model.itemInserted.connect(self._onItemInserted)
        model.itemRemoved.connect(self._onItemRemoved)
        model.itemChanged.connect(self._onItemChanged)
        # 방어적 동기화 — 지금은 setModel이 항상 빈 모델로 불리지만(생성 직후),
        # 나중에 미리 채워진 모델이 들어와도 깨지지 않게 기존 항목을 반영한다
        for row in range(model.rowCount()):
            item = model.itemAt(row)
            if item is not None and item not in self._widgets:
                self._insertWidget(item, row)

    def widgetFor(self, item: ContentItem) -> ContentItemWidget | None:
        """아이템에 대응하는 위젯을 O(1) 조회한다 — 테스트·외부 호출용."""
        return self._widgets.get(item)

    # ---- 모델 시그널 핸들러 — 항상 아이템 하나만, 전체 재스캔 없음 ----

    def _onItemInserted(self, item: ContentItem) -> None:
        row = self._model.getRow(item)
        if row is not None:
            self._insertWidget(item, row)

    def _placeOf(self, item: ContentItem, row: int) -> int:
        """레이아웃에서 그 카드가 놓일 자리 — 보이는 차례(#495). 순서 객체가 없으면 목록 행."""
        return self._order.indexOf(item) if self._order is not None else row

    def _accepts(self, item: ContentItem) -> bool:
        """그 카드가 지금 필터에서 보이는지 — 순서 객체가 없으면 늘 보인다."""
        return self._order is None or self._order.accepts(item)

    def bindOrder(self, order) -> None:
        """필터 · 정렬 상태(`CardOrder`)와 잇는다 (#495).

        정렬이 바뀌면 카드를 다시 놓고, 칩이 바뀌면 보임을 다시 정한다 — 둘 다 사용자 조작 한 번에
        한 번이다. 카드 하나의 상태가 바뀌면 그 카드만 옮기고 그 카드의 보임만 고친다. 카드 위젯은
        다시 만들지 않는다.
        """
        self._order = order
        order.orderReset.connect(self._relayout)
        order.filterChanged.connect(self._applyFilter)
        order.itemMoved.connect(self._moveWidget)
        order.itemStateChanged.connect(self._applyVisibility)
        self._relayout()
        self._applyFilter()

    def _relayout(self) -> None:
        """보이는 차례대로 카드를 다시 놓는다 — 레이아웃 항목만 옮기고 위젯은 그대로."""
        if self._order is None:
            return
        for index, item in enumerate(self._order.ordered()):
            widget = self._widgets.get(item)
            if widget is not None and self._layout.indexOf(widget) != index:
                self._layout.removeWidget(widget)
                self._layout.insertWidget(index, widget)
        self._overlay.update()
        self._scheduleRehover()

    def _moveWidget(self, item: ContentItem) -> None:
        """카드 하나를 보이는 차례의 새 자리로 옮긴다."""
        widget = self._widgets.get(item)
        if widget is None:
            return
        self._layout.removeWidget(widget)
        self._layout.insertWidget(self._order.indexOf(item), widget)
        self._scheduleRehover()

    def _applyFilter(self) -> None:
        """고른 칩에 맞춰 모든 카드의 보임을 정한다.

        카드 컨테이너를 잠깐 숨긴 채 바꾼 뒤 한 번에 다시 보인다 — 보이는 컨테이너 안에서 카드를
        하나씩 보이면 그때마다 목록 레이아웃이 다시 계산돼, 숨었던 카드 800장을 다시 보이는 데
        16초가 걸렸다(#495 offscreen 실측, 카드 1000장).
        """
        container = self._container
        container.hide()
        for item, widget in self._widgets.items():
            widget.setVisible(self._accepts(item))
        container.show()
        self._overlay.update()
        self._scheduleRehover()
        self._scheduleDates()  # 다시 보인 카드가 연도를 못 넣을 수 있다 — 칩 조작 한 번에 한 번

    def _applyVisibility(self, item: ContentItem) -> None:
        """카드 하나의 보임을 지금 칩에 맞춘다."""
        widget = self._widgets.get(item)
        if widget is not None:
            widget.setVisible(self._accepts(item))
            self._scheduleRehover()

    def _insertWidget(self, item: ContentItem, row: int) -> None:
        widget = ContentItemWidget(item, row, self._container)
        widget.setData(item, row)
        widget.deleteRequest.connect(lambda it=item: self.onDeleteItem(it))
        widget.pauseRequest.connect(lambda it=item: self.pauseRequested.emit(it))
        widget.retryRequest.connect(lambda it=item: self.retryRequested.emit(it))
        widget.editRequest.connect(lambda it=item: self.editRequested.emit(it))
        widget.hoverChanged.connect(lambda hovered, w=widget: self._onCardHover(w, hovered))
        widget.loadRepresentations()
        widget.setShowYear(self._showYear)
        self._layout.insertWidget(self._placeOf(item, row), widget)
        widget.setVisible(self._accepts(item))
        self._widgets[item] = widget
        self._overlay.update()  # 빈 리스트 안내를 즉시 감춘다
        # 날짜 연도 — 들어온 카드만 다음 턴에 한 번에 본다(배치 뒤라야 폭이 있다)
        if not self._newCards:
            QTimer.singleShot(0, self, self._fitNewDates)
        self._newCards.append(widget)

    def _onItemRemoved(self, item: ContentItem) -> None:
        widget = self._widgets.pop(item, None)
        if widget is not None:
            if self._hoverCard is widget:
                self._hoverCard = None
            if widget in self._newCards:
                self._newCards.remove(widget)
            self._layout.removeWidget(widget)
            widget.deleteLater()  # 생명주기 직접 관리 — 빠뜨리면 누수 (#226)
        self._overlay.update()  # 마지막 카드가 지워졌으면 빈 리스트 안내를 다시 띄운다
        self._scheduleRenumber()  # 뒤쪽 카드 번호를 당긴다 (#235)
        self._scheduleRehover()  # 아래 카드가 포인터 밑으로 올라온다

    def _scheduleRenumber(self) -> None:
        """삭제 뒤 남은 카드들의 번호(표시용, `ContentItemWidget.setIndex`)를 다시 매긴다.

        `QTimer.singleShot(0, ...)`으로 다음 이벤트 루프 턴까지 미룬다 —
        `clrearFinishedItems()`처럼 한 번의 호출에서 `removeRows`가 여러 번
        연달아 일어나는 일괄 삭제가 있다(`app/viewmodels/content_viewmodel.py`).
        매번 즉시 전체를 다시 매기면 일괄 삭제 한 번이 O(n) 재번호매김을
        n번 반복해 O(n²)가 된다 — `_renumberPending` 플래그로 그 반복 호출을
        하나로 눌러, 배치가 끝난 뒤(다음 이벤트 루프 턴) 딱 한 번만 돈다.
        새 카드 삽입(`_insertWidget`)에서는 이 메서드를 부르지 않는다 —
        새 카드는 항상 끝에 붙어 앞 카드들의 번호에 영향이 없고, 삽입 단가를
        O(1)로 지키는 게 #226의 핵심이었다(전체 재번호매김을 얹으면 도로
        O(n)이 된다).

        `self`를 컨텍스트 객체로 넘긴다 — 콜백이 돌기 전에 `self`(이 뷰)가
        파괴되면 Qt가 알아서 호출을 취소한다. 뷰는 앱 생명주기 내내 살아
        있어 실사용에선 거의 안 걸리는 경로지만, 테스트마다 새 뷰를 만들고
        버리는 상황에서 이전 테스트가 예약해 둔 콜백이 다음 테스트로 새어
        들어가는 걸 막는다.
        """
        if not self._renumberPending:
            self._renumberPending = True
            QTimer.singleShot(0, self, self._renumberAll)

    def _renumberAll(self) -> None:
        self._renumberPending = False
        if self._model is None:
            return
        for row in range(self._model.rowCount()):
            item = self._model.itemAt(row)
            widget = self._widgets.get(item)
            if widget is not None and widget.index != row:
                widget.setIndex(row)

    def _onItemChanged(self, item: ContentItem) -> None:
        widget = self._widgets.get(item)
        if widget is not None:
            row = self._model.getRow(item)
            widget.setData(item, row)

    def onDeleteItem(self, item: ContentItem) -> None:
        """카드의 ✕ — 받는 중 카드는 「멈추고 지우기」를 물은 뒤에만 지운다 (#550).

        받는 중 · 일시정지 카드는 엔진이 쥐고 있다 — 고르면 `stopAndDeleteRequested`(엔진을 멈추고
        지운다), 취소면 아무 일도 없다. 그 밖 카드는 바로 `deleteRequest`다.

        확인 창은 모달 이벤트 루프를 돌리므로 열린 동안 그 카드가 끝날 수 있다(배치는 다음 카드로
        넘어간다). 그래서 닫힌 뒤 상태를 다시 보고, 더는 받는 중이 아니면 그냥 지운다 — 멈추면 다음
        카드의 다운로드가 멈춘다.
        """
        if item.downloadState in BUSY_STATES:
            if not confirm_stop_and_delete(self):
                return
            if item.downloadState in BUSY_STATES:
                self.stopAndDeleteRequested.emit(item)
                return
        self.deleteRequest.emit(item)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        # 드래그된 데이터가 텍스트인지 확인
        if event.mimeData().hasText():
            self._dragActive = True  # 드래그 시작 플래그 활성화
            self._overlay.update()  # 뷰 갱신(화면에 표시)
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        # 드래그 중인 데이터가 텍스트일 경우 계속 수락
        if event.mimeData().hasText():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self._dragActive = False  # 드래그 종료 플래그 비활성화
        self._overlay.update()  # 화면 갱신
        event.accept()

    def dropEvent(self, event: QDropEvent) -> None:
        # 드롭된 데이터가 텍스트라면 처리
        if event.mimeData().hasText():
            self._dragActive = False  # 드래그 상태 해제
            self._overlay.update()  # 뷰 갱신
            dropped_text = event.mimeData().text()
            self.fetchRequested.emit(dropped_text)
            event.acceptProposedAction()
        else:
            event.ignore()

    def _paintOverlay(self, viewport_widget) -> None:
        # 드래그 중일 때만 오버레이 텍스트 출력
        if self._dragActive:
            painter = QPainter(viewport_widget)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            # 전체 영역에 반투명 검은색 오버레이를 그립니다.
            overlay_color = QColor(0, 0, 0, 128)  # (R, G, B, Alpha) Alpha=128은 50% 투명도
            painter.fillRect(viewport_widget.rect(), overlay_color)
            # 중앙에 흰색 텍스트를 그립니다.
            painter.drawText(
                viewport_widget.rect(), Qt.AlignmentFlag.AlignCenter, self.tr("Drag the URL here.")
            )
            painter.end()
        elif self._model is not None and self._model.isEmpty():
            painter = QPainter(viewport_widget)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            # 중앙에 흰색 텍스트를 그립니다.
            painter.drawText(
                viewport_widget.rect(),
                Qt.AlignmentFlag.AlignCenter,
                self.tr("Add VOD or Drag the URL here."),
            )
            painter.end()

    def bind(self, viewmodel) -> None:
        """뷰모델과 배선한다 (#259 B2 — 구 ContentManager 바인더의 8줄).

        구체 클래스를 알지 않는다 — `model`, 시그널 5개(itemStarted/itemStopped/
        itemPaused/itemResumed/itemFinished)와 `fetchRequested`, `removeItem`만
        있으면 된다(테스트가 대역을 넣을 수 있게). 목록 뷰는 부품이라 자기 배선을
        스스로 안다 — 탭이 늘어도 배선을 복사하지 않는다.
        """
        self.setModel(viewmodel.model)
        order = getattr(viewmodel, "order", None)  # 대역 뷰모델에는 없을 수 있다
        if order is not None:
            self.bindOrder(order)
        self.deleteRequest.connect(viewmodel.removeItem)
        self.fetchRequested.connect(viewmodel.fetchRequested)
        # 구 view 직접 호출 6곳의 반전 (#169): 뷰모델은 시그널로 말하고 뷰가 자기를 건다
        viewmodel.itemStarted.connect(self.onDownloadStarted)
        viewmodel.itemStopped.connect(self.onDownloadStoped)
        viewmodel.itemPaused.connect(self.onDownloadPaused)
        viewmodel.itemResumed.connect(self.onDownloadResumed)
        viewmodel.itemFinished.connect(self.onDownloadFinished)

    def onDownloadStarted(self, item: ContentItem) -> None:
        widget = self._widgets.get(item)
        if widget:
            widget.setData(item, self._model.getRow(item))

    def onDownloadStoped(self, item: ContentItem) -> None:
        widget = self._widgets.get(item)
        if widget:
            widget.setData(item, self._model.getRow(item))

    def onDownloadPaused(self, item: ContentItem) -> None:
        widget = self._widgets.get(item)
        if widget:
            widget.setData(item, self._model.getRow(item))

    def onDownloadResumed(self, item: ContentItem) -> None:
        widget = self._widgets.get(item)
        if widget:
            widget.setData(item, self._model.getRow(item))

    def onDownloadFinished(self, item: ContentItem, isFinish: bool) -> None:
        widget = self._widgets.get(item)
        if widget:
            # 완료·실패 색은 여기서 직접 칠하지 않는다 (#227) — setData가
            # 아이템 상태(FINISHED/FAILED)를 보고 applyStateStyle()로
            # 카드 테두리·진행바를 함께 맞춘다. 색을 이 자리에 박아두면
            # 대기·진행 두 상태만 색이 없는 반쪽짜리가 되고, 값도 theme.py와
            # 갈라진다
            widget.setData(item, self._model.getRow(item))

    # TODO: 중복되는 부분 통합
