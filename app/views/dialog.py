import app.theme as theme
from app.system import settings
from app.widgets.choice_combo import ChoiceCombo
from app.widgets.focus_ring import watch_dialog_buttons
from PySide6.QtWidgets import (
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
from PySide6.QtCore import (
    QMetaObject,
    QSize,
    Qt,
    QUrl,
)


# 라벨 열 / 필드 열 — 구 QFormLayout의 LabelRole / FieldRole 자리
_LABEL_COLUMN = 0
_FIELD_COLUMN = 1
# 구 QFormLayout이 라벨에 적용하던 정렬 — 가로는 왼쪽(Fusion의 SH_FormLayoutLabelAlignment),
# 세로는 **행 상단**. 정렬을 주면 라벨 위젯은 셀을 채우지 않고 자기 sizeHint 크기로 줄어
# 왼쪽 위에 붙는다. 세로 위치가 구 폼과 같아지려면 sizeHint **높이**도 구 폼 규칙이어야
# 한다 — 그것은 `_FormLabel`이 낸다.
_LABEL_ALIGNMENT = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop


class _FormLabel(QLabel):
    """구 QFormLayout이 라벨에 주던 높이를 sizeHint로 내는 라벨 (v2.9.7 S2 후속).

    QFormLayout은 라벨을 행 상단에 놓되 높이를 `min(행 높이, 라벨 sizeHint 높이 × 7/4)`로
    준다(qformlayout.cpp `arrangeWidgets` — 세로로 안 늘어나는 라벨의 `qMin(height,
    qMin(label->sizeHint.height() * 7 / 4, label->maxSize.height()))`). 글자는 그 높이
    안에서 가운데 그려지므로, 격자에서 라벨을 sizeHint 그대로(15px) 상단에 붙이면
    글자가 5px 위로, AlignVCenter로 두면 2px 아래로 어긋난다(실측·오너 실기 확인).
    sizeHint 높이를 같은 규칙(×7/4)으로 내면 `_LABEL_ALIGNMENT`(왼쪽·상단)와 합쳐
    구 폼과 같은 자리에 같은 높이로 놓인다. 행 높이 상한은 두지 않는다 — 이 화면의
    필드(29px)는 전부 그보다 높아 상한이 걸리지 않고, 폰트가 바뀌어도 비율이라 같이 간다.
    """

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        return QSize(hint.width(), hint.height() * 7 // 4)


def _label_field_grid(box: QGroupBox, object_name: str) -> QGridLayout:
    """그룹 상자 안의 「라벨 열 + 필드 열」 격자를 만든다 (v2.9.7 S2 — QFormLayout 대체).

    QFormLayout이 암묵적으로 해주던 것 가운데 이 화면이 실제로 쓰고 있던 둘을
    명시적으로 재현한다(1단계 실측 — 그 밖의 buddy·니모닉·행 줄바꿈은 쓰이지
    않았다):

    - **필드 성장**(FieldGrowthPolicy.AllNonFixedFieldsGrow): 남는 가로 공간은
      필드 열이 전부 흡수하고 라벨 열은 가장 넓은 라벨의 sizeHint 폭에 머문다
      → 열 stretch 0/1.
    - **라벨 정렬**(가로 왼쪽 + 세로 상단): 각 addWidget에 `_LABEL_ALIGNMENT`를
      준다 — 격자는 정렬을 주지 않으면 라벨을 셀 폭으로 늘리고, 세로 정렬을
      가운데로 주면 구 폼보다 라벨이 내려앉는다.

    간격·여백은 주지 않는다 — QFormLayout과 QGridLayout 모두 스타일의
    PM_Layout* 기본값을 쓰므로 그대로 두어야 같은 값(Fusion 6/9)이 나온다.

    행을 다 넣은 뒤 `_pin_rows_to_top()`을 불러야 한다 — 세 번째로 살려야 했던
    암묵 동작(남는 세로 공간의 행선지)은 그쪽이 맡는다.
    """
    grid = QGridLayout(box)
    grid.setObjectName(object_name)
    grid.setColumnStretch(_LABEL_COLUMN, 0)
    grid.setColumnStretch(_FIELD_COLUMN, 1)
    return grid


def _pin_rows_to_top(grid: QGridLayout) -> None:
    """남는 세로 공간을 행 아래 빈 행 하나에 몰아, 행들이 구 QFormLayout처럼 상단에 붙게 한다.

    창을 최소 높이보다 키우면 그룹 상자 셋이 세로로 같이 늘어나는데, QGridLayout은
    늘어날 수 있는 행이 없으면 남는 공간을 **행 앞·사이·뒤에 나눠 뿌린다**
    (qlayoutengine.cpp `qGeomCalc` — 모든 항목이 최대 크기에 닿으면 남는 공간을
    `count + 2` 등분해 첫 행 앞에 한 몫, 행 사이마다 한 몫, 끝에 두 몫). 행 하나짜리
    상자는 행이 가운데로 내려오고, 행 둘은 벌어진다. 구 QFormLayout은 남는 공간을
    전부 마지막 행 뒤에 두어 행들이 상단에 머물렀다(실측·오너 실기 확인 — 쿠키 상자만
    3행째의 가로 스페이서(세로 Minimum)가 그 공간을 흡수해 홀로 상단에 남아 있었다).

    빈 꼬리 행에 stretch 1을 주면 남는 공간은 그 행이 전부 가져가고 실제 행들은
    자기 sizeHint 높이로 위에서부터 쌓인다. 빈 행은 sizeHint·최소 크기에 아무것도
    보태지 않으므로(빈 행에는 행 간격도 붙지 않는다) 창 최소 크기와 최소 높이에서의
    배치는 이 호출 전과 같다.
    """
    grid.setRowStretch(grid.rowCount(), 1)


class SettingDialog(QDialog):
    """설정 창 — 쿠키 · 다운로드 후 동작 · 언어 · 로그 폴더.

    위젯·레이아웃은 `.ui` 생성물 없이 이 파일이 직접 짠다(#244 ③). 소유자가 Qt
    Designer를 쓰지 않으므로 `.ui`를 정본으로 둘 이유가 없었다. 구 `ui/settingDialog.py`
    (uic 생성물)의 `setupUi`·`retranslateUi`를 그대로 옮긴 것이며 구조·objectName·
    번역 원문은 바꾸지 않았다 — ⚠️ objectName 넷(`nidaut`·`nidses`·`afterDownload`·
    `language`)은 계약 게이트(tests/app/gui/test_setting_dialog_contract.py)의 일부이고,
    번역 컨텍스트는 클래스 이름(`SettingDialog`)이라 원문 11개를 글자 하나 바꾸면
    `.ts`에 항목이 새로 생기고 옛 항목이 지워진다(`.qm` 재컴파일 = rc 대상).
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        self.worker = None

        self.setupUi(self)
        self.setupDynamicUi()
        self._keyFocus = watch_dialog_buttons(self)  # 키보드 초점에만 버튼 테두리(#622)

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
        self.nidautLabel = _FormLabel(self.cookiesBox)
        self.nidautLabel.setObjectName("nidautLabel")
        self.cookiesGridLayout.addWidget(self.nidautLabel, 0, _LABEL_COLUMN, _LABEL_ALIGNMENT)
        self.nidaut = QLineEdit(self.cookiesBox)
        self.nidaut.setObjectName("nidaut")
        self.nidaut.setClearButtonEnabled(True)
        self.cookiesGridLayout.addWidget(self.nidaut, 0, _FIELD_COLUMN)
        self.nidsesLabel = _FormLabel(self.cookiesBox)
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
        _pin_rows_to_top(self.cookiesGridLayout)
        self.dialogLayout.addWidget(self.cookiesBox)

        # ---- 다운로드 ----
        self.downloadBox = QGroupBox(dialog)
        self.downloadBox.setObjectName("downloadBox")
        self.downloadGridLayout = _label_field_grid(self.downloadBox, "downloadGridLayout")
        self.afterDownloadLabel = _FormLabel(self.downloadBox)
        self.afterDownloadLabel.setObjectName("afterDownloadLabel")
        self.downloadGridLayout.addWidget(
            self.afterDownloadLabel, 0, _LABEL_COLUMN, _LABEL_ALIGNMENT
        )
        self.afterDownload = ChoiceCombo(self.downloadBox)
        self.afterDownload.setObjectName("afterDownload")
        self.downloadGridLayout.addWidget(self.afterDownload, 0, _FIELD_COLUMN)
        _pin_rows_to_top(self.downloadGridLayout)
        self.dialogLayout.addWidget(self.downloadBox)

        # ---- 일반 ----
        self.commonBox = QGroupBox(dialog)
        self.commonBox.setObjectName("commonBox")
        self.commonGridLayout = _label_field_grid(self.commonBox, "commonGridLayout")
        self.languageLabel = _FormLabel(self.commonBox)
        self.languageLabel.setObjectName("languageLabel")
        self.commonGridLayout.addWidget(self.languageLabel, 0, _LABEL_COLUMN, _LABEL_ALIGNMENT)
        self.language = ChoiceCombo(self.commonBox)
        self.language.setObjectName("language")
        self.commonGridLayout.addWidget(self.language, 0, _FIELD_COLUMN)
        self.logsFolderLabel = _FormLabel(self.commonBox)
        self.logsFolderLabel.setObjectName("logsFolderLabel")
        self.commonGridLayout.addWidget(self.logsFolderLabel, 1, _LABEL_COLUMN, _LABEL_ALIGNMENT)
        self.logsFolder = QPushButton(self.commonBox)
        self.logsFolder.setObjectName("logsFolder")
        self.commonGridLayout.addWidget(self.logsFolder, 1, _FIELD_COLUMN)
        _pin_rows_to_top(self.commonGridLayout)
        self.dialogLayout.addWidget(self.commonBox)

        self.settingLayout.addLayout(self.dialogLayout)

        # ---- OK / Cancel ----
        self.dialogButtonBox = QDialogButtonBox(dialog)
        self.dialogButtonBox.setObjectName("dialogButtonBox")
        self.dialogButtonBox.setStandardButtons(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        # OK가 주 동작(강조색 채움 — 편집 창 저장 · 구간 창 확인과 같다, #622). 버튼 상자가 만들며 이미 스타일을
        # 입혔다 — 속성을 바꾼 뒤 다시 입힌다
        ok = self.dialogButtonBox.button(QDialogButtonBox.StandardButton.Ok)
        ok.setProperty("role", "primary")
        theme.repolish(ok)
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

    def setupDynamicUi(self) -> None:
        cookies = settings.load_cookies()  # 창을 열 때의 값 — 저장은 OK에서 이 창의 세 키만 (#519)
        self.nidaut.setText(cookies["NID_AUT"])  # 쿠키값을 불러와서 QLineEdit에 세팅
        self.nidses.setText(cookies["NID_SES"])

        self.helpButton.clicked.connect(self.showHelp)  # 도움말 버튼 클릭 시 showHelp 메소드 호출

        self.afterDownload.addItem(
            self.tr("none"), "none"
        )  # 다운로드 완료 후 동작을 선택할 수 있는 QComboBox 생성
        self.afterDownload.addItem(self.tr("sleep"), "sleep")
        self.afterDownload.addItem(self.tr("shutdown"), "shutdown")

        # 현재 설정된 afterDownload 값을 불러옴
        currentAfterDownload = settings.load_setting("afterDownload")
        index = self.afterDownload.findData(currentAfterDownload)
        if index != -1:
            self.afterDownload.setCurrentIndex(index)

        self.language.addItem(
            "English", "en_US"
        )  # 언어 선택을 위한 QComboBox 생성 TODO: 언어 리스트는 project.pro에서 관리
        self.language.addItem("한국어", "ko_KR")

        currentLang = settings.load_setting("language")  # 현재 설정된 언어에 맞는 인덱스 찾기
        index = self.language.findData(currentLang)
        if index != -1:
            self.language.setCurrentIndex(index)

        self.logsFolder.clicked.connect(
            self.openLogsFolder
        )  # 로그 폴더 열기 버튼 클릭 시 openLogsFolder 메소드 호출

    def accept(self) -> None:
        # 이 창이 보이는 세 키만 쓴다 — 창을 연 뒤 다른 곳이 바꾼 키를 되돌리지 않는다 (#519)
        settings.update_settings(
            {
                "cookies": {"NID_AUT": self.nidaut.text(), "NID_SES": self.nidses.text()},
                "afterDownload": self.afterDownload.currentData(),
                "language": self.language.currentData(),  # 선택된 언어 코드 저장
            }
        )
        return super().accept()

    def reject(self) -> None:
        return super().reject()

    def showHelp(self) -> None:
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

    def openLogsFolder(self) -> None:
        # os.startfile은 Windows 전용이라 macOS·Linux에서 무조건 AttributeError였다
        # (#181). QDesktopServices.openUrl은 3-OS 공통으로 폴더를 연다.
        path = settings.logs_dir()
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
            QMessageBox.warning(self, self.tr("Warning"), f"'{path}'을(를) 열 수 없습니다.")
