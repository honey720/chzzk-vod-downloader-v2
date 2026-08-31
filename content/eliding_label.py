"""내용에 맞춰 넓어지려는 QLabel을 대체하는 말줄임 라벨 (PR #229 후속).

`QLabel`은 기본적으로 전체 텍스트를 한 줄로 보여줄 수 있는 폭을 요구한다
(`minimumSizeHint()`가 내용 폭 그대로) — 긴 제목·다운로드 경로가 카드를
밀고 카드가 `QScrollArea` 뷰포트를 밀어 가로 스크롤이 생기던 원인이었다.
이 라벨은 (a) `minimumSizeHint()`를 말줄임표 하나 폭의 작은 고정값으로
override해 레이아웃이 내용 폭보다 좁게 줄 수 있게 하고, `sizeHint()`는
원문(`_full_text`) 기준으로 override해 여유가 있으면 레이아웃이 실제로
넓게 줄 수 있게 하고 (b) 실제 폭보다 넘치면 `elidedText()`로 잘라 보여주고
(c) 잘린 전체 값은 툴팁으로 노출한다.

`ui/contentItemWidget.py`(Designer 생성)가 `titleLabel`/`directoryLabel`
생성 시 `QLabel` 대신 이 클래스를 쓴다 — `content/widget.py`가 아니라 이
파일에 둔 이유는 `content/widget.py`가 `ui/contentItemWidget.py`를
import하므로, 거꾸로 `ui/`가 `content/widget.py`를 import하면 순환
임포트가 나기 때문이다(`content/view.py`가 `ContentListView`를 이렇게
독립 모듈에 두는 것과 같은 이유).
"""

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QLabel, QSizePolicy


class ElidingLabel(QLabel):
    def __init__(self, parent=None, elide_mode: Qt.TextElideMode = Qt.TextElideMode.ElideRight):
        super().__init__(parent)
        self._elide_mode = elide_mode
        self._full_text = ""
        self._last_elide_width = -1
        # Ignored였던 첫 시도는 실제로 폭 0에 눌렸다(PR #229 오너 실기 확인 후속 실측 — 아래 참고).
        # Preferred(QLabel 기본값)를 유지하되 sizeHint를 작고 고정된 값으로
        # 바꿔치기하는 쪽이 맞다 — Ignored는 "sizeHint를 아예 안 본다"는
        # 뜻이라 레이아웃이 존중할 대상 자체가 없어져, 같은 줄(topLayout)의
        # Expanding 스페이서가 공간을 먼저 채가면 Ignored 라벨은 진짜로
        # 0을 받았다(모든 다운로드 상태에서 실측 확인). Preferred는 내가
        # 돌려주는(고정·작은) sizeHint를 레이아웃이 실제로 존중해준다.
        policy = self.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Policy.Preferred)
        self.setSizePolicy(policy)

    def minimumSizeHint(self) -> QSize:
        # 원문 길이와 무관하게 항상 이 작은 고정값을 돌려준다 — 이게 없으면
        # (a) 기본 QLabel처럼 원문 전체 폭이 최소치가 돼 #226/#229 이전처럼
        # 카드가 넘치거나, (b) 지금 표시 중인(이미 elide된) 텍스트 기준으로
        # 계산하면 극단적으로 좁아져 elidedText()가 ""를 반환할 때
        # minimumSizeHint도 0이 되어 레이아웃이 "필요 없다"로 읽고 다음
        # 패스에서도 0을 주는 되먹임 루프가 생긴다(PR #229 오너 실기 확인 후속 실측).
        # `sizeHint()`도 아래에서 override한다 — QLabel 기본 구현에 맡기면
        # "지금 표시 중인(이미 elide된) 텍스트" 기준으로 계산되어 또 다른
        # 되먹임 루프(여유가 생겨도 영원히 "..."에 갇힘)가 생긴다.
        metrics = QFontMetrics(self.font())
        return QSize(metrics.horizontalAdvance("…") + 4, metrics.height())

    def sizeHint(self) -> QSize:
        # override 안 하면 QLabel 기본 구현이 "지금 화면에 그려진(이미
        # elide된) 텍스트" 기준으로 계산한다 — 한 번이라도 좁은 폭에서
        # "..."까지 줄어들고 나면, 그 뒤로 창을 아무리 넓혀도 sizeHint가
        # 계속 "..." 하나 폭만 요구하니 레이아웃이 다시는 더 넓게 주지
        # 않는 게 되먹임 루프에 갇힌다(오너 실기 확인 — 여백이 충분해도
        # 파일 크기가 항상 "..."으로만 보이던 회귀). 항상 원문(`_full_text`)
        # 기준 폭을 요구해야, 레이아웃이 여유가 있을 때 그만큼 실제로
        # 돌려준다.
        metrics = QFontMetrics(self.font())
        return QSize(metrics.horizontalAdvance(self._full_text) + 4, metrics.height())

    def setElideMode(self, mode: Qt.TextElideMode) -> None:
        self._elide_mode = mode
        self._applyElide(force=True)

    def setText(self, text: str) -> None:
        self._full_text = text
        self.setToolTip(text)
        self._applyElide(force=True)

    def text(self) -> str:
        return self._full_text

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._applyElide()

    def _applyElide(self, force: bool = False) -> None:
        # 카드 1000개 상태에서 리사이즈 한 번마다 라벨 4000개가 다시 이 메서드를
        # 타는데(#229 후속 실측: 최대 블로킹 구간 55ms→278ms), 폭이 실제로
        # 안 바뀌었으면(세로만 바뀐 리사이즈 등) elidedText 재계산·setText·
        # 리페인트 예약을 건너뛴다 — 매 리사이즈마다 값어치 없는 작업이었다.
        width = self.width()
        if not force and width == self._last_elide_width:
            return
        self._last_elide_width = width
        metrics = QFontMetrics(self.font())
        elided = metrics.elidedText(self._full_text, self._elide_mode, width)
        if elided != super().text():
            super().setText(elided)


class PathLabel(ElidingLabel):
    """카드 3행 경로 라벨 — **줄어드는 순서가 정해져 있다** (#245).

    ① 전체(축약형 원문) → ② 중간 폴더 접기(`뿌리/…/마지막폴더`, 마지막 폴더는
    온전) → ③ 마지막 폴더에만 ElideMiddle(접두 `뿌리/…/`는 고정) → ④ 아이콘만
    (이 단계는 ContentItemWidget._layoutPathLabel이 정한다). 파일이 실제로
    들어가는 곳은 **마지막 폴더**이므로 정보 가치가 낮은 중간 폴더부터 접고,
    마지막 폴더는 가장 늦게 잘린다 — 3행의 "정체를 살리고 맥락을 접는다"를
    경로 문자열 안에서도 지킨다. 중간 폴더가 하나뿐이어도 ②를 거친다.

    단계 정보는 `setPathParts()`로 받는다(문자열 분해는 content/widget.py의
    `path_display_parts` — 이 모듈은 위젯을 import할 수 없다). `text()`·
    `sizeHint()`는 항상 ① 기준이라 창을 넓히면 ①로 회복된다(되먹임 없음).
    `setText()`만 부르면(Designer 초기 문구 등) 단계 없이 기본 말줄임이다.
    """

    def __init__(self, parent=None):
        super().__init__(parent, elide_mode=Qt.TextElideMode.ElideMiddle)
        self._prefix = ""
        self._last = ""

    def setPathParts(self, full: str, prefix: str, last: str) -> None:
        """①의 문자열 `full`, ②③의 고정 접두 `prefix`(`뿌리/…/`), 마지막 폴더 `last`."""
        super().setText(full)
        self._prefix = prefix
        self._last = last
        self._applyElide(force=True)

    def setText(self, text: str) -> None:
        """단계 정보 없이 텍스트만 바꾸면 기본 ElidingLabel로 동작한다."""
        self._prefix = ""
        self._last = ""
        super().setText(text)

    def _applyElide(self, force: bool = False) -> None:
        if not self._last:
            super()._applyElide(force)
            return
        width = self.width()
        if not force and width == self._last_elide_width:
            return
        self._last_elide_width = width
        metrics = QFontMetrics(self.font())
        folded = self._prefix + self._last
        if metrics.horizontalAdvance(self._full_text) <= width:
            shown = self._full_text  # ① 전체
        elif metrics.horizontalAdvance(folded) <= width:
            shown = folded  # ② 중간 폴더 접기 — 마지막 폴더 온전
        else:
            room = max(width - metrics.horizontalAdvance(self._prefix), 0)
            shown = self._prefix + metrics.elidedText(self._last, Qt.TextElideMode.ElideMiddle, room)  # ③
        if shown != QLabel.text(self):
            QLabel.setText(self, shown)
