import os
import config.config as config
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpacerItem,
    QVBoxLayout,
)
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QEvent, QItemSelectionModel, QMetaObject, QObject, Qt, QTimer, QUrl


class _ComboBoxPopupHighlightResync(QObject):
    """콤보 팝업의 강조를 콤보의 실제 현재값으로 되돌린다 (#241 후속).

    Fusion의 콤보 팝업은 `SH_ComboBox_ListMouseTracking` 힌트 때문에 마우스가
    지나간 항목으로 `QItemSelectionModel`의 selection 자체를 옮겨버린다 —
    진짜 선택값이 `:selected` 상태를 잃는다(실측 확인 — `#240` 감사 후속으로
    native Windows 스타일과 대조해보니 이 힌트가 꺼져 있어 호버해도 selection이
    전혀 안 움직인다. `#227`의 Fusion 고정이 만든 동작이지 Qt 보편 동작이
    아니다). 그리고 팝업을 닫았다 다시 열어도 이 selection을 되돌리지 않는다
    — 그래서 "마지막으로 호버한 항목이 다음에 열어도 강조돼 있다"는 증상이
    난다.

    QSS로는 못 고친다: `::item:hover`와 `::item:selected`를 다른 색으로 나눠도,
    호버가 지나가는 순간 진짜 선택값 쪽 `:selected` 상태 자체가 사라지므로
    구분해서 칠할 대상이 없다.

    **`SH_ComboBox_ListMouseTracking`을 아예 꺼서 오염 자체를 없애는 방향도
    검토했지만 기각했다(`#241` 후속, `theme.build_style()`의 `SH_ComboBox_Popup`과
    같은 구조를 시도해본 것).** `QProxyStyle`로 이 힌트까지 0으로 강제해
    실측한 결과: (1) 오염은 실제로 멈춘다(`currentIndex`가 호버에 안 움직임)
    (2) 하지만 그 대가로 **호버 시각 피드백 자체가 통째로 사라진다** —
    `view.viewport().hasMouseTracking()`이 `False`로 떨어지고, 호버한 행과
    안 한 행의 렌더 픽셀이 완전히 같아진다(`::item:hover`가 반응할 상태
    자체가 안 생김). `viewport().setMouseTracking(True)`를 수동으로 다시
    켜봐도 복구되지 않는다 — Fusion은 이 힌트 하나에 "마우스 추적 켜짐"과
    "호버 시 selection 이동" 둘 다를 함께 묶어놨다(둘을 분리해 호버 페인트만
    남기는 하위 훅이 없다). 참고로 native Windows 스타일은 이 힌트가 항상
    0인데도 호버한 행만 다른 색으로 뚜렷이 바뀐다(실측 확인, 행마다 색이
    바뀜) — 즉 native는 이 힌트와 **무관한 별도 경로**로 호버를 그린다.
    Fusion에는 그 별도 경로가 없어서, 이 힌트를 끄면 Fusion만 호버 자체를
    잃는다. 결론: 이 힌트는 그대로 두고(`theme.build_style()`이 안 건드림,
    `TestComboBoxDropDownStyle`에 이 사실을 고정해 둠), 지금처럼 오염이 생긴
    *뒤에* 사후 복원하는 이 이벤트 필터를 유지한다 — 오염 자체를 막을 수
    없다면 되돌리는 것 말고는 방법이 없다.

    **왜 Hide 시점에 되돌리는가(Show가 아니라) — 깜빡임 회귀 후속.** 팝업
    컨테이너는 열고 닫을 때마다 새로 안 만들어지고 재사용된다(실측 확인,
    `view()`/`view().window()`의 파이썬 id가 여러 open/close 사이클에서 동일).
    그런데 `Show` 이벤트로 되돌리면 이미 늦다 — 실제 이벤트 순서를 로깅해
    확인한 결과 `view`/`viewport` 자신의 `Show`가 컨테이너(`window()`)의
    `Show`보다 먼저 온다. 그 사이에 재사용된 위젯이 이전 세션의(오염된)
    백킹스토어 내용으로 먼저 화면에 다시 노출됐다가, 우리 복원이 끝난
    뒤에야 새로 칠해진다 — 그리기 → 복원 → 다시 그리기가 되어 오너 실기에서
    한 프레임 깜빡였다. `Hide` 시점에 미리 되돌려 두면 팝업이 다음에 뜰 때는
    이미 깨끗한 상태라 이 이중 그리기 자체가 없다.

    **`Hide` 시점에 `combo.currentIndex()`를 바로 읽으면 안 된다.** 항목을
    클릭해 고르는 경우 `Hide` 이벤트가 먼저 오고 `combo.currentIndex()`는
    그 다음에야 갱신된다(실측 확인 — `Hide` 시점엔 아직 옛 값, 반면
    `view.currentIndex()`는 이미 방금 클릭한 값으로 정확하다). 그 순간
    `view`를 `combo.currentIndex()`(옛 값)로 되돌리면 방금 고른 값을
    도로 뭉갠다. `QTimer.singleShot(0, ...)`로 다음 이벤트 루프 턴까지
    미루면 그때는 `combo.currentIndex()`가 정착돼 있어 클릭 선택이든
    Escape·바깥 클릭으로 그냥 닫은 경우든 항상 맞는 값을 읽는다
    (`app/widgets/view.py::_scheduleRenumber`와 같은 컨텍스트 객체 패턴 —
    `self`가 콜백 전에 파괴되면 Qt가 알아서 취소한다).

    **`Show` 시점 복원도 남겨 둔다(보험, 근거).** 팝업을 한 번도 연 적 없는
    상태에서 `combo.setCurrentIndex()`(설정 로드 등)를 불러도 `view`는
    Qt가 알아서 따라간다(실측 확인) — 그래서 오늘 아는 모든 경로에서는
    `Hide` 쪽만으로 충분하다. 그래도 `Show` 쪽을 지우지 않는 이유는 (1)
    같은 값을 다시 써도 아무 부작용이 없고(멱등) (2) 앞으로 어떤 코드가
    팝업이 열려 있는 동안 `view()`를 직접 건드리는 경로가 생겨도 열릴 때
    한 번 더 방어선이 있는 편이 안전하기 때문 — 비용 없는 이중 방어다.
    """

    def __init__(self, combo: QComboBox) -> None:
        super().__init__(combo)
        self._combo = combo

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.Type.Show:
            self._resync()
        elif event.type() == QEvent.Type.Hide:
            QTimer.singleShot(0, self, self._resync)
        return False

    def _resync(self) -> None:
        combo = self._combo
        view = combo.view()
        index = combo.model().index(combo.currentIndex(), combo.modelColumn(), combo.rootModelIndex())
        view.setCurrentIndex(index)
        selection_model = view.selectionModel()
        if selection_model is not None:
            selection_model.select(index, QItemSelectionModel.SelectionFlag.ClearAndSelect)


def _wire_popup_highlight_resync(combo: QComboBox) -> None:
    combo.view().window().installEventFilter(_ComboBoxPopupHighlightResync(combo))


# 라벨 열 / 필드 열 — 구 QFormLayout의 LabelRole / FieldRole 자리
_LABEL_COLUMN = 0
_FIELD_COLUMN = 1
# 구 QFormLayout이 라벨에 적용하던 정렬(Fusion의 SH_FormLayoutLabelAlignment = 왼쪽).
# 정렬을 주면 라벨 위젯은 셀을 채우지 않고 자기 sizeHint 폭으로 줄어 왼쪽에 붙는다
# — 구 폼이 라벨 열을 그리던 방식과 같다
_LABEL_ALIGNMENT = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter


def _label_field_grid(box: QGroupBox, object_name: str) -> QGridLayout:
    """그룹 상자 안의 「라벨 열 + 필드 열」 격자를 만든다 (v2.9.7 S2 — QFormLayout 대체).

    QFormLayout이 암묵적으로 해주던 것 가운데 이 화면이 실제로 쓰고 있던 둘을
    명시적으로 재현한다(1단계 실측 — 그 밖의 buddy·니모닉·행 줄바꿈은 쓰이지
    않았다):

    - **필드 성장**(FieldGrowthPolicy.AllNonFixedFieldsGrow): 남는 가로 공간은
      필드 열이 전부 흡수하고 라벨 열은 가장 넓은 라벨의 sizeHint 폭에 머문다
      → 열 stretch 0/1.
    - **라벨 정렬**(labelAlignment = 왼쪽): 각 addWidget에 `_LABEL_ALIGNMENT`를
      준다 — 격자는 정렬을 주지 않으면 라벨을 셀 폭으로 늘린다.

    간격·여백은 주지 않는다 — QFormLayout과 QGridLayout 모두 스타일의
    PM_Layout* 기본값을 쓰므로 그대로 두어야 같은 값(Fusion 6/9)이 나온다.
    """
    grid = QGridLayout(box)
    grid.setObjectName(object_name)
    grid.setColumnStretch(_LABEL_COLUMN, 0)
    grid.setColumnStretch(_FIELD_COLUMN, 1)
    return grid


class SettingDialog(QDialog):
    """설정 창 — 쿠키 · 다운로드 후 동작 · 언어 · 로그 폴더.

    위젯·레이아웃은 `.ui` 생성물 없이 이 파일이 직접 짠다(#244 ③). 소유자가 Qt
    Designer를 쓰지 않으므로 `.ui`를 정본으로 둘 이유가 없었다. 구 `ui/settingDialog.py`
    (uic 생성물)의 `setupUi`·`retranslateUi`를 그대로 옮긴 것이며 구조·objectName·
    번역 원문은 바꾸지 않았다 — ⚠️ objectName 넷(`nidaut`·`nidses`·`afterDownload`·
    `language`)은 계약 게이트(tests/unit/test_setting_dialog_contract.py)의 일부이고,
    번역 컨텍스트는 클래스 이름(`SettingDialog`)이라 원문 11개를 글자 하나 바꾸면
    `.ts`에 항목이 새로 생기고 옛 항목이 지워진다(`.qm` 재컴파일 = rc 대상).
    """

    def __init__(self, parent=None):
        super().__init__(parent)

        self.config = config.load_config()
        self.worker = None

        self.setupUi(self)
        self.setupDynamicUi()

    def setupUi(self, dialog: QDialog) -> None:
        """위젯·레이아웃 조립 — 구 uic 생성물의 setupUi와 같은 구조·순서·objectName."""
        if not dialog.objectName():
            dialog.setObjectName("SettingDialog")
        dialog.resize(400, 350)
        self.settingLayout = QVBoxLayout(dialog)
        self.settingLayout.setObjectName("settingLayout")
        self.dialogLayout = QVBoxLayout()
        self.dialogLayout.setObjectName("dialogLayout")

        # ---- 쿠키 ----
        self.cookiesBox = QGroupBox(dialog)
        self.cookiesBox.setObjectName("cookiesBox")
        self.cookiesGridLayout = _label_field_grid(self.cookiesBox, "cookiesGridLayout")
        self.nidautLabel = QLabel(self.cookiesBox)
        self.nidautLabel.setObjectName("nidautLabel")
        self.cookiesGridLayout.addWidget(self.nidautLabel, 0, _LABEL_COLUMN, _LABEL_ALIGNMENT)
        self.nidaut = QLineEdit(self.cookiesBox)
        self.nidaut.setObjectName("nidaut")
        self.nidaut.setClearButtonEnabled(True)
        self.cookiesGridLayout.addWidget(self.nidaut, 0, _FIELD_COLUMN)
        self.nidsesLabel = QLabel(self.cookiesBox)
        self.nidsesLabel.setObjectName("nidsesLabel")
        self.cookiesGridLayout.addWidget(self.nidsesLabel, 1, _LABEL_COLUMN, _LABEL_ALIGNMENT)
        self.nidses = QLineEdit(self.cookiesBox)
        self.nidses.setObjectName("nidses")
        self.nidses.setClearButtonEnabled(True)
        self.cookiesGridLayout.addWidget(self.nidses, 1, _FIELD_COLUMN)
        self.helpButton = QPushButton(self.cookiesBox)
        self.helpButton.setObjectName("helpButton")
        # 도움말 버튼은 구 폼의 라벨 자리(라벨 열)에 있었다 — 자리와 정렬을 그대로 둔다
        self.cookiesGridLayout.addWidget(self.helpButton, 2, _LABEL_COLUMN, _LABEL_ALIGNMENT)
        self.cookieSpacer = QSpacerItem(
            0, 0, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum
        )
        self.cookiesGridLayout.addItem(self.cookieSpacer, 2, _FIELD_COLUMN)
        self.dialogLayout.addWidget(self.cookiesBox)

        # ---- 다운로드 ----
        self.downloadBox = QGroupBox(dialog)
        self.downloadBox.setObjectName("downloadBox")
        self.downloadGridLayout = _label_field_grid(self.downloadBox, "downloadGridLayout")
        self.afterDownloadLabel = QLabel(self.downloadBox)
        self.afterDownloadLabel.setObjectName("afterDownloadLabel")
        self.downloadGridLayout.addWidget(self.afterDownloadLabel, 0, _LABEL_COLUMN, _LABEL_ALIGNMENT)
        self.afterDownload = QComboBox(self.downloadBox)
        self.afterDownload.setObjectName("afterDownload")
        self.downloadGridLayout.addWidget(self.afterDownload, 0, _FIELD_COLUMN)
        self.dialogLayout.addWidget(self.downloadBox)

        # ---- 일반 ----
        self.commonBox = QGroupBox(dialog)
        self.commonBox.setObjectName("commonBox")
        self.commonGridLayout = _label_field_grid(self.commonBox, "commonGridLayout")
        self.languageLabel = QLabel(self.commonBox)
        self.languageLabel.setObjectName("languageLabel")
        self.commonGridLayout.addWidget(self.languageLabel, 0, _LABEL_COLUMN, _LABEL_ALIGNMENT)
        self.language = QComboBox(self.commonBox)
        self.language.setObjectName("language")
        self.commonGridLayout.addWidget(self.language, 0, _FIELD_COLUMN)
        self.logsFolderLabel = QLabel(self.commonBox)
        self.logsFolderLabel.setObjectName("logsFolderLabel")
        self.commonGridLayout.addWidget(self.logsFolderLabel, 1, _LABEL_COLUMN, _LABEL_ALIGNMENT)
        self.logsFolder = QPushButton(self.commonBox)
        self.logsFolder.setObjectName("logsFolder")
        self.commonGridLayout.addWidget(self.logsFolder, 1, _FIELD_COLUMN)
        self.dialogLayout.addWidget(self.commonBox)

        self.settingLayout.addLayout(self.dialogLayout)

        # ---- OK / Cancel ----
        self.dialogButtonBox = QDialogButtonBox(dialog)
        self.dialogButtonBox.setObjectName("dialogButtonBox")
        self.dialogButtonBox.setStandardButtons(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        self.settingLayout.addWidget(self.dialogButtonBox)

        self.retranslateUi(dialog)
        self.dialogButtonBox.accepted.connect(dialog.accept)
        self.dialogButtonBox.rejected.connect(dialog.reject)
        QMetaObject.connectSlotsByName(dialog)

    def retranslateUi(self, dialog: QDialog) -> None:
        """표시 문자열 — 원문 11개는 구 `.ui`와 글자 하나 다르지 않다(번역 컨텍스트 SettingDialog)."""
        dialog.setWindowTitle(self.tr("Settings"))
        self.cookiesBox.setTitle(self.tr("Cookies"))
        self.nidautLabel.setText(self.tr("NID_AUT"))
        self.nidsesLabel.setText(self.tr("NID_SES"))
        self.helpButton.setText(self.tr("Help"))
        self.downloadBox.setTitle(self.tr("Download"))
        self.afterDownloadLabel.setText(self.tr("After Download"))
        self.commonBox.setTitle(self.tr("Common"))
        self.languageLabel.setText(self.tr("Language"))
        self.logsFolderLabel.setText(self.tr("Logs Folder"))
        self.logsFolder.setText(self.tr("Open"))

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
        _wire_popup_highlight_resync(self.afterDownload)

        self.language.addItem("English", "en_US") # 언어 선택을 위한 QComboBox 생성 TODO: 언어 리스트는 project.pro에서 관리
        self.language.addItem("한국어", "ko_KR")

        currentLang = self.config.get("language", "en_US") # 현재 설정된 언어에 맞는 인덱스 찾기
        index = self.language.findData(currentLang)
        if index != -1:
            self.language.setCurrentIndex(index)
        _wire_popup_highlight_resync(self.language)

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