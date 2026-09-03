import os
import re
import threading
from PySide6.QtWidgets import QWidget, QPushButton, QMessageBox, QFileDialog, QHBoxLayout, QSizePolicy
from PySide6.QtGui import QPainter, QPainterPath, QPixmap, QDesktopServices, QRegion
from PySide6.QtCore import Qt, Signal, QUrl, QDir, QProcess, QRectF
from content.data import ContentItem
from content.network import REQUEST_TIMEOUT, get_thread_session
from content.pill import ResolutionPill
from core.models.download_state import DownloadState
from app.viewmodels.item_state import ItemState
from ui.contentItemWidget import Ui_ContentItemWidget
import theme
from time import strftime, gmtime
import platform
import logging

logger = logging.getLogger(__name__)

# 3행 슬롯 텍스트의 상태 접두(완료/실패 표시). 텍스트 안에 들어가는 글자라
# 폰트 문자를 쓴다 — 둘 다 기본 문장부호 블록(Dingbats)의 흔한 글리프다.
# 색은 파이썬이 아니라 전역 QSS `#statusLabel[state=...]`가 theme.py
# 토큰으로 정한다.
# 1행 우측의 조작·삭제 아이콘은 문자가 아니라 content/icons.py가 그리는
# 도형이다(#245 — ‖(U+2016)은 문장부호라 일시정지로 안 읽히고, 글리프
# 모양은 macOS·Linux 실기 없이는 확인할 길이 없었다).
STATE_ICON = {
    "finished": "✓",  # 체크 — 완료 (U+2713)
    "failed": "✕",    # 엑스 — 실패 (U+2715)
}

# 전역 설정의 다운로드 경로 — 카드는 자기 경로가 이 값과 **다를 때만**
# 3행에 경로를 표시한다(#245 — 같은 값을 카드마다 반복 표시하는 것이
# 정보 과다의 큰 몫이었다. 다르다는 것 자체가 정보다).
# application/mainWindow.py가 시작 시·경로 변경 시 밀어 넣는다.
# 모듈 전역을 호출 시점에 조회하므로 테스트에서 monkeypatch 가능하다.
_global_download_path = ""

#: Qt의 QWIDGETSIZE_MAX(PySide6가 노출하지 않음) — 최대폭 제한을 푸는 값.
_NO_MAX_WIDTH = (1 << 24) - 1


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
    home_norm = os.path.normpath(home if home is not None else os.path.expanduser("~")).replace("\\", "/")
    def _same(a: str, b: str) -> bool:
        return os.path.normcase(a) == os.path.normcase(b)
    if _same(norm, home_norm):
        return "~", [], True
    if _same(norm[: len(home_norm) + 1], home_norm + "/"):
        root, rest = "~", norm[len(home_norm) + 1:]
    else:
        # 드라이브 판정은 os.path.splitdrive가 아니라 직접 한다 — POSIX 파이썬은
        # "D:"를 드라이브로 보지 않아(splitdrive가 빈 문자열 반환) 같은 경로
        # 문자열이 OS마다 다르게 축약된다(CI 3-OS에서 실측). 이 함수는 표시
        # 전용이고 "OS 무관 렌더"가 계약이므로 판정도 OS 무관이어야 한다.
        drive_match = re.match(r"[A-Za-z]:(?=/|$)", norm)
        if drive_match:
            root, rest = drive_match.group(0), norm[drive_match.end():].lstrip("/")
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



class ContentItemWidget(QWidget, Ui_ContentItemWidget):
    """컨텐츠 정보를 표시하는 커스텀 위젯"""

    textChanged = Signal(str)
    deleteRequest = Signal()
    pauseRequest = Signal()   # 진행 카드의 ⏸ (#245 상태별 조작)
    retryRequest = Signal()   # 실패 카드의 ↻ (#245 상태별 조작)
    expandedChanged = Signal(bool)  # 해상도 펼침/접힘 — 목록이 "한 번에 하나"를 맞춘다

    # 워커 스레드 → 메인 스레드 중계 (#168). 위젯·아이템 조작은 반드시 메인
    # 스레드 슬롯에서 한다 — download 경로가 qt_bridge로 세운 스레드 경계
    # 규칙을 content 경로에도 적용한다
    _repSizeFetched = Signal(int, str)  # (해상도 index, 크기 텍스트 — 세그먼트 기반이면 "")
    _imageFetched = Signal(object, object, str, int, str)  # (label, bytes, url, maxHeight, type)

    def __init__(self, item: ContentItem, index=0, parent=None):
        super().__init__(parent)
        self.item = item  # ContentItem 저장
        self.index = index  # 인덱스 저장
        self.isEditing = False
        # 해상도 pill 모드(#244 3행 정리) — setupUi 전에 있어야 applyStateStyle이 읽는다.
        # 모드는 _layoutRowThree가 폭을 보고 정한다: "all"(전부, 들어갈 때) /
        # "collapsed"(선택 하나 [▾], 안 들어갈 때) / "expanded"(접힌 것을 유저가 펼침) /
        # "hidden"(대기 아님). _expanded는 유저 조작 기억이고 모드는 폭이 정한다.
        self._expanded = False
        self._pillMode = "all"
        self._layingOutRowThree = False
        self._selectedButton: ResolutionPill | None = None
        self._pillRows: list[QHBoxLayout] = []  # 펼쳐서 넘친 pill을 받는 추가 행(0개가 기본)
        self._pillRowOf: dict[int, int] = {}    # id(pill) → 지금 놓인 행 번호(0 = 3행)
        self._rowStrut: QWidget | None = None       # 펼친 동안 3행 세로 정책을 지키는 0폭 버팀목
        self.buttons: list[ResolutionPill] = []
        self.setupUi(self)
        self._sizeThumbnail()  # 컨텐츠 열 높이가 정해진 뒤, 이미지 로드 전에
        self._setupThreadRelays()  # setupDynamicUi가 스레드를 띄우기 전에 연결돼야 한다 (#168)
        self.setupDynamicUi()
        self.setupSignals()  # 시그널 연결

    def _setupThreadRelays(self):
        self._repSizeFetched.connect(self._onRepSizeFetched)
        self._imageFetched.connect(self._onImageFetched)

    def _sizeThumbnail(self) -> None:
        """썸네일을 우측 컨텐츠 열의 실제 높이에 맞춰 16:9로 고정한다.

        "원하는 카드 높이에서 16:9로 폭이 나온다"(#244 확정 설계) — 카드
        높이는 우측 3행(글자 크기·행 간격 토큰)이 정하고, 썸네일은 그
        높이를 가득 채우도록 따라간다. theme.py 토큰을 바꾸면 썸네일도
        자동으로 맞춰진다.

        `ensurePolished()`가 먼저다 — 전역 QSS의 위계 폰트(@fontSizeTitle
        등)는 polish 시점에 위젯 폰트로 병합되는데, 그 전에 sizeHint를
        읽으면 기본 폰트 기준의 틀린 높이가 나온다(실측 확인).
        """
        if self._expanded:
            # 펼쳐서 늘어난 높이는 잠깐이다 — 썸네일은 접힌 높이를 유지한다
            return
        for label in (self.channelNameLabel, self.titleLabel, self.statusLabel,
                      self.fileSizeLabel, self.directoryLabel):
            label.ensurePolished()
        height = self.contentLayout.sizeHint().height()
        self.thumbnailLabel.setFixedSize(round(height * 16 / 9), height)

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
        # 눌려 카드 반지름(12px)을 못 따라가고, 그 결과 막대 양끝이 카드의
        # 둥근 모서리 바깥으로 튀어나온다(실기 렌더 실측 — 바닥 모서리에
        # 트랙·진행색 조각이 카드 몸통 밖에 남는다). 프레임 테두리 안쪽의
        # 둥근 사각형을 바 좌표계로 옮겨 마스크로 건다. 값(QRegion)이라
        # 카드에 상주하는 파이썬 객체가 아니다.
        radius = theme.METRICS["cardRadius"] - 1
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 1 - bar_y, frame.width() - 2, frame.height() - 2), radius, radius)
        self.progressBar.setMask(QRegion(path.toFillPolygon().toPolygon(), Qt.FillRule.WindingFill))
        self.progressBar.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._placeProgressBar()
        self._layoutRowThree()  # 폭이 바뀌면 pill 모드·줄바꿈·경로 모양이 바뀐다

    def showEvent(self, event):
        """첫 표시 직후 3행 판정을 한 번 더 돌린다 (#245).

        첫 표시에서 resizeEvent는 자식 contentFrame의 레이아웃이 활성화되기
        **전에** 도착한다 — Qt의 show_helper가 대기 중이던 Resize를 자식 표시
        전에 보내고, showEvent는 자식 표시 **후에** 보낸다. 그때 3행 폭이 0이라
        _layoutRowThree가 "배치 전"으로 끝나면, 이후 크기가 안 바뀌는 한 판정이
        영영 안 돈다(보이는 목록에 카드를 추가하는 실제 경로에서 좁은 창인데
        경로가 아이콘으로 접히지 않고, 창을 흔들어야 고쳐지는 결함 — 실기·
        offscreen 둘 다 재현). showEvent 시점엔 자식 레이아웃이 잡혀 있다.
        """
        super().showEvent(event)
        self._layoutRowThree()

    def _clampChannelMinWidth(self) -> None:
        """채널명 최소폭을 "자연 폭과 64px 중 작은 쪽"으로 맞춘다.

        고정 최소폭 64는 좁은 창에서 채널명이 "..." 하나로 붕괴하는 것을
        막는 장치(#239 흡수)인데, "LCK"처럼 자연 폭이 64보다 좁은 이름에는
        쓰지도 않는 폭을 예약해 이름과 종류("· video") 사이가 벌어진다
        (1600px 실기 렌더에서 확인). 이름이 정해진 뒤 최소폭을 자연 폭
        이하로 눌러 짧은 이름은 딱 붙고, 긴 이름은 64px 바닥을 유지한다.
        """
        natural = self.channelNameLabel.sizeHint().width()
        self.channelNameLabel.setMinimumWidth(min(64, natural))

    def setIndex(self, index: int) -> None:
        """카드의 정렬 순번을 갱신한다 (#235 → #244 재설계로 표시는 제거).

        "#0" 번호 라벨은 #244 카드 재설계에서 없어졌다(오너 확정 — 유저에게
        의미 있는 정보가 아니었다). 순번 자체는 삭제 후 재번호매김(#235,
        `content/view.py::_renumberAll`)이 이 메서드로 계속 유지한다 —
        표시가 없어도 위젯·모델 행의 대응이 어긋나지 않게 하는 값이다.
        """
        self.index = index

    def setupDynamicUi(self):
        icon = theme.METRICS["iconSize"]
        self.loadImageFromUrl(self.channelImageLabel, self.item.channel_image_url, icon, "channel")
        self.loadImageFromUrl(self.thumbnailLabel, self.item.thumbnail_url, self.thumbnailLabel.height(), "thumbnail")
        self.channelNameLabel.setText(self.item.channel_name) # 채널 이름 업데이트
        self._clampChannelMinWidth()
        # 조작 도형(content/icons.py) — 평소 muted, 호버에서 강조. 삭제는
        # 호버에서만 실패색(빨강)이 된다(항상 빨간 ❌는 카드에서 삭제만 튀는
        # 위계 역전이었다 #244). pauseButton의 도형(pause↔resume)은 상태에
        # 따라 applyStateStyle이 바꾼다.
        self.deleteButton.setIconName("delete")
        self.deleteButton.setHoverToken("stateFailed")
        self.pauseButton.setIconName("pause")
        self.retryButton.setIconName("retry")
        self.openDirectoryButton.setIconName("folder")
        self.setIndex(self.index)  # 인덱스 업데이트
        self.titleLabel.setText(self.item.title) # 제목 업데이트
        self.titleEdit.setText(self.item.title) # 제목 업데이트
        self.titleEdit.setVisible(False) # 제목 수정용 QLineEdit 숨김
        self._refreshPathLabel()
        self.applyStateStyle()  # setData 전에도 카드가 무스타일로 보이지 않게 (#227)

    def _refreshPathLabel(self) -> None:
        """경로 라벨 — 표시는 축약형(폭이 모자라면 PathLabel이 단계별로 더 줄임), 전문은 툴팁 (#245)."""
        self.directoryLabel.setPathParts(*path_display_parts(self.item.download_path))
        self.directoryLabel.setToolTip(self.item.download_path)
        self.pathIconButton.setToolTip(self.item.download_path)

    def setupSignals(self):
        self.deleteButton.clicked.connect(self.requestDelete)
        self.titleLabel.mousePressEvent = self.startTitleEditing
        self.titleEdit.editingFinished.connect(self.finishTitleEditing)
        self.directoryLabel.mousePressEvent = self.choosePath
        self.pathIconButton.clicked.connect(self.choosePath)  # 아이콘만 남아도 같은 진입점
        self.openDirectoryButton.clicked.connect(self.requestOpenDir)
        self.pauseButton.clicked.connect(self.pauseRequest.emit)
        self.retryButton.clicked.connect(self.retryRequest.emit)

    def addRepresentationButtons(self):
        """
        해상도 목록(Representation)을 정렬 후, 버튼을 생성해 Resolution 영역에 배치한다.
        """

        self.buttons = []
        # LOADING 자리표시 아이템은 해상도 목록이 아직 없다 (#124)
        if not self.item.unique_reps:
            return
        # 표시 순서는 **내림차순**(높은 해상도가 왼쓸) — #245 오너 확정.
        # ①기본 선택이 최고 해상도라, 오름차순이면 선택 pill이 맨 오른쪽에
        # 놓여 해상도 개수가 다른 카드(VOD 3개/클립 2개) 사이에서 선택
        # 표시의 x가 지그재그로 흩어진다. 내림차순이면 항상 맨 앞 한 줄이다.
        # ②pill은 어떤 폭에서도 전부 보인다(접지 않는다 — #245 확정). 3행에서
        # 줄어드는 것은 다운로드 경로 하나뿐이다(_layoutRowThree) — 단 pill 전부가
        # 경로 아이콘·크기와 함께 안 들어가는 폭에서는 pill이 선택 하나로 접힌다.
        # core/api·content/network의 내부 정렬(오름차순, 마지막이 자동 선택)은
        # 건드리지 않고 표시 계층에서만 뒤집는다.
        # ⚠️ 순서는 고정이다 — 클릭해도 pill을 앞으로 옮기지 않는다(옮기면
        # 연속으로 눌러볼 수 없다). 선택만 바뀐다.
        self.item.unique_reps.sort(key=lambda rep: _resolution_key(rep[0]), reverse=True)
        for unique_rep in self.item.unique_reps:
            # 크기 조회가 끝나기 전 표시 — "Unknown"은 실패로 읽혀 "확인 중"으로 표기 (#124)
            unique_rep.append(self.tr("Checking..."))  # 초기 값 설정

        for index, (resolution, base_url, _) in enumerate(self.item.unique_reps):
            self.addRepresentationButton(resolution, base_url, index)

        # 기본 선택 = 최고 해상도 = 내림차순의 첫 pill. 버튼을 넘겨 선택
        # 표시(비활성=채움)까지 바로 건다 — 크기 조회가 끝나기 전에도 무엇이
        # 골라져 있는지 보여야 하고, 그래야 카드마다 선택 x가 한 줄로 선다.
        self.setresolutionUrlSize(self.item.unique_reps[0][0], self.item.unique_reps[0][1], 0, self.buttons[0])

        # pill(높이 pillHeight)이 3행에 꽂히면 컨텐츠 열이 몇 px 자랄 수
        # 있다 — 썸네일이 그 높이를 계속 가득 채우도록 다시 맞춘다(#244).
        self._sizeThumbnail()
        self._layoutRowThree()

    def _reserveFileSizeWidth(self) -> None:
        """3행 우측 군집(파일 크기 또는 재생 시간)의 폭을 **먼저** 확보한다 (#245).

        파일 크기·재생 시간은 어떤 폭에서도 말줄임하지 않는다 — 3행에서
        줄어드는 것은 경로 하나뿐이다. 확보 폭은 **가장 긴 경우** 기준이다:
        크기 조회 전에는 그 자리에 재생 시간("HH:MM:SS")이 들어오고 그것이
        크기보다 길 수 있다(실기에서 잘리던 원인 중 하나). 대기 이후에는
        확정 해상도 접두("1080p · ")가 붙으므로 그 틀도 후보에 넣는다.
        ElidingLabel의 minimumSizeHint는 작게 고정돼 있어 setMinimumWidth로
        바닥을 올려야 레이아웃이 이 라벨을 쥐어짜지 않는다.
        """
        label = self.fileSizeLabel
        metrics = label.fontMetrics()
        item = self.item
        candidates = [label.text(), strftime("%H:%M:%S", gmtime(item.duration or 0)), "0000.00 MB"]
        if item.downloadState != DownloadState.WAITING and item.resolution:
            candidates.append(f"{item.resolution}p · 0000.00 MB")
        label.setMinimumWidth(max(metrics.horizontalAdvance(text) for text in candidates) + 4)

    def _pathMinTextWidth(self) -> int:
        """경로 텍스트가 의미 있게 남는 최소 폭 — 뿌리 + `…` + 마지막 폴더 여섯 자쯤."""
        return self.directoryLabel.fontMetrics().horizontalAdvance("~/…/abcdef")

    def _sizeShown(self) -> bool:
        """파일 크기(또는 재생 시간)가 3행에 보이는 상태인가 — 실패만 사유가 그 자리를 쓴다."""
        return self.item.downloadState != DownloadState.FAILED

    def _pillsFit(self, row_width: int, spacing: int) -> bool:
        """pill 전부가 경로 아이콘·크기와 함께 3행 한 줄에 들어가는가 — 접힘 여부의 유일한 판정.

        절대 px가 아니라 지금 pill들의 자연 폭(naturalWidth — ▾ 몫 제외)으로 묻는다.
        손익분기는 OS 폰트·유저 폰트 크기·DPI·pill 개수마다 다르다(실측 Win 408 /
        macOS 416 / Ubuntu 413, 4자리 세트면 +14). 경로는 아이콘 한 칸만 요구한다 —
        #245의 우선순위대로 텍스트는 접힘보다 먼저 양보하기 때문이다.
        """
        need = sum(b.naturalWidth() for b in self.buttons) + (len(self.buttons) - 1) * spacing
        if self._sizeShown():
            need += spacing + max(self.fileSizeLabel.minimumWidth(), self.fileSizeLabel.sizeHint().width())
        if getattr(self, "_pathShown", False):
            need += spacing + self.pathIconButton.minimumWidth()
        return need <= row_width

    def pillMode(self) -> str:
        """지금 3행의 pill 모드 — "all" / "collapsed" / "expanded" / "hidden" (테스트·상태 확인용)."""
        return self._pillMode

    def _layoutRowThree(self) -> None:
        """3행이 폭을 보고 모양을 정하는 **유일한 지점** (#245 경로 → #244 3행 정리 pill).

        순서(우선순위 — 3행에서 무엇이 먼저 양보하나):
        ① 우측 군집(파일 크기/재생 시간) 확보 폭은 고정 — 잘리지 않는다
        ② pill: 전부 + 경로 아이콘 + 크기가 들어가면 **전부**(클릭 한 번에 고른다).
           안 들어가면 **선택 하나로 접힘**(`[1080p ▾]`) — 누르면 그 자리에서 펼쳐지고
           그동안 경로·크기는 숨는다(펼침 = 유저 조작, 폭이 넓어져 전부 들어가면 풀린다)
        ③ 남는 폭 전부 = 경로(ElideMiddle). 최소치(_pathMinTextWidth) 아래면 텍스트를
           숨기고 pathIconButton만 남긴다 — **텍스트가 사라져도 클릭 대상은 남는다**

        판정은 라벨·pill 자신의 현재 폭이 아니라 "행 폭 − 자연 폭들"로만 하므로 표시
        모드가 바뀌어도 되먹임이 없다. 배치 전(행 폭 0)이면 전부 보이는 모습으로 두고
        resizeEvent/showEvent에서 다시 온다. 경로가 먼저 양보하도록 라벨 최대폭을
        "행 폭 − 다른 항목"으로 씌운다(안 씌우면 Qt가 같은 Preferred 정책의 슬롯 텍스트와
        경로를 나눠 줄여 상태 슬롯이 잘린다 — 560px 실기).
        """
        if self._layingOutRowThree:
            return
        self._layingOutRowThree = True
        try:
            layout = self.resolutionLayout
            spacing = layout.spacing()
            row_width = layout.geometry().width()
            known = row_width > 0
            # ② pill 모드
            if not self._slotShowsPills():
                mode = "hidden"
            elif not known or self._pillsFit(row_width, spacing):
                mode = "all"
            elif self._expanded:
                mode = "expanded"
            else:
                mode = "collapsed"
            if mode != "expanded" and self._expanded:
                self._expanded = False  # 전부 들어가거나 대기가 아니면 펼침은 의미가 없다
                self.expandedChanged.emit(False)
            self._pillMode = mode
            for button in self.buttons:
                button.setCaret(mode == "collapsed")
                button.setVisible(
                    mode in ("all", "expanded") or (mode == "collapsed" and button is self._selectedButton)
                )
            self.fileSizeLabel.setVisible(self._sizeShown() and mode != "expanded")
            self._packPills()
            # ③ 경로
            if not getattr(self, "_pathShown", False) or mode == "expanded":
                self.directoryLabel.setVisible(False)
                self.pathIconButton.setVisible(False)
                return
            if not known:
                self.directoryLabel.setVisible(True)
                self.pathIconButton.setVisible(False)
                return
            used = 0
            for button in self.buttons:
                if button.isVisibleTo(self):
                    used += button.sizeHint().width() + spacing
            if self.statusLabel.isVisibleTo(self):
                used += self.statusLabel.sizeHint().width() + spacing
            if self.fileSizeLabel.isVisibleTo(self):
                used += max(self.fileSizeLabel.minimumWidth(), self.fileSizeLabel.sizeHint().width()) + spacing
            available = row_width - used
            icon_only = available < self._pathMinTextWidth()
            self.directoryLabel.setMaximumWidth(max(available, 0) if not icon_only else _NO_MAX_WIDTH)
            self.directoryLabel.setVisible(not icon_only)
            self.pathIconButton.setVisible(icon_only)
        finally:
            self._layingOutRowThree = False

    def addRepresentationButton(self, resolution, base_url, index):
        """
        해상도 버튼을 추가하고, 비동기로 파일 사이즈를 헤더에서 가져와 버튼 텍스트를 업데이트한다.
        """
        # pill 모양·선택 표시는 전역 QSS의 [role="resolution"] 규칙이 그린다 (#227).
        # QSS는 `.className` 선택자를 지원하지 않아 조용히 무시하므로, 동적
        # 속성(role·selected·caret — content/pill.py가 심는다)을 속성 선택자로 잡는다
        button = ResolutionPill(f'{resolution}p', self)
        # 접혀 있으면 누르는 것은 "펼치기", 펼쳐져 있으면 "고르고 접기"(#244 3행 정리)
        button.clicked.connect(lambda: self._onPillClicked(index))
        # 3행 왼쪽부터 순서대로 꽂는다 — 이미 붙은 버튼 수가 곧 다음 자리다
        # (그 뒤로 [가운데 스트레치, 파일 크기 라벨]이 이어진다). 스트레치
        # 뒤에 addWidget하면 버튼이 오른쪽으로 밀리고, 스트레치가 없으면
        # 넓은 창에서 버튼 사이가 균등 분배로 벌어진다(오너 실기 확인,
        # 1600px에서 간격 335px 실측 — tests/unit/test_card_layout.py 게이트).
        self.resolutionLayout.insertWidget(len(self.buttons), button)
        # 소형 pill — 높이만 토큰으로 고정하고 폭은 텍스트에 맞긴다(가로
        # 여백은 전역 QSS [role="resolution"]의 padding이 준다).
        button.setFixedHeight(theme.METRICS["pillHeight"])
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # 가시성은 _layoutRowThree가 정한다(대기에서만, 안 들어가면 선택 pill만) —
        # 생성 직후엔 숨겨 두고 addRepresentationButtons 끝에서 한 번에 맞춘다
        button.setVisible(False)
        self.buttons.append(button)
        self._pillRowOf[id(button)] = 0

        def update_button_text():
            # 네트워크만 담당한다 — 위젯·아이템 반영은 _onRepSizeFetched(메인 스레드)로 (#168)
            size_text = ""
            if not self.item.is_segment_based:
                try:
                    resp = get_thread_session().head(base_url, timeout=REQUEST_TIMEOUT)
                    resp.raise_for_status()
                    size = int(resp.headers.get('content-length', 0))
                    if size == 0:
                        resp = get_thread_session().get(base_url, stream=True, timeout=REQUEST_TIMEOUT)
                        resp.raise_for_status()
                        size = int(resp.headers.get('content-length', 0))
                        resp.close()
                    size_text = self.setSize(size)
                except Exception:
                    # 기존 동작 유지: 조회 실패는 조용히 "Checking..."으로 남는다
                    return
            try:
                self._repSizeFetched.emit(index, size_text)
            except RuntimeError:
                pass  # 위젯이 이미 파괴된 뒤의 늦은 완료

        thread = threading.Thread(target=update_button_text, daemon=True)
        thread.start()

    def _onRepSizeFetched(self, index, size_text):
        """해상도 크기 조회 결과를 위젯·아이템에 반영한다 — 메인 스레드 (#168)."""
        if index >= len(self.buttons) or index >= len(self.item.unique_reps):
            return  # setData로 아이템이 교체된 뒤의 늦은 완료
        if size_text:
            self.item.unique_reps[index][-1] = size_text
            self.buttons[index].setToolTip(size_text)
        if index == 0:
            # 기본 선택(최고 해상도 = 내림차순 첫 항목)의 크기가 도착하면 그
            # 값으로 파일 크기 표시를 채운다 — 유저가 이미 다른 pill을 골랐으면
            # setresolutionUrlSize가 대기 상태에서만 동작하므로 그 선택을 덮는다는
            # 뜻은 아니다(기존 동작 그대로, 항목 위치만 [-1]→[0]).
            resolution, base_url = self.item.unique_reps[index][0], self.item.unique_reps[index][1]
            self.setresolutionUrlSize(resolution, base_url, index, self.buttons[index])

    def setresolutionUrlSize(self, resolution, base_url, index=None, button:QPushButton = None):
        if self.item.downloadState == DownloadState.WAITING:
            if button is not None:
                # 선택 = 채움 표시(content/pill.py) — 버튼은 전부 활성으로 둔다.
                # 접힌 pill을 눌러 펼쳐야 하므로 선택 pill도 눌려야 한다.
                for btn in self.buttons:
                    btn.setSelected(btn is button)
                if button is not self._selectedButton:
                    self._selectedButton = button
                    self._layoutRowThree()  # 접힘이면 보이는 pill이 바뀐다
            self.item.resolution = resolution
            self.item.base_url = base_url
            # 세그먼트 기반(m3u8·hls_aes)은 total_size를 미리 알 수 없어 처리하지 않음
            if not self.item.is_segment_based and index is not None:
                self.item.total_size = self.item.unique_reps[index][-1]
                self.fileSizeLabel.setText(f" {self.item.unique_reps[index][-1]}")

    def loadImageFromUrl(self, label, url, maxHeight, type):
        """
        주어진 URL에서 이미지를 다운로드해 QLabel에 띄운다.
        세로 높이를 고정하고 가로 크기를 비율에 맞게 조정한다.
        """
        if not url:
            label.clear()
            return
        
        # 이미지 로딩 스레드 시작
        thread = threading.Thread(target=self.fetchImage, args=(label, url, maxHeight, type), daemon=True)
        thread.start()

    def fetchImage(self, label, url, maxHeight, type):
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

    def _onImageFetched(self, label, data, url, maxHeight, type):
        """이미지 디코드·스케일·표시 — 메인 스레드 (#168)."""
        try:
            image = QPixmap()
            image.loadFromData(data)

            # 원본 이미지의 비율 계산
            original_width = image.width()
            original_height = image.height()

            if type == "thumbnail":
                # 도착 시점의 라벨 실제 크기(16:9 상자)에 맞춰 합성한다 —
                # 라벨 크기는 pill 추가 등으로 요청 이후에도 재계산되므로
                # (_sizeThumbnail) 요청 시점 값(maxHeight)을 쓰지 않는다.
                label.setPixmap(self._composeThumbnail(image, label.size()))
                return
            if type == "channel" and original_height > original_width:
                # 가로 높이를 고정하고 세로 크기를 비율에 맞게 계산
                aspect_ratio = original_height / original_width
                new_width = maxHeight
                new_height = int(maxHeight * aspect_ratio)
            else:
                # 세로 높이를 고정하고 가로 크기를 비율에 맞게 계산
                aspect_ratio = original_width / original_height
                new_height = maxHeight
                new_width = int(new_height * aspect_ratio)

            scaled_image = image.scaled(
                new_width, new_height, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
            if type == "channel":
                # 채널 이미지는 원형으로 자른다(#244 재설계 — 1행의 ⬤ 아바타).
                # QSS border-radius는 배경만 둥글게 하고 pixmap은 못 자르므로
                # 로드 시점에 한 번 마스킹한다 — 카드에 상주하는 파이썬
                # 객체가 아니라 일회성 변환이라 O(1) 삽입과 무관하다.
                scaled_image = self._circled(scaled_image)
            label.setPixmap(scaled_image)
        except Exception as e:
            logger.error(f"Error loading image from {url}: {e}")

    @staticmethod
    def _composeThumbnail(pixmap: QPixmap, box) -> QPixmap:
        """16:9 고정 상자에 맞춘 썸네일 합성본을 돌려준다 (#245).

        - 원본 비율 유지로 상자 안에 맞추고, 남는 letterbox 여백은 **이미지
          평균색**으로 채운다(1x1 스케일다운 한 번) — 검정은 도드라지고 카드
          배경색은 붕 떠서, 평균색이어야 이미지와 이어져 보인다(오너 확정).
          가로 16:9 원본은 상자를 정확히 채워 여백 자체가 안 생기고, 세로
          썸네일(클립)에서만 좌우 여백이 나타난다.
        - 모서리는 thumbRadius(카드보다 작게)로 합성 시점에 잘라낸다 — QSS
          border-radius는 pixmap을 못 자른다.
        - 로드 시점 일회성 변환이다 — 카드에 상주하는 파이썬 객체가 아니라
          O(1) 삽입과 무관하다.
        """
        average = pixmap.scaled(1, 1, Qt.AspectRatioMode.IgnoreAspectRatio,
                                Qt.TransformationMode.SmoothTransformation).toImage().pixelColor(0, 0)
        fitted = pixmap.scaled(box, Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
        result = QPixmap(box)
        result.fill(Qt.GlobalColor.transparent)
        painter = QPainter(result)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        radius = theme.METRICS["thumbRadius"]
        path.addRoundedRect(0, 0, box.width(), box.height(), radius, radius)
        painter.setClipPath(path)
        painter.fillRect(0, 0, box.width(), box.height(), average)
        painter.drawPixmap((box.width() - fitted.width()) // 2,
                           (box.height() - fitted.height()) // 2, fitted)
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

    def _shortRemain(self, remain: str) -> str:
        """"HH:MM:SS" 시간을 짧은 표시("3:12")로 줄인다 — 표시 정책.

        진행 중의 남은 시간과 완료의 소요 시간이 같은 포맷을 쓴다(#245).

        계산 자체는 어댑터(download/qt_bridge.py)가 ProgressEvent 값으로
        이미 해 둔 것을 받는다(core 무관) — 여기서는 표기만 줄인다.
        형식이 예상 밖이면("N/A" 등) 받은 그대로 보여준다.
        """
        parts = remain.split(":")
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            return remain
        hours, minutes, seconds = (int(p) for p in parts)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"

    def setData(self, item: ContentItem, index: int):
        """✅ 모델 데이터를 위젯에 반영 — 3행 슬롯 텍스트는 상태별로 다르다 (#245).

        유저가 알고 싶은 것은 상태마다 다르다 — 대기 "어느 화질로?"(pill),
        진행 "얼마나?"(%·속도·남은시간), 일시정지 "어디까지 받고 멈췄나?"
        (%·일시정지됨), 완료 "어디에?"(✓ 완료 + 폴더 버튼), 실패 "왜?"(✕ 사유).
        합집합을 늘 보여주던 정보 과다를 슬롯 교체로 줄인다. 슬롯·조작의
        가시성은 applyStateStyle이 맞춘다.

        3행 우측(파일 크기)에는 pill이 사라진 뒤(대기 이후)부터 **확정된
        해상도**를 함께 적는다 — "1080p · 595.34 MB". 같은 자리에 붙이므로
        행이 늘지 않는다.
        """
        self.item = item
        self.setIndex(index)
        self.channelNameLabel.setText(item.channel_name)
        self._clampChannelMinWidth()
        self.titleLabel.setText(item.title)
        self._refreshPathLabel()

        if self.item.downloadState == ItemState.LOADING:
            self.statusLabel.setText(self.tr("Loading information..."))
            self.fileSizeLabel.setText("")

        elif self.item.downloadState == DownloadState.WAITING:
            # 슬롯은 해상도 pill이 차지한다 — statusLabel은 숨겨지지만
            # 값은 유지한다(테스트·툴팁 등 텍스트 조회 경로 보존)
            self.statusLabel.setText(self.tr("Download waiting"))
            if self.item.is_segment_based:
                self.fileSizeLabel.setText(strftime('%H:%M:%S', gmtime(item.duration)))
            else:
                self.fileSizeLabel.setText(f"{item.total_size}")

        elif self.item.downloadState == DownloadState.RUNNING:
            if self.item.is_segment_based and self.item.post_process:
                # "13% · 후처리 중" — 후처리 진행률(download_progress는 후처리에서
                # 0부터 다시 차오른다)을 **앞에** 둔다. 전송 "42% · …"·일시정지
                # "13% · 일시정지됨"과 자리를 맞춰 상태가 바뀌어도 퍼센트 위치가
                # 흔들리지 않게(#245). tr()은 f-string 밖(lupdate가 못 읽음).
                postprocess_text = self.tr("Post-processing")
                self.statusLabel.setText(f"{item.download_progress}% · {postprocess_text}")
            else:
                remain = self._shortRemain(item.download_remain_time)
                self.statusLabel.setText(
                    f"{item.download_progress}% · {item.download_speed} · "
                    + self.tr("{0} left").format(remain)
                )
            self.fileSizeLabel.setText(self._withResolution(self._sizeText(item)))

        elif self.item.downloadState == DownloadState.PAUSED:
            # 진행분이 먼저, 상태가 뒤 — "54% · 일시정지됨". 진행 슬롯과 같은
            # 자리에서 숫자가 그대로 이어지고, 뒤 문구만 바뀐다.
            # ⚠️ tr()은 f-string 밖에 둔다 — pyside6-lupdate는 f-string 중괄호
            # 안의 tr()을 못 읽어 -no-obsolete 재생성에서 항목이 지워진다(실측).
            paused_text = self.tr("Paused")
            self.statusLabel.setText(f"{item.download_progress}% · {paused_text}")
            self.fileSizeLabel.setText(self._withResolution(self._sizeText(item)))

        elif self.item.downloadState == DownloadState.FINISHED:
            # "✓ 완료 · 2:12" — 소요 시간은 진행 중의 남은 시간과 같은 짧은
            # 포맷(#245). 값은 어댑터(download/qt_bridge.py)가 엔진의
            # start_time~end_time으로 만든 "HH:MM:SS"이고, end_time은 후처리가
            # 끝난 뒤 찍히므로 유저가 체감하는 전체(로그의 "Download completed")다.
            # 값이 없으면(앱 재시작 복원 등) 시간 없이 "✓ 완료"만.
            completed = f"{STATE_ICON['finished']} " + self.tr("Completed")
            elapsed = self._shortRemain(item.download_time) if item.download_time else ""
            self.statusLabel.setText(f"{completed} · {elapsed}" if elapsed else completed)
            self.fileSizeLabel.setText(self._withResolution(self.setSize(item.download_size)))

        elif self.item.downloadState == DownloadState.FAILED:
            # 사유(stateMessage)는 키 기반 매핑을 거친 번역 문자열만 온다.
            # 매핑 밖 예외는 사유가 없다(SPEC §5 — str(e) 폴백을 안 둔 것이
            # 의도) — 그때는 "실패"만 표시하고 억지로 채우지 않는다(#245).
            # 매핑 밖 예외는 사유가 비어 온다. SPEC §5의 "str(e) 폴백 없음"은
            # 내부 정보(원시 HTTP 오류·경로) 노출을 막기 위한 것이지 침묵이
            # 아니다 — 틀린 문구도 없는 문구도 진단을 막는다(#183 교훈). 틀리지
            # 않으면서 다음 행동(로그 폴더 열기 #181)을 주는 안내를 띄운다(#245).
            reason = getattr(item, "stateMessage", "")
            text = reason if reason else self.tr("Unknown error - check the log")
            # 사유 문구는 **첫 줄이 핵심 한 줄**(무엇을 해야 하는지 포함), 둘째
            # 줄부터는 상세다 — 번역 문자열이 줄바꿈으로 나눈다. 3행에는 첫 줄만
            # 올리고 전문은 툴팁으로(#245). 실패 사유는 마우스를 올려야 보이면 안
            # 된다 — 640px에서 잘리던 두 문구를 이렇게 갈랐다(ko 실측).
            headline = text.splitlines()[0] if text else text
            self.statusLabel.setText(f"{STATE_ICON['failed']} {headline}")
            self.statusLabel.setToolTip(text)

        self.applyStateStyle()

    def _sizeText(self, item: ContentItem) -> str:
        """진행·일시정지 카드의 크기 표기 — 세그먼트 기반은 받은 양, 그 외는 총량."""
        if item.is_segment_based:
            return self.setSize(item.download_size)
        return f"{item.total_size}"

    def _withResolution(self, size_text: str) -> str:
        """확정 해상도를 크기 앞에 붙인다 — "1080p · 595.34 MB" (#245).

        대기에서는 pill이 선택을 보여주므로 붙이지 않는다(중복). 해상도가
        아직 없으면(조회 중 자리표시 등) 크기만 돌려준다.
        """
        resolution = self.item.resolution
        if not resolution or self.item.downloadState == DownloadState.WAITING:
            return size_text
        return f"{resolution}p · {size_text}" if size_text else f"{resolution}p"

    def _slotShowsPills(self) -> bool:
        """3행 슬롯에 해상도 pill이 보이는 상태인가 — 대기(선택의 시간)만이다."""
        return self.item.downloadState == DownloadState.WAITING

    def _hasProgress(self) -> bool:
        """진행분이 있는 상태인가 — 진행·일시정지. 진행바는 이때만 보인다.

        "진행 중일 때만"이 아니다(#245 정정) — 일시정지는 멈췄을 뿐 받은
        양이 있고, 그 양을 바가 계속 보여줘야 한다(색만 muted로 바꿔
        "돌고 있지 않다"를 알린다). 완료(100%)·실패는 바가 정보를 더하지
        않으므로 숨긴다.
        """
        return self.item.downloadState in (DownloadState.RUNNING, DownloadState.PAUSED)

    def applyStateStyle(self):
        """상태에 따라 슬롯·조작·색·진행바를 맞춘다 (#227→#245 상태별 슬롯).

        색은 theme.py 한 곳에서만 정의된다. 슬롯 텍스트·진행바는 위젯별
        `setStyleSheet` 없이 동적 속성(`state`)만 바꾸고, 색은 전역 QSS의
        `[state="..."]` 규칙이 고른다 — 속성만 바꾸면 이미 계산된 스타일이
        갱신되지 않으므로 theme.repolish()가 항상 함께 필요하다.

        가시성 매트릭스(#245 확정 — 상태는 다섯, 행 수·행 높이·카드 높이는
        상태와 무관하게 불변, 목록이 들썩이면 안 된다):

        | 상태     | 3행 슬롯              | 조작        | 진행바        |
        |----------|-----------------------|-------------|---------------|
        | 대기     | 해상도 pill           | —           | 없음          |
        | 진행     | % · 속도 · 남은 시간  | 일시정지    | 있음          |
        | 일시정지 | % · 일시정지됨        | 재개        | 있음(muted)   |
        | 완료     | ✓ 완료                | 폴더 열기   | 없음          |
        | 실패     | ✕ 사유                | 재시도      | 없음          |

        - 삭제는 항상 / 파일 크기(+확정 해상도)는 실패에서만 숨김(사유가 그
          자리를 쓴다) / 경로는 전역 설정 경로와 다를 때만 표시
        - 일시정지의 조작은 **재개**다 — 멈춰 있는데 일시정지를 또 권하면
          이미 한 일을 다시 시키는 것이다. 버튼 하나(pauseButton)가 도형·
          툴팁만 pause↔resume으로 바꾼다(시그널 경로는 같은 토글).
        """
        state = self._cardState()
        raw = self.item.downloadState
        self.progressBar.setValue(self._progressValue())
        self.progressBar.setVisible(self._hasProgress())
        if self.progressBar.property("state") != state:
            self.progressBar.setProperty("state", state)
            theme.repolish(self.progressBar)
        if self.statusLabel.property("state") != state:
            self.statusLabel.setProperty("state", state)
            theme.repolish(self.statusLabel)

        self.statusLabel.setVisible(not self._slotShowsPills())
        self._applyTitleEditability()

        self.pauseButton.setVisible(raw in (DownloadState.RUNNING, DownloadState.PAUSED))
        if raw == DownloadState.PAUSED:
            self.pauseButton.setIconName("resume")
            self.pauseButton.setToolTip(self.tr("Resume"))
        else:
            self.pauseButton.setIconName("pause")
            self.pauseButton.setToolTip(self.tr("Pause"))
        self.openDirectoryButton.setVisible(raw == DownloadState.FINISHED)
        self.retryButton.setVisible(raw == DownloadState.FAILED)
        self.fileSizeLabel.setVisible(raw != DownloadState.FAILED)
        self._reserveFileSizeWidth()  # 우측 군집 폭을 먼저 확보 — 크기·시간은 잘리지 않는다
        self._updatePathVisibility()  # → _layoutRowThree: pill 모드·경로 모양을 한 곳에서

    # ---- 해상도 펼침 (#244 3행 정리) ----

    def isExpanded(self) -> bool:
        """접힌 pill을 유저가 펼쳐 둔 상태인가(모드 "expanded")."""
        return self._expanded

    def setExpanded(self, expanded: bool) -> None:
        """접힌 해상도 pill을 그 자리에서 펼치거나 접는다 — 팝업이 아니다.

        들어갈 때:   1080p  720p  480p  360p  144p ····· 경로 ····· 크기   (클릭 한 번)
        안 들어갈 때: [1080p ▾] ····· 경로 ····· 크기
        펼침:        1080p  720p  480p  360p  144p        ← 경로·크기는 잠깐 숨는다
        고른 뒤:     [720p ▾] ····· 경로 ····· 크기

        펼침은 **접힌 상태에서만** 성립한다 — 전부 보이고 있으면 펼칠 것이 없다.
        펼치는 동안 3행을 통째로 pill에 내주고, 그래도 안 들어가면 줄을 바꾼다
        (_packPills). 카드 높이가 잠깐 변하는 것은 허용한다(§3.4의 "높이 고정"은
        **상태 변화**로 목록이 들썩이지 말라는 뜻이고, 이것은 유저가 눌러서 일으킨
        예상된 변화다) — 접히면 원래 높이로 정확히 돌아온다. 펼친 채 창을 넓혀
        전부 들어가게 되면 펼침은 풀리고 전부 보이는 모습이 된다(경로·크기 복귀) —
        펼침은 기억되지 않는다.
        """
        if expanded and self._pillMode != "collapsed":
            return
        if expanded == self._expanded:
            return
        self._expanded = expanded
        self._layoutRowThree()
        if self._expanded == expanded:  # _layoutRowThree가 되돌리지 않았을 때만 알린다
            self.expandedChanged.emit(expanded)

    def _onPillClicked(self, index: int) -> None:
        """전부 보이면 그 자리에서 고르고, 접혀 있으면 펼치고, 펼쳐져 있으면 고르고 접는다.

        펼친 상태에서 이미 선택된 pill을 누르면 선택은 그대로 두고 접기만 한다.
        """
        if not self._slotShowsPills() or index >= len(self.buttons):
            return
        if self._pillMode == "collapsed":
            self.setExpanded(True)
            return
        resolution, base_url = self.item.unique_reps[index][0], self.item.unique_reps[index][1]
        self.setresolutionUrlSize(resolution, base_url, index, self.buttons[index])
        if self._pillMode == "expanded":
            self.setExpanded(False)

    def _packPills(self) -> None:
        """펼친 pill을 3행에, 넘치면 그 아래 추가 행에 왼쪽부터 채운다 — 줄바꿈.

        줄바꿈이 여전히 필요한 이유: 접힘의 판정은 "pill + 경로 아이콘 + 크기"이고
        펼침은 경로·크기를 숨겨 pill만 두므로, 그 사이 폭(pill만 들어가는 폭 ~
        전부 들어가는 폭)에서는 펼쳐도 한 줄이지만 **그보다 좁으면**(창 콘텐츠
        최소폭 근처 — 실측 뷰포트 372~382 vs 5-pill 243+165) 펼친 pill이 한 줄에
        안 들어간다. 가로 오버플로는 금지라 줄을 바꾸는 것이 유일한 답이다.

        판정은 3행 레이아웃의 실제 폭으로 한다(배치 전이라 0이면 전부 3행에 두고
        resizeEvent/showEvent에서 다시 온다). 펼침이 아니면 항상 3행 한 줄이고
        추가 행은 전부 제거된다 — 이것이 "접히면 원래 높이로 정확히 돌아온다"의
        구현이다(추가 행이 남으면 카드가 늘어난 채 굳는다).

        가로 오버플로는 만들지 않는다(§3.4) — pill 하나가 행보다 넓으면 혼자 한 줄.
        """
        visible = [b for b in self.buttons if not b.isHidden()]
        row_width = self.resolutionLayout.geometry().width()
        spacing = self.resolutionLayout.spacing()
        pill_h = theme.METRICS["pillHeight"]
        # 3행 세로 버팀목 — 경로·크기 라벨(세로 Preferred)이 숨으면 3행에는 고정
        # 높이 pill만 남아 행의 최대 높이가 pill 높이로 잠기고, 컨텐츠 열의 남는
        # 세로 공간이 2행에만 가서 3행이 3px 내려앉는다(실측). QBoxLayout은 빈
        # 항목(스페이서·숨은 위젯)을 최대 높이 계산에서 빼므로 스페이서로는 안
        # 되고, **보이는 0폭 위젯**(세로 Preferred)이어야 라벨과 같은 세로 정책이
        # 된다. 펼친 동안만 행 끝에 두고 접히면 뺀다(항목 수 원복). 위젯이라
        # 앞 항목과의 간격(spacing) 하나를 먹는다 — 줄바꿈 판정에서 뺀다.
        if self._expanded and self._rowStrut is None:
            self._rowStrut = QWidget(self.contentFrame)
            self._rowStrut.setObjectName("pillRowStrut")
            self._rowStrut.setFixedWidth(0)
            self._rowStrut.setMinimumHeight(pill_h)
            self._rowStrut.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
            self.resolutionLayout.addWidget(self._rowStrut)
            self._rowStrut.show()
        elif not self._expanded and self._rowStrut is not None:
            self.resolutionLayout.removeWidget(self._rowStrut)
            self._rowStrut.hide()
            self._rowStrut.deleteLater()
            self._rowStrut = None
        rows: list[list[ResolutionPill]] = [[]]
        if self._expanded and row_width > 0:
            available = row_width - spacing  # 버팀목 앞 간격
            used = 0
            for button in visible:
                width = button.sizeHint().width()
                if rows[-1] and used + spacing + width > available:
                    rows.append([button])
                    used = width
                else:
                    used = width if not rows[-1] else used + spacing + width
                    rows[-1].append(button)
        else:
            rows = [visible]
        target = {id(b): k for k, row in enumerate(rows) for b in row}
        # 필요한 추가 행을 만든다 — 3행 바로 아래, 같은 간격·왼쪽 정렬(스트레치가 끝에)
        while len(self._pillRows) < len(rows) - 1:
            extra = QHBoxLayout()
            extra.setSpacing(spacing)
            extra.addStretch(1)
            anchor = self.contentLayout.indexOf(self.resolutionLayout) + 1 + len(self._pillRows)
            self.contentLayout.insertLayout(anchor, extra)
            self._pillRows.append(extra)
        # 각 pill을 목표 행의 제자리(버튼 순서)로 옮긴다 — 숨은 pill은 3행에 남긴다
        for position, button in enumerate(self.buttons):
            row_index = target.get(id(button), 0)
            dest = self.resolutionLayout if row_index == 0 else self._pillRows[row_index - 1]
            slot = sum(1 for other in self.buttons[:position] if target.get(id(other), 0) == row_index)
            current_index = dest.indexOf(button)
            if self._pillRowOf.get(id(button), 0) != row_index or current_index != slot:
                if current_index == -1:
                    source_row = self._pillRowOf.get(id(button), 0)
                    source = self.resolutionLayout if source_row == 0 else self._pillRows[source_row - 1]
                    source.removeWidget(button)
                else:
                    dest.removeWidget(button)
                dest.insertWidget(slot, button)
                self._pillRowOf[id(button)] = row_index
        # 비게 된 추가 행은 없앤다 — 접힌 카드의 높이가 원래대로 돌아오는 지점
        while len(self._pillRows) > len(rows) - 1:
            extra = self._pillRows.pop()
            self.contentLayout.removeItem(extra)
            extra.setParent(None)
            extra.deleteLater()
        self.updateGeometry()

    def _applyTitleEditability(self) -> None:
        """제목의 편집 가능 표시(호버 강조·IBeam 커서)를 클릭 가능 여부와 일치시킨다.

        제목 클릭(파일명 편집, startTitleEditing)은 **대기에서만** 성립한다 —
        진행 중이면 이미 그 이름으로 쓰고 있고 완료됐으면 이미 저장됐다.
        그런데 호버 강조는 상태와 무관하게 떠서 "클릭하면 뭔가 된다"고
        거짓말을 했다(실기 확인). 강조 색은 전역 QSS
        `#titleLabel[editable="true"]:hover`가 이 속성을 보고 고른다.
        """
        editable = self.item.downloadState == DownloadState.WAITING
        if self.titleLabel.property("editable") != editable:
            self.titleLabel.setProperty("editable", editable)
            theme.repolish(self.titleLabel)
        self.titleLabel.setCursor(
            Qt.CursorShape.IBeamCursor if editable else Qt.CursorShape.ArrowCursor
        )

    def _updatePathVisibility(self) -> None:
        """경로는 **대기면 항상, 그 외엔 전역 설정 경로와 다를 때만** 보인다 (#245).

        받기 전에는 "어디에 받을지"가 유효한 정보이고 확인·변경하는 시점이
        바로 대기다 — 여기서 라벨을 숨기면 클릭할 대상이 없어 카드별 경로
        변경(choosePath) 진입점이 사라진다(첫 "다를 때만" 규칙의 회귀).
        받기 시작한 뒤에는 같은 값을 카드마다 반복하는 것이 정보 과다라
        다를 때만 남긴다 — 다르다는 것 자체가 정보다.
        """
        path = self.item.download_path
        differs = bool(path) and path != _global_download_path
        if self.item.downloadState == DownloadState.WAITING:
            self._pathShown = bool(path)
        else:
            self._pathShown = differs
        # 아이콘 모드의 도형 — 전역과 다르면 점 표시 + 밝은 본체색(다르다는 것이 정보)
        self.pathIconButton.setIconName("folder_dot" if differs else "folder")
        self.pathIconButton.setIdleToken("text" if differs else "textMuted")
        self.pathIconButton.setAccentToken("accent" if differs else "")
        self._layoutRowThree()

    def _cardState(self) -> str:
        """현재 아이템 상태를 카드 상태 어휘(theme.CARD_STATES)로 옮긴다.

        PAUSED는 자기 색("paused" — 진행 파랑의 채도를 뺀 muted)을 가진다 —
        진행바가 남아 있어야 하는 상태라 "돌고 있지 않다"가 색으로 읽혀야
        한다(#245). LOADING(조회 중)은 대기 회색이다.
        """
        state = self.item.downloadState
        if state == DownloadState.RUNNING:
            return "running"
        if state == DownloadState.PAUSED:
            return "paused"
        if state == DownloadState.FINISHED:
            return "finished"
        if state == DownloadState.FAILED:
            return "failed"
        return "waiting"

    def _progressValue(self) -> int:
        """진행바에 넣을 0~100 값. 아직 시작 전(대기·조회 중)이면 0이다."""
        if self.item.downloadState in (DownloadState.WAITING, ItemState.LOADING):
            return 0
        try:
            return max(0, min(100, int(self.item.download_progress)))
        except (TypeError, ValueError):
            return 0

    def getData(self) -> ContentItem:
        """✅ 위젯에서 입력된 데이터를 가져와서 ContentItem으로 반환"""
        return ContentItem(
            #index=self.item.index,
            channel_name=self.channelNameLabel.text(),
            title=self.titleEdit.text(),
            directory=self.directoryLabel.text(),
            #status=self.statusLabel.text(),
            progress=self.item.download_progress,
            #remaining_time=self.remainingTimeLabel.text(),
            #size_info=self.sizeInfoLabel.text(),
        )

    def startTitleEditing(self, event):
        """✅ QLabel을 더블클릭하면 QLineEdit로 변경"""
        if self.item.downloadState == DownloadState.WAITING:
            if not self.isEditing:
                self.isEditing = True
                self.titleEdit.setText(self.titleLabel.text())  # ✅ 현재 값 적용
                self.titleLabel.setVisible(False)
                self.titleEdit.setVisible(True)
                self.titleEdit.setFocus()  # ✅ 포커스 이동

    def finishTitleEditing(self):
        """✅ QLineEdit에서 Enter 또는 포커스 해제 시 QLabel로 복귀"""
        self.isEditing = False
        self.titleEdit.setVisible(False)
        self.titleLabel.setVisible(True)
        new_text = self.titleEdit.text().strip()
        if new_text:
            self.titleLabel.setText(new_text)  # ✅ UI 업데이트
            self.item.title = new_text  # ✅ 데이터 업데이트
            self.textChanged.emit(new_text)  # ✅ 모델에도 반영하도록 시그널 전송
        else:
            self.titleLabel.setText(self.item.default_title)
            
    def choosePath(self, event=None):
        """경로를 클릭하면 폴더 선택 대화상자로 이 카드의 저장 경로를 바꾼다 (#245).

        인라인 편집(QLineEdit)의 **교체**다 — 기능(카드별 경로 변경)은 그대로,
        수단만 상단 [경로 찾기]와 같은 폴더 선택으로 바뀐다. 폴더 선택은
        존재하는 폴더만 고르므로 존재 검증·거부 안내(#148, #146 이관 ④)가
        필요 없어졌다 — 그 코드 경로(check_card_edit_path·"Path does not
        exist." 팝업)는 이 교체로 제거됐다. 대기 상태에서만 동작한다(받기
        시작한 뒤의 경로 변경은 의미가 없다). 취소(빈 반환)는 무변경.
        """
        if self.item.downloadState != DownloadState.WAITING:
            return
        chosen = QFileDialog.getExistingDirectory(
            self, self.tr("Select download folder"), self.item.download_path or ""
        )
        if not chosen:
            return
        self.item.download_path = chosen
        self._refreshPathLabel()
        self._updatePathVisibility()
        self.textChanged.emit(chosen)  # 모델에도 반영
        logger.info("카드 저장 경로 변경: %r", chosen)

    def requestDelete(self):
        """✅ 삭제 요청"""
        # print("widget - requestDelete") # Debugging
        self.deleteRequest.emit()

    def requestOpenDir(self):
        try:
            # 라벨 텍스트는 축약형이다 — 실제 경로는 아이템에서 읽는다(#245)
            path = self.item.download_path
            if self.item.downloadState != DownloadState.WAITING:
                path = self.item.output_path
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
                    raise OSError(f"'{path}'을(를) 찾을 수 없습니다.")
            else:
                url = QUrl.fromLocalFile(path)
                if not QDesktopServices.openUrl(url):  # openUrl이 False를 반환하면 실패
                    raise OSError(f"'{path}'을(를) 열 수 없습니다.")
        except Exception as e:
            QMessageBox.warning(self, "경고", str(e))
            return
            

    def setSize(self, size):
        try:
            size = float(size)
        except (ValueError, TypeError):
            return "0 B"
        units = ["B", "KB", "MB", "GB", "TB"]
        unit_index = 0
        while size >= 1024 and unit_index < len(units) - 1:
            size /= 1024
            unit_index += 1 
        return f'{size:.2f} {units[unit_index]}'