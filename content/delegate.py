from PySide6.QtWidgets import QStyledItemDelegate
from PySide6.QtCore import QSize

class ContentListDelegate(QStyledItemDelegate):
    """✅ 리스트 아이템을 렌더링하는 Delegate"""

    def paint(self, painter, option, index):
        """✅ QWidget 대신 `setIndexWidget()`을 사용하므로 빈 배경만 그림"""
        # 기본적으로 아이템을 직접 그리지 않음 (setIndexWidget() 사용)
        pass

    def sizeHint(self, option, index):
        #✅ 아이템 크기를 위젯 크기에 맞게 조정
        return QSize(450, 120)  # ✅ `ContentItemWidget`과 동일한 크기 설정