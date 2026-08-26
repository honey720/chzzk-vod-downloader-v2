from PySide6.QtWidgets import QScrollArea, QWidget, QVBoxLayout
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDragLeaveEvent, QDropEvent, QPainter, QColor
from content.widget import ContentItemWidget
from content.data import ContentItem


class _Overlay(QWidget):
    """`ContentListView` 위에 얹는 오버레이 — 드래그 반투명·빈 리스트 안내 (#226).

    `QAbstractItemView`는 항목을 뷰포트에 직접 그려서 view의 `paintEvent`가
    끝까지 그 위에 덧그릴 수 있었지만, `QScrollArea`는 카드 컨테이너가
    뷰포트의 자식 위젯이라 뷰포트에 그린 건 컨테이너가 그 위를 덮어버린다
    (실측으로 확인 — 빈 리스트 안내가 안 보였다). 그래서 오버레이를
    `ContentListView`의 형제뻘 자식으로 얹어 항상 맨 위에 띄운다.
    """

    def __init__(self, owner: "ContentListView"):
        super().__init__(owner)
        self._owner = owner
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)

    def paintEvent(self, event):
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

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        # 카드는 창 폭에 맞춰야 한다 — 넘치는 제목·경로·상태 문구는 ElidingLabel이
        # 알아서 잘라 보여준다(PR #229 후속). 가로 스크롤은 그 자체가 버그였다.
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(10, 10, 10, 0)
        self._layout.setSpacing(0)
        self._layout.addStretch(1)  # 카드는 항상 이 스트레치 앞에 insertWidget된다
        self.setWidget(self._container)

        self.setAcceptDrops(True)

        self._model = None
        self._widgets: dict[ContentItem, ContentItemWidget] = {}
        self._dragActive = False    # 드래그 상태 플래그 TODO: 대체 가능한 메서드 사용

        self._overlay = _Overlay(self)
        self._overlay.setGeometry(self.rect())
        self._overlay.raise_()
        self._overlay.show()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._overlay.setGeometry(self.rect())

    def setModel(self, model):
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

    def _onItemInserted(self, item: ContentItem):
        row = self._model.getRow(item)
        if row is not None:
            self._insertWidget(item, row)

    def _insertWidget(self, item: ContentItem, row: int):
        widget = ContentItemWidget(item, row, self._container)
        widget.setData(item, row)
        widget.deleteRequest.connect(lambda it=item: self.onDeleteItem(it))
        widget.addRepresentationButtons()
        self._layout.insertWidget(row, widget)
        self._widgets[item] = widget
        self._overlay.update()  # 빈 리스트 안내를 즉시 감춘다

    def _onItemRemoved(self, item: ContentItem):
        widget = self._widgets.pop(item, None)
        if widget is not None:
            self._layout.removeWidget(widget)
            widget.deleteLater()  # 생명주기 직접 관리 — 빠뜨리면 누수 (#226)
        self._overlay.update()  # 마지막 카드가 지워졌으면 빈 리스트 안내를 다시 띄운다

    def _onItemChanged(self, item: ContentItem):
        widget = self._widgets.get(item)
        if widget is not None:
            row = self._model.getRow(item)
            widget.setData(item, row)

    def onDeleteItem(self, item):
        # print("view - onDeleteItem") # Debugging
        self.deleteRequest.emit(item)

    def dragEnterEvent(self, event: QDragEnterEvent):
        # 드래그된 데이터가 텍스트인지 확인
        if event.mimeData().hasText():
            self._dragActive = True  # 드래그 시작 플래그 활성화
            self._overlay.update()            # 뷰 갱신(화면에 표시)
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent):
        # 드래그 중인 데이터가 텍스트일 경우 계속 수락
        if event.mimeData().hasText():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event: QDragLeaveEvent):
        self._dragActive = False  # 드래그 종료 플래그 비활성화
        self._overlay.update()             # 화면 갱신
        event.accept()

    def dropEvent(self, event: QDropEvent):
        # 드롭된 데이터가 텍스트라면 처리
        if event.mimeData().hasText():
            self._dragActive = False  # 드래그 상태 해제
            self._overlay.update()             # 뷰 갱신
            dropped_text = event.mimeData().text()
            self.fetchRequested.emit(dropped_text)
            event.acceptProposedAction()
        else:
            event.ignore()

    def _paintOverlay(self, viewport_widget):
        # 드래그 중일 때만 오버레이 텍스트 출력
        if self._dragActive:
            painter = QPainter(viewport_widget)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            # 전체 영역에 반투명 검은색 오버레이를 그립니다.
            overlay_color = QColor(0, 0, 0, 128)  # (R, G, B, Alpha) Alpha=128은 50% 투명도
            painter.fillRect(viewport_widget.rect(), overlay_color)
            # 중앙에 흰색 텍스트를 그립니다.
            painter.drawText(viewport_widget.rect(), Qt.AlignmentFlag.AlignCenter, self.tr("Drag the URL here."))
            painter.end()
        elif self._model is not None and self._model.isEmpty():
            painter = QPainter(viewport_widget)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            # 중앙에 흰색 텍스트를 그립니다.
            painter.drawText(viewport_widget.rect(), Qt.AlignmentFlag.AlignCenter, self.tr("Add VOD or Drag the URL here."))
            painter.end()

    def onDownloadStarted(self, item: ContentItem):
        widget = self._widgets.get(item)
        if widget:
            widget.deleteButton.setEnabled(False)
            widget.setData(item, self._model.getRow(item))

    def onDownloadStoped(self, item: ContentItem):
        widget = self._widgets.get(item)
        if widget:
            widget.deleteButton.setEnabled(True)
            widget.setData(item, self._model.getRow(item))

    def onDownloadPaused(self, item: ContentItem):
        widget = self._widgets.get(item)
        if widget:
            widget.setData(item, self._model.getRow(item))

    def onDownloadResumed(self, item: ContentItem):
        widget = self._widgets.get(item)
        if widget:
            widget.setData(item, self._model.getRow(item))

    def onDownloadFinished(self, item: ContentItem, isFinish: bool):
        widget = self._widgets.get(item)
        if widget:
            widget.deleteButton.setEnabled(True)
            if isFinish:
                widget.contentFrame.setStyleSheet("""
                #contentFrame {
                    background-color: #55B5FF;  /* ✅ 불투명한 배경 */
                    border-radius: 8px;
                    padding: 0px;
                    color: #ffffff;
                }
                """)
            else:
                widget.contentFrame.setStyleSheet("""
                #contentFrame {
                    background-color: #FF6969;  /* ✅ 불투명한 배경 */
                    border-radius: 8px;
                    padding: 0px;
                    color: #ffffff;
                }
                """)
            widget.setData(item, self._model.getRow(item))

    #TODO: 중복되는 부분 통합
