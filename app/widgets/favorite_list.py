"""설정 창 다운로드 탭의 즐겨찾기 목록 — 별칭 · 흐린 경로 한 줄씩, 고른 줄 하나 (#646).

목록은 Tab 정지 하나다. 줄을 누르거나 위 · 아래 화살표(Home · End)로 고른다. 줄 자체는 keyboard focus를 받지 않는다 —
줄마다 Tab이 멈추면 즐겨찾기 10개를 지나야 다음 칸에 닿는다. 끌어 옮기기는 없다(설계 §14.6 — 순서는 위 ·
아래 버튼). 모양은 전역 QSS(`QFrame#favoriteList` · `QFrame#favoriteRow` · `QLabel#favoritePath`)가 칠한다 —
입력 칸과 같은 면 · 테두리이고 focus를 받으면 테두리가 강조색이다.

목록 값은 들고 있지 않다 — 보일 줄(`setEntries`)과 고른 줄 번호만 든다. 더하기 · 순서 · 지우기는 설정 창이 한다.
"""

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

import app.theme as theme
from app.widgets.eliding_label import ElidingLabel, PathLabel
from app.widgets.widget import path_display_parts

NO_SELECTION = -1  # 고른 줄이 없다(목록이 비었을 때)
MIN_CHARS = 24  # 목록 최소폭(평균 글자 폭 단위) — 짧은 별칭 하나와 접은 경로 끝이 함께 보이는 폭


class _FavoriteRow(QFrame):
    """즐겨찾기 한 줄 — 별칭(말줄임) · 흐린 경로(접는 말줄임). 누르면 목록에 알린다."""

    def __init__(self, owner: "FavoriteList", index: int, name: str, path: str) -> None:
        super().__init__(owner)
        self.setObjectName("favoriteRow")
        self.setProperty("selected", False)
        self._owner = owner
        self._index = index
        layout = QHBoxLayout(self)
        pad = theme.METRICS["menuRowPad"]
        layout.setContentsMargins(pad, theme.METRICS["fieldPadY"], pad, theme.METRICS["fieldPadY"])
        layout.setSpacing(theme.METRICS["menuIconGap"])
        self.nameLabel = ElidingLabel(self, tip_when_elided=True)
        self.nameLabel.setObjectName("favoriteName")
        self.nameLabel.setText(name)
        self.pathLabel = PathLabel(self)
        self.pathLabel.setObjectName("favoritePath")
        self.pathLabel.setPathParts(*path_display_parts(path))
        self.pathLabel.setToolTip(path)
        layout.addWidget(self.nameLabel, 0)
        layout.addWidget(self.pathLabel, 1)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """왼쪽 단추로 누르면 이 줄을 고르고 목록에 focus를 준다."""
        if event.button() == Qt.MouseButton.LeftButton:
            self._owner.setFocus(Qt.FocusReason.MouseFocusReason)
            self._owner.select(self._index)
            event.accept()
            return
        super().mousePressEvent(event)


class FavoriteList(QFrame):
    """즐겨찾기 줄 목록 — 고른 줄이 바뀌면 `selectionChanged(번호)`를 낸다(없으면 `NO_SELECTION`)."""

    selectionChanged = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("favoriteList")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._layout = QVBoxLayout(self)
        inset = theme.METRICS["menuVPad"]
        self._layout.setContentsMargins(inset, inset, inset, inset)
        self._layout.setSpacing(0)
        self.emptyLabel = QLabel(self)
        self.emptyLabel.setObjectName("favoriteEmpty")
        pad = theme.METRICS["menuRowPad"]
        self.emptyLabel.setContentsMargins(pad, theme.METRICS["fieldPadY"], pad, 0)
        self._layout.addWidget(self.emptyLabel)
        self._layout.addStretch(1)
        self._rows: list[_FavoriteRow] = []
        self._selected = NO_SELECTION

    def minimumSizeHint(self) -> QSize:
        """최소폭은 글자 `MIN_CHARS`개 폭이다 — 줄 라벨은 말줄임이라 최소폭이 거의 0이어서, 그대로 두면 탭
        줄이 짧은 언어(한국어)의 창 최소폭에서 별칭이 「…」만 남는다(offscreen 스크린샷)."""
        hint = super().minimumSizeHint()
        width = self.fontMetrics().averageCharWidth() * MIN_CHARS
        return QSize(max(hint.width(), width), hint.height())

    def firstLineCenter(self) -> int:
        """목록 위쪽 끝에서 첫 줄(빈 목록이면 안내 글) 글자 가운데까지의 높이(px).

        설정 창이 즐겨찾기 행 라벨을 첫 줄 글자에 맞출 때 쓴다(#646). 테두리 · 안쪽 여백 · 줄 위 여백 ·
        글자 높이의 반이다 — 줄과 안내 글은 위 여백(`fieldPadY`)과 글꼴이 같다.
        """
        return (
            self.contentsRect().top()
            + self._layout.contentsMargins().top()
            + theme.METRICS["fieldPadY"]
            + self.fontMetrics().height() // 2
        )

    def setEmptyText(self, text: str) -> None:
        """목록이 비었을 때 보이는 흐린 안내 글(번역은 부르는 쪽)."""
        self.emptyLabel.setText(text)

    def setEntries(self, entries: list[dict], selected: int) -> None:
        """줄을 `entries`(`{"name", "path"}`) 차례로 다시 만들고 `selected` 번호의 줄을 고른다.

        번호가 범위 밖이면 마지막 줄을 고른다(목록이 비었으면 고른 줄 없음).
        """
        for row in self._rows:
            self._layout.removeWidget(row)
            row.hide()
            row.deleteLater()
        self._rows = []
        for index, entry in enumerate(entries):
            row = _FavoriteRow(self, index, entry["name"], entry["path"])
            self._layout.insertWidget(index, row)
            self._rows.append(row)
        self.emptyLabel.setVisible(not entries)
        self._selected = NO_SELECTION
        self.select(min(selected, len(entries) - 1))

    def count(self) -> int:
        """줄 수."""
        return len(self._rows)

    def row(self, index: int) -> _FavoriteRow:
        """그 번호의 줄 위젯(테스트 · 설정 창이 본다)."""
        return self._rows[index]

    def selected(self) -> int:
        """고른 줄 번호 — 없으면 `NO_SELECTION`."""
        return self._selected

    def select(self, index: int) -> None:
        """그 번호의 줄을 고른다 — 범위 밖이면 고른 줄 없음. 바뀌었을 때만 알린다."""
        if not 0 <= index < len(self._rows):
            index = NO_SELECTION
        if index == self._selected:
            return
        self._selected = index
        for number, row in enumerate(self._rows):
            row.setProperty("selected", number == index)
            theme.repolish(row)
        self.selectionChanged.emit(index)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """위 · 아래 화살표는 한 줄, Home · End는 처음 · 끝 줄을 고른다. 그 밖의 키는 넘긴다."""
        if not self._rows:
            super().keyPressEvent(event)
            return
        current = max(self._selected, 0)
        targets = {
            Qt.Key.Key_Up: current - 1,
            Qt.Key.Key_Down: current + 1,
            Qt.Key.Key_Home: 0,
            Qt.Key.Key_End: len(self._rows) - 1,
        }
        key = Qt.Key(event.key())
        if key not in targets or event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier:
            super().keyPressEvent(event)
            return
        self.select(max(0, min(targets[key], len(self._rows) - 1)))
        event.accept()
