import os
import config.config as config
from PySide6.QtWidgets import QComboBox, QDialog, QMessageBox
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QEvent, QItemSelectionModel, QObject, QUrl

from ui.settingDialog import Ui_SettingDialog


class _ComboBoxPopupHighlightResync(QObject):
    """콤보 팝업이 열릴 때 강조를 콤보의 실제 현재값으로 되돌린다 (#241 후속).

    Qt의 콤보 팝업은 마우스가 지나간 항목으로 `QItemSelectionModel`의 selection
    자체를 옮겨버린다 — 진짜 선택값이 `:selected` 상태를 잃는다(실측 확인:
    스타일시트를 아예 안 입힌 순정 `QComboBox`에서도 재현되는 Qt 자체 동작이다).
    그리고 팝업을 닫았다 다시 열어도 이 selection을 되돌리지 않는다 — 그래서
    "마지막으로 호버한 항목이 다음에 열어도 강조돼 있다"는 증상이 난다.

    QSS로는 못 고친다: `::item:hover`와 `::item:selected`를 다른 색으로 나눠도,
    호버가 지나가는 순간 진짜 선택값 쪽 `:selected` 상태 자체가 사라지므로
    구분해서 칠할 대상이 없다. 팝업이 뜨는 시점(`Show` 이벤트)마다 selection을
    코드로 직접 되돌리는 것 외에 방법이 없다.
    """

    def __init__(self, combo: QComboBox) -> None:
        super().__init__(combo)
        self._combo = combo

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.Type.Show:
            combo = self._combo
            view = combo.view()
            index = combo.model().index(combo.currentIndex(), combo.modelColumn(), combo.rootModelIndex())
            view.setCurrentIndex(index)
            selection_model = view.selectionModel()
            if selection_model is not None:
                selection_model.select(index, QItemSelectionModel.SelectionFlag.ClearAndSelect)
        return False


def _resync_popup_highlight_on_show(combo: QComboBox) -> None:
    combo.view().window().installEventFilter(_ComboBoxPopupHighlightResync(combo))


class SettingDialog(QDialog, Ui_SettingDialog):
    """
    쿠키 설정을 위한 팝업창 예시.
    이전에 저장된 쿠키값을 인자로 받아, QLineEdit에 미리 세팅한다.
    """

    def __init__(self, parent=None):
        super().__init__(parent)

        self.config = config.load_config()
        self.worker = None

        self.setupUi(self)
        self.setupDynamicUi()

    def setupDynamicUi(self):
        self.nidaut.setText(self.config.get("cookies", {}).get("NID_AUT", "")) # 쿠키값을 불러와서 QLineEdit에 세팅
        self.nidses.setText(self.config.get("cookies", {}).get("NID_SES", ""))

        self.helpButton.clicked.connect(self.showHelp) # 도움말 버튼 클릭 시 showHelp 메소드 호출

        self.afterDownload.addItem(self.tr("none"), "none") # 다운로드 완료 후 동작을 선택할 수 있는 QComboBox 생성
        self.afterDownload.addItem(self.tr("sleep"), "sleep")
        self.afterDownload.addItem(self.tr("shutdown"), "shutdown")

        currentAfterDownload = self.config.get("afterDownload", "none") # 현재 설정된 afterDownload 값을 불러옴
        index = self.afterDownload.findData(currentAfterDownload)
        if index != -1:
            self.afterDownload.setCurrentIndex(index)
        _resync_popup_highlight_on_show(self.afterDownload)

        self.language.addItem("English", "en_US") # 언어 선택을 위한 QComboBox 생성 TODO: 언어 리스트는 project.pro에서 관리
        self.language.addItem("한국어", "ko_KR")

        currentLang = self.config.get("language", "en_US") # 현재 설정된 언어에 맞는 인덱스 찾기
        index = self.language.findData(currentLang)
        if index != -1:
            self.language.setCurrentIndex(index)
        _resync_popup_highlight_on_show(self.language)

        self.logsFolder.clicked.connect(self.openLogsFolder) # 로그 폴더 열기 버튼 클릭 시 openLogsFolder 메소드 호출

    def accept(self):
        self.config['cookies'] = {"NID_AUT": self.nidaut.text(), "NID_SES": self.nidses.text()}
        self.config['afterDownload'] = self.afterDownload.currentData()
        self.config['language'] = self.language.currentData()  # 선택된 언어 코드 저장
        config.save_config(self.config)
        return super().accept()
    
    def reject(self):
        return super().reject()

    def showHelp(self):
        """
        쿠키를 얻는 방법 안내 메시지.
        """
        link = "https://chzzk.naver.com"
        msg = self.tr(
            "How to get a Chzzk cookie<br>"
            "1. Log in to <a href='{}'>Chzzk</a>.<br>"
            "2. Press F12 to open the developer tool. <br>"
            "3. Click Cookies > https://chzzk.naver.com on the Application tab. <br>"
            "4. Add the values of 'NID_AUT' and 'NID_SES'."
            ).format(link, link)
        QMessageBox.information(self, self.tr("Helper"), msg)

    def openLogsFolder(self):
        # os.startfile은 Windows 전용이라 macOS·Linux에서 무조건 AttributeError였다
        # (#181). QDesktopServices.openUrl은 3-OS 공통으로 폴더를 연다.
        path = os.path.join(config.CONFIG_DIR, "logs")
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
            QMessageBox.warning(self, self.tr("Warning"), f"'{path}'을(를) 열 수 없습니다.")

    def getCookies(self):
        """
        호출 측에서 다이얼로그가 닫힌 후, 입력한 쿠키값을 받아갈 수 있도록 하는 헬퍼 함수.
        """
        return self.nidaut.text(), self.nidses.text()
    
    def onApply(self):
        """
        '적용' 버튼을 클릭하면 설정 값을 저장하고 다이얼로그를 닫는다.
        """