# coding: utf-8
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout

from qfluentwidgets import TitleLabel, CaptionLabel


class BaseInterface(QWidget):
    """공통 인터페이스 기반 클래스

    제목 헤더와 콘텐츠 영역 레이아웃을 제공한다.
    서브클래스는 self.contentLayout 에 위젯을 추가한다.
    """

    def __init__(self, title: str, subtitle: str = '', parent=None):
        super().__init__(parent=parent)
        self.titleLabel = TitleLabel(title, self)

        self._vBoxLayout = QVBoxLayout(self)
        self.contentLayout = QVBoxLayout()

        self.__initLayout(subtitle)
        self.setObjectName(title.replace(' ', '-'))

    def __initLayout(self, subtitle: str):
        self._vBoxLayout.setContentsMargins(36, 20, 36, 20)
        self._vBoxLayout.setSpacing(0)
        self._vBoxLayout.addWidget(self.titleLabel)
        self._vBoxLayout.addSpacing(16)

        if subtitle:
            self.subtitleLabel = CaptionLabel(subtitle, self)
            self._vBoxLayout.addWidget(self.subtitleLabel)
            self._vBoxLayout.addSpacing(8)

        # stretch=1 로 추가해서 contentLayout이 남은 공간을 모두 차지하게 함
        self._vBoxLayout.addLayout(self.contentLayout, 1)