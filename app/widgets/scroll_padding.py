"""스크롤 영역의 세로 스크롤바가 내용의 폭을 바꾸지 않게 한다 (v2.10.1 · #309).

세로 스크롤바를 필요할 때만 보이면(AsNeeded) 바가 생기고 사라질 때마다 뷰포트의 폭이 바의
폭만큼 달라져 내용이 좌우로 움직인다. 내용 레이아웃의 **오른쪽 여백 안에 바를 넣으면** 내용의
폭과 자리가 바의 유무와 무관해진다 — 바가 보이는 동안 오른쪽 여백에서 바의 폭을 빼고, 숨으면
되돌린다.

메인 카드 목록(``app/widgets/view.py``)과 구간 편집 창의 구간 목록
(``app/views/section_dialog.py``)이 함께 쓴다.
"""

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QAbstractScrollArea, QLayout


class ScrollBarPadding(QObject):
    """세로 스크롤바가 보이는 동안 내용 레이아웃의 오른쪽 여백에서 바의 폭을 뺀다.

    내용의 오른쪽 끝은 바의 유무와 무관하게 늘 "영역의 오른쪽 끝 − right"다. 바가 여백보다
    넓으면 여백은 0에서 멈춘다 — 그때는 모자란 만큼 내용이 좁아진다. 그렇게 되지 않으려면
    ``right``를 바의 폭 이상으로 준다.
    """

    def __init__(self, area: QAbstractScrollArea, layout: QLayout, right: int):
        """
        Args:
            area: 세로 스크롤바를 가진 스크롤 영역 — 이 객체의 부모가 된다
            layout: 영역 안 내용의 레이아웃 — 오른쪽 여백만 고친다
            right: 바가 없을 때의 오른쪽 여백(px)
        """
        super().__init__(area)
        self._area = area
        self._layout = layout
        self._right = right
        area.verticalScrollBar().installEventFilter(self)  # Show/Hide → 오른쪽 여백 조정
        self.fit()

    def eventFilter(self, watched, event) -> bool:
        if event.type() in (QEvent.Type.Show, QEvent.Type.Hide):
            self.fit()
        return super().eventFilter(watched, event)

    def fit(self) -> None:
        """지금의 바 상태에 맞춰 오른쪽 여백을 정한다."""
        bar = self._area.verticalScrollBar()
        taken = bar.width() if bar.isVisible() else 0
        margins = self._layout.contentsMargins()
        self._layout.setContentsMargins(
            margins.left(), margins.top(), max(0, self._right - taken), margins.bottom()
        )
