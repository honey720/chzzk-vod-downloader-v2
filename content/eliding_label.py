"""내용에 맞춰 넓어지려는 QLabel을 대체하는 말줄임 라벨 (PR #229 후속).

`QLabel`은 기본적으로 전체 텍스트를 한 줄로 보여줄 수 있는 폭을 요구한다
(`minimumSizeHint()`가 내용 폭 그대로) — 긴 제목·다운로드 경로가 카드를
밀고 카드가 `QScrollArea` 뷰포트를 밀어 가로 스크롤이 생기던 원인이었다.
이 라벨은 (a) 가로 사이즈 정책을 `Ignored`로 둬 레이아웃이 내용 폭보다
좁게 줄 수 있게 하고 (b) 실제 폭보다 넘치면 `elidedText()`로 잘라 보여주고
(c) 잘린 전체 값은 툴팁으로 노출한다.

`ui/contentItemWidget.py`(Designer 생성)가 `titleLabel`/`directoryLabel`
생성 시 `QLabel` 대신 이 클래스를 쓴다 — `content/widget.py`가 아니라 이
파일에 둔 이유는 `content/widget.py`가 `ui/contentItemWidget.py`를
import하므로, 거꾸로 `ui/`가 `content/widget.py`를 import하면 순환
임포트가 나기 때문이다(`content/view.py`가 `ContentListView`를 이렇게
독립 모듈에 두는 것과 같은 이유).
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QLabel, QSizePolicy


class ElidingLabel(QLabel):
    def __init__(self, parent=None, elide_mode: Qt.TextElideMode = Qt.TextElideMode.ElideRight):
        super().__init__(parent)
        self._elide_mode = elide_mode
        self._full_text = ""
        self._last_elide_width = -1
        policy = self.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
        self.setSizePolicy(policy)

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
