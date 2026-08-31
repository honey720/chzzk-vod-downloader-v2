import os
import threading
from PySide6.QtWidgets import QWidget, QPushButton, QMessageBox
from PySide6.QtGui import QPainter, QPainterPath, QPixmap, QDesktopServices, QRegion
from PySide6.QtCore import Qt, Signal, QUrl, QDir, QProcess, QRectF
from content.data import ContentItem
from content.network import REQUEST_TIMEOUT, get_thread_session
from core.models.download_state import DownloadState
from app.viewmodels.item_state import ItemState
from app.viewmodels.path_gates import check_card_edit_path
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


def set_global_download_path(path: str) -> None:
    """전역 다운로드 경로를 갱신한다 — 카드의 경로 표시 여부 판단 기준."""
    global _global_download_path
    _global_download_path = path


def _resolution_key(resolution) -> int:
    """해상도 값을 정렬용 정수로 — API는 int(min(w,h))를 주지만 문자열("1080")도 받는다."""
    try:
        return int(resolution)
    except (TypeError, ValueError):
        return 0


class _PillButton(QPushButton):
    """해상도 pill — 최소폭을 0으로 신고해 카드 최소폭을 부풀리지 않는다 (#245).

    pill 폭이 카드 최소폭에 들어가면 pill이 많을 때 카드가 뷰포트보다
    넓어져 **오른쪽이 잘리기만 하고 접히지 않는다**(QScrollArea는 최소폭
    이하로 못 줄인다 — 실측). 최소폭을 빼면 레이아웃이 카드를 뷰포트 폭에
    맞추고, 어떤 pill을 접을지는 ContentItemWidget._foldPills가 정한다.
    sizeHint(자연 폭)는 그대로라 자리가 있을 때는 텍스트 폭으로 놓인다.
    """

    def minimumSizeHint(self):
        hint = self.sizeHint()
        hint.setWidth(0)
        return hint


class ContentItemWidget(QWidget, Ui_ContentItemWidget):
    """컨텐츠 정보를 표시하는 커스텀 위젯"""

    textChanged = Signal(str)
    deleteRequest = Signal()
    pauseRequest = Signal()   # 진행 카드의 ⏸ (#245 상태별 조작)
    retryRequest = Signal()   # 실패 카드의 ↻ (#245 상태별 조작)

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
        self._foldPills()

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
        self.directoryLabel.setText(self.item.download_path) # 다운로드 경로 업데이트
        self.directoryEdit.setText(self.item.download_path) # 다운로드 경로 업데이트
        self.directoryEdit.setVisible(False) # 다운로드 경로 수정용 QLineEdit 숨김
        self.applyStateStyle()  # setData 전에도 카드가 무스타일로 보이지 않게 (#227)

    def setupSignals(self):
        self.deleteButton.clicked.connect(self.requestDelete)
        self.titleLabel.mousePressEvent = self.startTitleEditing
        self.titleEdit.editingFinished.connect(self.finishTitleEditing)
        self.directoryLabel.mousePressEvent = self.startPathEditing
        self.directoryEdit.editingFinished.connect(self.finishPathEditing)
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
        # ②폭이 모자라 접힐 때 저화질부터 사라진다(_foldPills) — "좁으면 덜
        # 중요한 것부터 접는다". core/api·content/network의 내부 정렬(오름차순,
        # 마지막이 자동 선택)은 건드리지 않고 표시 계층에서만 뒤집는다.
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
        self._foldPills()

    def _foldPills(self) -> None:
        """3행에 pill이 다 안 들어가면 **오른쪽(저화질)부터** 숨긴다 (#245).

        pill은 폭이 고정이라 줄일 수 없다 — 대신 "좁으면 덜 중요한 것부터
        접는다". 내림차순 정렬이라 오른쓸 끝이 가장 낮은 해상도다. 남은
        자리는 3행 폭에서 우측 군집(경로·파일 크기)과 간격을 뺀 값이고,
        pill을 왼쪽부터 누적해 들어가는 만큼만 보인다. 대기가 아닐 때는
        슬롯이 pill을 안 쓰므로 applyStateStyle의 가시성 규칙에 맡긴다.
        """
        if not getattr(self, "buttons", None) or not self._slotShowsPills():
            return
        layout = self.resolutionLayout
        spacing = layout.spacing()
        row_width = layout.geometry().width()
        if row_width <= 0:
            return  # 아직 배치 전 — resizeEvent에서 다시 온다
        # 우측 군집 — 파일 크기는 자연 폭을 지킨다(pill이 그 자리를 먹으면 안
        # 된다), 경로는 원래 말줄임되는 라벨이라 최소폭만 남긴다.
        right_cluster = 0
        if self.fileSizeLabel.isVisibleTo(self):
            right_cluster += self.fileSizeLabel.sizeHint().width() + spacing
        if self.directoryLabel.isVisibleTo(self):
            right_cluster += self.directoryLabel.minimumSizeHint().width() + spacing
        available = row_width - right_cluster
        used = 0
        for button in self.buttons:
            width = button.sizeHint().width()
            fits = used + width <= available
            button.setVisible(fits)
            used += width + spacing if fits else 0
            if not fits:
                available = -1  # 하나가 안 들어가면 그 뒤(더 낮은 화질)도 전부 접는다

    def addRepresentationButton(self, resolution, base_url, index):
        """
        해상도 버튼을 추가하고, 비동기로 파일 사이즈를 헤더에서 가져와 버튼 텍스트를 업데이트한다.
        """
        button = _PillButton(f'{resolution}p', self)
        button.clicked.connect(lambda: self.setresolutionUrlSize(resolution, base_url, index, button))
        # pill 모양·선택 표시는 전역 QSS의 [role="resolution"] 규칙이 그린다 (#227).
        # QSS는 `.className` 선택자를 지원하지 않아 조용히 무시하므로, 동적
        # 속성을 심어 속성 선택자로 잡는 게 유일한 방법이다
        button.setProperty("role", "resolution")
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
        # 슬롯 규칙(#245): pill은 대기 상태에서만 보인다 — 생성 시점의
        # 상태에 맞춰 시작 가시성을 정한다(이후는 applyStateStyle이 맞춤)
        button.setVisible(self._slotShowsPills())
        self.buttons.append(button)

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
                for btn in self.buttons:
                    btn.setEnabled(True)
                button.setDisabled(True)
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
        self.directoryLabel.setText(item.download_path)

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
            reason = getattr(item, "stateMessage", "")
            text = reason if reason else self.tr("Download failed")
            self.statusLabel.setText(f"{STATE_ICON['failed']} {text}")
            self.statusLabel.setToolTip(reason)

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

        pills = self._slotShowsPills()
        self.statusLabel.setVisible(not pills)
        for button in getattr(self, "buttons", []):
            button.setVisible(pills)

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
        self._updatePathVisibility()
        self._foldPills()  # pill 전부 켠 뒤 폭에 안 맞는 저화질부터 다시 접는다

    def _updatePathVisibility(self) -> None:
        """경로는 전역 설정 경로와 다를 때만 보인다 (#245).

        같은 값을 카드마다 반복 표시하는 것이 정보 과다의 큰 몫이었다 —
        다르다는 것 자체가 정보다. 편집 중(directoryEdit 표시)에는 라벨
        가시성을 건드리지 않는다.
        """
        if self.isEditing and self.directoryEdit.isVisible():
            return
        path = self.item.download_path
        differs = bool(path) and path != _global_download_path
        self.directoryLabel.setVisible(differs)

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
            
    def startPathEditing(self, event):
        """✅ QLabel을 더블클릭하면 QLineEdit로 변경"""
        if self.item.downloadState == DownloadState.WAITING:
            if not self.isEditing:
                self.isEditing = True
                self.directoryEdit.setText(self.directoryLabel.text())  # ✅ 현재 값 적용
                self.directoryLabel.setVisible(False)
                self.directoryEdit.setVisible(True)
                self.directoryEdit.setFocus()  # ✅ 포커스 이동

    def finishPathEditing(self):
        """✅ QLineEdit에서 Enter 또는 포커스 해제 시 QLabel로 복귀

        존재하지 않는 경로는 기존처럼 반영하지 않되, 아무 표시 없이 무시하던
        것을 안내·로그로 남긴다 (#148 — #146 감사의 무피드백 지점).
        """
        if not self.isEditing:
            # returnPressed와 포커스 이탈이 editingFinished를 연달아 낼 수 있다 —
            # 첫 종료만 처리해 거부 안내가 중복되지 않게 한다
            return
        self.isEditing = False
        self.directoryEdit.setVisible(False)
        new_path = self.directoryEdit.text().strip()
        # 판정은 path_gates가 단일 지점으로 담당한다 (#169 — #146 ⓑ1)
        if check_card_edit_path(new_path):
            self.directoryLabel.setText(new_path)  # ✅ UI 업데이트
            self.item.download_path = new_path  # ✅ 데이터 업데이트
            self.textChanged.emit(new_path)  # ✅ 모델에도 반영하도록 시그널 전송
            logger.info("카드 저장 경로 변경: %r", new_path)
        elif new_path and new_path != self.item.download_path:
            # 경로는 repr로 남긴다 — 공백 유사 문자(U+00A0 등)·오염(따옴표 등)을
            # 육안 구분할 수 있는 유일한 표기다 (#144 실측)
            logger.warning("카드 저장 경로 거부 — 존재하지 않음: %r", new_path)
            QMessageBox.warning(self, self.tr("Warning"), self.tr("Path does not exist."))

    def requestDelete(self):
        """✅ 삭제 요청"""
        # print("widget - requestDelete") # Debugging
        self.deleteRequest.emit()

    def requestOpenDir(self):
        try:
            path = self.directoryLabel.text()
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