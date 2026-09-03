import os
import config.config as config
import theme
from PySide6.QtWidgets import QAbstractItemView, QComboBox, QDialog, QMessageBox
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QEvent, QItemSelectionModel, QObject, Qt, QTimer, QUrl

from ui.settingDialog import Ui_SettingDialog


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
    (`content/view.py::_scheduleRenumber`와 같은 컨텍스트 객체 패턴 —
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


class _ComboBoxPopupCloseKeyGuard(QObject):
    """[J-2 후보 B] 팝업을 닫은 키의 `KeyPress`가 콤보까지 새어 들어오면 삼킨다.

    macOS에서 드롭다운을 열고 Enter로 고르면 설정 창까지 닫히는 회귀의
    경로는 `theme._DropDownComboBoxStyle` docstring에 있다 — 요지는 Qt가
    팝업을 `ShortcutOverride` 단계에서 즉시 닫은 뒤, 같은 키의 `KeyPress`를
    macOS에서는(키보드 grab이 없어) 다이얼로그 창으로 배달하고, 그것이
    콤보 → 다이얼로그로 올라가 기본 버튼(OK)을 누른다는 것이다.

    이 필터는 두 곳에 걸린다. (1) 팝업 뷰: 팝업이 떠 있는 동안 팝업을 닫는
    키(Enter/Return/F4/Alt+Down — `QComboBoxPrivateContainer::eventFilter`가
    `ShortcutOverride`에서 처리하는 키 집합)의 `ShortcutOverride`가 오면
    그 키를 "무장"한다. Qt의 컨테이너 필터는 뷰에 먼저 설치돼 있어 이
    필터 *다음*에 불리므로(나중에 설치한 필터가 먼저 불린다, 실측) 여기서
    False를 돌려주면 Qt가 평소처럼 팝업을 닫고 항목을 고른다. (2) 콤보
    자신: 무장된 키의 `KeyPress`가 같은 이벤트 루프 턴 안에 콤보에 도착하면
    삼킨다 — 다이얼로그까지 올라가지 않는다. 무장은
    `QTimer.singleShot(0, ...)`으로 다음 턴에 풀린다 — 새는 `KeyPress`는
    OS 키 이벤트 하나를 처리하는 동안 동기적으로 도착하므로 그 안에 반드시
    들어오고, 사람이 다음에 누르는 Enter는 새 턴이라 절대 삼키지 않는다.

    Windows에서는 그 `KeyPress`가 팝업 창으로 가서 콤보에 도착하지 않으므로
    무장만 됐다가 그냥 풀린다 — 동작 차이가 없다.
    """

    def __init__(self, combo: QComboBox) -> None:
        super().__init__(combo)
        self._combo = combo
        self._armed_key: int | None = None

    def eventFilter(self, watched, event) -> bool:
        event_type = event.type()
        if event_type == QEvent.Type.ShortcutOverride and isinstance(watched, QAbstractItemView):
            if watched.isVisible() and self._closes_popup(event):
                self._armed_key = event.key()
                QTimer.singleShot(0, self, self._disarm)
        elif event_type == QEvent.Type.KeyPress and isinstance(watched, QComboBox):
            if self._armed_key is not None and event.key() == self._armed_key:
                self._armed_key = None
                event.accept()
                return True
        return False

    @staticmethod
    def _closes_popup(event) -> bool:
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_F4):
            return True
        return key == Qt.Key.Key_Down and bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)

    def _disarm(self) -> None:
        self._armed_key = None


def _wire_popup_close_key_guard(combo: QComboBox) -> None:
    """[J-2 후보 B] 환경변수 `CVD_J2_CANDIDATE=swallow`일 때만 배선한다 (실험 토글)."""
    if theme.J2_CANDIDATE != "swallow":
        return
    guard = _ComboBoxPopupCloseKeyGuard(combo)
    combo.view().installEventFilter(guard)
    combo.installEventFilter(guard)


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
        _wire_popup_highlight_resync(self.afterDownload)
        _wire_popup_close_key_guard(self.afterDownload)

        self.language.addItem("English", "en_US") # 언어 선택을 위한 QComboBox 생성 TODO: 언어 리스트는 project.pro에서 관리
        self.language.addItem("한국어", "ko_KR")

        currentLang = self.config.get("language", "en_US") # 현재 설정된 언어에 맞는 인덱스 찾기
        index = self.language.findData(currentLang)
        if index != -1:
            self.language.setCurrentIndex(index)
        _wire_popup_highlight_resync(self.language)
        _wire_popup_close_key_guard(self.language)

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