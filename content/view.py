from PySide6.QtWidgets import QListView
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDragLeaveEvent, QDropEvent, QPainter, QColor
from content.widget import ContentItemWidget
from content.data import ContentItem

class ContentListView(QListView):
    """✅ QTableView 기반으로 메타데이터 리스트를 표시하는 View"""

    deleteRequest = Signal(object)
    fetchRequested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self.setDragDropMode(QListView.DragDropMode.DragDrop)
        self.setAcceptDrops(True)
        self.setDragEnabled(True)

        self._dragActive = False    # 드래그 상태 플래그 TODO: 대체 가능한 메서드 사용

    def setModel(self, model):
        """✅ 모델을 설정하고 위젯을 자동으로 연결"""
        super().setModel(model)
        model.layoutChanged.connect(self.updateWidgets)
        model.dataChanged.connect(self.updateWidgets)
        model.rowsInserted.connect(self.updateWidgets)
        model.rowsRemoved.connect(self.updateWidgets)

    def updateWidgets(self):
        """✅ 리스트가 변경될 때 setIndexWidget()을 호출하여 UI 적용"""
        # print("view - updateWidgets") # Debugging
        model = self.model()
        if not model:
            return
        
        for row in range(model.rowCount()):
            index = model.index(row, 0)
            item = model.data(index, Qt.ItemDataRole.UserRole)
            if isinstance(item, ContentItem):
                # 기존 위젯이 있으면 업데이트, 없으면 새로 생성
                widget: ContentItemWidget = self.indexWidget(index)
                if widget:
                    # print("view - setData") # Debugging
                    widget.setData(item, row)  # ✅ 기존 위젯 업데이트
                else:
                    widget = ContentItemWidget(item, row, self)
                    widget.deleteRequest.connect(lambda: self.onDeleteItem(item))
                    widget.addRepresentationButtons()
                    self.setIndexWidget(index, widget)  # ✅ 새 위젯 생성

    def onDeleteItem(self, item):
        # print("view - onDeleteItem") # Debugging
        self.deleteRequest.emit(item)

    def onDataChanged(self, topLeft, bottomRight, roles):
        """✅ 특정 아이템 데이터가 변경될 때 해당 위젯만 업데이트"""
        for row in range(topLeft.row(), bottomRight.row() + 1):
            index = self.model().index(row, 0)
            item = self.model().data(index, Qt.ItemDataRole.UserRole)

            if isinstance(item, ContentItem):
                widget = self.indexWidget(index)
                if widget:
                    widget.setData(item)  # ✅ 변경된 데이터만 업데이트

    def dragEnterEvent(self, event: QDragEnterEvent):
        # 드래그된 데이터가 텍스트인지 확인
        if event.mimeData().hasText():
            self._dragActive = True  # 드래그 시작 플래그 활성화
            self.viewport().update()            # 뷰 갱신(화면에 표시)
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
        self.viewport().update()             # 화면 갱신
        event.accept()

    def dropEvent(self, event: QDropEvent):
        # 드롭된 데이터가 텍스트라면 처리
        if event.mimeData().hasText():
            self._dragActive = False  # 드래그 상태 해제
            self.viewport().update()             # 뷰 갱신
            dropped_text = event.mimeData().text()
            self.fetchRequested.emit(dropped_text)
            event.acceptProposedAction()
        else:
            event.ignore()

    def paintEvent(self, event):
        # QListView의 기본 그리기 실행
        super().paintEvent(event)
        # 드래그 중일 때만 오버레이 텍스트 출력
        if self._dragActive:
            painter = QPainter(self.viewport())
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            # 전체 영역에 반투명 검은색 오버레이를 그립니다.
            overlay_color = QColor(0, 0, 0, 128)  # (R, G, B, Alpha) Alpha=128은 50% 투명도
            painter.fillRect(self.viewport().rect(), overlay_color)
            # 중앙에 흰색 텍스트를 그립니다.
            painter.drawText(self.viewport().rect(), Qt.AlignmentFlag.AlignCenter, self.tr("Drag the URL here."))
            painter.end()
        elif self.model().isEmpty():
            painter = QPainter(self.viewport())
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            # 중앙에 흰색 텍스트를 그립니다.
            painter.drawText(self.viewport().rect(), Qt.AlignmentFlag.AlignCenter, self.tr("Add VOD or Drag the URL here."))
            painter.end()


    def onDownloadStarted(self, item: ContentItem):
        row = self.model().getRow(item)
        index = self.model().index(row, 0)
        widget: ContentItemWidget = self.indexWidget(index)
        if widget:
            widget.delete_btn.setEnabled(False)
            widget.setData(item, row)

    def onDownloadStoped(self, item: ContentItem):
        row = self.model().getRow(item)
        index = self.model().index(row, 0)
        widget: ContentItemWidget = self.indexWidget(index)
        if widget:
            widget.delete_btn.setEnabled(True)
            widget.setData(item, row)

    def onDownloadPaused(self, item: ContentItem):
        row = self.model().getRow(item)
        index = self.model().index(row, 0)
        widget: ContentItemWidget = self.indexWidget(index)
        if widget:
            widget.setData(item, row)

    def onDownloadResumed(self, item: ContentItem):
        row = self.model().getRow(item)
        index = self.model().index(row, 0)
        widget: ContentItemWidget = self.indexWidget(index)
        if widget:
            widget.setData(item, row)
    
    def onDownloadFinished(self, item: ContentItem, isFinish: bool):
        row = self.model().getRow(item)
        index = self.model().index(row, 0)
        widget: ContentItemWidget = self.indexWidget(index)
        if widget:
            widget.delete_btn.setEnabled(True)
            if isFinish:
                widget.frame.setStyleSheet("""
                QFrame {
                    background-color: #55B5FF;  /* ✅ 불투명한 배경 */
                    border-radius: 8px;  
                    padding: 0px;
                }                       
                """)
            else:
                widget.frame.setStyleSheet("""
                QFrame {
                    background-color: #FF6969;  /* ✅ 불투명한 배경 */
                    border-radius: 8px;  
                    padding: 0px;
                }                       
                """)
            widget.setData(item, row)

    #TODO: 중복되는 부분 통합