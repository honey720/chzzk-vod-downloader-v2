"""카드 목록의 상태 필터 · 정렬 · 보이는 차례 (#495 — 설계 §2 · §8-4).

목록(`ContentListModel.items`)은 추가된 순서 그대로 둔다. 정렬은 보이는 순서만 바꾸고, 그 순서가
곧 다운로드 차례다 — 받을 차례가 올 때마다 이 순서에서 위부터 첫 대기 카드를 고른다
(`ContentViewModel.findItem`). 필터로 가린 카드도 차례에 든다(프로토 `sortedItems` — 「필터와
무관 — 화면 순서이자 다운로드 차례」). 필터는 보임만 정한다.

보이는 차례는 `_ordered`에 늘 정렬된 채로 둔다. 카드를 더하면 그 자리를 이분 탐색으로 찾아
끼우고(목록 전체를 다시 정렬하지 않는다), 정렬을 바꿀 때만 전체를 다시 정렬한다. 카드 상태가
바뀌면 바뀐 카드만 — 그것도 한 이벤트 루프 차례에 한 번 모아서 — 칩 숫자를 고치고, 추가된 순서가
아니면 그 카드만 제자리로 옮긴다. 카드 1000장에서 상태가 바뀔 때마다 목록 전체를 다시 맞추면 카드 수의 제곱으로
느려진다(프로토 inbox 64).

상태는 여러 곳(다운로드 뷰모델 · 재시도 · 실패 처리)에서 바뀌지만 바뀐 뒤에는 늘 카드 갱신
알림(모델 `itemChanged` 또는 뷰모델 `itemStarted` 등)이 온다 — 카드도 그 알림으로 다시 그린다.
알림이 상태를 바꾸기 전에 오는 자리가 있어(시작: `itemStarted` 뒤에 RUNNING) 알림을 받은 그
자리에서 읽지 않고 그 차례가 끝난 뒤(`QTimer.singleShot(0)`) 읽는다.
"""

from bisect import bisect_left
from functools import total_ordering

from PySide6.QtCore import QObject, QTimer, Signal

from app.system import settings
from app.viewmodels.data import ContentItem
from app.viewmodels.item_state import ItemState
from core.models.download_state import DownloadState

#: 상태 필터 칩의 키 — 화면 차례(전체 · 대기 · 진행 · 일시정지 · 완료 · 실패)
FILTER_KEYS = ("all", "waiting", "running", "paused", "finished", "failed")
#: 정렬 기준의 키 — 메뉴 차례(추가된 순서 · 제목순 · 채널명순 · 업로드순 · 상태순)
SORT_KEYS = ("added", "title", "channel", "upload", "state")

#: 카드 상태 → 칩. 조회 중 카드는 대기 칩에 센다 — 조회가 끝나면 대기 카드가 된다
_STATE_FILTER = {
    ItemState.LOADING: "waiting",
    DownloadState.WAITING: "waiting",
    DownloadState.RUNNING: "running",
    DownloadState.PAUSED: "paused",
    DownloadState.FINISHED: "finished",
    DownloadState.FAILED: "failed",
}
#: 상태순의 차례 — 받는 중 · 일시정지 · 대기(조회 중 포함) · 실패 · 완료(프로토 STATE_ORDER)
_STATE_RANK = {
    DownloadState.RUNNING: 0,
    DownloadState.PAUSED: 1,
    DownloadState.WAITING: 2,
    ItemState.LOADING: 2,
    DownloadState.FAILED: 3,
    DownloadState.FINISHED: 4,
}


def filter_key(item: ContentItem) -> str:
    """카드가 드는 칩의 키."""
    return _STATE_FILTER.get(item.downloadState, "waiting")


def _upload_date(item: ContentItem) -> str | None:
    """업로드순의 날짜 — 카드의 날짜 글(`YYYY-MM-DD HH:MM:SS`)을 그대로. 모르면 None.

    카드가 가진 날짜는 VOD의 방송 시작 시각 · 클립의 생성 시각이다(오너 2026-11-06). 글의 앞이
    `YYYY-MM-DD`이면 글자 차례가 시간 차례와 같아 그대로 비교한다. 그 밖(`Unknown Date`, 빈 글)은
    모르는 날짜다.
    """
    text = item.live_open_date if isinstance(item.live_open_date, str) else ""
    head = text[:10]
    if len(head) == 10 and head[4] == "-" and head[7] == "-" and (head[:4] + head[5:7]).isdigit():
        return text
    return None


@total_ordering
class _Reversed:
    """감싼 값의 차례를 뒤집는다 — 내림차순 키를 오름차순 비교로 쓴다."""

    __slots__ = ("value",)

    def __init__(self, value) -> None:
        self.value = value

    def __eq__(self, other) -> bool:
        return self.value == other.value

    def __lt__(self, other) -> bool:
        return other.value < self.value


class CardOrder(QObject):
    """카드 목록의 필터 · 정렬 상태와 보이는 차례를 맡는다 (#495).

    시그널:
        orderReset: 정렬이 바뀌었다 — 보이는 차례 전체가 바뀌었다
        filterChanged: 고른 칩이 바뀌었다 — 모든 카드의 보임이 바뀌었을 수 있다
        itemMoved(item): 그 카드 하나의 자리가 바뀌었다(정렬 기준 값이 바뀜 — 상태순의 상태 등)
        itemStateChanged(item): 그 카드의 칩이 바뀌었다 — 보임이 바뀌었을 수 있다
        countsChanged: 칩 숫자나 조회 중 카드 수가 바뀌었다
    """

    orderReset = Signal()
    filterChanged = Signal()
    itemMoved = Signal(object)
    itemStateChanged = Signal(object)
    countsChanged = Signal()

    def __init__(self, model, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._model = model
        saved = settings.load_setting("sortOrder")
        by, _, direction = saved.partition(":")
        self._sortBy = by if by in SORT_KEYS else "added"
        self._descending = direction == "desc"
        self._filter = "all"
        self._seq: dict[
            ContentItem, float
        ] = {}  # 추가된 차례 — 같은 자리 교체는 앞 카드의 값을 잇는다
        self._nextSeq = 0.0
        self._shown: dict[ContentItem, str] = {}  # 마지막으로 센 칩 — 바뀐 것만 고친다
        self._counts = dict.fromkeys(FILTER_KEYS, 0)
        # 조회 중 카드 — 대기 칩에 함께 세지만, 받을 수 있는 대기 카드와는 따로 센다(#504 다운로드 버튼)
        self._loading: set[ContentItem] = set()
        self._ordered: list[ContentItem] = []
        self._dirty: dict[ContentItem, None] = {}  # 차례가 끝난 뒤 다시 볼 카드(넣은 차례 유지)
        self._flushPending = False
        # ⚠️ 목록 뷰보다 먼저 잇는다 — 뷰가 카드를 넣을 때 이 객체에 자리를 묻는다(같은 시그널은 이은 차례로 돈다)
        model.itemInserted.connect(self._onInserted)
        model.itemRemoved.connect(self._onRemoved)
        model.itemChanged.connect(self.touch)

    # ---- 상태 ----

    def sortBy(self) -> str:
        """정렬 기준의 키(SORT_KEYS 중 하나)."""
        return self._sortBy

    def isDescending(self) -> bool:
        """내림차순인지."""
        return self._descending

    def filterKey(self) -> str:
        """고른 칩의 키(FILTER_KEYS 중 하나)."""
        return self._filter

    def counts(self) -> dict[str, int]:
        """칩마다의 카드 수 — 「all」은 목록의 모든 카드."""
        return dict(self._counts)

    def loadingCount(self) -> int:
        """조회 중 카드 수 — 대기 칩 숫자에 들어 있다."""
        return len(self._loading)

    def ordered(self) -> list[ContentItem]:
        """보이는 차례의 모든 카드(필터와 무관) — 다운로드 차례이기도 하다."""
        return list(self._ordered)

    def indexOf(self, item: ContentItem) -> int:
        """보이는 차례에서 그 카드의 자리."""
        return self._ordered.index(item)

    def accepts(self, item: ContentItem) -> bool:
        """그 카드가 지금 고른 칩에서 보이는지."""
        return self._filter == "all" or filter_key(item) == self._filter

    # ---- 바꾸기 ----

    def setSort(self, by: str, descending: bool) -> None:
        """정렬을 바꾸고 기억한다 — 보이는 차례 전체를 다시 정렬한다."""
        if by not in SORT_KEYS:
            raise ValueError(f"알 수 없는 정렬: {by!r}")
        if (by, descending) == (self._sortBy, self._descending):
            return
        self._sortBy, self._descending = by, descending
        self._ordered.sort(key=self._key)
        value = f"{by}:{'desc' if descending else 'asc'}"
        settings.update_settings({"sortOrder": value})  # 저장된 값과 같으면 쓰지 않는다
        self.orderReset.emit()

    def setFilter(self, key: str) -> None:
        """칩을 고른다. 고른 칩을 다시 고르면 전체로 돌아간다(설계 §2)."""
        if key not in FILTER_KEYS:
            raise ValueError(f"알 수 없는 필터: {key!r}")
        if key == self._filter and key != "all":
            key = "all"
        if key == self._filter:
            return
        self._filter = key
        self.filterChanged.emit()

    def touch(self, item: ContentItem) -> None:
        """그 카드가 바뀌었을 수 있다 — 이 차례가 끝난 뒤 한 번 다시 본다(모아서)."""
        if item not in self._shown:
            return
        self._dirty[item] = None
        if not self._flushPending:
            self._flushPending = True
            QTimer.singleShot(0, self, self._flush)

    # ---- 안 ----

    def _key(self, item: ContentItem) -> tuple:
        """정렬 키 — (모르는 날짜를 끝으로 · 기준 값 · 추가된 차례). 내림차순은 뒤의 둘을 뒤집는다.

        업로드순의 오름차순은 최신 먼저다(오너 2026-11-06 — 프로토와 같다). 모르는 날짜는 방향과
        무관하게 끝이다. 같은 값끼리는 추가된 차례 — 내림차순이면 그것도 뒤집혀 목록 전체가 뒤집힌
        차례와 같다.
        """
        seq = self._seq[item]
        unknown = 0
        if self._sortBy == "title":
            value = item.title.casefold()
        elif self._sortBy == "channel":
            value = item.channel_name.casefold()
        elif self._sortBy == "upload":
            date = _upload_date(item)
            unknown = 0 if date is not None else 1
            value = _Reversed(date or "")  # 오름차순 = 최신 먼저
        elif self._sortBy == "state":
            value = _STATE_RANK.get(item.downloadState, 2)
        else:
            value = 0
        if self._descending:
            return (unknown, _Reversed(value), _Reversed(seq))
        return (unknown, value, seq)

    def _onInserted(self, item: ContentItem) -> None:
        """카드가 목록에 들어왔다 — 추가된 차례를 매기고 보이는 차례에 끼운다."""
        row = self._model.getRow(item)
        items = self._model.items
        before = items[row - 1] if row is not None and row > 0 else None
        after = items[row + 1] if row is not None and row + 1 < len(items) else None
        if after is None or after not in self._seq:
            self._nextSeq += 1
            seq = self._nextSeq
        elif before is None or before not in self._seq:
            seq = self._seq[after] - 1  # 맨 앞 교체 — 뒤 카드보다 앞
        else:
            seq = (self._seq[before] + self._seq[after]) / 2  # 같은 자리 교체(조회 중 → 카드, #124)
        self._seq[item] = seq
        key = self._key(item)
        self._ordered.insert(bisect_left(self._ordered, key, key=self._key), item)
        chip = filter_key(item)
        self._shown[item] = chip
        if item.downloadState == ItemState.LOADING:
            self._loading.add(item)
        self._counts[chip] += 1
        self._counts["all"] += 1
        self.countsChanged.emit()

    def _onRemoved(self, item: ContentItem) -> None:
        """카드가 목록에서 빠졌다 — 보이는 차례 · 칩 숫자에서 뺀다."""
        chip = self._shown.pop(item, None)
        if chip is None:
            return
        self._ordered.remove(item)
        self._loading.discard(item)
        self._seq.pop(item, None)
        self._dirty.pop(item, None)
        self._counts[chip] -= 1
        self._counts["all"] -= 1
        self.countsChanged.emit()

    def _flush(self) -> None:
        """모아 둔 카드의 칩이 바뀌었으면 숫자를 고치고, 상태순이면 그 카드만 옮긴다."""
        self._flushPending = False
        dirty, self._dirty = list(self._dirty), {}
        changed = False
        for item in dirty:
            old = self._shown.get(item)
            if old is None:
                continue  # 그 사이 지워졌다
            new = filter_key(item)
            if (item.downloadState == ItemState.LOADING) != (item in self._loading):
                # 조회 중 ↔ 대기 — 칩은 그대로 대기라 숫자는 같지만 받을 수 있는 카드 수가 바뀐다
                self._loading.symmetric_difference_update({item})
                changed = True
            if self._sortBy != "added":  # 상태순만이 아니다 — 대기 카드의 제목도 바뀐다(#288)
                self._reposition(item)
            if new == old:
                continue
            self._shown[item] = new
            self._counts[old] -= 1
            self._counts[new] += 1
            changed = True
            self.itemStateChanged.emit(item)
        if changed:
            self.countsChanged.emit()

    def _reposition(self, item: ContentItem) -> None:
        """그 카드 하나를 정렬 키에 맞는 자리로 옮긴다 — 자리가 그대로면 아무것도 하지 않는다."""
        index = self._ordered.index(item)
        del self._ordered[index]
        target = bisect_left(self._ordered, self._key(item), key=self._key)
        self._ordered.insert(target, item)
        if target != index:
            self.itemMoved.emit(item)
