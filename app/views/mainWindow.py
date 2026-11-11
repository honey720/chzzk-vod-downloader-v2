import logging
import os
import platform
from app.section_input.basis import SECTION_CONTENT_TYPES
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
    QCoreApplication,
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
from PySide6.QtGui import QResizeEvent

from app.system.free_space import FreeSpaceProbe
from app.system.process_memory import log_process_memory, resident_bytes
from app.viewmodels.download_viewmodel import DownloadViewModel
from app.viewmodels.footer_monitor import RunResult, combine_readings
from app.viewmodels.menu_bubble import RESULT_STATES, MenuBubble
from app.viewmodels.path_gates import check_fetch_path
from app.viewmodels.save_location import SaveLocations
from app.views.dialog import SettingDialog
from app.views.card_edit_dialog import CardEditDialog
from app.views.section_dialog import SectionEditDialog
from app.viewmodels.section_edit import SectionEditViewModel
from app.viewmodels.data import ContentItem
from app.viewmodels.content_viewmodel import ContentViewModel
import config.config as config
from app.widgets import widget as content_widget
from app.widgets.footer_bar import DROP_ORDER, FooterMonitor, RunResultLabel
from app.widgets.header_controls import BusyButton, FilterChips, SaveLocationButton, SortButton
from app.widgets.icons import IconButton
from app.widgets.menu_button import MenuButton
from app.widgets.narrow_steps import step_level
from app.widgets.tip_bubble import ErrorTip, close_floats_if_left, install_tips, shortcut_tip
from app.widgets.os_notice import DownloadEndNotice
from app.widgets.view import ContentListView
from core.models.download_state import DownloadState
from core.models.plan import TimeRange
import app.theme as theme

logger = logging.getLogger(__name__)

FRAME_RETRY_MS = 50  # 표시 뒤 프레임 여백을 다시 보는 간격(ms) — X11 창 관리자는 테두리를 표시 뒤 비동기로 붙인다 (#487)
FRAME_RETRIES = 10  # 그만큼(약 0.5초) 기다려도 여백이 없으면 그만둔다 — 창 관리자가 없는 X 서버 등
MONITOR_INTERVAL_MS = 1000  # 하단바 모니터링을 고치는 간격(ms) — 설계 §2 「1초마다」 (#504)
URL_MIN_WIDTH = (
    160  # URL 칸 최소폭(px) — 이보다 좁아지기 전에 저장 위치 버튼을 아이콘으로(프로토 11v, #529)
)
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


WORST_SECTIONS = 99  # 견본 카드의 구간 수 — 상태 글의 「 · 99/99」가 두 자리일 때(#554)
WORST_TIME = "99:59:59"  # 견본 카드의 남은 · 걸린 시간 — 짧은 표기에서 가장 긴 꼴(#554)
# 견본 카드의 진행률 — 「99.9%」가 소수 진행률(#557)에서 가장 긴 꼴이다(「100%」보다 점 하나만큼 넓다)
WORST_PROGRESS = 99.9
WORST_RECEIVED = (
    1023 * 1024 * 1024 + 1000 * 1024
)  # 견본 카드의 받은 바이트 — 「1023.98 MB」(MB 표기의 끝)


def _worst_card_items() -> list[ContentItem]:
    """창 최소폭을 셀 견본 아이템 — 상태마다 상태 글이 가장 긴 모양 하나씩 (#554).

    대기 · 받는 중 · 자르기 준비 · 일시정지 · 완료 · 실패(구간 일부)다. 저장 위치는 기본 위치와 다르게
    둔다(다를 때만 3줄에 보인다 — 다 접으면 빠지지만 접기 전 단계의 필요 폭이 바뀐다).
    """
    sections = tuple(TimeRange(i * 10, i * 10 + 5) for i in range(WORST_SECTIONS))
    metadata = dict(REFERENCE_CARD_METADATA, channelName="Chzzk VOD Downloader Channel")

    def sample(state: DownloadState, **fields: object) -> ContentItem:
        item = ContentItem("", dict(metadata), [], None, "", "Reference folder", "video", None)
        item.resolution = 1080
        item.total_size = "0000.00 MB"
        item.downloadState = state
        item.download_progress = WORST_PROGRESS
        item.download_size = WORST_RECEIVED
        item.download_remain_time = WORST_TIME
        item.download_time = WORST_TIME
        for name, value in fields.items():
            setattr(item, name, value)
        return item

    done = {"selections": sections, "sections_done": WORST_SECTIONS}
    return [
        sample(DownloadState.WAITING),
        sample(DownloadState.WAITING, selections=sections),
        sample(DownloadState.RUNNING),
        sample(DownloadState.RUNNING, post_process=True, cut_preparing=True, **done),
        sample(DownloadState.PAUSED),
        sample(DownloadState.FINISHED, **done),
        sample(DownloadState.FAILED, sections_failed=WORST_SECTIONS, **done),
    ]


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
        # 좁은 폭 모양 단계(#529) — 헤더바 1줄(0 저장 위치 글 · 1 아이콘), 2줄(0 칩 · 1 드롭다운 ·
        # 2 드롭다운 + 정렬 아이콘), 하단바(뺀 칸 수)
        self._urlRowLevel = 0
        self._headerLevel = 0
        self._footerLevel = 0
        # 모양이 바뀐 횟수(헤더바 1줄 · 2줄 · 하단바) — 창 크기 한 번에 한 번만 바뀌는지 테스트가 센다
        self.urlRowShapeChanges = 0
        self.headerShapeChanges = 0
        self.footerShapeChanges = 0
        self.minimumParts: dict[str, int] = {}  # 창 최소폭을 정한 줄마다의 폭(_contentMinimumWidth)
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
        # 실패 0 ↔ N이면 좁은 폭의 실패 칩 자리가 생기거나 없어진다 — 헤더바 2줄 모양을 다시 본다(#529)
        self.contentManager.order.countsChanged.connect(self._applyHeaderShape)
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
        self.downloadEndNotice = DownloadEndNotice(self, self._bringToFront, APP_DISPLAY_NAME)
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
        self._editDialog: CardEditDialog | None = None  # 열린 카드 편집 창 (#545)
        self._editItem: ContentItem | None = None
        self.setupThreadSignals()
        self.setupSignals()
        self._applyUrlRowShape()
        self._applyHeaderShape()
        self._applyFooterShape()
        # 시작 초점은 목록(#536 — 설계 §12.1, 프로토 focusList). 정하지 않으면 Qt가 탭 순서의 첫 위젯(메뉴바
        # 「VOD 다운」)에 초점을 주고, 키보드를 쓰지 않았는데 강조색 초점 테두리가 보인다(§11.5 — 키보드 초점에만)
        self.listView.setFocus(Qt.FocusReason.OtherFocusReason)

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
        self.menuDownloadButton.setProperty("role", "menu")  # 메뉴 버튼 모양(#529 — 전역 QSS)
        self.menuDownloadButton.setCheckable(True)
        self.menuDownloadButton.setChecked(True)
        # 고른 메뉴는 굵게 — 속성(on)으로 건다: :checked의 글꼴은 위젯 글꼴에 들지 않아 크기 힌트가 굵은 글
        # 폭을 모른다(#536, 프로토 11s)
        self.menuDownloadButton.setProperty("on", True)
        self.menuDownloadButton.toggled.connect(self._onMenuToggled)
        self.appMenuLayout.addWidget(self.menuDownloadButton)
        self.menuSettingsButton = QPushButton(self.appMenuBar)
        self.menuSettingsButton.setObjectName("menuSettingsButton")
        self.menuSettingsButton.setProperty("role", "menu")
        self.menuSettingsButton.setProperty("on", False)
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
        #   ┌ [URL 입력                              ][📁 폴더 이름 ⌄][+] ┐
        #   └ [칩 여섯][정렬 ⌄] ……………………………………………… [완료 지우기] ┘
        # 등록 오류는 줄이 아니라 URL 칸 아래 오류 말풍선이다(#536)
        # 남는 폭은 URL 입력이 다 받는다(설계: URL 입력이 남는 폭). 저장 위치 버튼은 고정 폭이다
        # (SaveLocationButton.applyWidth). 설정 톱니(#245)는 메뉴바 「설정」으로 옮겼다.
        self.urlRowLayout = QHBoxLayout()
        self.urlRowLayout.setSpacing(8)
        self.urlRowLayout.setObjectName("urlRowLayout")
        self.urlInput = QLineEdit(self.headerFrame)
        self.urlInput.setObjectName("urlInput")
        self.urlInput.setClearButtonEnabled(True)
        self.urlInput.setMinimumWidth(URL_MIN_WIDTH)  # 좁은 폭에서도 URL이 보이는 폭(#529)
        self.urlRowLayout.addWidget(self.urlInput, 1)
        self.saveLocationButton = SaveLocationButton(self.headerFrame)
        self.saveLocationButton.setObjectName("saveLocationButton")
        self.urlRowLayout.addWidget(self.saveLocationButton)
        self.fetchButton = BusyButton(
            self.headerFrame
        )  # 「+」 — 불러오는 동안 그 자리에 도는 표시(#495 · #536)
        self.fetchButton.setObjectName("fetchButton")
        self.urlRowLayout.addWidget(self.fetchButton)

        self.headerFrameLayout.addLayout(self.urlRowLayout)

        # 등록 오류(#536 — 설계 §2 알림 자리 · §12.1): 잘못된 URL · 조회 실패를 URL 칸 아래 오류 말풍선(꼬리 위,
        # 실패 색 계열)으로. 포인터가 나가도 남고, 입력을 고치거나 Esc · 다른 곳을 누르면 닫힌다. 말풍선은 떠 있는
        # 창이라 헤더바에 오류 줄 자리를 비워 두지 않는다 — 오류가 떠도 목록이 움직이지 않는다. 긴 글은 말풍선 폭
        # 상한에서 줄을 바꾼다. 「불러오는 중」 · 「가져왔다」는 글로 띄우지 않는다 — 등록 버튼의 도는 표시 · 카드의
        # 조회 중 상태가 보인다
        self.registerError = ErrorTip(self.urlInput)

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

        # 상단·하단 바는 각자 행(headerRow·infoRow)에 담는다. 바는 창 끝까지 붙어 행 여백도 0이다(#536 —
        # 설계 §11.1). 여백 값은 _applyLayoutMetrics가 런타임에 건다.
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
        # 좁은 폭 상태 드롭다운은 칩 앞 — 보이는 모양의 것만 Tab이 들른다(숨은 위젯은 건너뛴다)
        QWidget.setTabOrder(self.fetchButton, self.filterChipBar.dropButton)
        QWidget.setTabOrder(self.filterChipBar.dropButton, self.filterChips["all"])
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
        self.urlInput.setPlaceholderText(self.tr("Enter Chzzk URL · Enter to add"))
        # 등록은 「+」 아이콘 버튼 — 이름 · 단축키는 툴팁(#536 — 설계 §12.1, 프로토 「등록 · Enter」)
        self.fetchButton.setToolTip(shortcut_tip(self.tr("Add"), "Enter"))
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
        # 말풍선(#536) — 세 바의 위젯마다 필터를 건다. 앱 전체가 아니라 메인 창의 바에만: 설정 창 · 편집 창 ·
        # 구간 창의 툴팁은 설정 메뉴 단계(§12.3)에서 정하고, 카드는 카드 단계에서 본다
        for bar in (self.appMenuBar, self.headerFrame, self.infoFrame):
            install_tips(bar)

    def _applyLayoutMetrics(self) -> None:
        """메뉴바 · 헤더바 · 목록 · 하단바의 여백을 theme.METRICS 토큰으로 건다 (#244 · #536).

        여백을 조립(setupUi)과 따로 여기서 런타임에 건다(`.ui` 시절에는 theme를 연결할 수
        없었다) — 오너가 theme.py의 숫자만 바꾸고 `uv run python CVDv2.py`로 바로 확인할 수 있게
        하기 위해서다. 바는 창 끝까지 붙고, 바 안 내용의 좌우 끝과 카드 바깥 끝이 같은 두 선이다(§11.1).
        """
        # 바는 창 끝까지 붙는다 — 바깥 여백 0, 바 · 목록 사이 간격 0(바의 경계선 하나로만 나눈다). 바 안쪽
        # 여백은 세로 barPadY · 가로 barPadX(설계 §11.2), 가로 barPadX는 목록 카드의 좌우 여백(outerMargin,
        # app/widgets/view.py)과 같은 12라 바 안 내용 끝과 카드 바깥 끝이 같은 두 선에 놓인다(§11.1 —
        # #536, 프로토 11b). 카드 목록은 창 좌우 끝까지 가서 세로 스크롤바가 창 끝에 붙는다(v2.10.1).
        # 메뉴 버튼 높이는 barControl(#529 — §11.3)
        pad_x, pad_y = theme.METRICS["barPadX"], theme.METRICS["barPadY"]
        self.centralWidgetLayout.setContentsMargins(0, 0, 0, 0)
        self.centralWidgetLayout.setSpacing(0)
        self.menuRow.setContentsMargins(0, 0, 0, 0)
        self.appMenuLayout.setContentsMargins(pad_x, pad_y, pad_x, pad_y)
        self._applyBarControls()
        self.headerRow.setContentsMargins(0, 0, 0, 0)
        self.infoRow.setContentsMargins(0, 0, 0, 0)
        self.headerFrameLayout.setContentsMargins(pad_x, pad_y, pad_x, pad_y)
        self.infoLayout.setContentsMargins(pad_x, pad_y, pad_x, pad_y)
        self._equalizeHeaderButtons()
        self._squareFooterButtons()
        # 헤더바 2줄의 칩 · 정렬 버튼 폭(숫자 세 자리 · 가장 긴 이름) — 창 최소폭 계산 전에 건다
        self.filterChipBar.applyWidth()
        self.sortButton.applyWidth()

    def barControls(self) -> list[QWidget]:
        """메뉴바 · 헤더바 · 하단바에서 누르거나 쓰는 컨트롤 — 높이 · 커서 · 초점 · 말풍선 규칙이 같다 (#536)."""
        return [
            self.menuDownloadButton,
            self.menuSettingsButton,
            self.urlInput,
            self.saveLocationButton,
            self.fetchButton,
            self.filterChipBar.dropButton,
            *self.filterChips.values(),
            self.sortButton,
            self.clearFinishedButton,
            self.downloadButton,
            self.stopButton,
        ]

    def _applyBarControls(self) -> None:
        """바 컨트롤의 높이 · 커서 · 초점 정책을 건다 (#536 — 설계 §11.3 · §11.7 · §2 「말풍선」).

        높이는 바 컨트롤 높이(`barControl`). 누르는 컨트롤은 손 모양 커서이고, 초점은 키보드(Tab)로만 받는다 —
        마우스로 누른 뒤 초점 테두리(강조색)가 남지 않는다. 입력칸은 눌러서 쓰는 곳이라 초점 정책을 그대로 둔다.
        """
        control = theme.METRICS["barControl"]
        for widget in self.barControls():
            widget.setFixedHeight(control)
            if widget is self.urlInput:
                continue
            widget.setCursor(Qt.CursorShape.PointingHandCursor)
            widget.setFocusPolicy(Qt.FocusPolicy.TabFocus)

    def _onMenuToggled(self, on: bool) -> None:
        """「VOD 다운」 고름이 바뀌면 굵은 글 속성을 따라 바꾼다(전역 QSS `[on="true"]`, #536)."""
        self.menuDownloadButton.setProperty("on", on)
        theme.repolish(self.menuDownloadButton)

    def tipBoundary(self) -> int:
        """말풍선 방향의 경계(전역 y) — 헤더바 아래 끝. 그보다 위 대상은 아래로, 아니면 위로 뜬다 (#536)."""
        return self.headerFrame.mapToGlobal(QPoint(0, self.headerFrame.height())).y()

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
        self._flushLayoutCaches()
        self.minimumParts = self._minimumParts()
        return max(self.minimumParts.values())

    def _flushLayoutCaches(self) -> None:
        """콘텐츠 열의 위젯 · 레이아웃 크기 캐시를 비운다(`_contentMinimumWidth` · `_settleLayout`)."""
        for child in self.contentColumn.findChildren(QWidget):
            child.updateGeometry()
        # 중첩 레이아웃(헤더바 1 · 2줄 행)의 계산 캐시도 비운다 — updateGeometry()는 위젯 항목의
        # 캐시만 비우고, 위 레이아웃의 invalidate()는 안쪽 레이아웃으로 내려가지 않는다. 비우지 않으면
        # 칩 폭을 걸기 전 값이 남아 최소폭이 칩 줄을 덜 센다(#495 — offscreen 실측 행 322 vs 항목 합 547)
        for layout in self.contentColumn.findChildren(QLayout):
            layout.invalidate()

    def _minimumParts(self) -> dict[str, int]:
        """창 최소폭의 줄마다 몫 — 좁은 폭 모양이 있는 줄은 끝까지 줄인 모양으로 센다 (#529).

        헤더바 2줄 · 하단바를 지금 보이는 모양(칩 · 모니터링 칸)으로 세면 창이 그보다 좁아질 수 없어 줄인
        모양이 나오지 않는다(설계 §2). 끝까지 줄인 모양은 카드 내용과 무관하게 센다 — 실패 칩은 늘 있다고
        보고(숫자 세 자리), 하단바는 전체 속도 · 실행 결과 견본 글로 잰다. 목록은 카드 없는 목록의 최소폭이다.
        """
        header = self._barChrome(self.headerRow, self.headerFrame, self.headerFrameLayout)
        footer = self._barChrome(self.infoRow, self.infoFrame, self.infoLayout)
        gap = self.infoLayout.spacing()
        footer_items = [
            self.footerMonitor.sampleWidth(),
            self.runResultLabel.sampleWidth(),
            self.downloadButton.minimumWidth(),
            self.stopButton.minimumWidth(),
        ]
        return {
            "메뉴바": self.menuRow.minimumSize().width(),
            "헤더바 1줄": self.headerRow1Needs()[-1] + header,
            "헤더바 2줄": self.headerRow2Needs(True)[-1] + header,
            "목록": self.listView.minimumSizeHint().width(),
            "카드": self._referenceCardWidth() + 2 * theme.METRICS["outerMargin"],
            "하단바": sum(footer_items) + gap * (len(footer_items) - 1) + footer,
        }

    def _barChrome(self, row: QLayout, frame: QFrame, inner: QLayout) -> int:
        """창 폭 − 그 바 안쪽 폭 — 줄의 좌우 여백 · 바 좌우 테두리 · 바 안쪽 여백. 바의 내용과 무관하다.

        바 테두리는 목록 쪽 한 줄뿐이라(#536) 좌우 테두리 폭은 `frameWidth()`(한 변 값)가 아니라 바 사각형과
        내용 사각형의 폭 차이로 잰다 — `frameWidth()`는 좌우에 테두리가 없어도 1을 돌려준다(offscreen 실측).
        """
        outside = row.contentsMargins()
        margins = inner.contentsMargins()
        sides = frame.rect().width() - frame.contentsRect().width()
        return outside.left() + outside.right() + sides + margins.left() + margins.right()

    def headerRow1Needs(self) -> list[int]:
        """헤더바 1줄이 단계마다 바라는 폭 — 0 저장 위치 글 · 1 저장 위치 아이콘 (#529 — 프로토 11v).

        URL 칸은 최소폭(`URL_MIN_WIDTH`)으로 센다 — 그보다 좁아지기 전에 저장 위치 버튼을 아이콘으로 줄인다.
        등록 버튼은 늘 글자다. 지금 보이는 모양과 무관하게 두 모양의 폭과 사이 간격으로 센다.
        """
        save = self.saveLocationButton
        rest = URL_MIN_WIDTH + self.fetchButton.minimumWidth() + 2 * self.urlRowLayout.spacing()
        return [rest + save.textWidth(), rest + save.iconWidth()]

    def headerRow2Needs(self, failed: bool) -> list[int]:
        """헤더바 2줄이 단계마다 바라는 폭 — 0 칩 · 1 드롭다운 · 2 드롭다운 + 정렬 아이콘 (#529).

        지금 보이는 모양과 무관하다 — 칩 · 드롭다운 · 정렬 두 모양의 고정 폭과 「완료 지우기」 폭, 사이
        간격(늘어나는 빈칸 옆에는 간격이 없다)으로 센다. 줄어드는 폭이 큰 것(칩)부터 줄인다(설계 §2).

        Args:
            failed: 좁은 폭에서 실패 칩이 드롭다운 옆에 남는지(실패 카드가 있는지)
        """
        chips = self.filterChipBar
        sort = self.sortButton
        tail = self.clearFinishedButton.sizeHint().width() + 2 * self.filterRowLayout.spacing()
        compact = chips.compactWidth(failed)
        return [
            chips.chipsWidth() + sort.textWidth() + tail,
            compact + sort.textWidth() + tail,
            compact + sort.iconWidth() + tail,
        ]

    def footerNeeds(self) -> list[int]:
        """하단바가 단계마다 바라는 폭 — 단계 = 뺀 모니터링 칸 수(연결 · 메모리 · 남은 시간 · 여유) (#529).

        지금 뺀 칸과 무관하다 — 값이 있는 칸의 글 폭, 실행 결과 글(보일 때), 버튼 둘, 사이 간격으로 센다.
        모니터링 묶음은 칸이 다 빠져도 레이아웃 항목으로 남아 간격 하나를 차지한다.
        """
        fixed = [self.downloadButton.minimumWidth(), self.stopButton.minimumWidth()]
        if not self.runResultLabel.isHidden():
            fixed.insert(0, self.runResultLabel.sizeHint().width())
        gap = self.infoLayout.spacing()
        needs = []
        for dropped in range(len(DROP_ORDER) + 1):
            items = [self.footerMonitor.needWidth(dropped), *fixed]
            needs.append(sum(items) + gap * (len(items) - 1))
        return needs

    def _barRoom(self, row: QLayout, frame: QFrame, inner: QLayout) -> int:
        """그 바 안쪽에 쓸 수 있는 폭 — 창 폭에서 셈한다(지금 보이는 위젯 폭으로 재지 않는다, #529)."""
        return self.width() - self._barChrome(row, frame, inner)

    def _applyUrlRowShape(self) -> None:
        """헤더바 1줄을 창 폭에 맞는 단계의 모양으로 — 저장 위치 글 → 아이콘 (#529 — 프로토 11v)."""
        room = self._barRoom(self.headerRow, self.headerFrame, self.headerFrameLayout)
        level = step_level(self.headerRow1Needs(), room, self._urlRowLevel)
        if level == self._urlRowLevel:
            return
        self._urlRowLevel = level
        self.urlRowShapeChanges += 1
        self.saveLocationButton.setCompact(level >= 1)
        self._settleLayout()

    def _applyHeaderShape(self) -> None:
        """헤더바 2줄을 창 폭에 맞는 단계의 모양으로 — 칩 → 드롭다운, 정렬 글 → 아이콘 (#529)."""
        room = self._barRoom(self.headerRow, self.headerFrame, self.headerFrameLayout)
        needs = self.headerRow2Needs(self.filterChipBar.hasFailed())
        level = step_level(needs, room, self._headerLevel)
        if level == self._headerLevel:
            return
        self._headerLevel = level
        self.headerShapeChanges += 1
        self.filterChipBar.setCompact(level >= 1)
        self.sortButton.setCompact(level >= 2)
        self._settleLayout()

    def _applyFooterShape(self) -> None:
        """하단바 모니터링을 창 폭에 맞는 칸 수로 — 연결 · 메모리 · 남은 시간 · 여유 공간 차례로 뺀다 (#529)."""
        room = self._barRoom(self.infoRow, self.infoFrame, self.infoLayout)
        level = step_level(self.footerNeeds(), room, self._footerLevel)
        if level == self._footerLevel:
            return
        self._footerLevel = level
        self.footerShapeChanges += 1
        self.footerMonitor.setDropped(level)
        self._settleLayout()

    def _settleLayout(self) -> None:
        """모양을 바꾼 뒤 레이아웃 캐시를 비우고 밀린 레이아웃 요청을 바로 처리한다 (#529).

        창이 보이기 전에 위젯을 숨기고 보이면 레이아웃 캐시가 비지 않아 이전 모양(칩)의 최소폭이 남는다 — 창이
        처음 보일 때 창 전체 스크롤 영역이 그 폭으로 콘텐츠 열을 놓아 가로 스크롤바가 떴다가 다음 이벤트
        차례에 사라지고, 그 높이만큼 목록이 출렁였다(offscreen 실측). 보이는 창에서도 줄 · 열 · 스크롤 영역이
        레이아웃 요청을 차례로 주고받는 동안 같은 일이 한 차례 생긴다. 캐시를 비우고 그 자리에서 처리해
        그리기 전에 끝낸다.

        """
        self._flushBarCaches()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)

    def _flushBarCaches(self) -> None:
        """세 바(메뉴바 · 헤더바 · 하단바)와 콘텐츠 열 레이아웃의 크기 캐시만 비운다 (#530).

        목록은 열의 항목(위젯 하나)으로만 비우고 카드 위젯 안으로는 들어가지 않는다 — 콘텐츠 열 전체를
        돌면(`_flushLayoutCaches`) 카드 1000장에서 한 번에 약 220ms가 걸렸다(#530 리뷰, offscreen 실측).
        """
        # 하단바: 지금은 하단 칸 라벨 최소폭이 1px이라 비우지 않아도 가로 스크롤바가 생기지 않지만, 칸 최소폭이
        # 바뀌면 필요해지므로 함께 비운다
        for frame in (self.appMenuBar, self.headerFrame, self.infoFrame):
            for widget in (frame, *frame.findChildren(QWidget)):
                widget.updateGeometry()
            for layout in frame.findChildren(QLayout):
                layout.invalidate()
        for row in (self.menuRow, self.headerRow, self.infoRow):
            row.invalidate()
        self.listView.updateGeometry()
        self.centralWidgetLayout.invalidate()

    def resizeEvent(self, event: QResizeEvent) -> None:
        """창 폭이 바뀌면 헤더바 2줄 · 하단바의 좁은 폭 모양을 다시 고른다 (#529).

        창 최소폭은 여기서 다시 세지 않는다 — 끝까지 줄인 모양의 고정값이라 창 크기와 무관하다.
        """
        super().resizeEvent(event)
        if event.size().width() != event.oldSize().width():
            self._applyUrlRowShape()
            self._applyHeaderShape()
            self._applyFooterShape()

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

    def _referenceCardWidth(self) -> int:
        """가장 나쁜 견본 카드를 다 접었을 때의 폭 — 창 최소폭의 카드 몫 (#545 · #550 · #554).

        카드 버튼은 1줄 위에 겹쳐 놓으므로(#550) 최소폭에 들지 않는다. 카드는 3줄을 단계마다 접고
        날짜 연도를 빼므로(#554) 최소폭은 상태마다 다 접은 모양(`foldedMinimumWidth`)의 가장 큰 값이다.
        창 최소폭이 카드 없이 센 값이면 가장 좁은 창에서 카드가 넘친다.

        견본은 카드 내용과 무관한 고정 카드다 — 카드 수와 상관없이 최소폭이 같다. 상태 글이 가장 긴
        모양(구간 99개 · 진행률 100%)을 상태마다 하나씩 둔다. 채널명은 최소폭 상한
        (`_clampChannelMinWidth` 64px)에 닿는 긴 이름이다. 해상도 목록을 비워 크기 조회 스레드를 타지
        않고, 썸네일 · 채널 그림 주소가 비어 네트워크도 없다.
        """
        return max(self._foldedWidth(item) for item in _worst_card_items())

    def _foldedWidth(self, item: ContentItem) -> int:
        """견본 아이템 하나로 카드를 띄워 다 접은 폭을 잰다 — 그리지 않고 바로 버린다."""
        card = content_widget.ContentItemWidget(item)
        card.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        card.setData(item, 0)
        card.show()  # 글꼴(QSS)이 붙은 뒤에 잰다
        card.layout().activate()
        width = card.foldedMinimumWidth()
        card.hide()
        card.deleteLater()
        return width

    def _referenceCardHeight(self) -> int:
        """가장 높은 평소 카드 한 장의 높이 — 창 최소 높이의 목록 몫 (#492).

        카드 높이는 상태와 무관하게 같다 — 세 줄의 높이가 고정이다(#245 · #550, 상태가 바뀌어도
        목록이 들썩이지 않게). 견본은 완료 카드다 — 해상도 크기 조회 스레드를 타지 않는다. 썸네일 ·
        채널 그림 주소가 비어 네트워크도 없다.

        화면에 그리지 않는 표시(`WA_DontShowOnScreen`)로 한 번 띄워 잰 뒤 바로 버린다.
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
        """하단 아이콘 버튼 둘을 바 컨트롤 높이의 정사각으로 건다 (#504 · #536 — 설계 §11.3)."""
        side = theme.METRICS["barControl"]
        for button in (self.downloadButton, self.stopButton):
            button.setFixedSize(side, side)

    def _showRegisterError(self, text: str) -> None:
        """등록 오류를 URL 칸 아래 오류 말풍선으로 띄우고 URL 칸 테두리를 빨강으로 한다 (#495 · #536 — 설계 §12.1).

        말풍선은 떠 있어 목록이 움직이지 않는다. 입력을 고치면 사라진다(`_clearRegisterError`) — Esc · 다른 곳
        누름은 말풍선이 스스로 닫는다(`ErrorTip`). 테두리는 전역 QSS의 `QLineEdit[invalid="true"]`가 칠한다.
        """
        self.registerError.show(text)
        if self.urlInput.property("invalid") is not True:
            self.urlInput.setProperty("invalid", True)
            theme.repolish(self.urlInput)

    def _syncFetchButton(self) -> None:
        """URL 칸이 비었으면(공백만 포함) 「+」를 끄고, 무언가 있으면 켠다 (#536 오너 실기 — 설계 §11.6).

        빈 칸에서 Enter는 그대로 등록 오류 말풍선(「VOD URL을 입력하세요」)을 띄운다 — 버튼만 끈 모양이다.
        """
        self.fetchButton.setEnabled(bool(self.urlInput.text().strip()))

    def _clearRegisterError(self) -> None:
        """등록 오류 말풍선을 닫고 URL 칸 테두리를 되돌린다 — URL 입력이 바뀔 때."""
        self.registerError.hide()
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
        self.urlInput.textChanged.connect(self._syncFetchButton)
        self._syncFetchButton()
        self.menuSettingsButton.clicked.connect(self.onSetting)
        # 「VOD 다운」을 눌렀다 — 결과를 봤다(#523)
        self.menuDownloadButton.clicked.connect(self.menuDownloadButton.stopBlink)
        self.clearFinishedButton.clicked.connect(self.contentManager.clrearFinishedItems)
        self.downloadButton.clicked.connect(self.onDownloadPause)
        self.stopButton.clicked.connect(self.onStop)
        # 카드 상태별 조작(#245) — ⏸/↻ 는 뷰가 아이템을 붙여 올려준다
        self.listView.pauseRequested.connect(self.onCardPause)
        self.listView.retryRequested.connect(self.onCardRetry)
        self.listView.editRequested.connect(self.onCardEdit)
        self.listView.stopAndDeleteRequested.connect(self.onCardStopAndDelete)

    def onCardStopAndDelete(self, item: ContentItem) -> None:
        """받는 중 카드의 ✕에서 「멈추고 지우기」를 골랐다 — 다운로드를 멈추고 카드를 지운다 (#550).

        엔진은 동시 다운로드가 하나뿐이라 받는 중 · 일시정지 카드는 곧 엔진이 쥔 카드다. 멈추기는
        하단바 ■과 같은 경로(`stopDownload`)다. 확인은 목록이 이미 했다.

        엔진이 쥔 카드가 이 카드일 때만 멈춘다 — 확인 창이 열린 사이 이 카드가 끝나 배치가 다음
        카드로 넘어갔으면, 멈추기는 다른 카드의 다운로드를 멈춘다.
        """
        if self.downloadViewModel.isDownloading() and self.downloadViewModel.item is item:
            self.stopDownload()
        self.contentManager.removeItem(item)

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
        if item.downloadState != DownloadState.WAITING or self._dialogOpen():
            return
        if item.content_type not in SECTION_CONTENT_TYPES:
            return  # 구간 기능이 없는 종류(클립) — 카드가 하던 검사(#309)를 여기서 한다(#550)
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

    def _dialogOpen(self) -> bool:
        """카드 편집 창이나 구간 편집 창이 열려 있는지 — 둘은 한 번에 하나만 연다 (#545)."""
        return self._sectionDialog is not None or self._editDialog is not None

    def onCardEdit(self, item: ContentItem) -> None:
        """대기 카드의 ✎ · 더블 클릭 — 카드 편집 창을 모달로 연다 (#545).

        구간 편집 창과 같은 꼴이다. 창이 열린 동안 그 카드는 다운로드 차례에서 빠지고
        (``beginSectionEdit``), 닫히면 다시 대상이 된다. 창이 열린 사이 카드가 대기가 아니게
        되거나 지워지면 창의 저장이 쓰지 않는다(``CardEditDialog.writable``). 카드가 창이 열린
        사이 받은 해상도 크기는 카드의 알림으로 창의 해상도 목록에 들어간다.
        """
        if item.downloadState != DownloadState.WAITING or self._dialogOpen():
            return
        model = self.contentManager.model
        dialog = CardEditDialog(
            item,
            self.saveLocations.default(),
            refit=self.contentManager.refitSections,
            notify=model.notifyChanged,
            is_present=lambda it: model.getRow(it) is not None,
            parent=self,
        )
        card = self.listView.widgetFor(item)
        if card is not None:
            card.repSizeChanged.connect(dialog.updateRepSize)
        self._editDialog = dialog
        self._editItem = item
        self.contentManager.beginSectionEdit(item)
        self.contentManager.editWaitChanged.connect(dialog.setWaitingHint)
        dialog.finished.connect(self._onEditDialogFinished)
        dialog.open()

    def _onEditDialogFinished(self, _result: int) -> None:
        """카드 편집 창이 닫혔다 — 카드를 다시 다운로드 대상으로 돌리고 창을 버린다."""
        dialog, item = self._editDialog, self._editItem
        self._editDialog = None
        self._editItem = None
        if dialog is not None:
            self.contentManager.editWaitChanged.disconnect(dialog.setWaitingHint)
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

        self.urlInput.clear()  # 입력이 바뀌어 앞 오류 말풍선도 닫힌다

    def onFetchError(self, errorMessage: str) -> None:
        """조회 실패를 등록 오류 말풍선에 띄운다 — 대화상자를 띄우지 않는다 (#495 · #536 — 설계 알림 자리).

        뷰모델의 오류 글은 `"<주소>\n<까닭>"`이다(FetchJob). 까닭을 앞에 두고 주소를 뒤에 붙인다 — 길면
        말풍선 폭 상한에서 줄을 바꾼다. 여러 주소를 한꺼번에 등록했으면 마지막 실패가 남는다.
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
        self._applyFooterShape()  # 글 폭이 바뀌었을 수 있다 — 칸을 빼거나 되살린다(#529)

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
        """버블 툴팁 — 버블 이름 다음에 숫자. 버블이 없으면 빈 글(툴팁 없음).

        받는 중 · 일시정지는 「받는 중 · 완료 3/5 · 실패 1」. 결과 버블은 이름과 같은 낱말을 숫자 앞에 다시
        쓰지 않는다 — 완료 「완료 · 2/2」, 일부 실패 「일부 실패 · 완료 1 · 실패 1」, 전부 실패 「전부 실패 · 실패 3」
        (오너 실기 2026-11-07).
        """
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
        if state == "done":
            parts = [names[state], f"{finished}/{total}"]
        elif state == "some_failed":
            parts = [
                names[state],
                self.tr("Completed {0}").format(finished),
                self.tr("Failed {0}").format(failed),
            ]
        elif state == "all_failed":
            parts = [names[state], self.tr("Failed {0}").format(failed)]
        else:
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
            self.downloadEndNotice.notify(*self._runEndNotice(finished, failed))

    def _runEndNotice(self, finished: int, failed: int) -> tuple[str, str, str]:
        """OS 알림의 (제목, 글, 아이콘 상태) — 제목 「다운로드를 마쳤습니다」 · 글 「완료 N(· 실패 N)」.

        전부 실패면 제목 「다운로드에 실패했습니다」 · 글 「실패 N」. 앱 이름은 넣지 않는다 — OS가 알림
        머리줄에 앱 이름을 쓰므로 제목에 넣으면 두 번 나온다(오너 실기 2026-11-07).
        """
        if failed and not finished:
            return (
                self.tr("Download failed"),
                self.tr("Failed {0}").format(failed),
                "all_failed",
            )
        text = self.tr("Completed {0}").format(finished)
        if failed:
            text += " · " + self.tr("Failed {0}").format(failed)
        return self.tr("Download finished"), text, "some_failed" if failed else "done"

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
        """창이 포커스를 받았다 — VOD 다운 화면이면 결과를 본 것이라 깜빡임을 멈춘다(설정 화면이면 계속, #523).

        포커스를 받은 때: 활성이 바뀌었거나, 최소화가 풀렸는데 이미 활성이다(활성이 그대로면 활성 변화가
        오지 않는다 — offscreen이 그렇다).
        """
        super().changeEvent(event)
        if event.type() in (QEvent.Type.ActivationChange, QEvent.Type.WindowStateChange):
            close_floats_if_left(self)
        button = getattr(self, "menuDownloadButton", None)  # 창을 짜는 중에도 불린다
        if (
            button is not None
            and event.type() in (QEvent.Type.ActivationChange, QEvent.Type.WindowStateChange)
            and self.isActiveWindow()
            and not self.isMinimized()
            and self._screen() == "vod"
            and button.isBlinking()
        ):
            button.stopBlink()

    def _showRunResult(self) -> None:
        """실행 결과가 바뀌었다 — 하단바 결과 글을 고친다 (#504)."""
        finished, failed = self.runResult.counts()
        self.runResultLabel.showResult(self.runResult.isShown(), finished, failed)
        self._applyFooterShape()

    def onDownloadAllFinished(self) -> None:
        """다운로드 작업이 끝났다 — 결과를 보이고 알린 뒤 다운로드 뒤 동작(절전 · 종료)을 한다.

        끝 소식은 하단바 실행 결과 · 메뉴 버블 · 깜빡임 · OS 알림이 맡는다 — 끝 안내 대화상자는 없다
        (#523, 설계 §2 「알림 자리」: 확인이 필요한 것만 대화상자).
        """
        self.setStopButtonEnable(False)
        self._syncDownloadButton()
        # 하단바 실행 결과 (#504)
        self.runResult.finish()
        # 결과 버블 강조 · OS 알림(#523) — 절전 · 종료 전에 알린다
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
