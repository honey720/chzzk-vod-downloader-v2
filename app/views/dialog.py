"""설정 창 — 다섯 탭(일반 · 다운로드 · 화면 · 계정 · 고급) · 바뀐 값 저장 · 취소 · 닫기 확인 (#644)."""

import app.theme as theme
from app.system import settings
from app.widgets.choice_combo import ChoiceCombo
from app.widgets.focus_ring import watch_dialog_buttons
from app.widgets.header_controls import CHIP_GAP, FilterChip
from app.widgets.tip_bubble import close_floats_if_left, install_tips
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpacerItem,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtGui import QCloseEvent, QDesktopServices, QKeySequence, QShortcut
from PySide6.QtCore import (
    QEvent,
    QKeyCombination,
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

TAB_KEYS = (
    "general",
    "download",
    "display",
    "account",
    "advanced",
)  # 탭 차례 — 탭 버튼 · 쪽 이름의 머리
CLOSE_SAVE = "save"  # 닫기 확인 — 저장하고 닫는다
CLOSE_DISCARD = "discard"  # 닫기 확인 — 저장하지 않고 닫는다
CLOSE_BACK = "back"  # 닫기 확인 — 닫지 않고 설정 창으로 돌아간다(기본 · Esc)


def _tab_switch_keys(standard: QKeySequence.StandardKey) -> list[QKeySequence]:
    """탭 넘기기 표준 키의 OS 키 전부에, Shift+Backtab 키마다 같은 Shift+Tab 키를 더해 돌려준다.

    Qt 표준 키 PreviousChild는 `Ctrl+Shift+Backtab`이다. 그런데 실제 키보드로 Ctrl+Shift+Tab을 누르면
    단축키 후보는 OS 키 배치(Windows · macOS 키 매퍼)가 물리 키 Tab으로 만든 `Ctrl+Shift+Tab`이라
    Backtab 키와 맞지 않는다 — 앞 탭 키가 듣지 않았다(오너 실기 2026-11-27, #644). `QKeySequence(표준 키)`는
    첫 키만 담으므로 `keyBindings`로 전부 받는다.
    """
    keys = QKeySequence.keyBindings(standard)
    for key in list(keys):
        combo = key[0]
        if combo.key() == Qt.Key.Key_Backtab:
            tab = QKeySequence(QKeyCombination(combo.keyboardModifiers(), Qt.Key.Key_Tab))
            if tab not in keys:
                keys.append(tab)
    return keys


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


def _label_field_grid(page: QWidget, object_name: str) -> QGridLayout:
    """탭 쪽 안의 「라벨 열 + 필드 열」 격자를 만든다 (v2.9.7 S2 — QFormLayout 대체).

    QFormLayout이 암묵적으로 해주던 것 가운데 이 화면이 실제로 쓰고 있던 둘을
    명시적으로 재현한다(1단계 실측 — 그 밖의 buddy·니모닉·행 줄바꿈은 쓰이지
    않았다):

    - **필드 성장**(FieldGrowthPolicy.AllNonFixedFieldsGrow): 남는 가로 공간은
      필드 열이 전부 흡수하고 라벨 열은 가장 넓은 라벨의 sizeHint 폭에 머문다
      → 열 stretch 0/1.
    - **라벨 정렬**(가로 왼쪽 + 세로 상단): 각 addWidget에 `_LABEL_ALIGNMENT`를
      준다 — 격자는 정렬을 주지 않으면 라벨을 셀 폭으로 늘리고, 세로 정렬을
      가운데로 주면 구 폼보다 라벨이 내려앉는다.

    간격은 주지 않는다 — QFormLayout과 QGridLayout 모두 스타일의 PM_Layout* 기본값을 쓰므로
    그대로 두어야 같은 값(Fusion 6/9)이 나온다. 여백은 0이다 — 탭 쪽의 바깥 여백은 창 레이아웃이
    맡는다(#644 — 탭 줄 왼쪽 끝과 칸 왼쪽 끝이 같은 선).

    행을 다 넣은 뒤 `_pin_rows_to_top()`을 불러야 한다 — 세 번째로 살려야 했던
    암묵 동작(남는 세로 공간의 행선지)은 그쪽이 맡는다.
    """
    grid = QGridLayout(page)
    grid.setObjectName(object_name)
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setColumnStretch(_LABEL_COLUMN, 0)
    grid.setColumnStretch(_FIELD_COLUMN, 1)
    return grid


def _pin_rows_to_top(grid: QGridLayout) -> None:
    """남는 세로 공간을 행 아래 빈 행 하나에 몰아, 행들이 구 QFormLayout처럼 상단에 붙게 한다.

    창을 최소 높이보다 키우면 탭 쪽이 세로로 늘어나는데, QGridLayout은 늘어날 수 있는 행이
    없으면 남는 공간을 **행 앞·사이·뒤에 나눠 뿌린다**(qlayoutengine.cpp `qGeomCalc` — 모든
    항목이 최대 크기에 닿으면 남는 공간을 `count + 2` 등분해 첫 행 앞에 한 몫, 행 사이마다 한
    몫, 끝에 두 몫). 행 하나짜리 쪽은 행이 가운데로 내려오고, 행 둘은 벌어진다. 구 QFormLayout은
    남는 공간을 전부 마지막 행 뒤에 두어 행들이 상단에 머물렀다(실측·오너 실기 확인).

    빈 꼬리 행에 stretch 1을 주면 남는 공간은 그 행이 전부 가져가고 실제 행들은
    자기 sizeHint 높이로 위에서부터 쌓인다. 빈 행은 sizeHint·최소 크기에 아무것도
    보태지 않으므로(빈 행에는 행 간격도 붙지 않는다) 창 최소 크기와 최소 높이에서의
    배치는 이 호출 전과 같다.
    """
    grid.setRowStretch(grid.rowCount(), 1)


class SettingDialog(QDialog):
    """설정 창 — 탭 다섯 · 바뀐 값이 있을 때만 켜지는 저장 · 늘 켜진 취소 · 닫기 확인 (#644).

    탭 줄은 헤더바 상태 필터 칩과 같은 부품(`FilterChip`, `role="filter"`)이고 모양은 전역 QSS
    `QFrame#settingsTabs`가 칠한다. 탭을 누르면 아래 쪽(`QStackedWidget`)이 바뀐다. 항목 자리:
    일반 = 언어 · 다운로드 후 동작, 계정 = 쿠키 두 칸 · 도움말, 고급 = 로그 폴더. 다운로드 · 화면 탭은
    뒤따르는 PR이 항목을 더할 자리다.

    저장 · 취소: 창을 열 때 칸 값을 키마다 적어 두고(`_initial`), 지금 칸 값과 다른 키가 있을 때만 저장을
    켠다. 취소는 늘 켜져 있다 — 바뀐 값이 없으면 그냥 닫고, 있으면 버리고 닫는다(오너 2026-11-27).
    저장(버튼 · Ctrl+S · `accept`)은 바뀐 키만 `update_settings`로 한 번에 쓰고 닫는다 — 창을 연 뒤
    다른 곳이 바꾼 키를 되돌리지 않는다(#519). 취소(버튼 · Esc · `reject`)는 아무것도 쓰지
    않고 닫는다. 바뀐 값이 있는 채로 창을 닫으면(제목 줄 ✕ · `close`) 확인 대화상자를 띄운다 — 저장 /
    저장 안 함 / 돌아가기, 기본은 돌아가기.

    ⚠️ objectName 넷(`nidaut`·`nidses`·`afterDownload`·`language`)은 계약 게이트
    (tests/app/gui/test_setting_dialog_contract.py)의 일부다. 번역 컨텍스트는 클래스 이름
    (`SettingDialog`)이라 클래스 이름 · 원문을 바꾸면 `.ts` 항목이 바뀐다(`.qm` 재컴파일 = rc 대상).
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        self.worker = None
        self.closePrompt: QMessageBox | None = (
            None  # 열린 닫기 확인 대화상자(테스트가 버튼을 누른다)
        )

        self.setupUi(self)
        self.setupDynamicUi()
        self._initial = self.values()  # 창을 열 때의 칸 값 — 바뀐 키는 이것과 비교한다
        self._connectChanges()
        self._refreshButtons()
        self._keyFocus = watch_dialog_buttons(self)  # 키보드 초점에만 버튼 테두리(#622)
        # 툴팁은 모두 말풍선으로 뜬다 — 카드 편집 창과 같다(#629). 지금은 버튼 툴팁만 있어 바로 뜬다
        install_tips(self)

    def setupUi(self, dialog: QDialog) -> None:
        """위젯·레이아웃 조립 — 탭 줄 · 탭 쪽 다섯 · 버튼 상자 차례(키보드 초점 차례도 같다)."""
        if not dialog.objectName():
            dialog.setObjectName("SettingDialog")
        dialog.resize(400, 350)
        self.settingLayout = QVBoxLayout(dialog)
        self.settingLayout.setObjectName("settingLayout")
        margin = theme.METRICS["outerMargin"]  # 창 바깥 여백 — 본 창 바 안쪽 여백과 같다
        self.settingLayout.setContentsMargins(margin, margin, margin, margin)

        # ---- 탭 줄 ----
        self.tabBar = QFrame(dialog)
        self.tabBar.setObjectName("settingsTabs")
        tabLayout = QHBoxLayout(self.tabBar)
        tabLayout.setContentsMargins(0, 0, 0, 0)
        tabLayout.setSpacing(CHIP_GAP)
        self.tabGroup = QButtonGroup(dialog)
        self.tabGroup.setExclusive(True)
        self.tabs: dict[str, FilterChip] = {}
        for index, key in enumerate(TAB_KEYS):
            tab = FilterChip(self.tabBar)
            tab.setObjectName(f"settingsTab_{key}")
            tab.setProperty("role", "filter")
            tab.setCheckable(True)
            tab.setCursor(Qt.CursorShape.PointingHandCursor)
            tab.setFixedHeight(theme.METRICS["barControl"])
            # 초점을 받아도 기본 버튼이 되지 않는다 — 탭에서 친 Enter는 저장(기본 버튼)이다(Windows 속성 창과 같다)
            tab.setAutoDefault(False)
            self.tabGroup.addButton(tab, index)
            tabLayout.addWidget(tab)
            self.tabs[key] = tab
        tabLayout.addStretch(1)
        self.settingLayout.addWidget(self.tabBar)

        self.pages = QStackedWidget(dialog)
        self.pages.setObjectName("settingsPages")
        self.page: dict[str, QWidget] = {}
        for key in TAB_KEYS:
            page = QWidget(self.pages)
            page.setObjectName(f"{key}Page")
            self.pages.addWidget(page)
            self.page[key] = page

        # ---- 일반 ----
        general = self.page["general"]
        self.generalGridLayout = _label_field_grid(general, "generalGridLayout")
        self.languageLabel = _FormLabel(general)
        self.languageLabel.setObjectName("languageLabel")
        self.generalGridLayout.addWidget(self.languageLabel, 0, _LABEL_COLUMN, _LABEL_ALIGNMENT)
        self.language = ChoiceCombo(general)
        self.language.setObjectName("language")
        self.generalGridLayout.addWidget(self.language, 0, _FIELD_COLUMN)
        self.afterDownloadLabel = _FormLabel(general)
        self.afterDownloadLabel.setObjectName("afterDownloadLabel")
        self.generalGridLayout.addWidget(
            self.afterDownloadLabel, 1, _LABEL_COLUMN, _LABEL_ALIGNMENT
        )
        self.afterDownload = ChoiceCombo(general)
        self.afterDownload.setObjectName("afterDownload")
        self.generalGridLayout.addWidget(self.afterDownload, 1, _FIELD_COLUMN)
        _pin_rows_to_top(self.generalGridLayout)

        # ---- 다운로드 · 화면 — 뒤따르는 PR이 항목을 더할 자리 ----
        for key in ("download", "display"):
            _pin_rows_to_top(_label_field_grid(self.page[key], f"{key}GridLayout"))

        # ---- 계정 ----
        account = self.page["account"]
        self.accountGridLayout = _label_field_grid(account, "accountGridLayout")
        self.nidautLabel = _FormLabel(account)
        self.nidautLabel.setObjectName("nidautLabel")
        self.accountGridLayout.addWidget(self.nidautLabel, 0, _LABEL_COLUMN, _LABEL_ALIGNMENT)
        self.nidaut = QLineEdit(account)
        self.nidaut.setObjectName("nidaut")
        self.nidaut.setClearButtonEnabled(True)
        self.accountGridLayout.addWidget(self.nidaut, 0, _FIELD_COLUMN)
        self.nidsesLabel = _FormLabel(account)
        self.nidsesLabel.setObjectName("nidsesLabel")
        self.accountGridLayout.addWidget(self.nidsesLabel, 1, _LABEL_COLUMN, _LABEL_ALIGNMENT)
        self.nidses = QLineEdit(account)
        self.nidses.setObjectName("nidses")
        self.nidses.setClearButtonEnabled(True)
        self.accountGridLayout.addWidget(self.nidses, 1, _FIELD_COLUMN)
        self.helpButton = QPushButton(account)
        self.helpButton.setObjectName("helpButton")
        # 도움말 버튼은 구 폼의 라벨 자리(라벨 열)에 있었다 — 자리와 정렬을 그대로 둔다
        self.accountGridLayout.addWidget(self.helpButton, 2, _LABEL_COLUMN, _LABEL_ALIGNMENT)
        self.cookieSpacer = QSpacerItem(
            0, 0, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum
        )
        self.accountGridLayout.addItem(self.cookieSpacer, 2, _FIELD_COLUMN)
        _pin_rows_to_top(self.accountGridLayout)

        # ---- 고급 ----
        advanced = self.page["advanced"]
        self.advancedGridLayout = _label_field_grid(advanced, "advancedGridLayout")
        self.logsFolderLabel = _FormLabel(advanced)
        self.logsFolderLabel.setObjectName("logsFolderLabel")
        self.advancedGridLayout.addWidget(self.logsFolderLabel, 0, _LABEL_COLUMN, _LABEL_ALIGNMENT)
        self.logsFolder = QPushButton(advanced)
        self.logsFolder.setObjectName("logsFolder")
        self.advancedGridLayout.addWidget(self.logsFolder, 0, _FIELD_COLUMN)
        _pin_rows_to_top(self.advancedGridLayout)

        self.settingLayout.addWidget(self.pages, 1)

        # ---- 저장 / 취소 ----
        # 버튼 차례는 Qt 버튼 상자가 OS 관례로 정한다(설계 §12.1). 저장이 주 동작(채움 색 규칙)
        self.dialogButtonBox = QDialogButtonBox(dialog)
        self.dialogButtonBox.setObjectName("dialogButtonBox")
        self.dialogButtonBox.setStandardButtons(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Save
        )
        self.saveButton = self.dialogButtonBox.button(QDialogButtonBox.StandardButton.Save)
        self.cancelButton = self.dialogButtonBox.button(QDialogButtonBox.StandardButton.Cancel)
        # 버튼 상자가 만들며 이미 스타일을 입혔다 — 속성을 바꾼 뒤 다시 입힌다
        self.saveButton.setProperty("role", "primary")
        theme.repolish(self.saveButton)
        # Enter는 저장(옛 OK와 같다). 꺼져 있으면 버튼 상자가 기본 버튼으로 고르지 않으므로 직접 건다 —
        # 꺼진 기본 버튼은 Enter에 눌리지 않는다
        self.saveButton.setDefault(True)
        self.settingLayout.addWidget(self.dialogButtonBox)

        self.retranslateUi(dialog)
        self.dialogButtonBox.accepted.connect(dialog.accept)
        self.dialogButtonBox.rejected.connect(dialog.reject)
        self.tabGroup.idClicked.connect(self.showTab)
        # Ctrl+S 저장(macOS Cmd+S). 바뀐 값이 없으면 저장 버튼처럼 아무 일도 하지 않는다
        self.saveShortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Save), dialog)
        self.saveShortcut.activated.connect(self._onSaveShortcut)
        # Ctrl+Tab · Ctrl+Shift+Tab 탭 넘기기 — Qt 탭 위젯과 같은 키(OS 표준 키 전부)
        self.nextTabShortcut = QShortcut(dialog)
        self.nextTabShortcut.setKeys(_tab_switch_keys(QKeySequence.StandardKey.NextChild))
        self.nextTabShortcut.activated.connect(self._nextTab)
        self.prevTabShortcut = QShortcut(dialog)
        self.prevTabShortcut.setKeys(_tab_switch_keys(QKeySequence.StandardKey.PreviousChild))
        self.prevTabShortcut.activated.connect(self._previousTab)
        QMetaObject.connectSlotsByName(dialog)
        self.showTab(0)
        self._fitTabs()
        # 창을 열 때 초점은 고른 탭이다. 정하지 않으면 QDialog가 첫 초점 위젯이 버튼(탭)인 것을 보고 초점을
        # 기본 버튼(저장)으로 옮긴다(QDialog::setVisible) — 그러면 Tab 차례가 저장에서 시작한다
        self.tabs[TAB_KEYS[0]].setFocus()

    def retranslateUi(self, dialog: QDialog) -> None:
        """표시 문자열 — 탭 이름 다섯 · 칸 라벨 · 버튼(번역 컨텍스트 SettingDialog)."""
        dialog.setWindowTitle(self.tr("Settings"))
        names = self.tabNames()
        for key, tab in self.tabs.items():
            tab.setText(names[key])
        self.nidautLabel.setText(self.tr("NID_AUT"))
        self.nidsesLabel.setText(self.tr("NID_SES"))
        self.helpButton.setText(self.tr("Help"))
        self.afterDownloadLabel.setText(self.tr("After Download"))
        self.languageLabel.setText(self.tr("Language"))
        self.logsFolderLabel.setText(self.tr("Logs Folder"))
        self.logsFolder.setText(self.tr("Open"))

    def tabNames(self) -> dict[str, str]:
        """탭 키 → 보이는 탭 이름(번역). 영어 이름은 짧은 쪽(Download · Display)이다(#644)."""
        return {
            "general": self.tr("General"),
            "download": self.tr("Download"),
            "display": self.tr("Display"),
            "account": self.tr("Account"),
            "advanced": self.tr("Advanced"),
        }

    def _fitTabs(self) -> None:
        """탭마다 이름이 들어가는 폭을 고정한다 — 탭 줄 폭이 창 최소폭을 정하고 창이 넓어도 탭은 늘지 않는다.

        고른 탭 · 고르지 않은 탭의 글 굵기가 같아 sizeHint가 고름에 따라 바뀌지 않는다. 글꼴 · 번역이
        바뀌면 다시 부른다(헤더바 `FilterChips.applyWidth`와 같은 방식).
        """
        for tab in self.tabs.values():
            tab.ensurePolished()
            tab.setFixedWidth(tab.sizeHint().width())

    def showTab(self, index: int) -> None:
        """그 차례의 탭을 고르고 아래 쪽을 바꾼다 — 탭 누름 · Ctrl+Tab이 부른다."""
        self.tabs[TAB_KEYS[index]].setChecked(True)
        self.pages.setCurrentIndex(index)

    def currentTab(self) -> str:
        """지금 고른 탭의 키."""
        return TAB_KEYS[self.pages.currentIndex()]

    def _nextTab(self) -> None:
        """다음 탭으로 — 끝에서 처음으로 돈다(Ctrl+Tab)."""
        self.showTab((self.pages.currentIndex() + 1) % len(TAB_KEYS))

    def _previousTab(self) -> None:
        """앞 탭으로 — 처음에서 끝으로 돈다(Ctrl+Shift+Tab)."""
        self.showTab((self.pages.currentIndex() - 1) % len(TAB_KEYS))

    def setupDynamicUi(self) -> None:
        """칸에 선택지를 채우고 저장된 값으로 맞춘다 — 창을 열 때 한 번."""
        cookies = settings.load_cookies()
        self.nidaut.setText(cookies["NID_AUT"])
        self.nidses.setText(cookies["NID_SES"])

        self.helpButton.clicked.connect(self.showHelp)

        self.afterDownload.addItem(self.tr("none"), "none")
        self.afterDownload.addItem(self.tr("sleep"), "sleep")
        self.afterDownload.addItem(self.tr("shutdown"), "shutdown")
        index = self.afterDownload.findData(settings.load_setting("afterDownload"))
        if index != -1:
            self.afterDownload.setCurrentIndex(index)

        # 언어 이름은 그 언어로 쓴다 — 번역하지 않는다
        self.language.addItem("English", "en_US")
        self.language.addItem("한국어", "ko_KR")
        index = self.language.findData(settings.load_setting("language"))
        if index != -1:
            self.language.setCurrentIndex(index)

        self.logsFolder.clicked.connect(self.openLogsFolder)

    # ---- 바뀐 값 ----

    def values(self) -> dict:
        """지금 칸 값 — 설정 키 → 저장할 값. 이 창이 보이는 키만 담는다."""
        return {
            "cookies": {"NID_AUT": self.nidaut.text(), "NID_SES": self.nidses.text()},
            "afterDownload": self.afterDownload.currentData(),
            "language": self.language.currentData(),
        }

    def changes(self) -> dict:
        """창을 열 때와 값이 다른 키만 — 저장이 쓰는 것."""
        return {key: value for key, value in self.values().items() if value != self._initial[key]}

    def isDirty(self) -> bool:
        """바뀐 키가 하나라도 있는지."""
        return bool(self.changes())

    def _connectChanges(self) -> None:
        """칸 값이 바뀔 때마다 저장 켜짐을 다시 맞춘다."""
        self.nidaut.textChanged.connect(self._refreshButtons)
        self.nidses.textChanged.connect(self._refreshButtons)
        self.afterDownload.currentIndexChanged.connect(self._refreshButtons)
        self.language.currentIndexChanged.connect(self._refreshButtons)

    def _refreshButtons(self) -> None:
        """바뀐 값이 있을 때만 저장을 켠다 — 취소는 늘 켜져 있다(오너 2026-11-27, #644)."""
        self.saveButton.setEnabled(self.isDirty())

    def _onSaveShortcut(self) -> None:
        """Ctrl+S — 바뀐 값이 있으면 저장하고 닫는다."""
        if self.isDirty():
            self.accept()

    # ---- 닫기 ----

    def accept(self) -> None:
        """바뀐 키만 한 번에 쓰고 닫는다 — 창을 연 뒤 다른 곳이 바꾼 키를 되돌리지 않는다 (#519)."""
        changes = self.changes()
        if changes:
            settings.update_settings(changes)
        return super().accept()

    def reject(self) -> None:
        """아무것도 쓰지 않고 닫는다 — 취소 버튼 · Esc."""
        return super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        """바뀐 값이 있는 채로 닫으면 확인 대화상자를 띄운다 — 저장 / 저장 안 함 / 돌아가기."""
        if not self.isVisible() or not self.isDirty():
            super().closeEvent(event)
            return
        choice = self.askClose()
        if choice == CLOSE_BACK:
            event.ignore()
            return
        if choice == CLOSE_SAVE:
            self.accept()
        else:
            self.reject()
        event.accept()

    def askClose(self) -> str:
        """닫기 확인 대화상자를 띄워 고른 갈래(`CLOSE_SAVE` · `CLOSE_DISCARD` · `CLOSE_BACK`)를 돌려준다.

        버튼 차례는 Qt 메시지 상자가 역할로 OS 관례에 맞춘다. 기본 버튼(Enter)과 Esc는 돌아가기다 —
        잘못 눌러 바꾼 값을 잃지 않는다. 열린 동안 `closePrompt`에 둔다(테스트가 버튼을 누른다).
        """
        box = QMessageBox(self)
        box.setObjectName("settingsClosePrompt")
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(self.tr("Settings"))
        box.setText(self.tr("Save the changed settings?"))
        save = box.addButton(self.tr("Save"), QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton(self.tr("Don't Save"), QMessageBox.ButtonRole.DestructiveRole)
        back = box.addButton(self.tr("Go back"), QMessageBox.ButtonRole.RejectRole)
        save.setObjectName("closeSave")
        discard.setObjectName("closeDiscard")
        back.setObjectName("closeBack")
        box.setDefaultButton(back)
        box.setEscapeButton(back)
        watch_dialog_buttons(box)  # 키보드 초점에만 버튼 테두리(#622)
        self.closePrompt = box
        try:
            box.exec()
        finally:
            self.closePrompt = None
        clicked = box.clickedButton()
        box.deleteLater()
        if clicked is save:
            return CLOSE_SAVE
        if clicked is discard:
            return CLOSE_DISCARD
        return CLOSE_BACK

    def changeEvent(self, event: QEvent) -> None:
        """창을 떠나면(다른 창이 활성 · 최소화) 툴팁과 기다리던 툴팁을 그만둔다 — 편집 창과 같다(#629)."""
        super().changeEvent(event)
        if event.type() in (QEvent.Type.ActivationChange, QEvent.Type.WindowStateChange):
            close_floats_if_left(self)

    # ---- 버튼 ----

    def showHelp(self) -> None:
        """쿠키를 얻는 방법 안내 메시지."""
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
        """로그 폴더를 연다 — 3-OS 공통으로 `QDesktopServices`를 쓴다 (#181)."""
        path = settings.logs_dir()
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
            QMessageBox.warning(self, self.tr("Warning"), f"'{path}'을(를) 열 수 없습니다.")
