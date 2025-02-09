import sys
import os
from PySide6.QtWidgets import QApplication, QMainWindow
from PySide6.QtGui import QIcon

from downloader.main_window import VodDownloader

class main(QMainWindow):
    def __init__(self):
        super().__init__()


if __name__ == '__main__':
    app = QApplication(sys.argv)

    icon_path = os.path.join(os.path.dirname(__file__), 'resources', 'chzzk.ico')
    app.setWindowIcon(QIcon(icon_path))
    # 메인 UI 실행
    ex = VodDownloader()
    ex.setWindowIcon(QIcon(icon_path))

    sys.exit(app.exec())
