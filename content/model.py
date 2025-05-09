from PySide6.QtCore import Qt, QAbstractListModel, QModelIndex
from content.data import ContentItem

class ContentListModel(QAbstractListModel):
    # 메타데이터 리스트 데이터

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items = []
    
    def rowCount(self, parent=None):
        return len(self.items)
    
    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self.items):
            return None
        
        item: ContentItem = self.items[index.row()]

        if role == Qt.ItemDataRole.UserRole:
            return item
        
        elif role == Qt.ItemDataRole.DisplayRole:
            return item.title  # 기본적으로 제목 반환

        return None
    
    def setData(self, index, value, role=Qt.ItemDataRole.UserRole):
        """✅ 데이터 업데이트"""
        if not index.isValid() or index.row() >= len(self.items):
            return False
        
        if role == Qt.ItemDataRole.UserRole:
            self.items[index.row()] = value
            self.dataChanged.emit(index, index, [role])
            return True
        
        return False
    
    def addItem(self, item: ContentItem):
        """실제 ContentItem을 바로 모델에 삽입"""
        row = self.rowCount()  # 항상 맨 끝에 삽입하거나, 필요한 위치를 row로 지정
        self.beginInsertRows(QModelIndex(), row, row)
        self.items.insert(row, item)  # ✅ None이 아닌 실제 아이템을 삽입
        self.endInsertRows()

    def removeRows(self, row, count, parent=QModelIndex()):
        """✅ 아이템 삭제"""
        self.beginRemoveRows(parent, row, row + count - 1)
        del self.items[row: row + count]
        self.endRemoveRows()
        return True

    def getRow(self, item):
        """✅ ContentItem 객체의 row 찾기"""
        try:
            row = self.items.index(item)
            return row
        except ValueError:
            return None
        
    def isEmpty(self):
        return len(self.items) == 0
