import logging
import os
import platform
from app.system import settings

from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLayout,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import (
    QEvent,
    QMargins,
    QMetaObject,
    QPoint,
    QRect,
    QSize,
    QStandardPaths,
    Qt,
    QTimer,
)

from app.system.free_space import FreeSpaceProbe
from app.system.process_memory import log_process_memory, resident_bytes
from app.viewmodels.download_viewmodel import DownloadViewModel
from app.viewmodels.footer_monitor import RunResult, combine_readings
from app.viewmodels.menu_bubble import RESULT_STATES, MenuBubble
from app.viewmodels.path_gates import check_fetch_path
from app.viewmodels.save_location import SaveLocations
from app.views.dialog import SettingDialog
from app.views.section_dialog import SectionEditDialog
from app.viewmodels.section_edit import SectionEditViewModel
from app.viewmodels.data import ContentItem
from app.viewmodels.content_viewmodel import ContentViewModel
import config.config as config
from app.widgets import widget as content_widget
from app.widgets.eliding_label import ElidingLabel
from app.widgets.footer_bar import FooterMonitor, RunResultLabel
from app.widgets.header_controls import BusyButton, FilterChips, SaveLocationButton, SortButton
from app.widgets.icons import IconButton
from app.widgets.menu_button import MenuButton
from app.widgets.os_notice import DownloadEndNotice
from app.widgets.view import ContentListView
from core.models.download_state import DownloadState
import app.theme as theme

logger = logging.getLogger(__name__)

FRAME_RETRY_MS = 50  # 표시 뒤 프레임 여백을 다시 보는 간격(ms) — X11 창 관리자는 테두리를 표시 뒤 비동기로 붙인다 (#487)
FRAME_RETRIES = 10  # 그만큼(약 0.5초) 기다려도 여백이 없으면 그만둔다 — 창 관리자가 없는 X 서버 등
MONITOR_INTERVAL_MS = 1000  # 하단바 모니터링을 고치는 간격(ms) — 설계 §2 「1초마다」 (#504)
# 창 제목의 앱 이름 — 번역하지 않는다. 앱 데이터 폴더 이름(app/system/settings.py APP_NAME)과 따로 둔다:
# 표시 이름을 바꿔도 데이터 폴더가 그대로라 설정을 잃지 않는다 (#492)
APP_DISPLAY_NAME = "Chzzk VOD Downloader"
# 창 최소 높이를 셀 때 쓰는 견본 카드(완료 상태 — _referenceCardHeight)의 글 — 실제 목록 카드와 무관한
# 고정값이라 카드를 넣고 빼도 최소 높이가 바뀌지 않는다 (#492). 썸네일 · 채널 그림 주소는 비워 네트워크를 타지 않는다
REFERENCE_CARD_METADATA = {
    "title": "Chzzk VOD",
    "category": "",
    "channelName": "Chzzk",
    "createdDate": "",
    "duration": 3600,
}


def window_title(version: str) -> str:
    """창 제목을 만든다 — 표시 이름 + 버전 (#492).

    정식 `2.12.0` · rc `2.12.0-rc1`은 버전 문자열 그대로 붙인다. 소스 실행 · 로컬 빌드의
    `2.12.0+dev.<커밋>`은 커밋을 빼고 `2.12.0+dev`만 붙인다 — 커밋까지 담은 전체 문자열은
    설정의 정보 화면에서 보인다.
    """
    base, dev, _commit = version.partition("+")
    return f"{APP_DISPLAY_NAME} {base}+dev" if dev else f"{APP_DISPLAY_NAME} {version}"


def _default_download_path() -> str:
    """기본 저장 위치를 정한다 — 앱은 늘 이 위치로 시작한다 (#159 — #157 실측 근거, #495).

    cwd는 실행 방식에 따라 임의다 — macOS .app을 Finder/Dock으로 실행하면
    cwd가 '/'(쓰기 불가)임을 CI에서 실측했고(#157), Windows도 바로가기의
    '시작 위치' 설정에 좌우된다. 우선순위:
    ① 설정의 기본 저장 위치 `downloadPath`(실존할 때만 — 외장 드라이브 분리 대비)
    ② 시스템 다운로드 폴더(실존할 때만)
    ③ cwd — 소스 실행(개발) 관례를 유지하는 최후 폴백

    저장 위치를 골라도 `downloadPath`는 바뀌지 않는다(#495 — 바꾼 위치는 최근 목록). 2.11까지는
    마지막으로 쓴 경로를 거기 적었으므로(#165) 올린 유저는 그 경로로 시작한다.
    """
    saved = settings.load_setting("downloadPath")
    if saved and os.path.isdir(saved):
        return saved
    downloads = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
    if downloads and os.path.isdir(downloads):
        return downloads
    return os.getcwd()


def clamp_to_available(rect: QRect, available: QRect) -> QRect:
    """창 사각형을 현재 작업 영역 안으로 맞춘다 (#253) — 순수 함수, 테스트가 직접 잰다.

    크기는 작업 영역을 넘지 않게 줄이고, 위치는 오른쪽·아래로 넘치면 안으로 밀되
    왼쪽·위 가장자리보다 앞서지 않게 한다. 호출부는 `resize()` **뒤**의 실제 크기로
    부른다 — Qt가 최소 크기로 올린 뒤의 크기여야 위치가 맞는다.
    """
    width = min(rect.width(), available.width())
    height = min(rect.height(), available.height())
    x = max(available.left(), min(rect.x(), available.left() + available.width() - width))
    y = max(available.top(), min(rect.y(), available.top() + available.height() - height))
    return QRect(x, y, width, height)


class VodDownloader(QMainWindow):
    """치지직 VOD 다운로더 메인 창.

    위젯 · 레이아웃은 `.ui` 생성물 없이 이 파일이 직접 짠다(#492 — 설정 창 #292와 같은 방식).
    구 `ui/mainWindow.py`(uic 생성물)의 `setupUi` · `retranslateUi`를 옮긴 것이다. ⚠️ 번역 컨텍스트는
    클래스 이름(`VodDownloader`)이다 — 클래스 이름이나 원문을 글자 하나 바꾸면 `lupdate -no-obsolete`가
    기존 번역을 지운다.

    창은 위에서 아래로 네 칸이다(#492 — 2.12.0 셸 S0): 메뉴바(VOD 다운 · 설정) · 헤더바(1줄 URL 입력 ·
    저장 위치 버튼 · 등록, 조회 상태 한 줄, 2줄 상태 칩 · 정렬 — #495) · 목록 · 하단바(완료 지우기,
    오른쪽 끝 다운로드 · 중지).
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setupUi(self)
        self.setupDynamicUi()

        width_ratio = 0.45
        height_ratio = 0.5
        # 초기 크기와 최소 크기는 다른 관심사다 (#251, 오너 확정).
        #
        # 초기 폭  = min(작업 영역 폭 × 비율, theme.METRICS["initialWidthMax"]) (#253).
        #            0.25는 최소폭을 겸하던 시절의 값이라 작았다 — 1536 화면에서 384로
        #            계산돼 콘텐츠 최소로 클램프되고, 첫 화면부터 경로가 아이콘이고
        #            해상도가 접힌 채 떴다. 0.45면 1366 이상 화면에서 대기 카드 3행이
        #            pill 5개까지 펴진다(실측 임계 569, Windows). 그 임계를 여기 상수로
        #            박지 않는다 — 창을 띄우는 시점엔 카드가 0장이라 제품이 알 수 없고
        #            폰트·OS·pill 개수마다 달라 상수는 조용히 틀린다.
        #            상한(토큰)의 근거: 비율은 선형인데 적당한 창 크기는 선형이 아니다 —
        #            화면이 두 배가 됐다고 창도 두 배로 좋아지지 않고, 작은 화면에 맞춘
        #            비율은 큰 화면에서 과하다. ⚠️ 이것은 콘텐츠 최대폭 캡이 아니다(그
        #            설계는 철회됐다) — 유저는 창을 얼마든지 넓힐 수 있고 카드는 창 폭을
        #            그대로 쓴다. 상한은 "앱이 스스로 정하는 첫 크기"에만 걸린다.
        #            창 크기를 기억하므로(_restoreWindowState) 이 규칙은 기록이 없는
        #            첫 실행(또는 기록이 깨졌을 때)에만 쓰인다.
        # 화면 기준 = availableGeometry(작업표시줄·독 제외). 전체 화면(size())을 쓰면
        #            세로 작업표시줄·독 환경에서 폭이 어긋나 창이 화면 밖으로 나간다.
        # 최소 폭  = 콘텐츠 최소폭만. "최소"는 더 줄이면 UI가 깨지는 지점이지 화면
        #            크기에 비례할 이유가 없다 — 비율 항을 max()로 섞어 두면 큰 화면에서
        #            최소폭이 콘텐츠 요구보다 올라가 유저가 창을 줄이지 못한다.
        #            콘텐츠 최소는 상수가 아니라 레이아웃에 묻는다(폰트·DPI·번역에 따라
        #            다르다). 창의 중앙 위젯이 스크롤 영역이라 이 최소가 창에 자동으로
        #            전파되지 않으므로(그래야 접근성 배율에서 좌우 스크롤 안전망이
        #            서므로) 명시한다. 작은 화면(800×600)에서 상단 버튼이 프레임 밖으로
        #            넘치던 #249의 결함은 이 바닥으로 그대로 막힌다.
        # 최소 높이 = 메뉴바 · 헤더바 · 하단바의 버튼이 모두 보이고 목록에 카드 한 장이 다 보이는
        #            높이(_contentMinimumHeight, #492 — 설계 「창 최소 크기」, 오너 2026-11-06).
        #            화면 비율(#251의 작업 영역 높이 × 0.5)은 최소에서 걷고 초기 높이에만 남긴다.
        #            카드는 견본 하나로 재므로 목록의 카드를 넣고 빼도 바뀌지 않는다.
        #
        # 초기 크기가 최소보다 작은 화면에서는 Qt가 resize() 요청을 최소 크기로
        # 클램프한다(offscreen·Windows 실기 확인).
        available = self._availableGeometry()
        self.setMinimumSize(self._contentMinimumWidth(), self._contentMinimumHeight())
        initial_width = min(int(available.width() * width_ratio), theme.METRICS["initialWidthMax"])
        self.resize(initial_width, int(available.height() * height_ratio))
        self._startMaximized = self._restoreWindowState()

        self.contentManager = ContentViewModel(parent=self)
        self.listView.bind(self.contentManager)
        # 헤더바 2줄 — 칩 숫자 · 고른 칩 · 정렬을 순서 뷰모델에 잇는다(#495)
        self.filterChipBar.bind(self.contentManager.order)
        self.sortButton.bind(self.contentManager.order)
        # 다운로드 이벤트(진행·완료·실패)는 viewmodel이 content에 직결한다 (#170)
        # — 구 릴레이 슬롯 6개(_onProgress~_onFailed)는 함께 제거됐다
        self.downloadViewModel = DownloadViewModel(self.contentManager, parent=self)
        # 하단바(#504) — 실행 결과 · 여유 공간 측정 · 1초마다 모니터링
        self.runResult = RunResult(self.contentManager.model, self.contentManager.order, self)
        self.runResult.changed.connect(self._showRunResult)
        # 메뉴바 상태 버블(#523) — 하단바 실행 결과와 같은 RunResult를 본다(나타나고 사라지는 조건이 같다)
        self.menuBubble = MenuBubble(self.contentManager.order, self.runResult, self)
        self.menuBubble.changed.connect(self._showMenuBubble)
        self._showMenuBubble()
        # 창이 앞에 없을 때 다운로드 끝 OS 알림(#523, 설계 §8-11)
        self.downloadEndNotice = DownloadEndNotice(self, self._bringToFront)
        self.freeSpace = FreeSpaceProbe(self)
        self.freeSpace.measured.connect(self._onFreeSpaceMeasured)
        self.monitorTimer = QTimer(self)
        self.monitorTimer.setInterval(MONITOR_INTERVAL_MS)
        self.monitorTimer.timeout.connect(self._tickFooter)
        # 다운로드 버튼(▶)은 시작할 것이 있을 때만 켠다 — 카드 수 · 상태가 바뀔 때마다 다시 본다
        self.contentManager.order.countsChanged.connect(self._syncDownloadEnabled)
        self._syncDownloadEnabled()
        self.monitorTimer.start()
        self._tickFooter()  # 첫 1초를 기다리지 않고 여유 공간을 잰다
        # 열려 있는 구간 편집 창과 그 카드 (#309) — 창은 한 번에 하나다
        self._sectionDialog: SectionEditDialog | None = None
        self._sectionEditItem: ContentItem | None = None
        self.setupThreadSignals()
        self.setupSignals()

        if self._startMaximized:
            self.showMaximized()
        else:
            self.show()
            self._clampFrameAfterShow()

    def setupUi(self, window: QMainWindow) -> None:
        """위젯 · 레이아웃을 조립한다 — 메뉴바 · 헤더바 · 목록 · 하단바 네 칸(#492).

        구 uic 생성물에서 옮긴 위젯의 objectName은 그대로다. 메뉴바는 이번에 새로 생긴 칸이다.
        """
        if not window.objectName():
            window.setObjectName("VodDownloader")
        window.resize(600, 800)
        # 창 전체 셸(#249):
        #   VodDownloader
        #   └ windowScrollArea  — 창 폭이 콘텐츠 최소폭보다 좁아졌을 때만 좌우 스크롤이
        #     │                   뜨는 안전망(접근성 배율). 세로 스크롤은 카드 목록 것
        #     │                   하나뿐이라 여기서는 항상 끈다.
        #     └ contentColumn   — 메뉴바·헤더바·카드 목록·하단바를 담는 컨테이너. QScrollArea는
        #                         자식 위젯을 하나만 받으므로 넷을 스크롤 영역에 넣으려면
        #                         이 컨테이너가 필요하다. 그 밖의 역할(폭 제한·정렬)은 없다
        #                         — 창 폭을 그대로 채운다(오너 확정, #249).
        #     여백 수치는 _applyLayoutMetrics가 theme.METRICS로 런타임에 건다.
        self.windowScrollArea = QScrollArea(window)
        self.windowScrollArea.setObjectName("windowScrollArea")
        self.windowScrollArea.setFrameShape(QFrame.Shape.NoFrame)
        self.windowScrollArea.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.windowScrollArea.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.windowScrollArea.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.windowScrollArea.setWidgetResizable(True)
        self.contentColumn = QWidget()
        self.contentColumn.setObjectName("contentColumn")
        self.centralWidgetLayout = QVBoxLayout(self.contentColumn)
        self.centralWidgetLayout.setObjectName("centralWidgetLayout")

        # 메뉴바(#492) — 창 위 가로 줄, 메뉴 이름 글이 보인다. QMenuBar가 아니라 버튼 줄로 짠다:
        # macOS는 QMenuBar를 화면 맨 위 시스템 메뉴 막대로 옮겨 창 안에 남지 않고, 메뉴는
        # 펼치는 목록이지 고른 화면을 표시하지 않는다(「VOD 다운」이 눌린 채로 남아야 한다).
        # 「VOD 다운」의 오른쪽 안쪽 여백에 상태 버블이 붙는다(#523 — 셸 S3-1).
        # 「VOD 다운」 = 지금 화면(목록). 「설정」 = 설정 창을 띄우고 선택은 「VOD 다운」에 남는다.
        self.appMenuBar = QFrame(self.contentColumn)
        self.appMenuBar.setObjectName("appMenuBar")
        self.appMenuLayout = QHBoxLayout(self.appMenuBar)
        self.appMenuLayout.setSpacing(8)
        self.appMenuLayout.setObjectName("appMenuLayout")
        self.menuDownloadButton = MenuButton(self.appMenuBar)  # 상태 버블 · 깜빡이는 테두리(#523)
        self.menuDownloadButton.setObjectName("menuDownloadButton")
        self.menuDownloadButton.setCheckable(True)
        self.menuDownloadButton.setChecked(True)
        self.appMenuLayout.addWidget(self.menuDownloadButton)
        self.menuSettingsButton = QPushButton(self.appMenuBar)
        self.menuSettingsButton.setObjectName("menuSettingsButton")
        self.appMenuLayout.addWidget(self.menuSettingsButton)
        self.appMenuLayout.addStretch(1)
        # 고른 화면 메뉴는 하나 — 그룹이 배타라 눌린 「VOD 다운」을 다시 눌러도 풀리지 않는다.
        # 「설정」은 화면이 아니라 창을 여는 메뉴라 그룹에 넣지 않는다(설정 화면 개편 전까지)
        self.appMenuGroup = QButtonGroup(window)
        self.appMenuGroup.setExclusive(True)
        self.appMenuGroup.addButton(self.menuDownloadButton)
        self.menuRow = QHBoxLayout()
        self.menuRow.setObjectName("menuRow")
        self.menuRow.addWidget(self.appMenuBar)
        self.centralWidgetLayout.addLayout(self.menuRow)

        self.headerFrame = QFrame(self.contentColumn)
        self.headerFrame.setObjectName("headerFrame")
        self.headerFrame.setFrameShape(QFrame.Shape.Box)
        self.headerFrame.setFrameShadow(QFrame.Shadow.Sunken)
        self.headerFrameLayout = QVBoxLayout(self.headerFrame)
        self.headerFrameLayout.setSpacing(8)
        self.headerFrameLayout.setObjectName("headerFrameLayout")

        # 헤더바 1줄(#492 — 셸 S0, #495 — S1):
        #   ┌ [URL 입력                         ][📁 폴더 이름 ▾][VOD 추가] ┐
        #   │ 등록 오류 한 줄(빨강, 평소 빈 줄 — 자리는 늘 잡아 둔다)          │
        #   └ [칩 여섯][정렬 ▾] ……………………………………………… [완료 지우기] ┘
        # 남는 폭은 URL 입력이 다 받는다(설계: URL 입력이 남는 폭). 저장 위치 버튼은 고정 폭이다
        # (SaveLocationButton.applyWidth). 설정 톱니(#245)는 메뉴바 「설정」으로 옮겼다.
        self.urlRowLayout = QHBoxLayout()
        self.urlRowLayout.setSpacing(8)
        self.urlRowLayout.setObjectName("urlRowLayout")
        self.urlInput = QLineEdit(self.headerFrame)
        self.urlInput.setObjectName("urlInput")
        self.urlInput.setClearButtonEnabled(True)
        self.urlRowLayout.addWidget(self.urlInput, 1)
        self.saveLocationButton = SaveLocationButton(self.headerFrame)
        self.saveLocationButton.setObjectName("saveLocationButton")
        self.urlRowLayout.addWidget(self.saveLocationButton)
        self.fetchButton = BusyButton(self.headerFrame)  # 불러오는 동안 글 옆에 도는 표시(#495)
        self.fetchButton.setObjectName("fetchButton")
        self.urlRowLayout.addWidget(self.fetchButton)

        self.headerFrameLayout.addLayout(self.urlRowLayout)

        # 등록 오류 줄(#495 — 설계 알림 자리): 잘못된 URL · 조회 실패를 URL 칸 바로 아래 빨간 글 한 줄로.
        # 입력을 고치면 사라진다. 줄 자리는 오류가 없어도 늘 잡아 둔다 — 오류가 떠도 목록이 움직이지
        # 않는다(_reserveErrorLineHeight). 긴 글은 말줄임 + 툴팁(ElidingLabel — 헤더바를 넓히지 않는다).
        # 「불러오는 중」 · 「가져왔다」는 글로 띄우지 않는다 — 등록 버튼의 도는 표시 · 카드의 조회 중 상태가 보인다
        self.registerErrorLabel = ElidingLabel(self.headerFrame)
        self.registerErrorLabel.setObjectName("registerErrorLabel")
        self.headerFrameLayout.addWidget(self.registerErrorLabel)

        # 헤더바 2줄(#495 — 셸 S1): [상태 칩 여섯][정렬 ▾] ……
        # 칩 · 정렬 버튼은 고정 폭이다(숫자 세 자리 · 가장 긴 정렬 이름) — 남는 폭은 줄 끝 빈칸이 받는다
        self.filterRowLayout = QHBoxLayout()
        self.filterRowLayout.setSpacing(8)
        self.filterRowLayout.setObjectName("filterRowLayout")
        self.filterChipBar = FilterChips(self.headerFrame)
        self.filterChipBar.setObjectName("filterChipBar")
        self.filterChips = self.filterChipBar.chips
        self.filterRowLayout.addWidget(self.filterChipBar)
        self.sortButton = SortButton(self.headerFrame)
        self.sortButton.setObjectName("sortButton")
        self.filterRowLayout.addWidget(self.sortButton)
        self.filterRowLayout.addStretch(1)
        # 완료 지우기 — 선택 · 지우기 되돌리기(카드 단계)가 오기 전까지 헤더바 2줄 오른쪽 끝(#495)
        self.clearFinishedButton = QPushButton(self.headerFrame)
        self.clearFinishedButton.setObjectName("clearFinishedButton")
        self.clearFinishedButton.setProperty("role", "subtle")
        self.filterRowLayout.addWidget(self.clearFinishedButton)
        self.headerFrameLayout.addLayout(self.filterRowLayout)

        # 상단·하단 바는 각자 행(headerRow·infoRow)에 담아 좌우 outerMargin을 그 행이
        # 지게 한다 — 열(centralWidgetLayout)은 좌우 여백이 0이라 사이의 카드 목록이
        # 창 끝까지 간다(v2.10.1). 여백 값은 _applyLayoutMetrics가 런타임에 건다.
        self.headerRow = QHBoxLayout()
        self.headerRow.setObjectName("headerRow")
        self.headerRow.addWidget(self.headerFrame)
        self.centralWidgetLayout.addLayout(self.headerRow)

        self.listView = ContentListView(self.contentColumn)
        self.listView.setObjectName("listView")
        self.listView.setAcceptDrops(True)
        self.centralWidgetLayout.addWidget(self.listView)

        self.infoFrame = QFrame(self.contentColumn)
        self.infoFrame.setObjectName("infoFrame")
        self.infoFrame.setFrameShape(QFrame.Shape.Box)
        self.infoFrame.setFrameShadow(QFrame.Shadow.Sunken)
        self.infoLayout = QHBoxLayout(self.infoFrame)
        self.infoLayout.setSpacing(8)
        self.infoLayout.setObjectName("infoLayout")
        # 하단 「Downloads: x/y」 카운트는 없앴다 — 헤더바 상태 칩의 숫자가 대신한다(#495)
        # 하단바(#504 — 셸 S2): [모니터링 다섯 칸] …… [실행 결과][▶/‖][■]
        self.footerMonitor = FooterMonitor(self.infoFrame)
        self.footerMonitor.setObjectName("footerMonitor")
        self.infoLayout.addWidget(self.footerMonitor)
        self.infoLayout.addStretch(1)
        # 실행 결과는 다운로드 버튼 바로 왼쪽 — 모니터링 글을 밀어내지 않는다(설계 §12.1)
        self.runResultLabel = RunResultLabel(self.infoFrame)
        self.infoLayout.addWidget(self.runResultLabel)
        # 버튼은 아이콘만, 글은 툴팁(설계 §2). 다운로드는 강조색 채움이라 도형은 채움 안 글 색이다
        self.downloadButton = IconButton(self.infoFrame)
        self.downloadButton.setObjectName("downloadButton")
        self.downloadButton.setGlyphMetric("barGlyph")
        self.downloadButton.setIdleToken("onAccent")
        self.downloadButton.setHoverToken("onAccent")
        self.downloadButton.setIconName("resume")
        self.infoLayout.addWidget(self.downloadButton)
        self.stopButton = IconButton(self.infoFrame)
        self.stopButton.setObjectName("stopButton")
        self.stopButton.setGlyphMetric("barGlyph")
        self.stopButton.setIconName("stop")
        self.stopButton.setEnabled(False)
        self.infoLayout.addWidget(self.stopButton)

        self.infoRow = QHBoxLayout()
        self.infoRow.setObjectName("infoRow")
        self.infoRow.addWidget(self.infoFrame)
        self.centralWidgetLayout.addLayout(self.infoRow)

        self.windowScrollArea.setWidget(self.contentColumn)
        window.setCentralWidget(self.windowScrollArea)
        # 탭 순서 = 화면 순서(위 → 아래, 왼쪽 → 오른쪽): 메뉴 · 헤더 1줄 · 목록 · 하단
        QWidget.setTabOrder(self.menuDownloadButton, self.menuSettingsButton)
        QWidget.setTabOrder(self.menuSettingsButton, self.urlInput)
        QWidget.setTabOrder(self.urlInput, self.saveLocationButton)
        QWidget.setTabOrder(self.saveLocationButton, self.fetchButton)
        QWidget.setTabOrder(self.fetchButton, self.filterChips["all"])
        chips = list(self.filterChips.values())
        for before, after in zip(chips, chips[1:]):
            QWidget.setTabOrder(before, after)
        QWidget.setTabOrder(chips[-1], self.sortButton)
        QWidget.setTabOrder(self.sortButton, self.clearFinishedButton)
        QWidget.setTabOrder(self.clearFinishedButton, self.listView)
        QWidget.setTabOrder(self.listView, self.downloadButton)
        QWidget.setTabOrder(self.downloadButton, self.stopButton)

        self.retranslateUi(window)
        QMetaObject.connectSlotsByName(window)

    def retranslateUi(self, window: QMainWindow) -> None:
        """표시 문자열 — 원문은 구 `.ui`와 같다(번역 컨텍스트 VodDownloader).

        창 제목만 번역하지 않는다 — 앱 이름 + 버전(window_title, #492).
        """
        window.setWindowTitle(window_title(config.get_app_version()))
        self.menuDownloadButton.setText(self.tr("VOD Download"))
        self.menuSettingsButton.setText(self.tr("Settings"))
        self.urlInput.setPlaceholderText(self.tr("Enter Chzzk URL"))
        self.fetchButton.setText(self.tr("Add VOD"))
        self.clearFinishedButton.setText(self.tr("Clear Finished"))
        # 하단 버튼은 아이콘만 — 이름은 툴팁이다(#504). 다운로드 버튼의 이름은 상태를 따른다
        self.downloadButton.setToolTip(self.tr("Download"))
        self.stopButton.setToolTip(self.tr("Stop"))

    def setupDynamicUi(self) -> None:
        """
        UI 동적 설정을 수행한다.
        """

        # 저장 위치 — 늘 기본 저장 위치로 시작한다(#495, 오너 2026-11-05). 고른 위치는 최근에 남는다
        self.saveLocations = SaveLocations(_default_download_path(), self)
        self.saveLocationButton.bind(self.saveLocations)
        # 카드의 "경로는 전역 설정과 다를 때만 표시" 판단 기준(#245) — 시작 때와 위치를 고를 때마다
        content_widget.set_global_download_path(self.saveLocations.current())
        self.saveLocations.changed.connect(self._onSaveLocationChanged)
        self._applyLayoutMetrics()

    def _applyLayoutMetrics(self) -> None:
        """상단·카드·하단의 좌우 정렬선을 theme.METRICS 토큰 하나로 관통시킨다 (#244).

        여백을 조립(setupUi)과 따로 여기서 런타임에 건다(`.ui` 시절에는 theme를 연결할 수
        없었다) — 오너가 theme.py의 outerMargin/framePadding 숫자만
        바꾸고 `uv run python CVDv2.py`로 바로 확인할 수 있게 하기 위해서다.
        상단바·카드 목록·하단바가 같은 outerMargin에서 시작하고, 바 안쪽
        여백(framePadding)이 카드 안쪽 여백(cardPadding)과 같으면 입력창·
        썸네일·하단 요약의 왼쪽 끝이 한 선에 놓인다.
        """
        outer = theme.METRICS["outerMargin"]
        frame_pad = theme.METRICS["framePadding"]
        # 좌우 여백은 열(contentColumn)이 아니라 상단·하단 바의 행(headerRow·infoRow)이
        # 진다(v2.10.1) — 카드 목록은 창 좌우 끝까지 가서 세로 스크롤바가 창 끝에 붙고,
        # 카드 쪽 여백은 목록의 컨테이너 패딩이 같은 값으로 진다(app/widgets/view.py).
        self.centralWidgetLayout.setContentsMargins(0, outer, 0, outer)
        self.centralWidgetLayout.setSpacing(8)
        self.menuRow.setContentsMargins(outer, 0, outer, 0)
        self.appMenuLayout.setContentsMargins(0, 0, 0, 0)
        self.headerRow.setContentsMargins(outer, 0, outer, 0)
        self.infoRow.setContentsMargins(outer, 0, outer, 0)
        self.headerFrameLayout.setContentsMargins(frame_pad, frame_pad, frame_pad, frame_pad)
        self.infoLayout.setContentsMargins(frame_pad, frame_pad, frame_pad, frame_pad)
        self._equalizeHeaderButtons()
        self._squareFooterButtons()
        # 헤더바 2줄의 칩 · 정렬 버튼 폭(숫자 세 자리 · 가장 긴 이름) — 창 최소폭 계산 전에 건다
        self.filterChipBar.applyWidth()
        self.sortButton.applyWidth()
        self._reserveErrorLineHeight()

    def _reserveErrorLineHeight(self) -> None:
        """등록 오류 줄의 높이를 오류 유무와 무관하게 고정한다 (#492 — #493 CI, #495).

        오류가 없을 때도 줄 자리를 비워 둔다 — 오류가 뜨고 사라질 때 목록이 움직이지 않는다(설계
        알림 자리). 빈 라벨과 글이 든 라벨은 글꼴에 따라 높이 힌트가 다르므로(대체 글꼴 offscreen
        실측: 빈 14 · 글 12) 빈 글과 이 줄에 띄우는 글을 이 라벨의 글꼴 · QSS로 재어 가장 높은 값을
        최소 높이로 건다 — 글꼴 · 번역을 따라간다. 창 최소 높이(`_contentMinimumHeight`)도 오류
        유무와 무관해진다.
        """
        label = self.registerErrorLabel
        shown = label.text()
        label.ensurePolished()
        heights = []
        for text in ("", self.tr("Please enter VOD URL."), self.tr("Path does not exist.")):
            label.setText(text)
            heights.append(label.sizeHint().height())
        label.setText(shown)
        label.setMinimumHeight(max(heights))

    def _availableGeometry(self, near: QPoint | None = None) -> QRect:
        """작업 영역(작업표시줄·독 제외) — 초기 크기·최소 높이·복원 클램프의 화면 기준 (#253).

        `near`가 있으면 그 점이 놓인 화면(기록된 창 위치가 있던 모니터), 없으면 주 화면.
        테스트가 화면을 주입하는 이음새다.
        """
        screen = QApplication.screenAt(near) if near is not None else None
        if screen is None:
            screen = QApplication.primaryScreen()
        return screen.availableGeometry()

    def _frameMargins(self) -> QMargins:
        """창 테두리 · 제목 표시줄 여백 — 띄우기 전에 네이티브 창을 만들어 OS에서 읽는다 (#487).

        Qt는 창을 만들기 전에는 프레임 여백을 모른다(`frameGeometry()` == `geometry()`).
        `winId()`로 네이티브 창만 만들면(보이지 않는다) Windows는 표시 전에도 실제 여백을 준다.
        여백을 주지 않는 플랫폼은 0이 나오고, 그때는 `_clampFrameAfterShow`가 표시 뒤에 맞춘다.
        테스트가 여백을 주입하는 이음새다.
        """
        self.winId()
        handle = self.windowHandle()
        return handle.frameMargins() if handle is not None else QMargins()

    def _restoreWindowState(self) -> bool:
        """설정에 기록된 창 크기·위치를 되살린다 (#253). 반환값 = 최대화 상태로 띄울지.

        기록이 없거나(첫 실행·옛 설정에서 넘어옴) 화이트리스트(`settings.parse_saved_window`)를 못 넘으면 초기
        크기 규칙(__init__)을 그대로 둔다. 기록값은 그대로 쓰지 않는다 — 모니터를 바꾸거나
        노트북을 분리하면 저장된 크기·위치가 현재 화면 밖이라 창이 안 보이거나 화면을
        넘는다. 기록 위치가 놓인 화면의 작업 영역으로 크기를 줄이고 위치를 안으로 민다.

        위치는 프레임(제목 표시줄 포함) 기준이고 크기는 클라이언트 기준이다 — 프레임 여백
        (`_frameMargins`)을 더한 사각형이 작업 영역에 들어가게 맞춘다 (#487). 여백이 0으로
        나오면(표시 전에 알려 주지 않는 플랫폼) 표시 뒤에 한 번 다시 맞춘다(`_clampFrameAfterShow`).

        순서가 중요하다: **resize() 먼저, 그 뒤 실제 size()로 위치를 클램프**한다. 기록
        크기가 최소 크기보다 작으면 Qt가 resize()에서 최소로 올리는데, 그 전 크기로 위치를
        정하면 커진 만큼 작업 영역 밖에 걸친다. 최소 크기를 미리 읽어 계산에 넣지 않는다 —
        첫 표시 전에는 Qt 6 레이아웃 캐시가 polish 전 값을 들고 있어 시점에 따라 틀린다
        (#249). 최대화였다면 보통 크기를 먼저 복원해 두고(해제 시 돌아갈 크기) 최대화로 띄운다.
        """
        parsed = settings.parse_saved_window(settings.load_setting("window"))
        if parsed is None:
            return False
        (x, y, width, height), maximized = parsed
        try:
            rect = QRect(x, y, width, height)  # Qt 변환은 여기서 — 설정 모듈은 Qt를 모른다 (#257)
            available = self._availableGeometry(rect.topLeft())
            margins = self._frameMargins()
            frame_extra = QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
            self.resize(
                min(rect.width(), available.width() - frame_extra.width()),
                min(rect.height(), available.height() - frame_extra.height()),
            )
            placed = clamp_to_available(QRect(rect.topLeft(), self.size() + frame_extra), available)
            self.move(placed.topLeft())
            self._clampAfterShow = available if margins.isNull() else None
        except (TypeError, ValueError, OverflowError):
            # 최후 방어선 — 화이트리스트가 이미 거른 뒤라 정상 경로에선 오지 않는다
            return False
        return maximized

    def _clampFrameAfterShow(self, retries: int = FRAME_RETRIES) -> None:
        """표시 전에 프레임 여백을 몰랐으면(0) 표시 뒤 실제 `frameGeometry()`로 한 번 다시 맞춘다 (#487).

        복원한 창에만, 한 번만 한다. 표시 직후에 여백이 아직 없으면(X11 창 관리자는 테두리를 비동기로 붙인다)
        `FRAME_RETRY_MS`마다 `retries`번까지 다시 보고, 그래도 없으면(창 관리자 없음) 그만둔다.
        """
        available = getattr(self, "_clampAfterShow", None)
        if available is None:
            return
        frame, client = self.frameGeometry(), self.geometry()
        if frame == client:
            if retries > 0:
                QTimer.singleShot(
                    FRAME_RETRY_MS, self, lambda: self._clampFrameAfterShow(retries - 1)
                )
            else:
                self._clampAfterShow = None
            return
        self._clampAfterShow = None
        extra = frame.size() - client.size()
        if frame.width() > available.width() or frame.height() > available.height():
            self.resize(
                min(client.width(), available.width() - extra.width()),
                min(client.height(), available.height() - extra.height()),
            )
        placed = clamp_to_available(QRect(frame.topLeft(), self.size() + extra), available)
        if placed.topLeft() != frame.topLeft():
            self.move(placed.topLeft())

    def _rememberWindowState(self) -> None:
        """창 크기·위치·최대화 상태를 설정에 기록한다 (#253) — 창을 닫을 때 한 번.

        저장 시점을 닫기로 둔 이유: 크기·이동마다 쓰면 드래그 한 번에 파일 쓰기가
        수십 번 일어난다(경로 보존 #165와 같은 "닫을 때 보존" 관례). 최대화 중이면
        `normalGeometry()`(해제 시 돌아갈 크기)를 기록하고 최대화 플래그를 함께 남긴다.
        위치는 프레임 기준(`pos()`), 크기는 클라이언트 기준(`size()`) — `move()`/`resize()`와
        같은 기준이라 왕복이 같은 값을 낸다.
        """
        maximized = self.isMaximized()
        if maximized:
            geometry = self.normalGeometry()
            x, y, width, height = geometry.x(), geometry.y(), geometry.width(), geometry.height()
        else:
            x, y, width, height = self.pos().x(), self.pos().y(), self.width(), self.height()
        state = {"x": x, "y": y, "width": width, "height": height, "maximized": maximized}
        settings.update_settings({"window": state})  # 저장된 값과 같으면 쓰지 않는다

    def _contentMinimumWidth(self) -> int:
        """콘텐츠 열이 실제로 요구하는 최소폭 — 레이아웃에 묻는다 (#244 목록/헤더).

        QSS padding·폰트는 polish 시점에 sizeHint에 들어오므로 먼저 polish한다
        (`_equalizeHeaderButtons`와 같은 이유). 상수를 박으면 폰트·DPI·OS·번역에서
        조용히 틀려진다 — offscreen 폰트와 Windows 실기가 이미 다르다(674 vs 406).

        ⚠️ polish만으로는 부족하다. Qt 6는 레이아웃 항목마다 크기 힌트를 캐시하고
        그 위젯의 `updateGeometry()`에서만 비우는데, 첫 표시 전에는 QSS polish가
        글꼴을 바꿔도 상단·하단 바(프레임)의 캐시가 polish 전 값으로 남아
        레이아웃 최소폭이 실제보다 작게 나온다(offscreen 실측 318 — 표시 후 674).
        자식 전부에 `updateGeometry()`를 걸어 캐시를 비우면 표시 후와 같은 값이 된다.
        """
        self.contentColumn.ensurePolished()
        for child in self.contentColumn.findChildren(QWidget):
            child.updateGeometry()
        # 중첩 레이아웃(헤더바 1 · 2줄 행)의 계산 캐시도 비운다 — updateGeometry()는 위젯 항목의
        # 캐시만 비우고, 위 레이아웃의 invalidate()는 안쪽 레이아웃으로 내려가지 않는다. 비우지 않으면
        # 칩 폭을 걸기 전 값이 남아 최소폭이 칩 줄을 덜 센다(#495 — offscreen 실측 행 322 vs 항목 합 547)
        for layout in self.contentColumn.findChildren(QLayout):
            layout.invalidate()
        return self.contentColumn.minimumSizeHint().width()

    def _contentMinimumHeight(self) -> int:
        """메뉴바 · 헤더바 · 하단바가 다 보이고 목록에 카드 한 장이 다 보이는 높이 (#492).

        세 바는 레이아웃 최소 높이를 묻는다(버튼이 잘리지 않는 높이 — 폰트 · DPI · QSS를 따른다).
        목록 몫은 견본 카드 한 장(`_referenceCardHeight`)과 목록 안쪽 여백 · 테두리다 — 목록에 있는
        카드로 재지 않으므로 카드를 넣고 빼도 값이 같다. 열의 위아래 여백과 칸 사이 간격을 더한다.
        첫 표시 전 캐시 문제는 `_contentMinimumWidth`와 같아 거기서 비운 뒤에 부른다.
        """
        column = self.centralWidgetLayout
        margins = column.contentsMargins()
        bars = sum(
            frame.minimumSizeHint().height()
            for frame in (self.appMenuBar, self.headerFrame, self.infoFrame)
        )
        container = self.listView.widget().layout().contentsMargins()
        list_height = (
            self._referenceCardHeight()
            + container.top()
            + container.bottom()
            + 2 * self.listView.frameWidth()
        )
        gaps = column.spacing() * (column.count() - 1)
        return margins.top() + bars + list_height + gaps + margins.bottom()

    def _referenceCardHeight(self) -> int:
        """가장 높은 평소 카드 한 장의 높이 — 창 최소 높이의 목록 몫 (#492).

        평소(펼침이 아닌) 카드 높이는 상태와 무관하게 같다: 3행 상태 글의 최소 높이를 해상도
        pill 높이에 맞춰 두었다(#245 — 상태가 바뀌어도 목록이 들썩이지 않게). 그 높이보다 낮은
        카드는 해상도가 없는 대기 카드(조회 중 자리표시) 하나뿐이다 — 상태 글도 pill도 없다.
        그래서 견본은 완료 카드다. 해상도 pill이 있는 대기 카드와 같은 높이이면서, pill을 만들 때
        시작되는 크기 조회 스레드를 타지 않는다. 썸네일 · 채널 그림 주소가 비어 네트워크도 없다.

        상태 글의 최소 높이는 카드가 표시된 뒤에야 레이아웃에 든다 — 화면에 그리지 않는 표시
        (`WA_DontShowOnScreen`)로 한 번 띄워 잰 뒤 바로 버린다.

        유저가 접힌 pill을 눌러 펼친 동안(좁은 폭에서 pill이 줄을 바꿈)은 카드가 더 높아진다.
        그 높이는 유저가 일으킨 잠깐의 변화라(`ContentItemWidget.setExpanded`) 최소 높이에 넣지 않는다.
        """
        item = ContentItem("", dict(REFERENCE_CARD_METADATA), [], None, "", "", "video", None)
        item.downloadState = DownloadState.FINISHED
        card = content_widget.ContentItemWidget(item)
        card.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        card.show()
        card.layout().activate()
        height = card.sizeHint().height()
        card.hide()
        card.deleteLater()
        return height

    def _equalizeHeaderButtons(self) -> None:
        """헤더바 1줄의 저장 위치 버튼 폭을 기준 글에 맞춰 건다 (#495).

        [경로 찾기] 글 버튼은 저장 위치 버튼(📁 ▾)으로 바뀌어 [VOD 추가]와 폭을 맞출 짝이 없다
        (#245의 같은 폭 규칙은 같은 종류의 글 버튼끼리였다). 폭은 글꼴 · QSS를 따라 polish 뒤에 잰다.
        """
        self.saveLocationButton.applyWidth()
        # [VOD 추가]는 도는 표시 자리를 늘 잡아 둔 폭이다 — 돌기 시작해도 폭 · 글 자리가 그대로(#495)
        self.fetchButton.setFixedWidth(self.fetchButton.reservedWidth())

    def _squareFooterButtons(self) -> None:
        """하단 아이콘 버튼 둘을 글 버튼의 높이인 정사각으로 건다 (#504).

        높이는 같은 QSS의 글 버튼 높이(글꼴 · 안쪽 여백을 따른다)라 하단바 높이가 글 버튼일 때와 같다.
        폭은 그 높이와 같다 — 글이 없어 폭 힌트가 안쪽 여백뿐이기 때문이다.
        """
        for button in (self.downloadButton, self.stopButton):
            button.ensurePolished()
            side = button.sizeHint().height()
            button.setFixedSize(side, side)

    def _showRegisterError(self, text: str) -> None:
        """등록 오류를 URL 칸 아래 줄에 띄우고 URL 칸 테두리를 빨강으로 한다 (#495 — 설계 §12.1).

        줄 자리는 늘 잡혀 있어 글만 바뀐다 — 목록이 움직이지 않는다. 입력을 고치면 사라진다
        (`_clearRegisterError`). 테두리는 전역 QSS의 `QLineEdit[invalid="true"]`가 칠한다.
        """
        self.registerErrorLabel.setText(text)
        if self.urlInput.property("invalid") is not True:
            self.urlInput.setProperty("invalid", True)
            theme.repolish(self.urlInput)

    def _clearRegisterError(self) -> None:
        """등록 오류 줄을 비우고 URL 칸 테두리를 되돌린다 — URL 입력이 바뀔 때."""
        if self.registerErrorLabel.text():
            self.registerErrorLabel.setText("")
        if self.urlInput.property("invalid"):
            self.urlInput.setProperty("invalid", False)
            theme.repolish(self.urlInput)

    def setupThreadSignals(self) -> None:
        """
        content 매니저와 UI를 연결하는 시그널 슬롯 설정.

        다운로드 이벤트 배선은 DownloadViewModel 내부로 이동했다 (#170).
        """
        # TODO 동시 다운로드 기능 추가시 로직 수정 필요
        self.contentManager.contentError.connect(self.onFetchError)
        self.contentManager.fetchingChanged.connect(self.fetchButton.setBusy)
        self.contentManager.fetchRequested.connect(self.fetchContents)
        self.contentManager.downloadRequested.connect(self.startDownload)
        self.contentManager.stopRequested.connect(self.onStop)
        self.contentManager.finishedAllRequested.connect(self.onDownloadAllFinished)

    def setupSignals(self) -> None:
        """
        각종 버튼 클릭 시그널 및 UI 내 이벤트를 핸들링할 슬롯을 연결한다.
        """
        self.urlInput.returnPressed.connect(self.onFetch)
        self.fetchButton.clicked.connect(self.onFetch)
        self.saveLocationButton.errorOccurred.connect(self._showRegisterError)
        self.urlInput.textChanged.connect(self._clearRegisterError)
        self.menuSettingsButton.clicked.connect(self.onSetting)
        # 「VOD 다운」을 눌렀다 — 결과를 봤다(#523)
        self.menuDownloadButton.clicked.connect(self.menuDownloadButton.stopBlink)
        self.clearFinishedButton.clicked.connect(self.contentManager.clrearFinishedItems)
        self.downloadButton.clicked.connect(self.onDownloadPause)
        self.stopButton.clicked.connect(self.onStop)
        # 카드 상태별 조작(#245) — ⏸/↻ 는 뷰가 아이템을 붙여 올려준다
        self.listView.pauseRequested.connect(self.onCardPause)
        self.listView.retryRequested.connect(self.onCardRetry)
        self.listView.sectionEditRequested.connect(self.onCardSectionEdit)
        self.listView.sectionRefitRequested.connect(self.contentManager.refitSections)

    def onCardPause(self, item: ContentItem) -> None:
        """진행 카드의 ⏸ — 전역 일시정지/재개 토글과 같은 경로를 탄다 (#245).

        엔진은 동시 다운로드가 하나뿐이라(주석 TODO — 동시 다운로드
        미지원) 카드의 일시정지는 곧 전역 일시정지다. 다운로드 중이
        아닐 때는 아무 일도 하지 않는다 — onDownloadPause의 시작 분기
        (새 배치 시작)로 새면 안 된다.
        """
        if self.downloadViewModel.isDownloading():
            self.onDownloadPause()

    def onCardRetry(self, item: ContentItem) -> None:
        """실패 카드의 ↻ — 아이템을 **대기로 되돌리기만** 한다 (#245 → #261). 시작은 유저 몫.

        실패 상태·사유·진행률을 초기화해 다시 다운로드 대상(findItem이 잡는
        WAITING)으로 만든다. **여기서 다운로드를 시작하지 않는다** — 즉시 시작하면
        실패 원인을 고칠 틈이 없다(쿠키 만료: 같은 쿠키로 바로 또 실패한다, #261).
        유저가 원인을 고친 뒤 전역 다운로드 버튼으로 시작하고, 그때 기존 시작 경로
        (onDownloadPause → downloadItem → downloadRequested → startDownload)가 새
        core 태스크를 WAITING부터 만든다 — 재시도 전용 실행 경로를 새로 만들지 않는다.

        층 구분(#135·#167): 여기서 바꾸는 것은 app 계층 `ContentItem.downloadState`
        (카드 표시 상태)다. 실패한 core `DownloadTaskModel`은 그대로 FAILED로 끝나고
        폐기된다 — core의 `FAILED → WAITING` 불허 전이는 건드리지 않는다.

        ⚠️ 정본의 *"전역 다운로드 버튼이 실패 카드를 자동 포함하는 것은 반대"*와
        충돌하지 않는다. 반대한 것은 **자동 포함**(실패인 채인 카드는 findItem이
        지금도 건너뛴다)이고, 재시도를 눌러 대기가 된 카드는 **유저가 명시적으로
        되돌린 것**이라 전역 버튼이 집어 가는 것이 맞다. 배치가 돌고 있는 중이면
        현재 항목이 끝난 뒤 체인(emitFinishedRequest → downloadItem)이 대기 카드를
        차례로 집어 가는 기존 동작 그대로다.

        파일시스템에 쓰지 않는다 — 후처리 실패로 보존된 세그먼트(#185)는 남는다.

        일부 구간만 실패한 구간 다운로드(#309)는 다음 다운로드가 실패한 구간만 다시
        처리한다. 끝낸 구간의 파일이 그대로 있는지는 **재시도를 누른 지금 한 번** 본다
        (prepareRetry) — 전역 다운로드 버튼은 그 확인을 하지 않고, 실패인 채인 카드를
        다시 돌리지도 않는다.
        """
        self.downloadViewModel.prepareRetry(item)
        item.stateMessage = ""
        item.downloadState = DownloadState.WAITING
        item.download_progress = 0
        self.contentManager.model.notifyChanged(item)

    def onCardSectionEdit(self, item: ContentItem) -> None:
        """대기 카드의 구간 요약 클릭 — 구간 편집 창을 모달로 연다 (#309).

        창이 열려 있는 동안 그 카드는 다운로드 대상에서 빠진다(``beginSectionEdit``). 받는
        중인 배치에서 그 카드의 차례가 오면 건너뛰고 다음 카드로 가며, 창을 닫으면 다시
        대상이 된다. 편집 중임을 카드의 상태(DownloadState)로 만들지 않는다 — 엔진의 실행
        루프는 WAITING만 종료 신호로 본다.

        ``exec()``가 아니라 ``open()``으로 연다 — 창은 모달이지만 이 함수는 바로 돌아가고,
        닫힘은 ``finished`` 시그널로 받는다.
        """
        if item.downloadState != DownloadState.WAITING or self._sectionDialog is not None:
            return
        viewmodel = SectionEditViewModel(item, self.contentManager.model.notifyChanged, self)
        dialog = SectionEditDialog(viewmodel, self)
        self._sectionDialog = dialog
        self._sectionEditItem = item
        self.contentManager.beginSectionEdit(item)
        self.contentManager.editWaitChanged.connect(dialog.setWaitingHint)
        dialog.finished.connect(self._onSectionDialogFinished)
        dialog.open()
        viewmodel.start()

    def _onSectionDialogFinished(self, _result: int) -> None:
        """구간 편집 창이 닫혔다 — 카드를 다시 다운로드 대상으로 돌리고 창을 버린다."""
        dialog, item = self._sectionDialog, self._sectionEditItem
        self._sectionDialog = None
        self._sectionEditItem = None
        if dialog is not None:
            self.contentManager.editWaitChanged.disconnect(dialog.setWaitingHint)
            dialog.viewModel().release()  # 조회하며 받은 moov를 놓는다
            log_process_memory("편집 창 닫힘")
            dialog.viewModel().deleteLater()
            dialog.deleteLater()
        if item is not None:
            self.contentManager.endSectionEdit(item)

    def fetchContents(self, urls: str) -> None:
        # URL 목록을 미리 준비합니다.
        self.urlsToFetch = [url.strip() for url in urls.splitlines() if url.strip() != ""]
        self.currentUrlIndex = 0
        self.scheduleNextFetch()

    def scheduleNextFetch(self) -> None:
        if self.currentUrlIndex < len(self.urlsToFetch):
            # 다음 URL을 가져와 처리합니다.
            url = self.urlsToFetch[self.currentUrlIndex]
            self.currentUrlIndex += 1
            self.onFetch(url)
            # 0.1초(100밀리초) 후에 다음 작업을 스케줄합니다.
            QTimer.singleShot(100, self.scheduleNextFetch)

    def onFetch(self, url=None) -> None:
        """
        VOD URL을 입력받아 메타데이터를 가져오고, 메타데이터 카드를 생성합니다.
        """
        if url:
            vod_url = url
        else:
            vod_url = self.urlInput.text().strip()

        if not vod_url:
            self._showRegisterError(self.tr("Please enter VOD URL."))
            return

        cookies = settings.load_cookies()  # 쿠키 조립의 단일 지점 (#170)

        # 저장 위치는 저장 위치 버튼의 지금 위치다(#495). 고를 때 실존 폴더만 받았지만(절대 경로로
        # 정규화 — #146 ⓑ-4, #219) 그 뒤 폴더가 사라졌을 수 있다(외장 드라이브 분리)
        downloadPath = self.saveLocations.current()

        # 판정은 path_gates가 단일 지점으로 담당한다 (#169 — #146 ⓑ1)
        if not check_fetch_path(downloadPath):
            # 유일한 안내가 팝업뿐이라 제보 진단이 불가능했다 (#146 감사) —
            # 경로를 repr로 남겨 공백 유사 문자·오염(따옴표 등)을 식별한다 (#148)
            logger.warning("조회 거부 — 존재하지 않는 저장 경로: %r", downloadPath)
            self._showRegisterError(self.tr("Path does not exist."))
            return

        self.contentManager.fetchContent(vod_url, cookies, downloadPath)

        self.urlInput.clear()  # 입력이 바뀌어 앞 오류 줄도 비워진다

    def onFetchError(self, errorMessage: str) -> None:
        """조회 실패를 등록 오류 줄에 띄운다 — 대화상자를 띄우지 않는다 (#495 — 설계 알림 자리).

        뷰모델의 오류 글은 `"<주소>\n<까닭>"`이다(FetchJob). 줄 하나에 까닭을 앞에 두고 주소를 뒤에
        붙인다 — 넘치면 뒤(주소)부터 줄이고 툴팁에 전문을 보인다(ElidingLabel). 여러 주소를 한꺼번에
        등록했으면 마지막 실패가 남는다.
        """
        url, _newline, reason = errorMessage.partition("\n")
        self._showRegisterError(f"{reason} · {url}" if reason else url)

    def onDownloadPause(self) -> None:
        """
        추가한 동영상에 대한 다운로드 버튼.
        """
        if self.downloadViewModel.isDownloading():
            # 일시정지 · 재개는 버튼의 글자가 아니라 엔진의 상태로 정한다 (#309). 클릭이 쌓였다가
            # 한꺼번에 처리돼도(앱이 잠깐 멈춘 사이의 연타) 누를 때마다 엔진이 실제로 바뀐
            # 만큼만 따라간다 — 글자로 정하면 엔진이 받지 않은 누름에도 글자가 뒤집혀 어긋난다
            if self.downloadViewModel.isPaused():
                self.downloadViewModel.resume()
            else:
                self.downloadViewModel.pause()
            self._syncDownloadButton()
        else:
            if not self.contentManager.findItem()[0]:
                # 조회 중인 아이템만 있는 경우와 아무것도 없는 경우를 구분해 안내 (#124)
                if self.contentManager.hasLoadingItems():
                    QMessageBox.warning(
                        self,
                        self.tr("Warning"),
                        self.tr("Still loading video information. Please try again in a moment."),
                    )
                else:
                    # ▶는 시작할 것이 없으면 꺼진다(#504) — 여기 오는 것은 대기 카드가 있지만 차례로 고를 수
                    # 없을 때(구간 편집 창에 열림 · 해상도 확인 중)뿐이다. 목록에 VOD가 있으니 「없다」고 하지 않는다
                    QMessageBox.warning(
                        self, self.tr("Warning"), self.tr("No VOD can be started right now.")
                    )
                return
            self.contentManager.downloadItem()
            self._syncDownloadButton()

    def onSetting(self) -> None:
        """
        설정 파일에 저장된 값 불러오기 버튼.
        """
        self.dialog = SettingDialog(parent=self)
        self.dialog.exec_()

    def _onSaveLocationChanged(self) -> None:
        """저장 위치를 골랐다 — 카드의 경로 표시 기준을 바꾸고 로그에 남긴다 (#245 · #148 · #495).

        경로를 repr로 남긴다 — 제보 때 「어느 위치로 등록했는가」를 로그로 다시 짤 수 있게(#148).
        """
        path = self.saveLocations.current()
        logger.info("저장 위치 고름: %r", path)
        content_widget.set_global_download_path(path)
        # 하단바 여유 공간은 새 위치의 것으로 — 다 재면 _onFreeSpaceMeasured가 보인다 (#504)
        self.freeSpace.request(path)
        self._showFooter()

    def onStop(self) -> None:
        """
        중지 버튼 콜백.
        """
        if self.downloadViewModel.task:
            reply = QMessageBox.warning(
                self,
                self.tr("Downloading"),
                self.tr("Download is in progress. Do you want to quit?"),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            # TODO: 메시지박스 중복 경고
            if reply == QMessageBox.Yes:
                self.stopDownload()

    def stopDownload(self) -> None:
        self.downloadViewModel.stop()
        self.setStopButtonEnable(False)
        self.downloadViewModel.removeThreads()
        self._syncDownloadButton()

    def _syncDownloadButton(self) -> None:
        """다운로드 버튼의 도형 · 이름(툴팁)을 엔진의 상태에 맞춘다 (#504 — 설계 §2 ▶ ↔ ‖).

        받는 중이면 ‖ 「모두 일시정지」, 일시정지면 ▶ 「이어 받기」, 아니면 ▶ 「다운로드」. 버튼의 지금
        모양이 아니라 엔진의 상태로 정한다(#309 — 쌓인 클릭에도 엔진이 바뀐 만큼만 따른다).
        하단바 모니터링도 바로 고친다 — 받기 시작 · 일시정지가 다음 1초를 기다리지 않는다.
        """
        if self.downloadViewModel.isDownloading():
            if self.downloadViewModel.isPaused():
                icon, tip = "resume", self.tr("Resume")
            else:
                icon, tip = "pause", self.tr("Pause all")
        else:
            icon, tip = "resume", self.tr("Download")
        self.downloadButton.setIconName(icon)
        self.downloadButton.setToolTip(tip)
        self._syncDownloadEnabled()
        self._showFooter()

    def _syncDownloadEnabled(self) -> None:
        """다운로드 버튼(▶)을 시작할 것이 있을 때만 켠다 (#504 — 오너 2026-11-07).

        켜짐 = 다운로드 작업이 진행 중(일시정지 포함 — ‖ · ▶ 이어 받기가 뜻을 가진다)이거나 대기 카드가
        하나 이상. 조회 중 카드는 대기 칩에 함께 세지만 받을 수 없어 뺀다. 숫자는 칩이 이미 센 값이라
        목록을 훑지 않는다. 카드 더하기 · 지우기 · 상태 바뀜은 `countsChanged`가, 다운로드 시작 · 끝 ·
        일시정지 · 중지는 `_syncDownloadButton`이 부른다.
        """
        order = self.contentManager.order
        waiting = order.counts()["waiting"] - order.loadingCount()
        self.downloadButton.setEnabled(self.downloadViewModel.isDownloading() or waiting > 0)

    def _tickFooter(self) -> None:
        """1초마다 — 여유 공간을 다시 재게 하고 하단바 모니터링을 고친다 (#504)."""
        self.freeSpace.request(self.saveLocations.current())
        self._showFooter()

    def _showFooter(self) -> None:
        """하단바 모니터링을 지금 값으로 고친다 — 하단바 라벨만 만지고 목록은 건드리지 않는다.

        속도 · 남은 시간 · 연결 수는 엔진의 진행 통지에서 뜬 값(`transferReadings`), 메모리는 앱
        프로세스의 메모리, 여유 공간은 저장 위치 버튼의 지금 위치를 마지막으로 잰 값이다.
        """
        readings = self.downloadViewModel.transferReadings()
        memory = resident_bytes() if readings else None
        free = self.freeSpace.value(self.saveLocations.current())
        self.footerMonitor.setSnapshot(combine_readings(readings, memory, free))

    def _onFreeSpaceMeasured(self, path: str, _free) -> None:
        """여유 공간을 다 쟀다 — 지금 위치의 것이면 바로 보인다."""
        if path == self.saveLocations.current():
            self._showFooter()

    def _showMenuBubble(self) -> None:
        """버블 상태 · 숫자가 바뀌었을 수 있다 — 버블과 툴팁을 고친다. 결과 버블이 사라지면 깜빡임도 멈춘다(#523)."""
        state = self.menuBubble.state()
        self.menuDownloadButton.setBubble(state)
        self.menuDownloadButton.setToolTip(self._menuBubbleTip(state))
        if state not in RESULT_STATES and self.menuDownloadButton.isBlinking():
            self.menuDownloadButton.stopBlink()

    def _menuBubbleTip(self, state: str) -> str:
        """버블 툴팁 — 「받는 중 · 완료 3/5 · 실패 1」. 버블이 없으면 빈 글(툴팁 없음)."""
        if not state:
            return ""
        # 구분 글 "menu bubble" — 같은 원문의 다른 자리(닫기 확인 창 제목 등)와 번역을 나눈다(설계 §2의 이름)
        names = {
            "running": self.tr("Downloading", "menu bubble"),
            "paused": self.tr("Paused", "menu bubble"),
            "all_failed": self.tr("All failed", "menu bubble"),
            "some_failed": self.tr("Some failed", "menu bubble"),
            "done": self.tr("Completed", "menu bubble"),
        }
        finished, failed, total = self.menuBubble.counts()
        parts = [names[state], self.tr("Completed {0}/{1}").format(finished, total)]
        if failed:
            parts.append(self.tr("Failed {0}").format(failed))
        return " · ".join(parts)

    def _announceRunEnd(self) -> None:
        """다운로드 작업이 끝났다 — 결과를 아직 못 봤으면 메뉴 테두리를 깜빡이고, 창이 앞에 없으면 OS 알림.

        칩 숫자는 마지막 카드의 상태가 바뀐 차례가 끝난 뒤에 고쳐지므로 여기서는 목록을 한 번 센다(작업
        끝마다 한 번). 깜빡임은 결과 버블이 보일 때만이다 — 받는 중 · 일시정지 카드가 남았으면 버블은 그
        상태다. VOD 다운 화면을 보고 있고 창이 앞이면 이미 본 것이라 깜빡이지 않는다(설계 §12.1).
        """
        front = self._inFront()
        active = any(
            item.downloadState in (DownloadState.RUNNING, DownloadState.PAUSED)
            for item in self.contentManager.model.items
        )
        if self.runResult.isShown() and not active and not (front and self._screen() == "vod"):
            self.menuDownloadButton.startBlink()
        if not front:
            finished, failed = self.runResult.counts()
            state = "all_failed" if failed and not finished else "some_failed" if failed else "done"
            self.downloadEndNotice.notify(
                APP_DISPLAY_NAME, self._runEndText(finished, failed), state
            )

    def _runEndText(self, finished: int, failed: int) -> str:
        """OS 알림 글 — 「다운로드를 마쳤습니다 · 완료 N(· 실패 N)」, 전부 실패면 「다운로드에 실패했습니다 · 실패 N」."""
        if failed and not finished:
            return self.tr("Download failed · Failed {0}").format(failed)
        text = self.tr("Download finished · Completed {0}").format(finished)
        if failed:
            text += " · " + self.tr("Failed {0}").format(failed)
        return text

    def _screen(self) -> str:
        """지금 보이는 화면 — 설정 창이 떠 있으면 "settings", 아니면 "vod"(VOD 다운).

        설정은 아직 창이다(설정 화면 개편 전) — 떠 있는 동안을 설정 화면으로 본다.
        """
        dialog = getattr(self, "dialog", None)
        return "settings" if dialog is not None and dialog.isVisible() else "vod"

    def _inFront(self) -> bool:
        """창이 앞에 있는지 — 최소화가 아니고, 활성 창이 이 창이거나 이 창의 자식 창(설정 · 확인 창 등)이다."""
        if self.isMinimized():
            return False
        active = QApplication.activeWindow()
        return active is not None and (active is self or self.isAncestorOf(active))

    def _bringToFront(self) -> None:
        """본 창을 앞으로 — 최소화면 풀고(최대화였으면 최대화로) 맨 앞으로 올려 활성으로 한다(#523)."""
        if self.isMinimized():
            if self.windowState() & Qt.WindowState.WindowMaximized:
                self.showMaximized()
            else:
                self.showNormal()
        self.raise_()
        self.activateWindow()

    def changeEvent(self, event: QEvent) -> None:
        """창이 포커스를 받았다 — VOD 다운 화면이면 결과를 본 것이라 깜빡임을 멈춘다(설정 화면이면 계속, #523)."""
        super().changeEvent(event)
        button = getattr(self, "menuDownloadButton", None)  # 창을 짜는 중에도 불린다
        if (
            button is not None
            and event.type() == QEvent.Type.ActivationChange
            and self.isActiveWindow()
            and self._screen() == "vod"
            and button.isBlinking()
        ):
            button.stopBlink()

    def _showRunResult(self) -> None:
        """실행 결과가 바뀌었다 — 하단바 결과 글을 고친다 (#504)."""
        finished, failed = self.runResult.counts()
        self.runResultLabel.showResult(self.runResult.isShown(), finished, failed)

    def onDownloadAllFinished(self) -> None:
        """
        모든 메타데이터 카드 다운로드 완료 콜백.
        """
        self.setStopButtonEnable(False)
        self._syncDownloadButton()
        # 하단바 실행 결과 — 끝났다는 안내(대화상자)를 띄우기 전에 보인다 (#504)
        self.runResult.finish()
        # 결과 버블 강조 · OS 알림(#523) — 안내 대화상자가 창의 활성을 가져가기 전에 정한다
        self._announceRunEnd()

        os_type = platform.system()

        afterDownload = settings.load_setting("afterDownload")

        if afterDownload == "sleep":
            if os_type == "Windows":
                os.system("rundll32.exe powrprof.dll,SetSuspendState 0,1,0")
            elif os_type == "Darwin":
                os.system("pmset sleepnow")
            elif os_type == "Linux":
                os.system("systemctl suspend")
            else:
                logger.warning(f"절전 모드는 {os_type}에서 지원되지 않습니다.")
        elif afterDownload == "shutdown":
            if os_type == "Windows":
                os.system("shutdown -s -t 0")
            elif os_type == "Darwin":
                os.system("osascript -e 'tell app \"System Events\" to shut down'")
            elif os_type == "Linux":
                os.system("shutdown -h now")
            else:
                logger.warning(f"시스템 종료는 {os_type}에서 지원되지 않습니다.")

        # 배치 종료 안내는 화면의 결과와 모순되지 않아야 한다 (#134 — #128 후속 ⑤).
        # 실패 카드가 보이는데 "완료했습니다"만 뜨던 문구를 결과별로 나눈다
        finished, failed = self.contentManager.downloadResultCounts()
        if failed and not finished:
            QMessageBox.warning(
                self,
                self.tr("Completed"),
                self.tr("All downloads failed. Check the failed cards for the reason."),
            )
        elif failed:
            QMessageBox.warning(
                self,
                self.tr("Completed"),
                self.tr(
                    "Download finished, but some items failed. Check the failed cards for the reason."
                ),
            )
        else:
            QMessageBox.information(self, self.tr("Completed"), self.tr("Download completed."))

    def setStopButtonEnable(self, bool) -> None:
        self.stopButton.setEnabled(bool)

    # ============ 다운로드 진행 준비 및 상태 업데이트 ============

    def startDownload(self, item: ContentItem) -> None:
        """
        특정 해상도에 대한 다운로드 스레드를 생성 및 시작하기 전 UI 상태 업데이트를 수행한다.
        """
        self.contentManager.start(item)
        self.downloadViewModel.start(item)
        self.setStopButtonEnable(True)
        self._syncDownloadButton()

    def closeEvent(self, event) -> None:
        """
        창을 닫을 때 실행되는 이벤트
        """
        if self.downloadViewModel.isDownloading():
            self._bringToFront()  # 확인 창 전에 본 창을 앞으로 — 최소화 · 가려진 창이 묻는 것을 보이게(#523)
            reply = QMessageBox.warning(
                self,
                self.tr("Downloading"),
                self.tr("Download is in progress. Do you want to quit?"),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply == QMessageBox.No:
                event.ignore()  # 창 닫기 취소
                return
            else:
                self.stopDownload()
        self._rememberWindowState()  # 다음 실행의 창 크기·위치 (#253)
        event.accept()  # 창 닫기 진행
