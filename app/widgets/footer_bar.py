"""하단바 부품 — 모니터링 다섯 칸 · 실행 결과 글 (#504 — 셸 S2).

모니터링은 왼쪽에 전체 속도 · 남은 시간 · 연결 수 · 메모리 · 여유 공간을 둔다. 받는 것이 없으면
여유 공간만 보인다. 값은 뷰모델이 묶은 `MonitorSnapshot`을 받아 글로 적기만 한다 — 이 모듈은
엔진도 목록도 읽지 않는다. 1초마다 고쳐도 이 라벨들만 다시 그린다.

라벨의 최소폭은 1px다 — 값의 글 길이가 창 최소폭을 올리지 않는다. 좁은 폭에서 칸을 차례로 빼는
것은 셸 S3다.
"""

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from app.viewmodels.clock_text import clock_text
from app.viewmodels.footer_monitor import MonitorSnapshot
from app.widgets.widget import short_time

MONITOR_KEYS = ("speed", "remain", "conn", "mem", "free")  # 모니터링 칸의 화면 차례
MONITOR_GAP = 8  # 모니터링 칸 사이 간격(px) — 하단바 버튼 사이 간격과 같다(설계 §11.2)
_MEBIBYTE = 1024 * 1024  # 속도 · 메모리의 MB — 카드의 속도 글과 같다
_GIBIBYTE = 1024 * 1024 * 1024  # 여유 공간의 GB


class FooterMonitor(QWidget):
    """하단바 왼쪽 모니터링 — 칸마다 라벨 하나, 툴팁은 「이름 · 값」(남은 시간은 글이 다 말해 없음)."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(MONITOR_GAP)
        self.labels: dict[str, QLabel] = {}
        for key in MONITOR_KEYS:
            label = QLabel(self)
            label.setObjectName("footerMonitor")
            label.setProperty("key", key)
            label.setMinimumWidth(1)  # 글 길이가 창 최소폭을 올리지 않는다
            label.hide()
            layout.addWidget(label)
            self.labels[key] = label

    def setSnapshot(self, snapshot: MonitorSnapshot) -> None:
        """한 번의 값으로 칸의 글 · 툴팁 · 보임을 고친다."""
        texts = self._texts(snapshot)
        for key, label in self.labels.items():
            shown = texts.get(key)
            if shown is None:
                label.setVisible(False)
                continue
            text, tip = shown
            if label.text() != text:
                label.setText(text)
            if label.toolTip() != tip:
                label.setToolTip(tip)
            label.setVisible(True)

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
            connections = str(snapshot.connections)
            texts["conn"] = (
                self.tr("Connections {0}").format(connections),
                self.tr("Connections · {0}").format(connections),
            )
            if snapshot.memory is not None:
                memory = f"{snapshot.memory / _MEBIBYTE:,.0f} MB"
                texts["mem"] = (
                    self.tr("Memory {0}").format(memory),
                    self.tr("Memory · {0}").format(memory),
                )
        if snapshot.free is not None:
            free = f"{snapshot.free / _GIBIBYTE:,.1f} GB"
            texts["free"] = (
                self.tr("Free {0}").format(free),
                self.tr("Free space · {0}").format(free),
            )
        return texts


class RunResultLabel(QLabel):
    """하단바의 실행 결과 글 — 「완료 N」, 실패가 있을 때만 「 · 실패 N」. 다운로드 버튼 바로 왼쪽."""

    def __init__(self, parent: QWidget | None = None):
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
