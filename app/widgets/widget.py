"""목록의 카드 위젯(ContentItemWidget)과 그 부속 함수를 정의한다."""

import logging
import os
import platform
import re
import threading

from PySide6.QtCore import QDir, QEvent, QProcess, QRectF, QSize, Qt, QUrl, Signal
from PySide6.QtGui import (
    QDesktopServices,
    QEnterEvent,
    QFocusEvent,
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
from app.viewmodels import card_text
from app.viewmodels.card_text import (  # noqa: F401 — 바깥(footer_bar · 테스트)이 이 자리에서 import한다
    STATE_ICON,
    short_time,
)
from app.viewmodels.data import ContentItem
from app.widgets.contentItemWidget import HoverShade, Ui_ContentItemWidget
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

# ✕를 누르면 「멈추고 지우기」를 묻는 상태 — 엔진이 그 카드를 쥐고 있다(#550)
BUSY_STATES = (DownloadState.RUNNING, DownloadState.PAUSED)

# 키보드 초점으로 보지 않는 까닭 — 카드는 마우스로 초점을 받지 않지만(TabFocus) 혹시 오면 테두리를 안 그린다
_POINTER_REASONS = (Qt.FocusReason.MouseFocusReason,)


def last_folder_name(path: str) -> str:
    """경로의 마지막 폴더 이름 — 카드 3줄 저장 위치 글(#550). 운영체제와 무관하게 자른다."""
    if not path:
        return ""
    root, segments, _ = _split_path(path)
    return segments[-1] if segments else (root or path)


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

    # 워커 스레드 → 메인 스레드 중계 (#168). 위젯·아이템 조작은 반드시 메인
    # 스레드 슬롯에서 한다 — download 경로가 DownloadViewModel로 세운 스레드 경계
    # 규칙을 content 경로에도 적용한다
    _repSizeFetched = Signal(int, str)  # (해상도 index, 크기 텍스트 — 세그먼트 기반이면 "")
    _imageFetched = Signal(object, object, str, int, str)  # (label, bytes, url, maxHeight, type)

    def __init__(self, item: ContentItem, index: int = 0, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.item = item  # ContentItem 저장
        self.index = index  # 인덱스 저장
        # 사용자가 해상도를 고른 적이 있는가(편집 창) — 늦게 온 크기 조회가 이를 덮지 않는다
        self._userPicked = False
        self._pickedIndex = 0  # 고른 해상도의 목록 번호(내림차순 정렬 뒤)
        self._hovered = False
        self._keyFocus = False
        self._tipsInstalled = False  # 말풍선 필터를 걸었는지 — 처음 호버할 때 건다
        self._infoVariants: list[str] = []  # 정보 글 — 긴 것부터, 폭이 모자라면 뒤의 것을 쓴다
        self.setupUi(self)
        # 카드 하나가 Tab 한 번이다. 마우스로는 초점을 받지 않는다 — 초점 테두리는 키보드 초점만
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
        self._fitInfo()

    def showEvent(self, event: QShowEvent) -> None:
        """첫 표시 직후 3줄 폭 맞추기를 한 번 더 돌린다 (#245).

        첫 표시에서 resizeEvent는 자식 contentFrame의 레이아웃이 활성화되기 **전에** 도착한다 —
        Qt의 show_helper가 대기 중이던 Resize를 자식 표시 전에 보내고, showEvent는 자식 표시
        **후에** 보낸다. 그때 3줄 폭이 0이라 맞추기가 "배치 전"으로 끝나면, 이후 크기가 안 바뀌는
        한 다시 돌지 않는다. showEvent 시점엔 자식 레이아웃이 잡혀 있다.
        """
        super().showEvent(event)
        self._fitInfo()

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
        """키보드 초점이 이 카드에 있는지 — 초점 테두리 · 카드 버튼을 보이는 때."""
        return self._keyFocus

    def _setKeyFocus(self, on: bool) -> None:
        if on == self._keyFocus:
            return
        self._keyFocus = on
        self.contentFrame.setProperty("keyFocus", on)
        theme.repolish(self.contentFrame)
        self._syncCardButtons()

    def focusInEvent(self, event: QFocusEvent) -> None:
        """키보드 초점이 들어왔다 — 목록 밖에서 Tab으로 들어오면 목록이 보이는 차례의 첫(끝) 카드로 옮긴다."""
        super().focusInEvent(event)
        self._setKeyFocus(event.reason() not in _POINTER_REASONS)
        owner = self._listOwner()
        if owner is not None:
            owner.cardFocusEntered(self, event.reason())

    def focusOutEvent(self, event: QFocusEvent) -> None:
        super().focusOutEvent(event)
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
        """Enter는 더블 클릭과 같은 일을 한다(대기 = 편집 창, 완료 = 폴더 열기)."""
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            event.accept()
            self.activate()
            return
        super().keyPressEvent(event)

    def _syncCardButtons(self) -> None:
        """카드 버튼 상자 · 덮개를 호버 · 키보드 초점에 맞춰 보이고 숨긴다."""
        shown = self._hovered or self._keyFocus
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
        self.dateLabel.setText(card_text.upload_date_text(self.item))
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
        """해상도 하나의 파일 크기를 워커 스레드에서 조회한다 — 결과는 `_onRepSizeFetched`로."""

        def fetch() -> None:
            # 네트워크만 담당한다 — 위젯·아이템 반영은 _onRepSizeFetched(메인 스레드)로 (#168)
            size_text = ""
            if not self.item.is_segment_based:
                try:
                    resp = get_thread_session().head(base_url, timeout=REQUEST_TIMEOUT)
                    resp.raise_for_status()
                    size = int(resp.headers.get("content-length", 0))
                    if size == 0:
                        resp = get_thread_session().get(
                            base_url, stream=True, timeout=REQUEST_TIMEOUT
                        )
                        resp.raise_for_status()
                        size = int(resp.headers.get("content-length", 0))
                        resp.close()
                    size_text = self.setSize(size)
                except Exception:
                    # 기존 동작 유지: 조회 실패는 조용히 "Checking..."으로 남는다
                    return
            try:
                self._repSizeFetched.emit(index, size_text)
            except RuntimeError:
                pass  # 위젯이 이미 파괴된 뒤의 늦은 완료

        threading.Thread(target=fetch, daemon=True).start()

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
        thread = threading.Thread(
            target=self.fetchImage, args=(label, url, maxHeight, type), daemon=True
        )
        thread.start()

    def fetchImage(self, label: QLabel, url: str, maxHeight: int, type: str) -> None:
        """이미지 바이트를 받아 메인 스레드로 중계한다 — 워커 스레드 (#168).

        QPixmap 생성·스케일·setPixmap은 GUI 스레드 전용이라 여기서 하지
        않는다. 디코드 이후는 _onImageFetched(메인 스레드)가 담당한다.
        """
        try:
            response = get_thread_session().get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            self._imageFetched.emit(label, response.content, url, maxHeight, type)
        except RuntimeError:
            pass  # 위젯이 이미 파괴된 뒤의 늦은 완료
        except Exception as e:
            logger.error(f"Error loading image from {url}: {e}")

    def _onImageFetched(
        self, label: QLabel, data: bytes, url: str, maxHeight: int, type: str
    ) -> None:
        """이미지 디코드·스케일·표시 — 메인 스레드 (#168)."""
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
        """3줄의 상태 아이콘 · 상태 글 · 정보 글을 아이템에 맞춘다.

        정보 글이 None(실패)이면 정보 칸을 숨긴다 — 사유가 그 자리를 쓴다. 정보 글은 폭이
        모자라면 짧은 꼴로 바꾼다(`_fitInfo`).
        """
        item = self.item
        texts = card_text.slot_texts(item)
        self.statusIcon.setState(_STATUS_BUBBLE.get(texts.icon, ""))
        self.statusLabel.setText(texts.status)
        self.statusLabel.setDetailTip(texts.status_tip or "")
        self.fileSizeLabel.setVisible(item.downloadState != DownloadState.FAILED)
        if texts.size is None:
            self._infoVariants = []
            return
        variants = [texts.size]
        state = item.downloadState
        if state == DownloadState.WAITING and card_text.section_count(item):
            variants.append(card_text.info_text(item, with_notice=False))
        if state in (DownloadState.RUNNING, DownloadState.PAUSED):
            variants.append(card_text.info_text(item, with_total=False))
        self._infoVariants = [v for k, v in enumerate(variants) if v not in variants[:k]]
        self._fitInfo()

    def _fitInfo(self) -> None:
        """3줄 정보 글을 폭에 맞춰 고르고, 줄어드는 차례대로 최대폭을 씌운다 (#309 · #550).

        폭이 줄면 ① 저장 위치 글 → ② 상태 글(「…」까지) → ③ 정보 글 차례로 줄어든다. 가로 넘침은 어떤
        폭에서도 만들지 않는다 — 정보 글은 ③에서만 말줄임하고 툴팁에 전체 글을 단다(접는 차례는 C3).
        긴 꼴(구간 알림 · 「/ 전체」 포함)이 상태 글 · 저장 위치와 함께 한 줄에 안 들어가면 짧은 꼴을
        쓴다.

        최소폭이 아니라 최대폭을 씌운다 — 최소폭은 카드 최소폭이 되어 카드가 목록보다 넓은 채
        굳는다(줄 폭이 카드 폭에서 나오므로 되먹임). 판정은 지금 표시 중인 글이 아니라 "줄 폭 − 다른
        항목"으로만 한다. 표시 전(줄 폭 0)이면 씌운 최대폭을 풀고 showEvent · resizeEvent에서 다시 온다.

        정보 글이 없는 카드(실패 — 사유가 그 자리를 쓴다)도 최대폭은 다시 씌운다 — 건너뛰면 앞 상태의
        좁은 폭에서 씌운 최대폭이 남아 저장 위치가 0폭으로 굳는다(#550).
        """
        label = self.fileSizeLabel
        spacing = self.resolutionLayout.spacing()
        row_width = self._rowWidth()
        path_shown = self._pathShown()
        icon_need = 0 if self.statusIcon.isHidden() else self.statusIcon.width() + spacing
        status_need = icon_need + self.statusLabel.sizeHint().width() + spacing
        # 저장 위치 아이콘 + 그 뒤 두 간격(아이콘 ↔ 글, 글 ↔ 정보 글) — 저장 위치 글이 0폭이어도 남는다
        path_need = self.pathIcon.width() + 2 * spacing if path_shown else 0
        path_text_min = (
            self.directoryLabel.fontMetrics().horizontalAdvance("abcdef") if path_shown else 0
        )
        status_floor = self.statusLabel.minimumSizeHint().width()
        info_width = 0
        clipped = False
        text = ""
        if self._infoVariants:
            metrics = label.fontMetrics()
            text = self._infoVariants[0]
            if row_width > 0:
                text = next(
                    (
                        v
                        for v in self._infoVariants
                        if metrics.horizontalAdvance(v)
                        + 4
                        + status_need
                        + path_need
                        + path_text_min
                        <= row_width
                    ),
                    self._infoVariants[-1],
                )
            if label.text() != text:
                label.setText(text)
            info_width = metrics.horizontalAdvance(text) + 4
            clipped = (
                row_width > 0
                and info_width > row_width - icon_need - status_floor - spacing - path_need
            )
            label.setDetailTip(self._infoTip(text, clipped))
        if row_width <= 0:
            self.statusLabel.setMaximumWidth(_NO_MAX_WIDTH)
            self.directoryLabel.setMaximumWidth(_NO_MAX_WIDTH)
            return
        # ② 상태 글 — 정보 글 · 아이콘들을 뺀 폭까지, 「…」 아래로는 줄지 않는다(그 뒤는 ③ 정보 글이 줄어든다)
        status_room = row_width - icon_need - spacing - path_need - info_width
        self.statusLabel.setMaximumWidth(max(status_room, status_floor))
        # ① 저장 위치 — 상태 글이 온전한 채 남는 폭만 준다(안 씌우면 Qt가 같은 정책의 상태 글과 나눠
        # 줄여 상태 글이 잘린다, #245 560px 실기)
        if path_shown:
            room = row_width - status_need - path_need - info_width
            self.directoryLabel.setMaximumWidth(max(room, 0))

    def _rowWidth(self) -> int:
        """세 줄의 폭 — 카드 폭에서 테두리 · 안쪽 여백 · 썸네일 · 썸네일 ↔ 글 간격을 뺀 값.

        줄 위젯의 지금 폭이 아니라 카드 폭으로 센다 — 카드의 resizeEvent 때는 안쪽 레이아웃이 아직
        옛 자리라 줄 폭이 낡아 있다. 아직 표시 전이면 0(배치 전)이다.
        """
        if not self.isVisible():
            return 0
        border = self.contentFrame.frameWidth()
        margins = self.bodyLayout.contentsMargins()
        inset = 2 * border + margins.left() + margins.right()
        inset += self.thumbnailLabel.width() + self.bodyLayout.spacing()
        return max(self.width() - inset, 0)

    def _infoTip(self, shown: str, clipped: bool = False) -> str:
        """정보 글의 툴팁 — 줄여 보였으면(짧은 꼴 · 말줄임) 전체 글, 구간 카드면 구간 설명 줄. 없으면 빈 글.

        보이는 글을 그대로 되풀이하는 툴팁은 달지 않는다(#550).
        """
        item = self.item
        full = self._infoVariants[0]
        lines = [full] if clipped or shown != full else []
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

    def applyStateStyle(self) -> None:
        """상태에 따라 3줄 글 · 카드 버튼 · 색 · 진행바를 맞춘다 (#227 → #245 → #550).

        색은 theme.py 한 곳에서만 정의된다. 상태 글·진행바는 위젯별 `setStyleSheet` 없이
        동적 속성(`state`)만 바꾸고, 색은 전역 QSS의 `[state="..."]` 규칙이 고른다 — 속성만
        바꾸면 이미 계산된 스타일이 갱신되지 않으므로 theme.repolish()가 함께 필요하다.

        카드 버튼은 `CARD_BUTTONS`가 상태마다 정한다. 줄 자리 · 카드 높이는 상태와 무관하다
        (목록이 들썩이면 안 된다). 일시정지의 조작은 **이어 받기**다 — 버튼 하나(pauseButton)가
        도형·툴팁만 pause↔resume으로 바꾼다(시그널 경로는 같은 토글).
        """
        state = self._cardState()
        raw = self.item.downloadState
        # 준비 중에는 막대를 값 없는(움직이는) 상태로 둔다 — 범위 0~0이 그 상태다
        busy_maximum = 0 if card_text.is_preparing(self.item) else 100
        if self.progressBar.maximum() != busy_maximum:
            self.progressBar.setRange(0, busy_maximum)
        self.progressBar.setValue(card_text.progress_value(self.item))
        self.progressBar.setVisible(card_text.has_progress(self.item))
        if self.progressBar.property("state") != state:
            self.progressBar.setProperty("state", state)
            theme.repolish(self.progressBar)
        if self.statusLabel.property("state") != state:
            self.statusLabel.setProperty("state", state)
            theme.repolish(self.statusLabel)

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
        """카드를 더블 클릭하면 `activate`한다. 버튼은 더블 클릭을 스스로 받아 여기 오지 않는다."""
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()
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
