import sys
import os
from PyQt5.QtWidgets import QApplication
from PyQt5.QtGui import QIcon

from ui.vodDownloader import VodDownloader

if __name__ == '__main__':
    app = QApplication(sys.argv)

    icon_path = os.path.join(os.path.dirname(__file__), 'resources', 'chzzk.ico')
    app.setWindowIcon(QIcon(icon_path))

    # 메인 UI 실행
    ex = VodDownloader()
    ex.setWindowIcon(QIcon(icon_path))

    sys.exit(app.exec_())
