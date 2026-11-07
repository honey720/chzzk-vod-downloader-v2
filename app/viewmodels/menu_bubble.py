"""메뉴바 「VOD 다운」의 상태 버블이 무엇을 보이는지 정한다 (#523, 설계 §2).

우선순위: 받는 중 > 일시정지 > 전부 실패 > 일부 실패 > 완료 > 없음. 대기만 있으면 없음이다.

- 받는 중 · 일시정지는 지금 카드 상태(칩 숫자)를 따른다.
- 결과 버블(완료 · 일부 실패 · 전부 실패)은 하단바 실행 결과(`RunResult`)가 보일 때만 나타난다 — 같은
  객체를 보므로 둘이 나타나고 사라지는 조건이 늘 같다. 모양은 목록 전체 카드의 지금 상태다.

셈은 `CardOrder`의 칩 숫자만 읽는다 — 카드가 1000장이어도 목록을 훑지 않는다. 칩 숫자는 카드 상태가
바뀐 그 이벤트 루프 차례가 끝난 뒤에 고쳐지므로(`CardOrder._flush`), 그 사이 한 차례는 바뀌기 전 숫자를
본다. 그 뒤 `countsChanged`로 다시 맞춘다.
"""

from PySide6.QtCore import QObject, Signal

from app.viewmodels.card_order import CardOrder
from app.viewmodels.footer_monitor import RunResult

#: 결과 버블 — 다운로드 작업이 끝났을 때만 나타나는 셋.
RESULT_STATES = ("all_failed", "some_failed", "done")

#: 버블이 없을 때의 상태 값.
NO_BUBBLE = ""


class MenuBubble(QObject):
    """「VOD 다운」 메뉴 버블의 상태와 툴팁 숫자를 맡는다.

    시그널:
        changed: 상태나 숫자가 바뀌었을 수 있다(칩 숫자 · 실행 결과가 바뀔 때마다)
    """

    changed = Signal()

    def __init__(self, order: CardOrder, result: RunResult, parent: QObject | None = None) -> None:
        """버블을 만든다.

        Args:
            order: 칩 숫자(`CardOrder`) — 상태와 숫자를 여기서 읽는다
            result: 하단바 실행 결과(`RunResult`) — 결과 버블이 보일 수 있는지
        """
        super().__init__(parent)
        self._order = order
        self._result = result
        order.countsChanged.connect(self.changed)
        result.changed.connect(self.changed)

    def state(self) -> str:
        """지금 버블 — `icons.BUBBLE_STATES` 가운데 하나, 없으면 `NO_BUBBLE`."""
        counts = self._order.counts()
        if counts["running"]:
            return "running"
        if counts["paused"]:
            return "paused"
        if not self._result.isShown():
            return NO_BUBBLE
        finished, failed = counts["finished"], counts["failed"]
        if failed and not finished:
            return "all_failed"
        if failed:
            return "some_failed"
        return "done"

    def counts(self) -> tuple[int, int, int]:
        """툴팁 숫자 (완료, 실패, 전체 카드) — 목록 전체 카드의 지금 상태."""
        counts = self._order.counts()
        return counts["finished"], counts["failed"], counts["all"]
