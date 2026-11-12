"""목록의 카드 위젯(ContentItemWidget)과 그 부속 함수를 정의한다."""

import logging
import math
import os
import platform
import re
import threading
import time

from PySide6.QtCore import (
    QDir,
    QEasingCurve,
    QEvent,
    QProcess,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QUrl,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QDesktopServices,
    QEnterEvent,
    QFocusEvent,
    QFontMetricsF,
    QHideEvent,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPixmap,
    QRegion,
    QResizeEvent,
    QShowEvent,
)
from PySide6.QtWidgets import QLabel, QMessageBox, QWidget

import app.theme as theme
from app.network import REQUEST_TIMEOUT
from app.system.thread_handoff import MainCall
from app.viewmodels import card_display, card_text
from app.viewmodels.card_text import (  # noqa: F401 — 바깥(footer_bar · 테스트)이 이 자리에서 import한다
    STATE_ICON,
    short_time,
)
from app.viewmodels.data import ContentItem
from app.widgets.contentItemWidget import HoverShade, Ui_ContentItemWidget
from app.widgets.narrow_steps import step_level
from app.widgets.tip_bubble import install_tips
from core.engine.session import get_thread_session
from core.models.download_state import DownloadState

logger = logging.getLogger(__name__)

# Qt의 QWIDGETSIZE_MAX(PySide6가 노출하지 않음) — 최대폭 제한을 푸는 값.
_NO_MAX_WIDTH = (1 << 24) - 1

# 전역 설정의 다운로드 경로 — 카드는 자기 경로가 이 값과 **다를 때만**
# 3행에 경로를 표시한다(#245 — 같은 값을 카드마다 반복 표시하는 것이
# 정보 과다의 큰 몫이었다. 다르다는 것 자체가 정보다).
# app/views/mainWindow.py가 시작 시·경로 변경 시 밀어 넣는다.
# 모듈 전역을 호출 시점에 조회하므로 테스트에서 monkeypatch 가능하다.
_global_download_path = ""


def set_global_download_path(path: str) -> None:
    """전역 다운로드 경로를 갱신한다 — 카드의 경로 표시 여부 판단 기준."""
    global _global_download_path
    _global_download_path = path


def _split_path(path: str, home: str | None = None) -> tuple[str, list[str], bool]:
    """경로를 (뿌리, 뿌리 뒤 단계들, 절대경로 여부)로 나눈다 — 축약·단계 표시의 공통 분해.

    뿌리는 홈 아래면 `~`, 아니면 드라이브(`D:`), POSIX 루트면 빈 문자열(절대
    경로 플래그로 구분). 구분자는 표시용으로 `/`로 통일한다(OS 무관 렌더).
    """
    norm = os.path.normpath(path).replace("\\", "/")
    home_norm = os.path.normpath(home if home is not None else os.path.expanduser("~")).replace(
        "\\", "/"
    )

    def _same(a: str, b: str) -> bool:
        return os.path.normcase(a) == os.path.normcase(b)

    if _same(norm, home_norm):
        return "~", [], True
    if _same(norm[: len(home_norm) + 1], home_norm + "/"):
        root, rest = "~", norm[len(home_norm) + 1 :]
    else:
        # 드라이브 판정은 os.path.splitdrive가 아니라 직접 한다 — POSIX 파이썬은
        # "D:"를 드라이브로 보지 않아(splitdrive가 빈 문자열 반환) 같은 경로
        # 문자열이 OS마다 다르게 축약된다(CI 3-OS에서 실측). 이 함수는 표시
        # 전용이고 "OS 무관 렌더"가 계약이므로 판정도 OS 무관이어야 한다.
        drive_match = re.match(r"[A-Za-z]:(?=/|$)", norm)
        if drive_match:
            root, rest = drive_match.group(0), norm[drive_match.end() :].lstrip("/")
        elif norm.startswith("/"):
            root, rest = "", norm.lstrip("/")
        else:
            root, rest = "", norm
    return root, [s for s in rest.split("/") if s], bool(root) or norm.startswith("/")


def abbreviate_path(path: str, home: str | None = None) -> str:
    """카드 3행용 경로 축약(①단계 문자열) — "~/…/마지막폴더" / "D:/…/마지막폴더" (#245).

    규칙: **뿌리 + 마지막 폴더**만 남긴다. 뿌리는 홈 아래면 `~`, 아니면 드라이브
    (`D:`) 또는 POSIX 루트(`/`)다. 뿌리 뒤 단계가 2개 이하면 전부 보인다
    (`~/Downloads`, `D:/vod/lck`) — 한 단계를 숨기고 `…`를 넣으면 글자가
    오히려 늘어 축약이 아니다. 3개 이상이면 가운데를 `…` 하나로 접는다.
    근거: 유저가 고르거나 이름 붙인 것은 **마지막 폴더**이고, 어느 디스크/홈
    인지는 뿌리가 말한다. 가운데 단계는 카드마다 반복되는 소음이다. 전체
    경로는 툴팁이 준다. 폭이 모자랄 때 더 줄이는 순서(② 중간 접기 → ③ 마지막
    폴더만 말줄임)는 `path_display_parts` + `PathLabel`이 맡는다.
    """
    if not path:
        return ""
    root, segments, absolute = _split_path(path, home)
    shown = segments if len(segments) <= 2 else ["…", segments[-1]]
    return "/".join([root, *shown]) if absolute else "/".join(shown)


def path_display_parts(path: str, home: str | None = None) -> tuple[str, str, str]:
    """경로를 줄이는 단계에 필요한 세 조각 — (①전체 축약형, ②③의 고정 접두, 마지막 폴더) (#245).

    접두는 중간 폴더가 하나라도 있으면 `뿌리/…/`(중간 폴더가 하나뿐이어도 접는다
    — 마지막 폴더가 중간 폴더보다 먼저 잘리면 안 된다), 없으면 `뿌리/`다.
    ② = 접두 + 마지막 폴더, ③ = 접두 + ElideMiddle(마지막 폴더). 뿌리만 있는
    경로(`~`)는 접두 없이 뿌리 자체가 마지막이다.
    """
    full = abbreviate_path(path, home)
    if not path:
        return "", "", ""
    root, segments, absolute = _split_path(path, home)
    if not segments:
        return full, "", full
    parts = ([root] if (root or absolute) else []) + (["…"] if len(segments) >= 2 else [])
    prefix = "/".join(parts) + "/" if parts else ""
    return full, prefix, segments[-1]


def _resolution_key(resolution) -> int:
    """해상도 값을 정렬용 정수로 — API는 int(min(w,h))를 주지만 문자열("1080")도 받는다."""
    try:
        return int(resolution)
    except (TypeError, ValueError):
        return 0


# 3줄 상태 원 아이콘(card_text.SlotTexts.icon) → 메뉴 버블의 상태 그림 — 같은 그림 · 같은 상태 색(#550)
_STATUS_BUBBLE = {"down": "running", "pause": "paused", "ok": "done", "err": "all_failed"}

# 카드 버튼 전부 — 왼쪽부터의 자리 차례(contentItemWidget.py가 이 차례로 놓는다)
CARD_BUTTON_NAMES = (
    "editButton",
    "pauseButton",
    "openDirectoryButton",
    "retryButton",
    "deleteButton",
)

# 상태마다 보이는 카드 버튼(설계 §12.1 카드 배치, #550). 자르는 중 · 합치는 중은 받는 중(RUNNING)이다.
# 표에 없는 상태(조회 중 LOADING)는 ✕만이다
CARD_BUTTONS = {
    DownloadState.WAITING: ("editButton", "deleteButton"),
    DownloadState.RUNNING: ("pauseButton", "deleteButton"),
    DownloadState.PAUSED: ("pauseButton", "deleteButton"),
    DownloadState.FINISHED: ("openDirectoryButton", "deleteButton"),
    DownloadState.FAILED: ("retryButton", "deleteButton"),
}

# 툴팁이 달릴 수 있는 카드 글 위젯 — 잘린 제목 · 채널명 · 상태 글 · 정보 글 · 저장 위치(글 · 아이콘)
TIP_LABEL_NAMES = (
    "titleLabel",
    "channelNameLabel",
    "statusLabel",
    "fileSizeLabel",
    "directoryLabel",
    "pathIcon",
)

STOP_MOTION_MS = 300  # 중지 모션 — 막대 · 채움이 왼쪽 끝까지 줄어드는 시간(ms, 설계 §12.1)
# 완료 모션 — 막대 · 채움이 끝까지 차며 완료 색이 되고 카드 색조가 나타나는 시간(ms, 프로토 FILL_FINISH_MS, #571)
FINISH_MOTION_MS = 400
# 실패 모션 — 막대 · 채움이 제자리에서 실패 색이 되며 사라지고 카드 색조가 나타나는 시간(ms, 프로토
# FILL_FAIL_MS). 프로토와 달리 끝까지 채우지 않는다 — 실패인데 다 받은 것처럼 보이지 않게(#571)
FAIL_MOTION_MS = 400
# 완료 모션 뒤 끝까지 찬 막대가 흐려지며 사라지는 시간(ms) — 프로토에 없는 단계(#571, 150 ~ 200에서 정함)
BAR_FADE_MS = 200
TINT_CLEAR_MS = 200  # 실패 → 대기(다시 시도) — 색조를 걷는 시간(ms, 프로토 FILL_CLEAR_MS, #571)
TINT_STATES = (DownloadState.FINISHED, DownloadState.FAILED)  # 카드 전체 색조 · 막대 없음(#571)
# 완료 · 실패 모션을 도는 앞 상태 — 그 밖(처음 뜬 카드 · 되살린 카드)은 모션 없이 바로 마지막 모양(#571)
TINT_MOTION_FROM = (DownloadState.WAITING, DownloadState.RUNNING, DownloadState.PAUSED)
_MOTION_EASING = QEasingCurve(
    QEasingCurve.Type.InOutQuad
)  # 완료 · 실패 · 색조 걷기 곡선(프로토와 같음)
# 막대 · 배경 채움이 통지 사이를 이어 움직이는 시간(ms) — 엔진의 진행 통지 간격(`PROGRESS_SECONDS`)에서
# 센다(card_display.PROGRESS_INTERVAL_MS, #557). 짧으면 움직이고 서기를 되풀이한다(오너 실기 2026-11-10).
# 다음 통지가 먼저 오면 그려진 값에서 잇는다
TICK_MOTION_MS = card_display.PROGRESS_INTERVAL_MS
PROGRESS_SCALE = 10000  # 진행 막대 범위(0.01% 단위) — 넓은 카드에서도 한 단계가 1px보다 작다
# 애니메이션 끔일 때 받는 중 막대가 새 값으로 바뀌는 간격(ms) — 진행 통지가 이보다 잦아도 이 간격에 한 번
# 마지막 값으로 바꾼다(오너 2026-11-10)
STILL_STEP_MS = 1000
STATUS_KEEP_CHARS = (
    3  # 상태 글을 끝까지 줄여도 남기는 앞 글자 수 — 프로토 가 「✕ 사유를…」의 「사유를」
)

# ✕를 누르면 「멈추고 지우기」를 묻는 상태 — 엔진이 그 카드를 쥐고 있다(#550)
BUSY_STATES = (DownloadState.RUNNING, DownloadState.PAUSED)

# 키보드 초점으로 보지 않는 까닭 — 카드는 마우스로 초점을 받지 않지만(TabFocus) 혹시 오면 focus ring을 안 그린다
_POINTER_REASONS = (Qt.FocusReason.MouseFocusReason,)


def _label_text_width(label: QLabel, text: str) -> int:
    """`QLabel`이 그 글에 바라는 폭 — 소수 글 폭을 올림한다(`QLabel.sizeHint`와 같은 몫).

    정수 `horizontalAdvance`는 반올림이라 글자 폭이 소수인 글꼴(리눅스 CI)에서 라벨이 실제로 받는
    폭보다 1px 좁을 수 있다. 그 차이만큼 옆 채널명 라벨이 줄었다(#554, `header_controls.text_width`와
    같은 셈 — 그 모듈이 이 모듈을 import해 여기 둔다).
    """
    return math.ceil(QFontMetricsF(label.font()).horizontalAdvance(text))


def _now_ms() -> float:
    """단조 시계(ms) — 테스트가 이 이름을 바꿔 시간을 정한다."""
    return time.monotonic() * 1000


def _snapped(fraction: float, width: int) -> float:
    """진행 비율을 폭 `width`px의 1px 단위로 묶는다 — 비율이 늘면 줄지 않는다(반올림은 단조)."""
    if width <= 0:
        return fraction
    return round(fraction * width) / width


def last_folder_name(path: str) -> str:
    """경로의 마지막 폴더 이름 — 카드 3줄 저장 위치 글(#550). 운영체제와 무관하게 자른다."""
    if not path:
        return ""
    root, segments, _ = _split_path(path)
    return segments[-1] if segments else (root or path)


def _fetch_rep_size(card: MainCall, index: int, base_url: str, segment_based: bool) -> None:
    """해상도 하나의 파일 크기를 조회해 카드에 넘긴다 — 워커 스레드 (#168 · #564).

    네트워크만 담당한다 — 위젯 · 아이템 반영은 카드의 `_onRepSizeFetched`(메인 스레드)가 한다.
    카드는 `MainCall`로만 가리킨다. 조회 실패는 조용히 "Checking..."으로 남긴다(기존 동작).
    """
    size_text = ""
    if not segment_based:
        try:
            resp = get_thread_session().head(base_url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            size = int(resp.headers.get("content-length", 0))
            if size == 0:
                resp = get_thread_session().get(base_url, stream=True, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                size = int(resp.headers.get("content-length", 0))
                resp.close()
            size_text = card_text.size_text(size)
        except Exception:
            return
    card("_repSizeFetched", index, size_text)


def _fetch_image(card: MainCall, kind: str, url: str, max_height: int) -> None:
    """이미지 바이트를 받아 카드에 넘긴다 — 워커 스레드 (#168 · #564).

    QPixmap 생성 · 스케일 · setPixmap은 GUI 스레드 전용이라 여기서 하지 않는다. 디코드 이후는
    카드의 `_onImageFetched`(메인 스레드)가 한다. 카드는 `MainCall`로만 가리킨다.
    """
    try:
        response = get_thread_session().get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        card("_imageFetched", kind, response.content, url, max_height)
    except Exception as e:
        logger.error(f"Error loading image from {url}: {e}")


class ContentItemWidget(QWidget, Ui_ContentItemWidget):
    """목록의 카드 한 장 — 세 줄(채널 · 날짜 / 제목 / 상태 · 정보)과 카드 버튼 (#244 · #245 · #550).

    카드는 보이기만 한다 — 제목 · 저장 위치 · 해상도 · 구간 바꾸기는 카드 편집 창(✎ · 더블 클릭)이
    한다(#545 · #550, 설계 §12.1). 카드 버튼은 평소 숨고 호버 · 키보드 초점 때 그 상태의 버튼을
    보인다. 키보드 초점은 카드 하나가 Tab 한 번이다 — 버튼은 Tab 차례에 없고 Enter가 더블 클릭과
    같은 일을 한다.
    """

    deleteRequest = Signal()  # ✕ — 받는 중 카드의 「멈추고 지우기」 확인은 목록이 한다
    pauseRequest = Signal()  # ‖ · ▶ — 전역 일시정지 · 이어 받기와 같다(#245)
    retryRequest = Signal()  # 실패 카드의 다시 시도 — 실패한 구간만 다시 받는다(#309)
    editRequest = Signal()  # 대기 카드의 ✎ · 더블 클릭 · Enter — 카드 편집 창 (#545)
    # 해상도 크기 조회 결과가 아이템에 들어갔다 (#545) — (해상도 목록 번호, 크기 글). 열린 카드
    # 편집 창이 해상도 목록의 크기를 고친다. 창이 닫혀 지워진 뒤에 와도 연결이 함께 끊긴다
    repSizeChanged = Signal(int, str)
    hoverChanged = Signal(bool)  # 호버가 켜지고 꺼졌다 — 목록이 포인터 밑 카드를 기억한다(#550)

    # 워커 스레드의 결과가 메인 스레드에서 들어오는 입구 (#168). 위젯·아이템 조작은 반드시 메인
    # 스레드 슬롯에서 한다 — download 경로가 DownloadViewModel로 세운 스레드 경계 규칙을 content
    # 경로에도 적용한다. 워커는 카드를 잡지 않고 `MainCall`로 이 시그널을 메인 스레드에서 낸다(#564)
    _repSizeFetched = Signal(int, str)  # (해상도 index, 크기 텍스트 — 세그먼트 기반이면 "")
    _imageFetched = Signal(str, object, str, int)  # (종류, bytes, url, maxHeight)

    def __init__(self, item: ContentItem, index: int = 0, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.item = item  # ContentItem 저장
        self.index = index  # 인덱스 저장
        # 사용자가 해상도를 고른 적이 있는가(편집 창) — 늦게 온 크기 조회가 이를 덮지 않는다
        self._userPicked = False
        self._pickedIndex = 0  # 고른 해상도의 목록 번호(내림차순 정렬 뒤)
        self._hovered = False
        self._keyFocus = False
        # 창이 비활성이 되어 초점을 잃을 때의 초점 종류(키보드면 True) — 창이 다시 활성이 되어
        # 초점이 돌아오면 이 값을 쓴다. 그 밖의 까닭으로 잃었으면 None (#561)
        self._focusBeforeLeaving: bool | None = None
        self._picking = (
            False  # 목록이 선택 모드다 — 카드 버튼을 숨기고 누름 · 더블 클릭이 고르기다(#561)
        )
        self._armed = False  # 고르기를 시작한 누름이 이어지는 중 — 카드 안에서 떼면 고른다(#561)
        self._armedModifiers = Qt.KeyboardModifier.NoModifier  # 그 누름 때의 수정자
        # 그 누름을 시작한 것 — "mouse"(왼쪽 버튼) · "key"(Space). 둘이 겹치면 먼저 시작한 쪽만 따른다
        self._armedBy = ""
        self._pressed = False  # 프레임의 pressed 속성(눌린 바탕)을 켰는지(#561)
        self._tipsInstalled = False  # 말풍선 필터를 걸었는지 — 처음 호버할 때 건다
        self._levelTexts: list[card_text.SlotTexts] = []  # 접는 단계마다의 3줄 글(#554)
        self._needs: list[int] | None = None  # 단계마다 3줄 필요 폭 — 글이 바뀌면 다시 센다
        self._level = 0  # 지금 3줄 접는 단계
        self._appliedLevel = -1  # 라벨에 놓은 단계 — 다르면 글을 다시 놓는다
        self._showYear = True  # 날짜에 연도를 적는지 — 목록이 정한다
        self._shown = 0.0  # 막대 · 채움이 지금 보이는 진행 비율(0 ~ 1)
        self._motion: QVariantAnimation | None = None  # 도는 진행 모션 — 없으면 None
        # 도는 모션이 완료 · 실패 · 색조 걷기면 그 모션이 가는 상태 — 아니면 None(#571)
        self._motionGoal: DownloadState | None = None
        self._stillAt = -float("inf")  # 애니메이션 끔에서 막대를 마지막으로 바꾼 때(ms, `_now_ms`)
        self._stillPending = False  # 애니메이션 끔에서 미룬 막대 바꾸기가 걸려 있는지
        self._previousState: object = None  # 직전 applyStateStyle의 상태 — 중지를 알아본다
        self._imageLabels: dict[str, QLabel] = {}  # 이미지 종류 → 받은 그림을 놓을 라벨(#564)
        self.setupUi(self)
        # 카드 하나가 Tab 한 번이다. 마우스로는 초점을 받지 않는다 — focus ring은 키보드 초점만
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self._setupThreadRelays()  # setupDynamicUi가 스레드를 띄우기 전에 연결돼야 한다 (#168)
        self.setupDynamicUi()
        self.setupSignals()  # 시그널 연결

    def _setupThreadRelays(self) -> None:
        self._repSizeFetched.connect(self._onRepSizeFetched)
        self._imageFetched.connect(self._onImageFetched)

    def _placeProgressBar(self) -> None:
        """하단 진행바를 카드 바닥에 오버레이로 배치한다 (#245).

        레이아웃 행으로 넣으면 보일 때만 카드가 barHeight만큼 자라
        "상태가 바뀌어도 카드 높이 불변"(목록 들썩임 금지)이 깨진다 —
        그래서 지오메트리를 직접 잡는다. 테두리(1px) 안쪽 전체 폭.
        """
        bar_h = theme.METRICS["barHeight"]
        frame = self.contentFrame
        bar_y = frame.height() - bar_h - 1
        self.progressBar.setGeometry(1, bar_y, frame.width() - 2, bar_h)
        # 카드 곡률로 잘라낸다 — 바 자체의 QSS border-radius는 높이(4px)에
        # 눌려 카드 반지름을 못 따라가고, 그 결과 막대 양끝이 카드의
        # 둥근 모서리 바깥으로 튀어나온다(실기 렌더 실측 — 바닥 모서리에
        # 트랙·진행색 조각이 카드 몸통 밖에 남는다). 프레임 테두리 안쪽의
        # 둥근 사각형을 바 좌표계로 옮겨 마스크로 건다. 값(QRegion)이라
        # 카드에 상주하는 파이썬 객체가 아니다.
        radius = theme.METRICS["cardRadius"] - 1
        path = QPainterPath()
        path.addRoundedRect(
            QRectF(0, 1 - bar_y, frame.width() - 2, frame.height() - 2), radius, radius
        )
        self.progressBar.setMask(QRegion(path.toFillPolygon().toPolygon(), Qt.FillRule.WindingFill))
        self.progressBar.raise_()

    def _placeCardButtons(self) -> None:
        """카드 버튼 상자를 1줄 오른쪽 끝에, 덮개를 그 밑에 놓는다 (#550).

        버튼 위끝은 썸네일 위끝과 같다. 덮개는 버튼 상자 왼쪽 ``SOLID + FADE``부터 1줄 오른쪽
        끝까지 덮는다 — 버튼 밑의 채널명 · 날짜 글이 버튼과 겹쳐 보이지 않게.
        """
        box = self.hoverBox
        # 숨은 동안 버튼 수가 바뀌었으면 레이아웃 힌트가 옛 값이다 — 먼저 다시 계산시킨다
        box.layout().invalidate()
        box.layout().activate()
        box.adjustSize()
        row = self.topRow.geometry()
        right = row.right() + 1
        top = self.thumbnailLabel.geometry().top()
        box.move(right - box.width(), top)
        shade_left = max(row.left(), box.x() - HoverShade.SOLID - HoverShade.FADE)
        self.hoverShade.setGeometry(shade_left, top, right - shade_left, box.height())
        self.hoverShade.raise_()
        box.raise_()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._placeProgressBar()
        if self.hoverBox.isVisibleTo(self):
            self._placeCardButtons()
        self._fitRow3()

    def showEvent(self, event: QShowEvent) -> None:
        """첫 표시 직후 3줄 폭 맞추기를 한 번 더 돌린다 (#245).

        첫 표시에서 resizeEvent는 자식 contentFrame의 레이아웃이 활성화되기 **전에** 도착한다 —
        Qt의 show_helper가 대기 중이던 Resize를 자식 표시 전에 보내고, showEvent는 자식 표시
        **후에** 보낸다. 그때 3줄 폭이 0이라 맞추기가 "배치 전"으로 끝나면, 이후 크기가 안 바뀌는
        한 다시 돌지 않는다. showEvent 시점엔 자식 레이아웃이 잡혀 있다.
        """
        super().showEvent(event)
        self._fitRow3()
        # 누른 채 숨었다가(지우기 · 필터) 다시 보이는 카드에 눌린 바탕이 남지 않게 한다(#561)
        if self._armed or self._pressed:
            self._disarm()

    def hideEvent(self, event: QHideEvent) -> None:
        """숨는 카드(필터 · 지우기)는 완료 · 실패 · 색조 걷기 모션을 멈추고 마지막 모양으로 간다 (#571)."""
        super().hideEvent(event)
        self._finishStepMotion()

    # ---- 호버 · 키보드 초점 — 카드 버튼을 보이는 때 (#550) ----

    def enterEvent(self, event: QEnterEvent) -> None:
        super().enterEvent(event)
        self.setHovered(True)

    def leaveEvent(self, event: QEvent) -> None:
        super().leaveEvent(event)
        self.setHovered(False)

    def isHovered(self) -> bool:
        """포인터가 이 카드 위에 있다고 보는지."""
        return self._hovered

    def setHovered(self, hovered: bool) -> None:
        """호버를 켜고 끈다 — 카드 바탕 한 단계(`cardHoverBg`)와 카드 버튼 (#550).

        포인터의 들어감 · 나감과 목록의 다시 맞추기(`ContentListView._rehover` — 목록이 바뀐 뒤
        포인터 밑의 카드가 달라졌을 때)가 부른다. 같은 값이면 아무것도 하지 않는다.
        """
        if hovered == self._hovered:
            return
        if hovered:
            self._installTips()
        self._hovered = hovered
        self.contentFrame.setProperty("hover", hovered)
        theme.repolish(self.contentFrame)
        self._syncCardButtons()
        self.hoverChanged.emit(hovered)

    def _installTips(self) -> None:
        """툴팁이 달릴 수 있는 위젯에 말풍선 필터를 건다 — 카드마다 처음 호버할 때 한 번 (#550).

        툴팁은 모두 말풍선(0ms)으로 뜬다(설계 §12.1 「툴팁」). 필터(#536)는 카드 버튼과 글 위젯에만
        건다 — 카드 전체 · 모든 자식에 걸면 그 위젯들의 모든 이벤트를 파이썬 필터로 끌어온다. 잘렸을
        때만 툴팁이 달리는 글도 미리 걸고, 툴팁이 비면 띄우지 않는다(`TipFilter._allowed`).

        만들 때가 아니라 처음 호버할 때 건다 — 카드 1000장의 목록에서 카드마다 필터를 걸면 삽입과
        필터 칩 전환(카드를 숨기고 보일 때마다 숨김 이벤트가 필터를 지난다)이 눈에 띄게 늘었다(inbox
        128 실측). 포인터는 카드에 먼저 들어가고 그 안의 위젯에 들어가므로 — Qt는 들어감을 바깥에서
        안쪽 차례로 보낸다 — 위젯이 들어감을 받기 전에 필터가 걸려 있다.
        """
        if self._tipsInstalled:
            return
        self._tipsInstalled = True
        for name in CARD_BUTTON_NAMES + TIP_LABEL_NAMES:
            install_tips(getattr(self, name))

    def hasKeyFocus(self) -> bool:
        """키보드 초점이 이 카드에 있는지 — focus ring · 카드 버튼을 보이는 때."""
        return self._keyFocus

    def _setKeyFocus(self, on: bool) -> None:
        """키보드 초점을 켜고 끈다 — 카드 버튼을 맞추고 목록에 알린다 (#550 · #561).

        초점 표시는 카드가 그리지 않는다 — 목록이 카드 바깥에 focus ring 하나를 그린다
        (`ContentListView.cardKeyFocusChanged`). 프레임의 `keyFocus` 속성은 남긴다(바깥이 읽는다) —
        이 속성을 보는 QSS 규칙이 없어 스타일은 다시 계산하지 않는다.
        """
        if on == self._keyFocus:
            return
        self._keyFocus = on
        self.contentFrame.setProperty("keyFocus", on)
        self._syncCardButtons()
        owner = self._listOwner()
        if owner is not None:
            owner.cardKeyFocusChanged(self, on)

    def focusInEvent(self, event: QFocusEvent) -> None:
        """초점이 들어왔다 — 목록 밖에서 Tab으로 들어오면 목록이 보이는 차례의 첫(끝) 카드로 옮긴다.

        키보드 초점인지는 초점 이벤트의 사유로 정한다. 창이 다시 활성이 되어 돌아온 초점
        (`ActiveWindowFocusReason`)은 창을 떠나기 전의 종류를 따른다 — 마우스로 받은 초점이
        창을 오갔다고 키보드 초점(focus ring)이 되지 않는다(#561).
        """
        super().focusInEvent(event)
        returning = event.reason() == Qt.FocusReason.ActiveWindowFocusReason
        if returning and self._focusBeforeLeaving is not None:
            key = self._focusBeforeLeaving
        else:
            key = event.reason() not in _POINTER_REASONS
        self._focusBeforeLeaving = None
        self._setKeyFocus(key)
        owner = self._listOwner()
        if owner is not None:
            owner.cardFocusEntered(self, event.reason())

    def focusOutEvent(self, event: QFocusEvent) -> None:
        """초점이 나갔다 — 창이 비활성이 되어 나갔으면 그때의 초점 종류를 기억한다(#561).

        Space를 누른 채 초점이 나가면(↑↓ · Tab · 다른 곳 클릭 · 창 비활성) 그 누름은 고르지 않고 끝낸다.
        """
        super().focusOutEvent(event)
        if self._armedBy == "key":
            self._disarm()
        leaving = event.reason() == Qt.FocusReason.ActiveWindowFocusReason
        self._focusBeforeLeaving = self._keyFocus if leaving else None
        self._setKeyFocus(False)

    def focusNextPrevChild(self, next: bool) -> bool:
        """Tab · Shift+Tab은 보이는 차례(정렬 · 필터 뒤)로 다음 카드에 간다 — 목록이 정한다."""
        owner = self._listOwner()
        if owner is not None and owner.stepCardFocus(self, next):
            return True
        return super().focusNextPrevChild(next)

    def _listOwner(self) -> QWidget | None:
        """이 카드를 든 목록(`ContentListView`) — 없으면(카드만 띄운 테스트 · 견본 카드) None.

        목록 모듈은 이 모듈을 import하므로 이름으로 찾지 않고 목록의 메서드로 찾는다.
        """
        parent = self.parentWidget()
        while parent is not None:
            if hasattr(parent, "stepCardFocus"):
                return parent
            parent = parent.parentWidget()
        return None

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """카드 키 — Enter · ↑↓ · 선택 모드의 Space (#550 · #561).

        Enter는 더블 클릭과 같은 일(대기 = 편집 창, 완료 = 폴더 열기)이고, 실패 카드에서는 다시 시도다(버튼이
        Tab 차례 밖이라 키보드로 다시 시도할 길이 이것뿐이다). 선택 모드에서는 Enter가 아무것도 열지 않는다.
        ↑↓는 보이는 차례로 앞 · 뒤 카드에 초점을 옮긴다 — 끝에서는 그대로다. Delete는 목록의 단축키가 받는다.
        선택 모드의 Space는 이 카드를 클릭한 것과 같다(Shift+Space = Shift+클릭) — 클릭과 같이 누르면
        눌린 바탕만 켜고, 떼면(`keyReleaseEvent`) 목록의 `cardPicked`로 간다(#561). 누른 채 초점이 다른
        곳으로 가면 고르지 않는다(`focusOutEvent`). 선택 모드 밖의 Space는 받지 않는다 — Shift가 붙어도
        선택 모드로 들어가지 않는다.
        """
        key = event.key()
        if key == Qt.Key.Key_Space and self._picking:
            owner = self._listOwner()
            if owner is not None:
                event.accept()
                # 누르고 있을 때 오는 키 반복은 넘긴다. 마우스로 누른 채면 먼저 시작한 마우스만 따른다
                if not event.isAutoRepeat() and not self._armed:
                    self._arm(True, event.modifiers(), "key")
                return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            event.accept()
            if self._picking:
                return
            if self.item.downloadState == DownloadState.FAILED:
                self.retryRequest.emit()
            else:
                self.activate()
            return
        # macOS는 방향키에 키패드 수정자를 단다(Qt 문서) — 그것만 빼고 다른 수정자가 없을 때
        modifiers = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down) and not modifiers:
            event.accept()
            owner = self._listOwner()
            if owner is not None:
                owner.moveCardFocus(self, key == Qt.Key.Key_Down)
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        """Space로 시작한 누름을 떼면 목록에 고르기를 알린다 — 수정자는 누를 때의 값이다 (#561)."""
        if event.key() == Qt.Key.Key_Space and self._armedBy == "key":
            event.accept()
            if event.isAutoRepeat():
                return  # 누르고 있을 때 오는 키 반복의 뗌 — 아직 누르고 있다
            modifiers = self._armedModifiers
            self._disarm()
            owner = self._listOwner()
            if owner is not None and self._picking:
                owner.cardPicked(self, modifiers)
            return
        super().keyReleaseEvent(event)

    # ---- 선택 모드 (#561) ----

    def isPicking(self) -> bool:
        """목록이 선택 모드인지 — 카드 버튼이 숨고 누름이 고르기다."""
        return self._picking

    def setPicking(self, on: bool) -> None:
        """선택 모드를 켜고 끈다 — 목록이 부른다."""
        if on != self._picking:
            self._picking = on
            self._finishStepMotion()  # 완료 · 실패 모션 도중이면 마지막 모양으로 바로(#571)
            self._syncCardButtons()
            self._syncPickMark()

    def _syncPickMark(self) -> None:
        """썸네일의 체크 원을 선택 모드 · 고름에 맞춘다 — 선택 모드에서만 그린다."""
        self.thumbnailLabel.setPickMark(self._picking, self.contentFrame.isSelected())

    def isSelected(self) -> bool:
        """고른 카드인지(강조색 테두리 · 채운 체크 원)."""
        return self.contentFrame.isSelected()

    def setSelected(self, on: bool) -> None:
        """고른 카드 모양을 켜고 끈다 — 목록이 부른다.

        강조색 테두리는 카드 면과 progress bar가 함께 그린다 — 면 위에 놓인 progress bar가 면의 아래 변을
        가리므로 그 자리의 테두리는 progress bar가 덧그린다(`CardProgress`).
        """
        self.contentFrame.setSelected(on)
        self.progressBar.setPicked(on)
        self._syncPickMark()

    # ---- 뗄 때 고르기 (#561) ----
    # 누르면 눌린 바탕만 켜고, 카드 안에서 떼면 고른다. 누른 채 카드 밖으로 나가면 눌린 바탕이 꺼지고 그대로
    # 떼면 아무 일도 없다. 버튼을 누른 동안에는 포인터가 밖에 있어도 옮김 · 뗌이 누른 위젯으로 온다(Qt)

    def isPressed(self) -> bool:
        """눌린 바탕이 켜져 있는지 — 고르기를 시작한 누름이 카드 안에 있는 동안."""
        return self._pressed

    def _setPressed(self, on: bool) -> None:
        """눌린 바탕(`cardPressedBg` 등)을 켜고 끈다 — 모션 없이 바로 바뀐다."""
        if on == self._pressed:
            return
        self._pressed = on
        frame = self.contentFrame
        frame.setProperty("pressed", on)
        theme.repolish(frame)
        frame.update()  # 배경 채움은 면이 직접 그린다 — 속성만 바뀌면 다시 그리지 않을 수 있다
        if self.hoverShade.isVisibleTo(self):
            self.hoverShade.update()

    def _arm(self, on: bool, modifiers: Qt.KeyboardModifier, source: str = "mouse") -> None:
        """고르기를 시작한 누름이면 뗌을 기다린다 — 수정자는 누를 때의 값을 든다.

        Args:
            source: 누름을 시작한 것 — "mouse"(왼쪽 버튼) · "key"(Space)
        """
        self._armed = on
        self._armedModifiers = modifiers
        self._armedBy = source if on else ""
        self._setPressed(on)

    def _disarm(self) -> None:
        self._armed = False
        self._armedBy = ""
        self._setPressed(False)

    def _holds(self, event: QMouseEvent) -> bool:
        """포인터 밑이 이 카드인지 — 굴러 나가 가려진 부분, 다른 위젯이 덮은 부분은 밖으로 본다.

        커서를 읽지 않고 이벤트의 자리로 창에 묻는다(`QWidget.childAt`) — 마우스를 받지 않는 위젯(focus
        ring)은 건너뛴다.
        """
        point = event.position().toPoint()
        top = self.window()
        if top is self:
            return self.rect().contains(point)
        under = top.childAt(self.mapTo(top, point))
        return under is self or (under is not None and self.isAncestorOf(under))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """카드 바탕 · 글 · 썸네일을 누르면 목록에 알린다 — 고르기는 뗄 때 한다(#561).

        목록이 「고르기를 시작하는 누름」이라고 답하면(선택 모드, 또는 Ctrl/⌘ · Shift와 함께) 눌린 바탕을
        켜고 뗌을 기다린다. 카드 버튼은 누름을 스스로 받아 여기 오지 않는다. 목록이 없으면(카드만 띄운
        테스트) 기본 처리다.
        """
        owner = self._listOwner()
        if event.button() == Qt.MouseButton.LeftButton and owner is not None:
            event.accept()
            if self._armedBy == "key":
                return  # Space를 누른 채다 — 먼저 시작한 Space만 따른다(두 번 고르지 않는다)
            self._arm(owner.cardPressed(self, event.modifiers()), event.modifiers())
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """누른 채 옮기면 눌린 바탕이 포인터가 카드 안에 있는지를 따른다 (#561)."""
        if self._armedBy == "mouse":
            event.accept()
            self._setPressed(self._holds(event))
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        """고르기를 시작한 누름을 카드 안에서 떼면 목록에 고르기를 알린다 — 밖에서 떼면 아무 일도 없다 (#561)."""
        if event.button() == Qt.MouseButton.LeftButton and self._armedBy == "key":
            event.accept()
            return  # Space를 누른 채 누른 마우스 — Space를 뗄 때 고른다
        if event.button() == Qt.MouseButton.LeftButton and self._armed:
            event.accept()
            inside = self._holds(event)
            modifiers = self._armedModifiers
            self._disarm()
            owner = self._listOwner()
            if inside and owner is not None:
                owner.cardPicked(self, modifiers)
            return
        super().mouseReleaseEvent(event)

    def _syncCardButtons(self) -> None:
        """카드 버튼 상자 · 덮개를 호버 · 키보드 초점에 맞춰 보이고 숨긴다 — 선택 모드에서는 늘 숨긴다."""
        shown = (self._hovered or self._keyFocus) and not self._picking
        if shown:
            self._placeCardButtons()
        self.hoverBox.setVisible(shown)
        self.hoverShade.setVisible(shown)
        if shown:
            self.hoverShade.update()

    def visibleCardButtons(self) -> list[str]:
        """지금 상태에서 상자에 놓인 카드 버튼 이름 — 왼쪽부터(호버 · 초점이 아니면 상자가 숨는다)."""
        return [name for name in CARD_BUTTON_NAMES if not getattr(self, name).isHidden()]

    def _clampChannelMinWidth(self) -> None:
        """채널명 최소폭을 "자연 폭과 64px 중 작은 쪽"으로 맞춘다.

        고정 최소폭 64는 좁은 창에서 채널명이 "..." 하나로 붕괴하는 것을
        막는 장치(#239 흡수)인데, "LCK"처럼 자연 폭이 64보다 좁은 이름에는
        쓰지도 않는 폭을 예약해 이름과 날짜 사이가 벌어진다. 이름이 정해진 뒤
        최소폭을 자연 폭 이하로 눌러 짧은 이름은 딱 붙고, 긴 이름은 64px 바닥을 유지한다.
        """
        natural = self.channelNameLabel.sizeHint().width()
        self.channelNameLabel.setMinimumWidth(min(64, natural))

    def setIndex(self, index: int) -> None:
        """카드의 정렬 순번을 갱신한다 (#235 → #244 재설계로 표시는 제거).

        순번 자체는 삭제 후 재번호매김(#235, `app/widgets/view.py::_renumberAll`)이 이
        메서드로 계속 유지한다 — 표시가 없어도 위젯·모델 행의 대응이 어긋나지 않게 하는 값이다.
        """
        self.index = index

    def setupDynamicUi(self) -> None:
        icon = theme.METRICS["cardStatusIcon"]
        self.loadImageFromUrl(self.channelImageLabel, self.item.channel_image_url, icon, "channel")
        self.loadImageFromUrl(
            self.thumbnailLabel, self.item.thumbnail_url, self.thumbnailLabel.height(), "thumbnail"
        )
        # 조작 도형(app/widgets/icons.py) — 평소 muted, 호버에서 강조. 삭제는 호버에서만
        # 실패색(빨강)이 된다(#244). pauseButton의 도형(pause↔resume)은 applyStateStyle이 바꾼다
        self.deleteButton.setIconName("delete")
        self.deleteButton.setHoverToken("stateFailed")
        self.pauseButton.setIconName("pause")
        self.retryButton.setIconName("retry")
        self.openDirectoryButton.setIconName("folder")
        self.editButton.setIconName("edit")  # 카드 편집 창 (#545)
        self.pathIcon.setIconName("folder_outline")
        self._applyHeader()
        self.applyStateStyle()  # setData 전에도 카드가 무스타일로 보이지 않게 (#227)

    def _applyHeader(self) -> None:
        """1 · 2줄(채널명 · 날짜 · 제목)과 3줄 저장 위치를 아이템에 맞춘다."""
        self.channelNameLabel.setText(self.item.channel_name)
        self._clampChannelMinWidth()
        self.dateLabel.setText(card_text.upload_date_text(self.item, self._showYear))
        self.titleLabel.setText(self.item.title)
        self._refreshPathLabel()

    def _pathShown(self) -> bool:
        """저장 위치를 3줄에 보이는지 — 기본 위치와 다를 때만이다(#245 · #550).

        같은 값을 카드마다 되풀이하는 것은 정보가 아니다 — 다르다는 것 자체가 정보다.
        """
        path = self.item.download_path
        return bool(path) and path != _global_download_path

    def _refreshPathLabel(self) -> None:
        """저장 위치 — 회색 폴더 윤곽 + 마지막 폴더 이름. 누를 수 없고 툴팁에 전체 경로 (#550)."""
        shown = self._pathShown()
        self._needs = None  # 저장 위치가 바뀌면 필요 폭이 바뀐다
        self._appliedLevel = -1
        self.pathIcon.setVisible(shown)
        self.directoryLabel.setVisible(shown)
        if not shown:
            return
        path = self.item.download_path
        self.directoryLabel.setText(last_folder_name(path))
        self.directoryLabel.setToolTip(path)
        self.pathIcon.setToolTip(path)

    def setupSignals(self) -> None:
        self.deleteButton.clicked.connect(self.requestDelete)
        self.editButton.clicked.connect(self.requestEdit)
        self.openDirectoryButton.clicked.connect(self.requestOpenDir)
        self.pauseButton.clicked.connect(self.pauseRequest.emit)
        self.retryButton.clicked.connect(self.retryRequest.emit)

    # ---- 해상도 — 카드는 고른 것을 보이기만 한다. 고르기는 편집 창 (#550) ----

    def loadRepresentations(self) -> None:
        """해상도 목록을 내림차순으로 정렬하고 크기를 조회하며 최고 해상도를 기본으로 고른다.

        카드에는 해상도 버튼이 없다(#550 — 고르기는 카드 편집 창). 정렬 · 크기 조회 · 기본
        선택은 그대로 카드가 한다 — 편집 창의 해상도 목록이 이 정렬과 크기를 읽는다.
        """
        self._userPicked = False
        self._pickedIndex = 0
        # LOADING 자리표시 아이템은 해상도 목록이 아직 없다 (#124)
        if not self.item.unique_reps:
            return
        # 순서는 **내림차순**(높은 해상도가 앞) — #245 오너 확정. 기본 선택이 첫 항목이다.
        # core/media·core/chzzk·app/network의 내부 정렬(오름차순)은 건드리지 않고 표시
        # 계층에서만 뒤집는다. 해상도가 같은 두 항목은 원본이 앞이다 (#318). 해상도와 원본
        # 여부가 같은 두 항목은 대역폭이 높은 쪽이 앞이다 (#336). 조회 목록은 그런 항목을
        # 대역폭 오름차순으로 둔다(core/media/dash.py · playback_tracks.list_streams) —
        # DASH 항목은 대역폭 값을 들지 않으므로 조회 목록의 자리로 정한다
        listed_at = {id(rep): number for number, rep in enumerate(self.item.unique_reps)}
        self.item.unique_reps.sort(
            key=lambda rep: (
                _resolution_key(rep[0]),
                ContentItem.rep_is_original(rep),
                listed_at[id(rep)],
            ),
            reverse=True,
        )
        for unique_rep in self.item.unique_reps:
            # 크기 조회가 끝나기 전 표시 — "Unknown"은 실패로 읽혀 "확인 중"으로 표기 (#124)
            unique_rep.append(self.tr("Checking..."))
        for index, (_resolution, base_url, _size) in enumerate(self.item.unique_reps):
            self._fetchRepSize(index, base_url)
        self._pick(0)

    def pickedIndex(self) -> int:
        """고른 해상도의 목록 번호(내림차순 정렬 뒤)."""
        return self._pickedIndex

    def _fetchRepSize(self, index: int, base_url: str) -> None:
        """해상도 하나의 파일 크기를 워커 스레드에서 조회한다 — 결과는 `_onRepSizeFetched`로.

        워커에는 값과 `MainCall`만 넘긴다 — 카드를 잡으면 카드가 워커 스레드에서 지워질 수
        있다(#564). 카드가 결과보다 먼저 지워지면 결과는 버려진다.
        """
        threading.Thread(
            target=_fetch_rep_size,
            args=(MainCall(self), index, base_url, self.item.is_segment_based),
            daemon=True,
        ).start()

    def _onRepSizeFetched(self, index: int, size_text: str) -> None:
        """해상도 크기 조회 결과를 아이템에 반영한다 — 메인 스레드 (#168).

        고른 해상도의 크기면 정보 글을 고친다. 사용자가 편집 창에서 고른 것을 늦게 온 조회가
        기본 선택으로 되돌리지 않는다(#244 P-4) — 고른 번호의 결과만 반영한다.
        """
        if index >= len(self.item.unique_reps):
            return  # setData로 아이템이 교체된 뒤의 늦은 완료
        if size_text:
            self.item.unique_reps[index][-1] = size_text
            self.repSizeChanged.emit(index, size_text)  # 열린 편집 창의 해상도 목록 (#545)
        if index == self._pickedIndex:
            self._pick(index)

    def _pick(self, index: int) -> None:
        """해상도 목록의 `index`번을 아이템의 해상도로 고른다 — 대기 카드에서만."""
        if self.item.downloadState != DownloadState.WAITING:
            return
        rep = self.item.unique_reps[index]
        self._pickedIndex = index
        self.item.resolution = rep[0]
        self.item.base_url = rep[1]
        # 해상도가 같은 항목이 둘일 수 있다 — 고른 항목의 스트림까지 기억한다 (#318)
        self.item.select_rep(rep)
        # 세그먼트 기반(m3u8·hls_aes)은 total_size를 미리 알 수 없어 처리하지 않음
        if not self.item.is_segment_based:
            self.item.total_size = rep[-1]
        self._applyTexts()

    def syncPickedResolution(self) -> None:
        """아이템의 해상도에 맞춰 고른 번호를 다시 잡는다 — 편집 창에서 바꾼 뒤 (#545).

        고른 것이 이미 같으면 아무것도 하지 않는다. 바꿨으면 사용자가 고른 것이므로, 늦게 온
        크기 조회가 기본 선택으로 되돌리지 않게 한다.

        항목은 해상도 · 주소 · 스트림으로 찾는다 — 세그먼트 목록(m3u8)은 항목마다 주소가 비어
        있어, 해상도 · 주소만 보면 같은 해상도의 다른 스트림(원본 · 60fps)을 가르지 못한다(#550).
        """
        item = self.item
        if item.downloadState != DownloadState.WAITING:
            return
        picked = (item.resolution, item.base_url, getattr(item, "stream", None))
        for index, rep in enumerate(item.unique_reps):
            if (rep[0], rep[1], getattr(rep, "stream", None)) == picked:
                break
        else:
            return
        if index == self._pickedIndex:
            return
        self._userPicked = True
        self._pickedIndex = index

    # ---- 이미지 ----

    def loadImageFromUrl(self, label: QLabel, url: str, maxHeight: int, type: str) -> None:
        """주어진 URL에서 이미지를 받아 라벨에 띄운다 — 받기는 워커 스레드, 표시는 메인 스레드."""
        if not url:
            label.clear()
            return
        # 결과가 오면 종류로 라벨을 찾는다 — 워커에 라벨(카드의 자식)을 넘기지 않는다 (#564)
        self._imageLabels[type] = label
        threading.Thread(
            target=_fetch_image, args=(MainCall(self), type, url, maxHeight), daemon=True
        ).start()

    def _onImageFetched(self, type: str, data: bytes, url: str, maxHeight: int) -> None:
        """이미지 디코드·스케일·표시 — 메인 스레드 (#168)."""
        label = self._imageLabels[type]
        try:
            image = QPixmap()
            image.loadFromData(data)
            if type == "thumbnail":
                # 썸네일 칸은 110×62 고정이다(#550) — 도착한 때의 칸 크기로 한 번 그린다
                label.setPixmap(self._composeThumbnail(image, label.size()))
                return
            original_width = image.width()
            original_height = image.height()
            if original_height > original_width:
                new_width = maxHeight
                new_height = int(maxHeight * original_height / original_width)
            else:
                new_height = maxHeight
                new_width = int(new_height * original_width / original_height)
            scaled_image = image.scaled(
                new_width,
                new_height,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            # 채널 이미지는 원형으로 자른다(#244 재설계 — 1줄의 ⬤ 아바타). QSS border-radius는
            # 배경만 둥글게 하고 pixmap은 못 자르므로 로드 시점에 한 번 마스킹한다
            label.setPixmap(self._circled(scaled_image))
        except Exception as e:
            logger.error(f"Error loading image from {url}: {e}")

    @staticmethod
    def _composeThumbnail(pixmap: QPixmap, box: QSize) -> QPixmap:
        """16:9 고정 상자에 맞춘 썸네일 합성본을 돌려준다 (#245).

        - 원본 비율 유지로 상자 안에 맞추고, 남는 letterbox 여백은 **이미지
          평균색**으로 채운다(1x1 스케일다운 한 번) — 검정은 도드라지고 카드
          배경색은 붕 떠서, 평균색이어야 이미지와 이어져 보인다(오너 확정).
          세로 썸네일(클립)에서만 좌우 여백이 나타난다.
        - 모서리는 thumbRadius로 합성 시점에 잘라낸다 — QSS border-radius는 pixmap을 못 자른다.
        - 로드 시점 일회성 변환이다 — 카드에 상주하는 파이썬 객체가 아니다.
        """
        average = (
            pixmap.scaled(
                1,
                1,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            .toImage()
            .pixelColor(0, 0)
        )
        fitted = pixmap.scaled(
            box, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        result = QPixmap(box)
        result.fill(Qt.GlobalColor.transparent)
        painter = QPainter(result)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        radius = theme.METRICS["thumbRadius"]
        path.addRoundedRect(0, 0, box.width(), box.height(), radius, radius)
        painter.setClipPath(path)
        painter.fillRect(0, 0, box.width(), box.height(), average)
        painter.drawPixmap(
            (box.width() - fitted.width()) // 2, (box.height() - fitted.height()) // 2, fitted
        )
        painter.end()
        return result

    @staticmethod
    def _circled(pixmap: QPixmap) -> QPixmap:
        """정사각 기준 원형으로 마스킹한 사본을 돌려준다 — 채널 아바타용."""
        side = min(pixmap.width(), pixmap.height())
        result = QPixmap(side, side)
        result.fill(Qt.GlobalColor.transparent)
        painter = QPainter(result)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addEllipse(0, 0, side, side)
        painter.setClipPath(path)
        painter.drawPixmap(0, 0, pixmap)
        painter.end()
        return result

    # ---- 3줄 글 ----

    def setData(self, item: ContentItem, index: int) -> None:
        """모델 데이터를 위젯에 반영한다 — 3줄 글은 상태마다 `card_text`가 정한다 (#245 · #550)."""
        self.item = item
        self.setIndex(index)
        self._applyHeader()
        self.applyStateStyle()
        self.syncPickedResolution()  # 편집 창이 바꾼 해상도 (#545)

    def _applyTexts(self) -> None:
        """3줄의 상태 아이콘과 단계마다의 상태 글 · 정보 글을 만들어 두고 폭에 맞는 단계를 고른다 (#554).

        글은 데이터가 바뀔 때 한 번 단계 수만큼 만든다 — 창 크기가 바뀔 때는 만들어 둔 글의 필요 폭으로
        단계만 고른다(`_fitRow3`).
        """
        item = self.item
        self._levelTexts = [
            card_text.slot_texts(item, level) for level in range(card_text.LEVEL_MAX + 1)
        ]
        self._needs = None
        texts = self._levelTexts[0]
        self.statusIcon.setState(_STATUS_BUBBLE.get(texts.icon, ""))
        self.statusLabel.setDetailTip(texts.status_tip or "")
        self._appliedLevel = -1
        self._fitRow3()

    def _levelNeeds(self) -> list[int]:
        """단계마다 3줄이 바라는 폭(px) — 지금 보이는 모양과 무관하게 글로 센다(설계 §2 좁은 폭).

        줄의 차례: 상태 원 · 상태 글 ··· 폴더 아이콘 · 폴더 이름 · 정보 글. 숨은 항목과 늘어나는 빈칸은
        간격을 받지 않는다(QBoxLayout). 라벨 하나는 글 폭 + 4(`ElidingLabel.sizeHint`)를 바란다.
        """
        if self._needs is not None:
            return self._needs
        spacing = self.resolutionLayout.spacing()
        status_metrics = self.statusLabel.fontMetrics()
        info_metrics = self.fileSizeLabel.fontMetrics()
        path_shown = self._pathShown()
        folder = last_folder_name(self.item.download_path) if path_shown else ""
        folder_width = self.directoryLabel.fontMetrics().horizontalAdvance(folder) + 4
        needs = []
        for level, texts in enumerate(self._levelTexts):
            widths = []
            if texts.icon:
                widths.append(self.statusIcon.width())
            widths.append(status_metrics.horizontalAdvance(texts.status) + 4)
            if path_shown and level < card_text.LEVEL_PATH_GONE:
                widths.append(self.pathIcon.width())
                if level < card_text.LEVEL_PATH_ICON:
                    widths.append(folder_width)
            if texts.size:
                widths.append(info_metrics.horizontalAdvance(texts.size) + 4)
            needs.append(sum(widths) + spacing * (len(widths) - 1))
        self._needs = needs
        return needs

    def _fitRow3(self) -> None:
        """3줄의 접는 단계를 폭에 맞춰 고르고 그 단계의 글을 놓는다 (#554, 설계 §3 「3행 상태 보호」).

        판정은 셸 좁은 폭과 같다(`narrow_steps.step_level` — 모자라면 올리고, 되돌아갈 때는 +8). 진행률 ·
        상태 단어 · 실패 사유 앞은 어느 단계에서도 빠지지 않는다. 마지막 단계에서도 모자라면(창 최소폭보다
        좁은 카드 — 단독으로 띄운 테스트 카드 등) 상태 글 → 정보 글 차례로 말줄임한다 — 가로 넘침은 없다.

        라벨에 최소폭을 걸지 않는다 — 최소폭은 카드 최소폭이 되어 카드가 목록보다 넓게 굳는다(줄 폭이 카드
        폭에서 나오므로 되먹임, #550). 표시 전(줄 폭 0)이면 넓은 모양으로 두고 showEvent에서 다시 온다.
        """
        if not self._levelTexts:
            return
        row_width = self._rowWidth()
        level = self._level
        if row_width > 0:
            level = step_level(self._levelNeeds(), row_width, self._level)
        self._level = level
        texts = self._levelTexts[level]
        if level != self._appliedLevel:
            self._appliedLevel = level
            self.statusLabel.setText(texts.status)
            info = self.fileSizeLabel
            info.setText(texts.size or "")
            info.setVisible(bool(texts.size))
            info.setDetailTip(self._infoTip(texts.size or ""))
            path_shown = self._pathShown()
            self.pathIcon.setVisible(path_shown and level < card_text.LEVEL_PATH_GONE)
            self.directoryLabel.setVisible(path_shown and level < card_text.LEVEL_PATH_ICON)
        self._capRow3(row_width, texts)

    def _capRow3(self, row_width: int, texts: card_text.SlotTexts) -> None:
        """마지막 수단 — 고른 단계에서도 모자라면 상태 글 → 정보 글 차례로 줄게 최대폭을 씌운다.

        고른 단계가 들어가면(대개) 최대폭을 푼다. 실패 사유 앞의 말줄임도 여기서 일어난다(마지막 단계).
        """
        status, info = self.statusLabel, self.fileSizeLabel
        if row_width <= 0 or self._levelNeeds()[self._level] <= row_width:
            status.setMaximumWidth(_NO_MAX_WIDTH)
            info.setMaximumWidth(_NO_MAX_WIDTH)
            return
        spacing = self.resolutionLayout.spacing()
        icon = self.statusIcon.width() + spacing if texts.icon else 0
        info_width = (
            info.fontMetrics().horizontalAdvance(texts.size) + 4 + spacing if texts.size else 0
        )
        floor = self._statusFloor(texts.status)
        status.setMaximumWidth(max(row_width - icon - info_width, floor))
        info.setMaximumWidth(max(row_width - icon - floor - spacing, 0))

    def _statusFloor(self, status: str) -> int:
        """상태 글을 말줄임할 때 남기는 폭 — 앞 `STATUS_KEEP_CHARS`글자 + 「…」(프로토 가 「✕ 사유를…」).

        실패 사유 앞은 끝까지 줄여도 무엇이 실패했는지 몇 글자가 남는다. 글이 그보다 짧으면 글 전체다.
        """
        metrics = self.statusLabel.fontMetrics()
        if len(status) <= STATUS_KEEP_CHARS:
            return metrics.horizontalAdvance(status) + 4
        return metrics.horizontalAdvance(status[:STATUS_KEEP_CHARS] + "…") + 4

    def _rowWidth(self) -> int:
        """3줄의 폭 — 카드 폭에서 테두리 · 안쪽 여백(· 썸네일 · 썸네일 ↔ 글 간격)을 뺀 값.

        썸네일 줄이기면 3줄은 썸네일 밑까지 넓어진다(#554). 줄 위젯의 지금 폭이 아니라 카드 폭으로
        센다 — 카드의 resizeEvent 때는 안쪽 레이아웃이 아직 옛 자리라 줄 폭이 낡아 있다. 아직 표시
        전이면 0(배치 전)이다.
        """
        if not self.isVisible():
            return 0
        inset = self._frameInset()
        if not self.compactThumbnail:
            inset += self._thumbColumn()
        return max(self.width() - inset, 0)

    def _frameInset(self) -> int:
        """카드 폭 − 안쪽 폭 — 테두리 둘 + 안쪽 여백 둘."""
        margins = self.bodyLayout.contentsMargins()
        return 2 * self.contentFrame.frameWidth() + margins.left() + margins.right()

    def _thumbColumn(self) -> int:
        """썸네일 열 — 썸네일 폭 + 썸네일 ↔ 글 간격."""
        return self.thumbnailLabel.width() + self.bodyLayout.horizontalSpacing()

    def _infoTip(self, shown: str) -> str:
        """정보 글의 툴팁 — 접혀 줄었으면 전체 글, 구간 카드면 구간 설명 줄. 없으면 빈 글.

        보이는 글을 그대로 되풀이하는 툴팁은 달지 않는다(#550).
        """
        item = self.item
        full = self._levelTexts[0].size or "" if self._levelTexts else ""
        lines = [full] if full and shown != full else []
        if card_text.section_count(item):
            detail = card_text.section_overlap_detail(item)
            if not detail and not item.is_segment_based:
                # mp4 구간 카드는 정보 글의 길이가 받을 양(합친 범위)이다 — 구간 길이의 합을 알린다.
                # 겹침이 있으면 겹침의 설명이 같은 줄을 이미 담고 있다
                detail = [
                    self.tr("Sections add up to {0}").format(
                        card_text.section_clock(card_text.section_total(item))
                    )
                ]
            lines.extend(detail)
            if item.downloadState == DownloadState.WAITING:
                lines.extend(card_text.section_notice_detail(item))
        return "\n".join(lines)

    # ---- 날짜 연도 — 목록 전체가 함께 정한다 (#554) ----

    def showsYear(self) -> bool:
        """날짜에 연도를 적고 있는지."""
        return self._showYear

    def setShowYear(self, show: bool) -> None:
        """날짜의 연도를 적거나 뺀다 — 목록(`ContentListView`)이 모든 카드에 같은 값을 준다."""
        if show == self._showYear:
            return
        self._showYear = show
        self.dateLabel.setText(card_text.upload_date_text(self.item, show))

    def row1Need(self, with_year: bool) -> int:
        """1줄(채널 그림 · 채널명 · 날짜)이 바라는 폭 — 채널명은 다 보이는 폭(최대 150)으로 센다."""
        spacing = self.topLayout.spacing()
        name = min(self.channelNameLabel.sizeHint().width(), self.channelNameLabel.maximumWidth())
        date = card_text.upload_date_text(self.item, with_year)
        date_width = _label_text_width(self.dateLabel, date)
        return self.channelImageLabel.width() + spacing + name + spacing + spacing + date_width

    def row1Room(self) -> int:
        """1줄의 폭 — 카드 폭 − 테두리 · 안쪽 여백 − 썸네일 열."""
        return max(self.width() - self._frameInset() - self._thumbColumn(), 0)

    # ---- 가장 나쁜 카드 폭 — 창 최소폭 (#554) ----

    def foldedMinimumWidth(self) -> int:
        """이 카드를 다 접었을 때 들어가는 카드 폭 — 연도 없는 날짜 · 채널명 최소폭 · 3줄 마지막 단계.

        실패 카드의 사유 앞은 마지막 단계에서도 말줄임할 수 있어 상태 글을 최소폭으로 센다. 제목은
        말줄임한다(최소폭 작음).
        """
        spacing = self.topLayout.spacing()
        date = card_text.upload_date_text(self.item, False)
        row1 = (
            self.channelImageLabel.width()
            + spacing
            + self.channelNameLabel.minimumWidth()
            + spacing
            + spacing
            + _label_text_width(self.dateLabel, date)
        )
        row3 = self._levelNeeds()[-1]
        if self.item.downloadState == DownloadState.FAILED:
            texts = self._levelTexts[-1]
            spacing3 = self.resolutionLayout.spacing()
            icon = self.statusIcon.width() + spacing3 if texts.icon else 0
            info = (
                self.fileSizeLabel.fontMetrics().horizontalAdvance(texts.size) + 4 + spacing3
                if texts.size
                else 0
            )
            row3 = icon + self._statusFloor(texts.status) + info
        inset = self._frameInset()
        if self.compactThumbnail:
            return max(inset + self._thumbColumn() + row1, inset + row3)
        return inset + self._thumbColumn() + max(row1, row3)

    # ---- 진행 막대 · 배경 채움 · 색조 · 중지 모션 (#554) ----

    def _target(self) -> tuple[float, str, bool]:
        """(보일 진행 비율, 상태 어휘, 값 없이 움직이는 막대인지) — 지금 아이템으로 정한다.

        완료 · 실패는 0이다 — 막대 · 채움이 없고 카드 전체 색조가 상태를 알린다(#571).
        """
        item = self.item
        raw = item.downloadState
        state = self._cardState()
        busy = card_text.is_preparing(item) or (
            raw == DownloadState.RUNNING
            and bool(card_text.section_count(item))
            and bool(item.post_process)
            and bool(getattr(item, "cut_preparing", False))
        )
        if raw in (DownloadState.RUNNING, DownloadState.PAUSED):
            return card_text.progress_value(item) / 100, state, busy
        return 0.0, state, False

    def _showProgress(self, fraction: float, state: str, busy: bool, snap: bool = False) -> None:
        """막대 · 채움에 진행을 그린다 — 진행 표시 방식(설정 키)을 따른다.

        값 없이 움직이는 막대(준비 중 · 자르기 준비 중)는 배경 채움 방식에서도 막대로 보인다 — 채울
        값이 없어 채움으로는 아무것도 안 보인다(#554 리뷰).

        `snap`이면(모션 프레임) 그리는 값을 그 폭의 1px 단위로 묶는다 — 화소가 바뀌지 않는 프레임은
        값이 같아 다시 그리지 않는다(오너 2026-11-10, 0.01% 단위의 프레임마다 다시 그리기를 막음).
        폭은 그릴 때마다 지금 폭으로 센다. 멈춘 값(틱 · 모션 끝)은 묶지 않는다.
        """
        self._shown = fraction
        display = card_display.current()
        bar = self.progressBar
        if display.fill and not busy:
            bar.setVisible(False)
            token = {"running": "cardFillRunning", "paused": "cardFillPaused"}.get(state, "")
            frame = self.contentFrame
            drawn = _snapped(fraction, frame.width() - 2) if snap else fraction
            frame.setFill(drawn, token)
            return
        self.contentFrame.setFill(0.0, "")
        maximum = 0 if busy else PROGRESS_SCALE
        if bar.maximum() != maximum:
            bar.setRange(0, maximum)
        drawn = _snapped(fraction, bar.width()) if snap else fraction
        bar.setValue(round(drawn * PROGRESS_SCALE))
        visible = busy or fraction > 0 or state in ("running", "paused")
        bar.setVisible(visible)
        if bar.property("state") != state:
            bar.setProperty("state", state)
            theme.repolish(bar)

    def motion(self) -> QVariantAnimation | None:
        """지금 도는 진행 모션(중지 · 틱 사이 이어 움직임 · 완료 · 실패 · 색조 걷기) — 없으면 None.

        테스트가 시간을 정한다(`setCurrentTime`).
        """
        return self._motion

    def _stopMotion(self) -> None:
        """도는 모션을 멈추고 지운다 — 완료 · 실패 모션이 칠하던 색조 · 덧칠도 걷는다(#571)."""
        if self._motion is not None:
            self._motion.stop()
            self._motion.deleteLater()
            self._motion = None
        self._motionGoal = None
        self.contentFrame.setWash("", 0.0)
        self.contentFrame.setFillLook("", 0.0, 1.0)
        self.progressBar.setLook("", 0.0, 1.0)

    def _setTint(self, tint: str) -> None:
        """카드 면의 QSS 색조(`tint` 속성 — "finished" · "failed" · "")를 건다."""
        frame = self.contentFrame
        if frame.property("tint") != tint:
            frame.setProperty("tint", tint)
            theme.repolish(frame)
            # 상태 글의 「색조 위」 색은 카드 면의 속성을 보는 규칙이다 — 조상 속성이 바뀌어도 자식은 다시
            # 계산되지 않는다(Qt 스타일시트). 빠뜨리면 새 카드만 옛 색으로 남고 테마 전환 뒤에야 맞는다
            theme.repolish(self.statusLabel)
            self.hoverShade.update()

    def _startStepMotion(self, goal: DownloadState, duration: int, step) -> None:
        """시간(ms)을 0 → `duration`으로 흘리며 `step(ms)`를 부르는 모션을 돈다 — 끝나면 마지막 모양.

        완료 · 실패 · 색조 걷기가 쓴다(#571). 곡선은 `step`이 단계마다 정한다 — 모션 자체는 일정 속도다.
        """
        motion = QVariantAnimation(self)
        motion.setStartValue(0.0)
        motion.setEndValue(float(duration))
        motion.setDuration(duration)
        motion.valueChanged.connect(lambda value: step(float(value)))
        motion.finished.connect(self._finishStepMotion)
        self._motion, self._motionGoal = motion, goal
        motion.start()

    def _finishStepMotion(self) -> None:
        """완료 · 실패 · 색조 걷기 모션을 끝낸다 — 모션을 지우고 지금 아이템의 마지막 모양으로 바로 간다.

        모션이 끝났을 때와, 모션 도중 카드가 숨거나 선택 모드를 드나들 때 부른다(#571).
        """
        if self._motionGoal is None:
            return
        self._stopMotion()
        raw = self.item.downloadState
        self._setTint(self._cardState() if raw in TINT_STATES else "")
        self._showProgress(*self._target())

    def _startTintMotion(self, previous: DownloadState, finished: bool, fill: bool) -> None:
        """받는 중 · 일시정지 · 대기 → 완료 · 실패 모션을 돈다 (#571).

        완료: 막대 방식은 막대가 그려진 값에서 끝까지 차며 앞 상태 색 → 완료 색으로 바뀌고 같은 시간에 카드
        색조가 0에서 나타난다(`FINISH_MOTION_MS`). 그 뒤 막대가 `BAR_FADE_MS`에 흐려진다. 배경 채움
        방식은 채움이 끝까지 차며 완료 색조로 바뀐다 — 다 찬 채움이 곧 카드 전체 색조다. 채움이 없던 대기
        카드는 채울 색이 없어 카드 색조만 나타난다.

        실패: 끝까지 채우지 않는다. 막대 방식은 막대가 제자리에서 실패 색으로 바뀌며 사라지고, 배경 채움
        방식은 채움이 제자리에서 사라진다. 어느 방식이든 같은 시간에 카드 색조가 나타난다(`FAIL_MOTION_MS`).

        모션 동안 QSS 색조는 걸지 않고 면이 칠한다(`CardFace.setWash`). 끝나면 `_finishStepMotion`이
        QSS 색조를 걸고 막대 · 채움을 없앤다.
        """
        start = self._shown
        old_state = "paused" if previous == DownloadState.PAUSED else "running"
        if previous == DownloadState.WAITING:
            start, old_state = 0.0, "waiting"
        old_fill = {"running": "cardFillRunning", "paused": "cardFillPaused"}.get(old_state, "")
        name = "Finished" if finished else "Failed"
        tint, color = f"cardTint{name}", f"state{name}"
        frame, bar = self.contentFrame, self.progressBar
        self._setTint("")
        if fill:
            bar.setVisible(False)
            frame.setFill(start, old_fill)
        else:
            frame.setFill(0.0, "")
            if bar.maximum() != PROGRESS_SCALE:
                bar.setRange(0, PROGRESS_SCALE)
            bar.setValue(round(start * PROGRESS_SCALE))
            if bar.property("state") != old_state:
                bar.setProperty("state", old_state)
                theme.repolish(bar)
            bar.setVisible(finished or start > 0)
        main = FINISH_MOTION_MS if finished else FAIL_MOTION_MS
        fade = BAR_FADE_MS if finished and not fill else 0

        def step(ms: float) -> None:
            t = _MOTION_EASING.valueForProgress(min(1.0, ms / main))
            if fill and finished and old_fill:
                frame.setFill(_snapped(start + (1.0 - start) * t, frame.width() - 2), old_fill)
                frame.setFillLook(tint, t, 1.0)
                return
            frame.setWash(tint, t)
            if fill:
                frame.setFillLook("", 0.0, 1.0 - t)
            elif finished:
                bar.setValue(
                    round(_snapped(start + (1.0 - start) * t, bar.width()) * PROGRESS_SCALE)
                )
                gone = _MOTION_EASING.valueForProgress(max(0.0, ms - main) / fade)
                bar.setLook(color, t, 1.0 - gone)
            else:
                bar.setLook(color, t, 1.0 - t)

        self._startStepMotion(self.item.downloadState, main + fade, step)

    def _startClearMotion(self, tint: str) -> None:
        """실패(완료) → 대기 — 카드 색조를 `TINT_CLEAR_MS`에 걷는다 (#571, 프로토 FILL_CLEAR_MS)."""
        frame = self.contentFrame
        token = "cardTintFinished" if tint == "finished" else "cardTintFailed"
        frame.setWash(token, 1.0)

        def step(ms: float) -> None:
            frame.setWash(token, 1.0 - _MOTION_EASING.valueForProgress(ms / TINT_CLEAR_MS))

        self._startStepMotion(self.item.downloadState, TINT_CLEAR_MS, step)

    def _animate(self, start: float, end: float, state: str, duration: int, easing) -> None:
        """진행 비율을 `start` → `end`로 움직인다 — 색(상태)은 그동안 `state` 그대로다."""
        self._stopMotion()
        motion = QVariantAnimation(self)
        motion.setStartValue(float(start))
        motion.setEndValue(float(end))
        motion.setDuration(duration)
        motion.setEasingCurve(easing)
        motion.valueChanged.connect(
            lambda value: self._showProgress(float(value), state, False, snap=True)
        )
        motion.finished.connect(self._onMotionFinished)
        self._motion = motion
        motion.start()

    def _onMotionFinished(self) -> None:
        """모션이 끝났다 — 지금 아이템의 진행으로 맞춘다(중지 모션이면 대기 — 막대 · 채움 없음)."""
        motion, self._motion = self._motion, None
        if motion is not None:
            motion.deleteLater()  # 끝난 모션이 카드의 자식으로 쌓이지 않게(이어 움직임은 틱마다 하나)
        self._showProgress(*self._target())

    def _applyProgress(self, previous: object) -> None:
        """상태가 바뀐 뒤 막대 · 채움 · 색조를 맞춘다 — 중지면 줄어드는 모션을 돈다(설계 §12.1 중지 모션).

        중지 확인 뒤 대기로 돌아가는 카드(받는 중 · 일시정지)는 막대 · 채움이 지금 자리에서 왼쪽 끝까지
        `STOP_MOTION_MS`에 줄어든다(처음 빠르고 끝에서 느림, 그 카드의 지금 색). 애니메이션을 끄면 바로
        사라진다. 모션 도중 다시 시작하면 모션을 멈추고 새 진행을 따른다. 받는 중 진행은 막대 · 배경
        채움 어느 방식이든 틱 사이를 `TICK_MOTION_MS`에 이어 움직인다(애니메이션 켬일 때).

        완료 · 실패가 되면 완료 · 실패 모션을 돌고(`_startTintMotion`), 실패 · 완료에서 다른 상태가 되면
        색조를 걷는 모션을 돈다(`_startClearMotion`) — 애니메이션 켬이고 카드가 보일 때만(#571). 그 밖
        (처음 뜬 카드 · 되살린 카드 · 애니메이션 끔)은 막대 없이 색조만 바로 건다. 이 모션 도중 상태가
        또 바뀌면 모션을 멈추고 새 상태의 마지막 모양으로 바로 간다(새 모션을 돌지 않는다).
        """
        display = card_display.current()
        raw = self.item.downloadState
        fraction, state, busy = self._target()
        if self._motionGoal is not None:
            if raw == self._motionGoal:
                return  # 완료 · 실패 · 색조 걷기 모션 도중의 다시 그리기 — 모션을 둔다. 끝에서 맞춘다
            # 모션 도중 상태가 또 바뀌었다(실패 직후 다시 시도 등) — 모션을 멈추고 마지막 상태의 모양으로
            self._stopMotion()
            self._setTint(state if raw in TINT_STATES else "")
            self._showProgress(fraction, state, busy)
            return
        moving = display.animations and self.isVisible() and previous != raw
        if moving and raw in TINT_STATES and previous in TINT_MOTION_FROM:
            self._stopMotion()
            self._startTintMotion(previous, raw == DownloadState.FINISHED, display.fill)
            return
        if moving and previous in TINT_STATES and raw not in TINT_STATES:
            self._stopMotion()
            self._setTint("")
            self._showProgress(fraction, state, busy)
            self._startClearMotion("finished" if previous == DownloadState.FINISHED else "failed")
            return
        self._setTint(state if raw in TINT_STATES else "")
        stopped = previous in BUSY_STATES and raw == DownloadState.WAITING and self._shown > 0
        if stopped and display.animations:
            old_state = "paused" if previous == DownloadState.PAUSED else "running"
            self._animate(self._shown, 0.0, old_state, STOP_MOTION_MS, QEasingCurve.Type.OutCubic)
            return
        motion = self._motion
        if (
            motion is not None
            and not busy
            and previous == raw
            and float(motion.endValue()) == fraction
        ):
            # 상태가 그대로이고 같은 목표로 가는 모션은 그대로 둔다 — 중지 모션 도중 늦게 온 대기 갱신(#554
            # 리뷰), 틱 안의 다른 갱신이 모션을 끊거나 처음부터 다시 돌리지 않게. 끝에서 `_onMotionFinished`가
            # 맞춘다. 상태가 바뀌면(일시정지 등) 모션이 쥔 옛 색이 남지 않게 아래 길로 간다
            return
        if (
            display.animations
            and raw == DownloadState.RUNNING
            and not busy
            and previous == DownloadState.RUNNING
            and fraction > self._shown
        ):
            self._animate(self._shown, fraction, state, TICK_MOTION_MS, QEasingCurve.Type.Linear)
            return
        if (
            not display.animations
            and raw == DownloadState.RUNNING
            and not busy
            and previous == DownloadState.RUNNING
        ):
            # 애니메이션 끔 — 받는 중 막대는 `STILL_STEP_MS`에 한 번만 바꾼다. 그 사이에 온 값은 미뤄 두고
            # 간격이 차면 그때의 마지막 값으로 바꾼다
            wait = STILL_STEP_MS - (_now_ms() - self._stillAt)
            if wait > 0:
                if not self._stillPending:
                    self._stillPending = True
                    QTimer.singleShot(max(1, math.ceil(wait)), self, self._onStillStep)
                return
            self._stillAt = _now_ms()
        self._stopMotion()
        self._showProgress(fraction, state, busy)

    def _onStillStep(self) -> None:
        """애니메이션 끔에서 미룬 막대 바꾸기 — 지금 아이템의 진행으로 맞춘다."""
        self._stillPending = False
        self._stillAt = _now_ms()
        self._showProgress(*self._target())

    def applyStateStyle(self) -> None:
        """상태에 따라 3줄 글 · 카드 버튼 · 색 · 진행 표시를 맞춘다 (#227 → #245 → #550 → #554).

        색은 theme.py 한 곳에서만 정의된다. 상태 글 · 진행바 · 카드 면은 위젯별 `setStyleSheet` 없이
        동적 속성(`state` · `tint`)만 바꾸고, 색은 전역 QSS 규칙이 고른다 — 속성만 바꾸면 이미 계산된
        스타일이 갱신되지 않으므로 theme.repolish()가 함께 필요하다.

        진행 표시(#554): 막대 방식은 받는 중 · 일시정지가 진행만큼이다. 배경 채움 방식은 받는 중 · 일시정지를
        카드 면에 칠한다. 어느 방식이든 완료 · 실패는 막대 · 채움 없이 카드 전체 색조다(#571). 준비 중 ·
        자르기 준비 중은 값 없이 움직이는 막대다.

        카드 버튼은 `CARD_BUTTONS`가 상태마다 정한다. 줄 자리 · 카드 높이는 상태와 무관하다
        (목록이 들썩이면 안 된다). 일시정지의 조작은 **이어 받기**다 — 버튼 하나(pauseButton)가
        도형·툴팁만 pause↔resume으로 바꾼다(시그널 경로는 같은 토글).
        """
        state = self._cardState()
        raw = self.item.downloadState
        previous, self._previousState = self._previousState, raw
        if self.statusLabel.property("state") != state:
            self.statusLabel.setProperty("state", state)
            theme.repolish(self.statusLabel)
        self._applyProgress(previous)

        if raw == DownloadState.PAUSED:
            self.pauseButton.setIconName("resume")
            self.pauseButton.setToolTip(self.tr("Resume"))
        else:
            self.pauseButton.setIconName("pause")
            self.pauseButton.setToolTip(self.tr("Pause"))
        shown = CARD_BUTTONS.get(raw, ("deleteButton",))
        for name in CARD_BUTTON_NAMES:
            getattr(self, name).setVisible(name in shown)
        self._applyTexts()
        if self.hoverBox.isVisibleTo(self):
            self._placeCardButtons()  # 버튼 수가 바뀌면 상자 폭이 바뀐다

    def _cardState(self) -> str:
        """카드 상태 어휘(theme.CARD_STATES) — `card_text.card_state`."""
        return card_text.card_state(self.item)

    # ---- 조작 ----

    def requestEdit(self) -> None:
        """카드 편집 창을 청한다 — 대기 카드에서만 (#545). ✎ · 더블 클릭 · Enter가 부른다."""
        if self.item.downloadState != DownloadState.WAITING:
            return
        self.editRequest.emit()

    def activate(self) -> None:
        """더블 클릭 · Enter의 일 — 대기 카드는 편집 창, 완료 카드는 폴더 열기 (#545 · #550).

        그 밖 상태는 아무 일도 없다.
        """
        state = self.item.downloadState
        if state == DownloadState.WAITING:
            self.requestEdit()
        elif state == DownloadState.FINISHED:
            self.requestOpenDir()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        """카드를 더블 클릭하면 `activate`한다. 버튼은 더블 클릭을 스스로 받아 여기 오지 않는다.

        선택 모드에서는 아무것도 열지 않는다(#561) — 두 번째 누름으로 보고 뗌을 기다린다(빠르게 두 번 누르면
        골랐다가 푼다). Qt는 더블 클릭의 두 번째 누름을 누름이 아니라 더블 클릭으로만 보낸다.
        """
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()
            owner = self._listOwner()
            if self._picking and owner is not None:
                if self._armedBy == "key":
                    return  # Space를 누른 채다 — 먼저 시작한 Space만 따른다(누름과 같은 규칙)
                self._arm(owner.cardPressed(self, event.modifiers()), event.modifiers())
                return
            if not self._picking:
                self.activate()
            return
        super().mouseDoubleClickEvent(event)

    def requestDelete(self) -> None:
        """✕ — 지우기를 청한다. 받는 중 카드면 목록이 「멈추고 지우기」를 먼저 묻는다(#550)."""
        self.deleteRequest.emit()

    def _openTarget(self) -> str:
        """폴더 열기가 열 경로 — `card_text.open_target`."""
        return card_text.open_target(self.item)

    def _missingOutput(self) -> str:
        """완료 카드의 받은 파일이 없으면 경고 글, 있으면 빈 글 (#550 — 설계 §5 폴더 열기).

        - 구간 카드: 구간 파일이 **하나도** 없을 때만 경고한다. 일부만 없으면 남은 파일을 연다
          (`card_text.open_target`이 남은 것 가운데 번호가 가장 작은 것을 고른다)
        - 그 밖의 완료 카드: 산출물 파일이 있는지 본다
        완료가 아닌 카드는 늘 빈 글이다(저장 폴더를 연다).
        """
        item = self.item
        if item.downloadState != DownloadState.FINISHED:
            return ""
        if card_text.section_count(item):
            paths = getattr(item, "section_paths", ())
            if not any(os.path.isfile(path) for path in paths):
                return self.tr("The downloaded section files are not there anymore.")
            return ""
        if not os.path.isfile(item.output_path or ""):
            return self.tr("The downloaded file is not there anymore.")
        return ""

    def requestOpenDir(self) -> None:
        """받은 파일을 고른 채 폴더를 연다 — 파일이 없으면 경고만 한다 (#550 — 설계 §5)."""
        missing = self._missingOutput()
        if missing:
            # 경고 창의 글은 화면 언어로 낸다 (#431)
            QMessageBox.warning(self, self.tr("Warning"), missing)
            return
        try:
            # 라벨 텍스트는 줄인 꼴이다 — 실제 경로는 아이템에서 읽는다(#245)
            path = self._openTarget()
            if os.path.isfile(path):
                nativePath = QDir.toNativeSeparators(path)
                success = False

                if platform.system() == "Windows":
                    success = QProcess.startDetached("explorer.exe", ["/select,", nativePath])
                elif platform.system() == "Darwin":
                    success = QProcess.startDetached("open", ["-R", nativePath])
                elif platform.system() == "Linux":
                    success = QProcess.startDetached("nautilus", [nativePath])

                if not success:
                    raise OSError(self.tr("Could not find '{0}'.").format(path))
            else:
                url = QUrl.fromLocalFile(path)
                if not QDesktopServices.openUrl(url):  # openUrl이 False를 반환하면 실패
                    raise OSError(self.tr("Could not open '{0}'.").format(path))
        except Exception as e:
            # 경고 창의 글은 화면 언어로 낸다 (#431)
            QMessageBox.warning(self, self.tr("Warning"), str(e))
            return

    def setSize(self, size: int) -> str:
        """바이트 수를 「595.34 MB」 꼴로 적는다 — `card_text.size_text`."""
        return card_text.size_text(size)
