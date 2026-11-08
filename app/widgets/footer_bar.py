"""하단바 부품 — 모니터링 다섯 칸 · 실행 결과 글 (#504 — 셸 S2).

모니터링은 왼쪽에 전체 속도 · 남은 시간 · 연결 수 · 메모리 · 여유 공간을 둔다. 받는 것이 없으면
여유 공간만 보인다. 값은 뷰모델이 묶은 `MonitorSnapshot`을 받아 글로 적기만 한다 — 이 모듈은
엔진도 목록도 읽지 않는다. 1초마다 고쳐도 이 라벨들만 다시 그린다.

라벨의 최소폭은 1px다 — 값의 글 길이가 창 최소폭을 올리지 않는다. 좁은 폭에서는 연결 · 메모리 ·
남은 시간 · 여유 공간 차례로 칸을 뺀다(`setDropped` — #529, 설계 §2 「좁은 폭」). 전체 속도는 끝까지 남는다.
"""

from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QPainter, QPaintEvent
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

import app.theme as theme
from app.viewmodels.clock_text import clock_text
from app.viewmodels.footer_monitor import MonitorSnapshot
from app.widgets.header_controls import text_width
from app.widgets.icons import action_pixmap
from app.widgets.widget import short_time

MONITOR_KEYS = ("speed", "remain", "conn", "mem", "free")  # 모니터링 칸의 화면 차례
# 이름 글 대신 아이콘을 두는 칸 — 뜻은 툴팁(설계 §2 「연결 · 메모리 · 여유에는 툴팁으로 뜻」, 프로토 11v, #536)
MONITOR_ICONS = {"conn": "link", "mem": "memory", "free": "disk"}
ICON_GAP = 4  # 칸 아이콘 ↔ 값(px) — 설계 §11.2, 프로토 MonitorLabel
DROP_ORDER = ("conn", "mem", "remain", "free")  # 좁은 폭에서 빼는 칸 차례 — 설계 §2
SPEED_SAMPLE = "↓ 999.9 MB/s"  # 창 최소폭에 넣는 전체 속도 칸의 글 — 값과 무관한 고정 견본
MONITOR_GAP = 8  # 모니터링 칸 사이 간격(px) — 하단바 버튼 사이 간격과 같다(설계 §11.2)
_MEBIBYTE = 1024 * 1024  # 속도 · 메모리의 MB — 카드의 속도 글과 같다
_GIBIBYTE = 1024 * 1024 * 1024  # 여유 공간의 GB


class MonitorLabel(QLabel):
    """하단바 모니터링 한 칸 — 아이콘(16px)이 있으면 그 오른쪽에 값 (#536, 프로토 MonitorLabel).

    아이콘 자리는 왼쪽 안쪽 여백으로 잡는다 — 크기 힌트가 아이콘 · 간격 · 값 글 폭을 함께 센다. 아이콘 색은 그릴
    때마다 흐린 글 토큰을 읽는다(실행 중 테마 전환 #247).
    """

    def __init__(self, icon: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.icon = icon  # icons의 이름 — 빈 글이면 글만
        if icon:
            self.setContentsMargins(theme.METRICS["narrowIcon"] + ICON_GAP, 0, 0, 0)

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        if not self.icon:
            return
        tokens = theme.current_tokens()
        side = theme.METRICS["narrowIcon"]
        pixmap = action_pixmap(self.icon, tokens["textMuted"], side, self.devicePixelRatioF())
        painter = QPainter(self)
        painter.drawPixmap(0, (self.height() - side) // 2, pixmap)
        painter.end()


class FooterMonitor(QWidget):
    """하단바 왼쪽 모니터링 — 칸마다 라벨 하나, 툴팁은 「이름 · 값」(남은 시간은 글이 다 말해 없음).

    연결 · 메모리 · 여유는 이름 글 대신 아이콘 + 값이다(#536) — 이름은 툴팁이 말한다.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(MONITOR_GAP)
        self.labels: dict[str, QLabel] = {}
        self._present: set[str] = set()  # 값이 있어 보일 수 있는 칸 — 마지막 setSnapshot
        self._dropped = 0  # 좁은 폭에서 뺀 칸 수 — DROP_ORDER 앞에서부터
        for key in MONITOR_KEYS:
            label = MonitorLabel(MONITOR_ICONS.get(key, ""), self)
            label.setObjectName("footerMonitor")
            label.setProperty("key", key)
            label.setMinimumWidth(1)  # 글 길이가 창 최소폭을 올리지 않는다
            label.hide()
            layout.addWidget(label)
            self.labels[key] = label

    def setSnapshot(self, snapshot: MonitorSnapshot) -> None:
        """한 번의 값으로 칸의 글 · 툴팁 · 보임을 고친다 — 좁은 폭에서 뺀 칸은 글만 고치고 숨긴 채 둔다."""
        texts = self._texts(snapshot)
        self._present = set(texts)
        for key, label in self.labels.items():
            shown = texts.get(key)
            if shown is None:
                continue
            text, tip = shown
            if label.text() != text:
                label.setText(text)
            if label.toolTip() != tip:
                label.setToolTip(tip)
        self._applyVisible()

    def dropped(self) -> int:
        """좁은 폭에서 뺀 칸 수."""
        return self._dropped

    def setDropped(self, count: int) -> None:
        """`DROP_ORDER` 앞에서부터 `count`칸을 뺀다(0이면 다 보인다)."""
        if count == self._dropped:
            return
        self._dropped = count
        self._applyVisible()

    def needWidth(self, dropped: int) -> int:
        """`dropped`칸을 뺐을 때 보일 칸의 폭과 사이 간격 — 지금 뺀 칸 수 · 보임과 무관하다.

        칸 폭은 지금 값의 글 폭(라벨 크기 힌트)이다. 값이 없는 칸은 세지 않는다.
        """
        widths = [self.labels[key].sizeHint().width() for key in self._keysFor(dropped)]
        return sum(widths) + MONITOR_GAP * max(0, len(widths) - 1)

    def sampleWidth(self) -> int:
        """칸을 끝까지 뺀 모양(전체 속도 하나)의 폭 — 값과 무관한 견본 글(`SPEED_SAMPLE`)로 잰다."""
        label = self.labels["speed"]
        label.ensurePolished()
        return text_width(label.fontMetrics(), SPEED_SAMPLE)

    def _keysFor(self, dropped: int) -> list[str]:
        """`dropped`칸을 뺐을 때 보일 칸 — 화면 차례."""
        gone = DROP_ORDER[:dropped]
        return [key for key in MONITOR_KEYS if key in self._present and key not in gone]

    def _applyVisible(self) -> None:
        shown = set(self._keysFor(self._dropped))
        for key, label in self.labels.items():
            label.setVisible(key in shown)

    def shownKeys(self) -> list[str]:
        """지금 보이는 칸의 키 — 화면 차례."""
        return [key for key, label in self.labels.items() if not label.isHidden()]

    def _texts(self, snapshot: MonitorSnapshot) -> dict[str, tuple[str, str]]:
        """칸 → (글, 툴팁). 보이지 않을 칸은 빠진다."""
        texts: dict[str, tuple[str, str]] = {}
        if snapshot.receiving:
            speed = f"{snapshot.speed / _MEBIBYTE:.1f} MB/s"
            texts["speed"] = (f"↓ {speed}", self.tr("Speed · {0}").format(speed))
            if snapshot.remaining_seconds is not None:
                remain = short_time(clock_text(snapshot.remaining_seconds))
                # 카드의 남은 시간과 같은 글 — 카드의 번역(ContentItemWidget 컨텍스트)을 그대로 쓴다
                left = QCoreApplication.translate("ContentItemWidget", "{0} left").format(remain)
                texts["remain"] = (left, "")
            # 연결 · 메모리 · 여유는 아이콘 + 값 — 이름은 툴팁에(#536)
            connections = str(snapshot.connections)
            texts["conn"] = (connections, self.tr("Connections · {0}").format(connections))
            if snapshot.memory is not None:
                memory = f"{snapshot.memory / _MEBIBYTE:,.0f} MB"
                texts["mem"] = (memory, self.tr("Memory · {0}").format(memory))
        if snapshot.free is not None:
            free = f"{snapshot.free / _GIBIBYTE:,.1f} GB"
            texts["free"] = (free, self.tr("Free space · {0}").format(free))
        return texts


class RunResultLabel(QLabel):
    """하단바의 실행 결과 글 — 「완료 N」, 실패가 있을 때만 「 · 실패 N」. 다운로드 버튼 바로 왼쪽."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("footerResult")
        self.setMinimumWidth(1)  # 글 길이가 창 최소폭을 올리지 않는다
        self.hide()

    def showResult(self, shown: bool, finished: int, failed: int) -> None:
        """결과를 보이거나 숨긴다 — 숫자는 목록 전체 카드의 지금 상태."""
        if not shown:
            self.hide()
            return
        text = self.tr("Completed {0}").format(finished)
        if failed:
            text += " · " + self.tr("Failed {0}").format(failed)
        self.setText(text)
        self.show()

    def sampleWidth(self) -> int:
        """창 최소폭에 넣는 결과 글의 폭 — 숫자 세 자리에 실패까지 있는 견본으로 잰다(값과 무관)."""
        sample = self.tr("Completed {0}").format(999) + " · " + self.tr("Failed {0}").format(999)
        self.ensurePolished()
        return text_width(self.fontMetrics(), sample)
