"""헤더바 부품 — 펼침 버튼 · 하나만 고르는 메뉴 · 저장 위치 버튼 · 정렬 버튼 · 상태 칩 (#495 — 셸 S1).

펼침 버튼은 `QToolButton` + `QMenu`다(설계 — `QComboBox` 아님). 글 · 도형 · ▾는 이 모듈이
`paintEvent`에서 그린다: 바탕 · 테두리는 전역 QSS가 칠하고, 도형 색은 그릴 때마다 theme 토큰을
새로 읽어 실행 중 테마 전환(#247)을 따른다. 메뉴는 열 때마다 다시 채운다 — 없는 경로 표시 ·
도형 색이 늘 지금 값이다.
"""

from PySide6.QtCore import QPointF, QRect, QSize, Qt, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPolygonF
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QMenu,
    QPushButton,
    QStyle,
    QStyleOptionToolButton,
    QStylePainter,
    QToolButton,
    QWidget,
)

import app.theme as theme
from app.viewmodels.card_order import FILTER_KEYS
from app.viewmodels.save_location import SaveLocations, folder_name
from app.widgets.icons import action_pixmap

BUTTON_PADDING = 10  # 펼침 버튼 안쪽 좌우 여백(px) — 프로토 v8 저장 위치 버튼
GLYPH_GAP = 6  # 앞 도형 ↔ 글(px)
CARET_WIDTH = 8  # ▾ 폭(px)
CARET_GAP = 6  # 글 ↔ ▾(px)
BUTTON_VPAD = 7  # 위아래(px) — QPushButton의 QSS padding 6 + 테두리 1과 같아 한 줄의 높이가 맞는다
WIDTH_SAMPLE = "Downloads"  # 저장 위치 버튼 폭의 기준 글 — 이 이름이 줄지 않고 들어간다(프로토 11o)
MENU_DETAIL_GAP = 16  # 메뉴 줄의 글 ↔ 흐린 글(px)


class ChoiceMenu(QMenu):
    """하나만 고르는 메뉴 — 고른 항목에만 ✓, 고르지 않은 항목에는 빈 상자를 그리지 않는다(설계 §2).

    체크형 항목(`setCheckable`)은 쓰지 않는다 — 스타일이 고르지 않은 항목에도 빈 상자를 그린다.
    대신 같은 크기의 아이콘 자리에 ✓ 도형 또는 빈 그림을 둔다. 항목 글의 탭 뒤(`"글\\t흐린 글"`)는
    Qt가 단축키 칸으로 재어 폭을 잡게 두고, 그리기에서는 탭 앞만 Qt가 그린 뒤 탭 뒤를 이 메뉴가
    흐린 색으로 그린다.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setToolTipsVisible(True)

    def addChoice(self, text: str, key: object, detail: str = "") -> QAction:
        """고르는 항목을 더한다 — `key`는 `mark`와 테스트가 항목을 찾는 값이다."""
        action = self.addAction(f"{text}\t{detail}" if detail else text)
        action.setData(key)
        action.setIcon(_blankIcon())
        return action

    def addCommand(self, text: str) -> QAction:
        """고르는 항목이 아닌 명령 — 같은 크기의 빈 아이콘으로 글 자리를 맞춘다."""
        action = self.addAction(text)
        action.setIcon(_blankIcon())
        return action

    def addHeader(self, text: str) -> QAction:
        """묶음 머리 — 꺼진 항목이라 고를 수 없고 키보드가 건너뛴다."""
        action = self.addAction(text)
        action.setEnabled(False)
        action.setProperty("menuHeader", True)
        return action

    def choices(self) -> list[QAction]:
        """고르는 항목(머리 · 명령 · 구분선 제외) — 메뉴 차례."""
        return [a for a in self.actions() if a.data() is not None]

    def mark(self, *keys: object) -> None:
        """그 키의 항목에만 ✓를 그린다(정렬처럼 한 메뉴에 묶음이 둘이면 키도 둘)."""
        tokens = theme.current_tokens()
        size = _menuIconSize(self)
        for action in self.choices():
            marked = action.data() in keys
            action.setProperty("marked", marked)
            if marked:
                pixmap = action_pixmap("check", tokens["text"], size, self.devicePixelRatioF())
                action.setIcon(QIcon(pixmap))
            else:
                action.setIcon(_blankIcon())

    def markedKeys(self) -> list:
        """✓를 그린 항목의 키 — 메뉴 차례."""
        return [a.data() for a in self.choices() if a.property("marked")]

    def initStyleOption(self, option, action) -> None:
        super().initStyleOption(option, action)
        option.text = option.text.split("\t")[0]  # 탭 뒤(흐린 글)는 paintEvent가 그린다

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        tokens = theme.current_tokens()
        metrics = self.fontMetrics()
        painter = None
        for action in self.actions():
            text = action.text()
            if "\t" not in text:
                continue
            if painter is None:
                painter = QPainter(self)
                painter.setFont(self.font())
            head, detail = text.split("\t", 1)
            rect = self.actionGeometry(action).adjusted(0, 0, -BUTTON_PADDING, 0)
            if action is self.activeAction() and action.isEnabled():
                color = tokens["onAccent"]  # 강조색으로 채운 줄 안의 글
            elif action.isEnabled():
                color = tokens["textMuted"]
            else:
                color = tokens["textDisabled"]
            painter.setPen(QColor(color))
            room = rect.width() - metrics.horizontalAdvance(head) - 3 * MENU_DETAIL_GAP
            shown = metrics.elidedText(detail, Qt.TextElideMode.ElideMiddle, max(20, room))
            painter.drawText(rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, shown)
        if painter is not None:
            painter.end()


def _menuIconSize(menu: QMenu) -> int:
    """메뉴 항목 아이콘 한 변 — 스타일이 정한 값."""
    return menu.style().pixelMetric(QStyle.PixelMetric.PM_SmallIconSize, None, menu)


def _blankIcon() -> QIcon:
    """✓ 자리를 비워 두는 빈 아이콘 — 고른 항목과 글 자리가 같다."""
    return QIcon()


class DropButton(QToolButton):
    """누르면 메뉴가 펼쳐지는 글 버튼 — [도형] 글 ▾ (#495).

    글은 `setFullText`로 주고, 폭에 맞춰 가운데를 줄여 그린다. 도형(`glyph`)은 `app.widgets.icons`의
    이름이다(없으면 글만). 폭은 호출자가 `setFixedWidth`로 건다 — 고른 값에 따라 버튼 폭이 바뀌면
    헤더바가 움직인다.
    """

    def __init__(self, glyph: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self._glyph = glyph
        self._full = ""
        self.setProperty("role", "drop")  # 전역 QSS의 바탕 · 테두리
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def setFullText(self, text: str) -> None:
        """보일 글(줄이기 전). 접근성 이름도 이 글이다."""
        self._full = text
        self.setAccessibleName(text)
        self.update()

    def fullText(self) -> str:
        """줄이기 전 글."""
        return self._full

    def shownText(self) -> str:
        """지금 폭에서 그리는 글(가운데 줄임 뒤)."""
        return self.fontMetrics().elidedText(
            self._full, Qt.TextElideMode.ElideMiddle, max(0, self._textRect().width())
        )

    def widthFor(self, text: str) -> int:
        """그 글이 줄지 않고 들어가는 버튼 폭 — 테두리 · 여백 · 도형 · ▾를 더한다."""
        self.ensurePolished()
        return self._chrome() + self.fontMetrics().horizontalAdvance(text)

    def sizeHint(self) -> QSize:
        self.ensurePolished()
        height = self.fontMetrics().height() + 2 * BUTTON_VPAD
        return QSize(self.widthFor(self._full), height)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def _chrome(self) -> int:
        """글 밖의 폭 — 테두리 둘 · 좌우 여백 · 도형과 그 간격 · ▾와 그 간격."""
        glyph = theme.METRICS["actionGlyph"] + GLYPH_GAP if self._glyph else 0
        return 2 + 2 * BUTTON_PADDING + glyph + CARET_GAP + CARET_WIDTH

    def _textRect(self) -> QRect:
        """글을 그리는 칸."""
        left = 1 + BUTTON_PADDING
        if self._glyph:
            left += theme.METRICS["actionGlyph"] + GLYPH_GAP
        right = 1 + BUTTON_PADDING + CARET_WIDTH + CARET_GAP
        return QRect(left, 0, self.width() - left - right, self.height())

    def paintEvent(self, event) -> None:
        option = QStyleOptionToolButton()
        self.initStyleOption(option)
        option.text = ""
        option.icon = QIcon()
        option.features &= ~QStyleOptionToolButton.ToolButtonFeature.HasMenu  # ▾는 아래에서 그린다
        painter = QStylePainter(self)
        painter.drawComplexControl(QStyle.ComplexControl.CC_ToolButton, option)
        tokens = theme.current_tokens()
        enabled = self.isEnabled()
        muted = QColor(tokens["textMuted" if enabled else "textDisabled"])
        if self._glyph:
            size = theme.METRICS["actionGlyph"]
            pixmap = action_pixmap(self._glyph, muted.name(), size, self.devicePixelRatioF())
            painter.drawPixmap(1 + BUTTON_PADDING, (self.height() - size) // 2, pixmap)
        painter.setPen(QColor(tokens["text" if enabled else "textDisabled"]))
        painter.drawText(self._textRect(), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         self.shownText())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(muted)
        right = self.width() - 1 - BUTTON_PADDING
        middle = self.height() / 2
        half = CARET_WIDTH / 2
        painter.drawPolygon(
            QPolygonF(
                [
                    QPointF(right - CARET_WIDTH, middle - half / 2),
                    QPointF(right, middle - half / 2),
                    QPointF(right - half, middle + half / 2 + 1),
                ]
            )
        )
        painter.end()


class SaveLocationButton(DropButton):
    """헤더바 저장 위치 버튼 — 「📁 폴더 이름 ▾」, 누르면 저장 위치 메뉴 (#495 — 설계 §2).

    폭은 기준 글(`WIDTH_SAMPLE`)로 한 번 정한다 — 고른 위치 · 기본 위치와 무관하다(바뀌면 헤더바와
    창 최소폭이 따라 움직인다). 긴 이름은 가운데를 줄이고 툴팁에 전체 경로를 보인다. 지금 위치가
    즐겨찾기면 별칭을 보인다.

    메뉴 차례: 지금 위치(✓) · 즐겨찾기(★ 별칭 + 흐린 경로) · 최근 · 구분선 · 「지금 위치를 즐겨찾기에」
    · 「폴더 찾기…」 · 「기본 위치로」. 없는 경로는 흐리게 「(없음)」을 붙이고 고를 수 없다.

    시그널:
        errorOccurred(str): 고르기 · 즐겨찾기 더하기가 막혔다 — 번역된 까닭
    """

    errorOccurred = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__("folder", parent)
        self._locations: SaveLocations | None = None
        self.menuWidget = ChoiceMenu(self)
        self.menuWidget.aboutToShow.connect(self._fillMenu)
        self.setMenu(self.menuWidget)

    def bind(self, locations: SaveLocations) -> None:
        """저장 위치 뷰모델과 잇는다 — 바뀔 때마다 글 · 툴팁을 다시 쓴다."""
        self._locations = locations
        locations.changed.connect(self._refresh)
        self.applyWidth()
        self._refresh()

    def applyWidth(self) -> None:
        """기준 글이 들어가는 폭을 건다 — 글꼴이 바뀌면 다시 부른다."""
        self.setFixedWidth(self.widthFor(WIDTH_SAMPLE))

    def _refresh(self) -> None:
        """글(별칭 또는 폴더 이름) · 툴팁(전체 경로)을 지금 위치로."""
        locations = self._locations
        self.setFullText(locations.display_name())
        self.setToolTip(locations.current())

    def _fillMenu(self) -> None:
        """메뉴를 지금 값으로 다시 채운다 — 열 때마다(없는 경로 · 도형 색이 늘 지금 값)."""
        menu = self.menuWidget
        menu.clear()
        locations = self._locations
        entries = locations.menu_entries()
        missing_text = self.tr("(missing)")
        headed: set[str] = set()
        for entry in entries:
            if entry.kind == "favorite" and "favorite" not in headed:
                menu.addHeader(self.tr("Favorites"))
                headed.add("favorite")
            if entry.kind == "recent" and "recent" not in headed:
                menu.addHeader(self.tr("Recent"))
                headed.add("recent")
            suffix = f"  {missing_text}" if entry.missing else ""
            if entry.kind == "favorite":
                action = menu.addChoice(f"★ {entry.name}", ("favorite", entry.path),
                                        entry.path + suffix)
            else:
                action = menu.addChoice(entry.path + suffix, (entry.kind, entry.path))
            action.setProperty("missing", entry.missing)
            action.setEnabled(not entry.missing or entry.kind == "current")
            action.triggered.connect(lambda _=False, p=entry.path: self._choose(p))
        menu.mark(("current", locations.current()))
        menu.addSeparator()
        add = menu.addCommand(self.tr("☆ Add current folder to favorites"))
        add.setObjectName("addFavoriteAction")
        add.setEnabled(locations.can_add_favorite())
        add.triggered.connect(self._addFavorite)
        find = menu.addCommand(self.tr("Find folder…"))
        find.setObjectName("findFolderAction")
        find.triggered.connect(self._findFolder)
        reset = menu.addCommand(self.tr("Default location"))
        reset.setObjectName("defaultLocationAction")
        reset.setToolTip(locations.default())
        reset.triggered.connect(locations.reset)

    def _choose(self, path: str) -> None:
        """그 위치를 고른다 — 그 사이 폴더가 사라졌으면 고르지 않고 알린다."""
        if not self._locations.choose(path):
            self.errorOccurred.emit(self.tr("The folder does not exist."))

    def _findFolder(self) -> None:
        """폴더 선택 창에서 고른 폴더를 지금 위치로."""
        path = QFileDialog.getExistingDirectory(
            self, self.tr("Find folder"), self._locations.current()
        )
        if path:
            self._choose(path)

    def _addFavorite(self) -> None:
        """작은 창에서 별칭을 받아(기본값 = 폴더 이름) 지금 위치를 즐겨찾기에 더한다."""
        path = self._locations.current()
        name, ok = QInputDialog.getText(
            self, self.tr("Add to favorites"), self.tr("Name") + "\n" + path, text=folder_name(path)
        )
        if not ok:
            return
        error = self._locations.add_favorite(name, path)
        if error:
            self.errorOccurred.emit(error)


class SortButton(DropButton):
    """헤더바 2줄 정렬 버튼 — 고른 정렬 이름 ▾, 누르면 정렬 메뉴 (#495 — 설계 §2 「정렬 버튼」).

    메뉴: 추가된 순서 · 제목순 · 채널명순 · 업로드순 · 상태순 · 구분선 · 오름차순 · 내림차순. 고른
    기준 · 방향에만 ✓. 폭은 가장 긴 이름이 들어가는 고정 폭이다 — 고른 정렬과 무관하다(프로토 11k).
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__("", parent)
        self._order = None
        self.menuWidget = ChoiceMenu(self)
        self.menuWidget.aboutToShow.connect(self._fillMenu)
        self.setMenu(self.menuWidget)

    def names(self) -> dict[str, str]:
        """정렬 기준 키 → 보이는 이름(번역)."""
        return {
            "added": self.tr("Added order"),
            "title": self.tr("Title"),
            "channel": self.tr("Channel"),
            "upload": self.tr("Upload date"),
            "state": self.tr("Status"),
        }

    def bind(self, order) -> None:
        """순서 뷰모델과 잇는다 — 정렬이 바뀌면 글을 다시 쓴다."""
        self._order = order
        order.orderReset.connect(self._refresh)
        self.applyWidth()
        self._refresh()

    def applyWidth(self) -> None:
        """가장 긴 이름이 들어가는 폭을 건다 — 글꼴 · 번역이 바뀌면 다시 부른다."""
        self.setFixedWidth(max(self.widthFor(name) for name in self.names().values()))

    def _refresh(self) -> None:
        self.setFullText(self.names()[self._order.sortBy()])

    def _fillMenu(self) -> None:
        menu = self.menuWidget
        menu.clear()
        order = self._order
        for key, name in self.names().items():
            action = menu.addChoice(name, ("by", key))
            action.triggered.connect(lambda _=False, k=key: order.setSort(k, order.isDescending()))
        menu.addSeparator()
        for descending, name in ((False, self.tr("Ascending")), (True, self.tr("Descending"))):
            action = menu.addChoice(name, ("direction", "desc" if descending else "asc"))
            action.triggered.connect(lambda _=False, d=descending: order.setSort(order.sortBy(), d))
        menu.mark(("by", order.sortBy()), ("direction", "desc" if order.isDescending() else "asc"))


CHIP_COUNT_RESERVE = 999  # 칩 폭을 잡는 숫자 — 세 자리까지 칩 · 헤더바 · 창 최소폭이 그대로다(옛 카운트의 999/999 예약)


class FilterChips(QWidget):
    """헤더바 2줄 상태 필터 — 상태별 숫자 칩(전체 · 대기 · 진행 · 일시정지 · 완료 · 실패) (#495).

    고른 칩만 눌린 상태다. 고른 칩을 다시 누르면 전체로 돌아간다(설계 §2 — 뜻은 `CardOrder.setFilter`).
    실패 칩은 실패 카드가 있을 때 강조한다(`alert` 속성 → 전역 QSS). 칩 폭은 숫자 세 자리
    (`CHIP_COUNT_RESERVE`)가 들어가는 고정 폭이다 — 숫자가 바뀌어도 줄이 움직이지 않는다.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._order = None
        self.chips: dict[str, QPushButton] = {}
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        for key in FILTER_KEYS:
            chip = QPushButton(self)
            chip.setObjectName(f"filterChip_{key}")
            chip.setProperty("role", "filter")
            chip.setProperty("alert", False)
            chip.setCheckable(True)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.clicked.connect(lambda _=False, k=key: self._order.setFilter(k))
            layout.addWidget(chip)
            self.chips[key] = chip

    def names(self) -> dict[str, str]:
        """칩 키 → 보이는 상태 이름(번역)."""
        return {
            "all": self.tr("All"),
            "waiting": self.tr("Waiting"),
            "running": self.tr("In progress"),
            "paused": self.tr("Paused"),
            "finished": self.tr("Finished"),
            "failed": self.tr("Failed"),
        }

    def bind(self, order) -> None:
        """순서 뷰모델과 잇는다 — 숫자 · 고른 칩이 바뀔 때마다 다시 쓴다."""
        self._order = order
        order.countsChanged.connect(self._refreshCounts)
        order.filterChanged.connect(self._refreshChecked)
        self.applyWidth()
        self._refreshCounts()
        self._refreshChecked()

    def applyWidth(self) -> None:
        """칩마다 「이름 999」가 들어가는 폭을 건다 — 글꼴 · 번역이 바뀌면 다시 부른다."""
        for key, name in self.names().items():
            chip = self.chips[key]
            shown = chip.text()
            chip.setText(f"{name} {CHIP_COUNT_RESERVE}")
            chip.ensurePolished()
            chip.setFixedWidth(chip.sizeHint().width())
            chip.setText(shown)

    def _refreshCounts(self) -> None:
        counts = self._order.counts()
        for key, name in self.names().items():
            chip = self.chips[key]
            chip.setText(f"{name} {counts[key]}")
            chip.setProperty("count", counts[key])
        failed = self.chips["failed"]
        alert = counts["failed"] > 0
        if failed.property("alert") != alert:
            failed.setProperty("alert", alert)
            theme.repolish(failed)

    def _refreshChecked(self) -> None:
        chosen = self._order.filterKey()
        for key, chip in self.chips.items():
            chip.setChecked(key == chosen)
