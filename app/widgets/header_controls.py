"""헤더바 부품 — 펼침 버튼 · 하나만 고르는 메뉴 · 저장 위치 버튼 · 정렬 버튼 · 상태 칩 · 도는 표시 버튼 (#495 — 셸 S1).

펼침 버튼은 `QToolButton` + `QMenu`다(설계 — `QComboBox` 아님). 글 · 도형 · ▾는 이 모듈이
`paintEvent`에서 그린다: 바탕 · 테두리는 전역 QSS가 칠하고, 도형 색은 그릴 때마다 theme 토큰을
새로 읽어 실행 중 테마 전환(#247)을 따른다. 메뉴는 열 때마다 다시 채운다 — 없는 경로 표시 ·
도형 색이 늘 지금 값이다.
"""

import math

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontMetrics,
    QFontMetricsF,
    QIcon,
    QPainter,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QMenu,
    QProxyStyle,
    QPushButton,
    QStyle,
    QStyleOptionMenuItem,
    QStyleOptionToolButton,
    QStylePainter,
    QToolButton,
    QWidget,
)

import app.theme as theme
from app.viewmodels.card_order import FILTER_KEYS, SORT_KEYS
from app.viewmodels.save_location import SaveLocations, folder_name
from app.widgets.widget import abbreviate_path, path_display_parts
from app.widgets.icons import action_pixmap
from app.widgets.tip_bubble import hide_tip, show_tip

BUTTON_PADDING = 10  # 펼침 버튼 안쪽 좌우 여백(px) — 프로토 v8 저장 위치 버튼
GLYPH_GAP = 6  # 앞 도형 ↔ 글(px)
CARET_WIDTH = 8  # ▾ 폭(px)
CARET_GAP = 6  # 글 ↔ ▾(px)
BUTTON_VPAD = 7  # 위아래(px) — QPushButton의 QSS padding 6 + 테두리 1과 같아 한 줄의 높이가 맞는다
WIDTH_SAMPLE = "Downloads"  # 저장 위치 버튼 폭의 기준 글 — 이 이름이 줄지 않고 들어간다(프로토 11o)
MENU_DETAIL_GAP = 16  # 메뉴 줄의 글 ↔ 흐린 글(px)
MENU_WINDOW_MARGIN = (
    24  # 저장 위치 메뉴 폭의 상한 = 창 폭 − 이 값(px) — 프로토 PATH_MENU_MARGIN(inbox 71)
)
MENU_MIN_LIMIT = 200  # 창이 아주 좁아도 메뉴 폭 상한은 이만큼은 둔다(px) — 프로토와 같다


MENU_SEP = "\x1f"  # 항목 글의 이름 ↔ 흐린 글 구분 — 탭이면 Qt가 단축키 칸 폭을 한 번 더 더한다(프로토 MENU_SEP)
MENU_HEADER_MARK = "\x01"  # 묶음 머리글 항목 글 앞 표시 — 꺼진 항목이라 키보드가 건너뛴다


class _MenuRowStyle(QProxyStyle):
    """메뉴 D의 줄 높이 · 폭과 여백을 정하는 프록시 스타일 — 그리기는 `ChoiceMenu.paintEvent`가 한다 (#536).

    전역 QSS에 `QMenu::item` 규칙이 없어 스타일이 줄 크기를 이 프록시에 묻는다(프로토 MenuRowStyle).
    """

    def __init__(self, menu: "ChoiceMenu") -> None:
        super().__init__()
        self._menu = menu

    def pixelMetric(self, metric, option=None, widget=None) -> int:
        if metric == QStyle.PixelMetric.PM_MenuHMargin:
            return 0
        if metric == QStyle.PixelMetric.PM_MenuVMargin:
            return theme.METRICS["menuVPad"]
        if metric == QStyle.PixelMetric.PM_MenuPanelWidth:
            return 1
        return super().pixelMetric(metric, option, widget)

    def sizeFromContents(self, kind, option, size, widget=None) -> QSize:
        if kind == QStyle.ContentsType.CT_MenuItem and option is not None:
            return self._menu.rowSize(option)
        return super().sizeFromContents(kind, option, size, widget)


class ChoiceMenu(QMenu):
    """하나만 고르는 메뉴 — 설계의 드롭다운 메뉴 D (#495 · #536, 설계 §12.1 「드롭다운 메뉴」).

    한 줄 항목(이름 왼쪽 · 흐린 경로 오른쪽 — 경로는 카드와 같은 표기로 줄인다) · 묶음 머리글(12px 흐린 색) ·
    묶음 사이 구분선 · 모든 줄의 16px 아이콘 칸(고른 항목 ✓ · 동작 줄 도형) · 고른 항목 굵은 글 · 은은한 호버 면
    (`menuHover` — 강조색 면 아님). 면은 바 면 색 · 테두리 1px · 모서리 8이다.

    항목은 Qt 메뉴 항목(`QAction`) 그대로라 화살표 키 · Enter · Esc · 꺼진 항목 건너뛰기가 Qt 동작이다. 줄 크기는
    `_MenuRowStyle`이, 면 · 글 · 아이콘 · 호버는 `paintEvent`가 그린다. 항목 글은 `이름\\x1f흐린 글`이고
    (`nameOf` · `detailOf`), 머리글은 `\\x01머리`다.

    둥근 모서리: 메뉴 창에 투명 바탕(`WA_TranslucentBackground`)과 프레임 없음 플래그(`FramelessWindowHint`)를
    둘 다 건다 — 플래그가 없으면 Windows가 메뉴 창을 화소 알파를 쓰는 창으로 만들지 않아 모서리 바깥이 검게
    남는다(프로토 inbox 74 실제 화면 측정). 합성 관리자가 없는 Linux X11은 검게 남는다.

    항목 툴팁은 말줄임으로 잘린 경로에만 그 전문을 말풍선으로 띄운다 — 보이는 글을 되풀이하는 툴팁은 없다.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowFlag(
            Qt.WindowType.NoDropShadowWindowHint
        )  # 둥근 모서리 밖에 네모 그림자가 남지 않게
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self._rowStyle = _MenuRowStyle(self)  # 참조 보관 — setStyle은 소유하지 않는다
        self.setStyle(self._rowStyle)
        self.anchor: QWidget | None = None  # 메뉴를 맞출 버튼 — 왼쪽 끝 맞춤 · menuOffset 아래

    def addChoice(self, text: str, key: object, detail: str = "", path: str = "") -> QAction:
        """고르는 항목을 더한다 — `key`는 `mark`와 테스트가 항목을 찾는 값이다.

        `path`를 주면 흐린 글은 그 경로를 카드와 같은 표기로 줄여 그린다(`fit_path` — #495 inbox 71).
        `detail`은 그 표기의 ①(전체 축약형) — 줄 폭을 이것으로 센다.
        """
        action = self.addAction(f"{text}{MENU_SEP}{detail}" if detail else text)
        action.setData(key)
        action.setProperty("detailPath", path)
        return action

    def addCommand(self, text: str, detail: str = "", path: str = "", glyph: str = "") -> QAction:
        """고르는 항목이 아닌 명령 — `glyph`(icons의 이름)를 아이콘 칸에 그린다. 흐린 글은 `addChoice`와 같다."""
        action = self.addAction(f"{text}{MENU_SEP}{detail}" if detail else text)
        action.setProperty("detailPath", path)
        action.setProperty("glyph", glyph)
        return action

    def addHeader(self, text: str) -> QAction:
        """묶음 머리 — 꺼진 항목이라 고를 수 없고 키보드가 건너뛴다."""
        action = self.addAction(MENU_HEADER_MARK + text)
        action.setEnabled(False)
        action.setProperty("menuHeader", True)
        return action

    @staticmethod
    def nameOf(action: QAction) -> str:
        """항목의 이름(흐린 글 · 머리글 표시를 뺀 글)."""
        return action.text().removeprefix(MENU_HEADER_MARK).partition(MENU_SEP)[0]

    @staticmethod
    def detailOf(action: QAction) -> str:
        """항목의 흐린 글(없으면 빈 글)."""
        return action.text().partition(MENU_SEP)[2]

    def choices(self) -> list[QAction]:
        """고르는 항목(머리 · 명령 · 구분선 제외) — 메뉴 차례."""
        return [a for a in self.actions() if a.data() is not None]

    def mark(self, *keys: object) -> None:
        """그 키의 항목에만 ✓ · 굵은 글(정렬처럼 한 메뉴에 묶음이 둘이면 키도 둘)."""
        for action in self.choices():
            action.setProperty("marked", action.data() in keys)
        self.update()

    def markedKeys(self) -> list:
        """✓를 그린 항목의 키 — 메뉴 차례."""
        return [a.data() for a in self.choices() if a.property("marked")]

    def _fonts(self) -> tuple[QFont, QFont, QFont]:
        """(이름 글꼴, 굵은 이름 글꼴, 흐린 글 · 머리글 글꼴)."""
        name = QFont(self.font())
        name.setPixelSize(theme.METRICS["menuNamePx"])
        bold = QFont(name)
        bold.setBold(True)
        detail = QFont(self.font())
        detail.setPixelSize(theme.METRICS["menuDetailPx"])
        return name, bold, detail

    def textLeft(self) -> int:
        """글이 시작하는 x(메뉴 좌표) — 아이콘 칸 뒤. 모든 줄에서 같다."""
        m = theme.METRICS
        return m["menuRowInset"] + m["menuRowPad"] + m["menuIcon"] + m["menuIconGap"]

    def iconRect(self, action: QAction) -> QRect:
        """아이콘 칸(메뉴 좌표) — 모든 줄에서 같은 x, 줄 높이 가운데."""
        m = theme.METRICS
        g = self.actionGeometry(action)
        top = g.top() + (g.height() - m["menuIcon"]) // 2
        return QRect(m["menuRowInset"] + m["menuRowPad"], top, m["menuIcon"], m["menuIcon"])

    def rowSize(self, option: QStyleOptionMenuItem) -> QSize:
        """줄 크기 — `_MenuRowStyle`이 묻는다. 이름은 굵은 글꼴로 재 고른 항목이 되어도 폭이 같다."""
        m = theme.METRICS
        name_font, bold_font, detail_font = self._fonts()
        fm_name, fm_bold, fm_detail = (
            QFontMetrics(name_font),
            QFontMetrics(bold_font),
            QFontMetrics(detail_font),
        )
        if option.menuItemType == QStyleOptionMenuItem.MenuItemType.Separator:
            return QSize(1, 2 * m["menuVPad"] + 1)  # 폭 0이면 Qt가 빈 줄로 보고 건너뛴다
        text = option.text
        if text.startswith(MENU_HEADER_MARK):
            title = text[len(MENU_HEADER_MARK) :]
            width = 2 * (m["menuRowInset"] + m["menuRowPad"]) + text_width(fm_detail, title)
            return QSize(width, m["menuRowPad"] + fm_detail.height() + m["menuVPad"])
        name, _sep, detail = text.partition(MENU_SEP)
        width = self.textLeft() + text_width(fm_bold, name) + m["menuRowPad"] + m["menuRowInset"]
        if detail:
            width += MENU_DETAIL_GAP + text_width(fm_detail, detail)
        height = m["menuRowPad"] + max(m["menuIcon"], fm_name.height()) + m["menuRowPad"]
        return QSize(width, height)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self.anchor is not None:
            # 버튼 왼쪽 끝 맞춤 · menuOffset 아래 — Qt가 버튼 바로 밑에 놓은 자리에서 옮긴다(위로 뒤집혔으면 그대로)
            offset = theme.METRICS["menuOffset"]
            below = self.anchor.mapToGlobal(QPoint(0, self.anchor.height() + offset))
            if abs(self.y() - (below.y() - offset)) <= 1:
                self.move(below.x(), below.y())

    def event(self, event: QEvent) -> bool:
        """툴팁 이벤트 — 말줄임으로 잘린 경로 항목이면 그 전문을 말풍선으로, 아니면 아무것도 띄우지 않는다."""
        if event.type() == QEvent.Type.ToolTip:
            action = self.actionAt(event.pos())
            tip = self.menuTip(action)
            if tip:
                rect = self.actionGeometry(action)
                show_tip(self, tip, QRect(self.mapToGlobal(rect.topLeft()), rect.size()))
            else:
                hide_tip(self)
            return True
        if event.type() in (QEvent.Type.Leave, QEvent.Type.Hide):
            hide_tip(self)
        return super().event(event)

    @staticmethod
    def menuTip(action: QAction | None) -> str:
        """그 항목에 띄울 툴팁 — 흐린 경로가 말줄임으로 잘렸을 때만 전체 경로, 아니면 빈 글."""
        if action is None or not action.property("detailElided"):
            return ""
        return action.property("detailPath") or ""

    def paintEvent(self, event) -> None:
        """면 · 테두리 · 머리글 · 구분선 · 아이콘 · 글 · 호버 면을 그린다 — 색은 그릴 때마다 토큰을 읽는다."""
        m = theme.METRICS
        tokens = theme.current_tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(tokens["border"]), 1))
        painter.setBrush(QColor(tokens["surface"]))
        radius = m["menuRadius"]
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        name_font, bold_font, detail_font = self._fonts()
        fm_detail = QFontMetrics(detail_font)
        inset, pad = m["menuRowInset"], m["menuRowPad"]
        right_edge = self.width() - inset - pad
        active = self.activeAction()
        for action in self.actions():
            g = self.actionGeometry(action)
            if not action.isVisible() or g.isEmpty():
                continue
            if action.isSeparator():
                y = g.top() + g.height() // 2
                painter.setPen(QPen(QColor(tokens["border"]), 1))
                painter.drawLine(inset + pad, y, self.width() - inset - pad, y)
                continue
            text = action.text()
            if text.startswith(MENU_HEADER_MARK):
                painter.setFont(detail_font)
                painter.setPen(QColor(tokens["textMuted"]))
                painter.drawText(
                    QRect(inset + pad, g.top() + pad, g.width(), fm_detail.height()),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    text[len(MENU_HEADER_MARK) :],
                )
                continue
            enabled = action.isEnabled()
            if action is active and enabled:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(tokens["menuHover"]))
                hover_radius = m["menuHoverRadius"]
                painter.drawRoundedRect(
                    QRectF(g.adjusted(inset, 0, -inset, 0)).intersected(QRectF(self.rect())),
                    hover_radius,
                    hover_radius,
                )
            name_color = tokens["text" if enabled else "textDisabled"]
            detail_color = tokens["textMuted" if enabled else "textDisabled"]
            glyph = "check" if action.property("marked") else (action.property("glyph") or "")
            if glyph:
                pixmap = action_pixmap(glyph, name_color, m["menuIcon"], self.devicePixelRatioF())
                painter.drawPixmap(self.iconRect(action).topLeft(), pixmap)
            name, _sep, detail = text.partition(MENU_SEP)
            left = self.textLeft()
            font = bold_font if action.property("marked") else name_font
            painter.setFont(font)
            painter.setPen(QColor(name_color))
            fm_name = QFontMetrics(font)
            name_w = min(text_width(fm_name, name), right_edge - left)
            painter.drawText(
                QRect(left, g.top(), name_w, g.height()),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                fm_name.elidedText(name, Qt.TextElideMode.ElideRight, name_w),
            )
            if detail:
                room = right_edge - left - name_w - MENU_DETAIL_GAP
                shown = self._detailText(fm_detail, action, detail, room)
                action.setProperty("detailElided", shown != detail)
                painter.setFont(detail_font)
                painter.setPen(QColor(detail_color))
                painter.drawText(
                    QRect(right_edge - room, g.top(), room, g.height()),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    shown,
                )
        painter.end()

    @staticmethod
    def _detailText(metrics: QFontMetrics, action: QAction, detail: str, room: int) -> str:
        """흐린 글 — 경로(`detailPath`)는 카드 표기 차례로 줄이고, 「(없음)」 같은 꼬리는 끝에 남긴다."""
        path = action.property("detailPath")
        if not path:
            return metrics.elidedText(detail, Qt.TextElideMode.ElideMiddle, max(room, 0))
        tail = detail[len(abbreviate_path(path)) :]
        return fit_path(metrics, path, max(room - text_width(metrics, tail), 0)) + tail


def text_width(metrics: QFontMetrics, text: str) -> int:
    """글이 줄지 않고 들어가는 칸 폭 — 소수 글 폭을 올림한다.

    정수 `horizontalAdvance`는 반올림이라 글자 폭이 소수인 글꼴 엔진(Windows 실제 화면)에서 실제 글보다
    좁을 수 있다. 그 칸으로 부른 `elidedText`는 소수 폭으로 비교해 글을 말줄임한다(#525 · #527).
    """
    return math.ceil(QFontMetricsF(metrics).horizontalAdvance(text))


def fit_path(metrics: QFontMetrics, path: str, room: int) -> str:
    """경로를 폭에 맞춘 글 — 카드 경로 라벨(PathLabel)과 같은 차례로 줄인다 (#495 inbox 71).

    ① 전체 축약형(홈은 `~`, 중간 폴더 접기 — `abbreviate_path`) → ② `뿌리/…/마지막 폴더` → ③ 마지막
    폴더만 가운데 말줄임. 조각은 카드와 같은 함수(`path_display_parts`)에서 받는다. 들어가는지는
    `text_width`로 잰다 — 들어가면 `elidedText`에 다시 맡기지 않는다(#527).
    """
    full, prefix, last = path_display_parts(path)
    if text_width(metrics, full) <= room:
        return full
    if not last:
        return metrics.elidedText(full, Qt.TextElideMode.ElideMiddle, room)
    if text_width(metrics, prefix + last) <= room:
        return prefix + last
    rest = max(room - text_width(metrics, prefix), 0)
    return prefix + metrics.elidedText(last, Qt.TextElideMode.ElideMiddle, rest)


class DropButton(QToolButton):
    """누르면 메뉴가 펼쳐지는 글 버튼 — [도형] 글 ▾ (#495).

    글은 `setFullText`로 주고, 폭에 맞춰 가운데를 줄여 그린다. 도형(`glyph`)은 `app.widgets.icons`의
    이름이다(없으면 글만). 폭은 호출자가 `setFixedWidth`로 건다 — 고른 값에 따라 버튼 폭이 바뀌면
    헤더바가 움직인다.
    """

    def __init__(self, glyph: str = "", parent: QWidget | None = None) -> None:
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
        """그 글이 줄지 않고 들어가는 버튼 폭 — 테두리 · 여백 · 도형 · ▾를 더한다.

        글 폭은 `text_width`로 잰다(소수 폭 올림, #525).
        """
        self.ensurePolished()
        return self._chrome() + text_width(self.fontMetrics(), text)

    def sizeHint(self) -> QSize:
        self.ensurePolished()
        # 높이는 바 컨트롤 높이(#536 — 설계 §11.3). 글이 더 높은 글꼴이면 글 높이를 따른다
        height = max(theme.METRICS["barControl"], self.fontMetrics().height() + 2 * BUTTON_VPAD)
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
        painter.drawText(
            self._textRect(),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            self.shownText(),
        )
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

    def _paintIcon(self, glyph: str) -> QStylePainter:
        """아이콘 모양(좁은 폭) — 같은 테두리 상자와 가운데 도형을 그리고 painter를 돌려준다(부른 쪽이 끝낸다).

        도형은 `narrowIcon` 크기, 색은 그릴 때마다 토큰을 읽는다(실행 중 테마 전환 #247) (#529).
        """
        option = QStyleOptionToolButton()
        self.initStyleOption(option)
        option.text = ""
        option.icon = QIcon()
        option.features &= ~QStyleOptionToolButton.ToolButtonFeature.HasMenu
        painter = QStylePainter(self)
        painter.drawComplexControl(QStyle.ComplexControl.CC_ToolButton, option)
        tokens = theme.current_tokens()
        side = theme.METRICS["narrowIcon"]
        color = tokens["textMuted" if self.isEnabled() else "textDisabled"]
        pixmap = action_pixmap(glyph, color, side, self.devicePixelRatioF())
        painter.drawPixmap((self.width() - side) // 2, (self.height() - side) // 2, pixmap)
        return painter


class SaveLocationButton(DropButton):
    """헤더바 저장 위치 버튼 — 「📁 폴더 이름 ▾」, 누르면 저장 위치 메뉴 (#495 — 설계 §2).

    폭은 기준 글(`WIDTH_SAMPLE`)로 한 번 정한다 — 고른 위치 · 기본 위치와 무관하다(바뀌면 헤더바와
    창 최소폭이 따라 움직인다). 긴 이름은 가운데를 줄이고 툴팁에 전체 경로를 보인다. 지금 위치가
    즐겨찾기면 별칭을 보인다.

    메뉴 차례: 지금 위치(✓) · 즐겨찾기(★ 별칭 + 흐린 경로) · 최근 · 구분선 · 「지금 위치를 즐겨찾기에」
    · 「폴더 찾기…」 · 「기본 위치로」. 없는 경로는 흐리게 「(없음)」을 붙이고 고를 수 없다.

    좁은 폭에서는 아이콘 모양이 된다(`setCompact` — #529, 프로토 11v): 버튼 높이의 정사각에 📁만 그리고, 글이
    없으므로 툴팁 첫 줄에 지금 위치 이름, 둘째 줄에 전체 경로를 둔다. 누르면 같은 메뉴가 열린다.

    시그널:
        errorOccurred(str): 고르기 · 즐겨찾기 더하기가 막혔다 — 번역된 까닭
    """

    errorOccurred = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("folder", parent)
        self._locations: SaveLocations | None = None
        self._compact = False
        self.menuWidget = ChoiceMenu(self)
        self.menuWidget.anchor = self  # 버튼 왼쪽 끝 맞춤 · 조금 아래(#536)
        self.menuWidget.aboutToShow.connect(self._fillMenu)
        self.setMenu(self.menuWidget)

    def bind(self, locations: SaveLocations) -> None:
        """저장 위치 뷰모델과 잇는다 — 바뀔 때마다 글 · 툴팁을 다시 쓴다."""
        self._locations = locations
        locations.changed.connect(self._refresh)
        self.applyWidth()
        self._refresh()

    def isCompact(self) -> bool:
        """아이콘 모양인지."""
        return self._compact

    def setCompact(self, compact: bool) -> None:
        """글 모양 ↔ 아이콘 모양을 바꾼다 — 폭 · 툴팁이 함께 바뀐다."""
        if compact == self._compact:
            return
        self._compact = compact
        self.applyWidth()
        self._refreshTip()
        self.update()

    def textWidth(self) -> int:
        """글 모양의 폭 — 기준 글(`WIDTH_SAMPLE`)이 들어가는 폭(지금 모양과 무관, 소수 폭 올림 #525)."""
        return self.widthFor(WIDTH_SAMPLE)

    def iconWidth(self) -> int:
        """아이콘 모양의 폭 — 바 컨트롤 높이의 정사각(지금 모양과 무관, #536)."""
        return theme.METRICS["barControl"]

    def applyWidth(self) -> None:
        """지금 모양의 폭을 건다 — 글꼴이 바뀌면 다시 부른다."""
        self.setFixedWidth(self.iconWidth() if self._compact else self.textWidth())

    def _refresh(self) -> None:
        """글(별칭 또는 폴더 이름) · 툴팁을 지금 위치로."""
        self.setFullText(self._locations.display_name())
        self._refreshTip()

    def _refreshTip(self) -> None:
        """글 모양은 전체 경로, 아이콘 모양은 「이름」 · 「전체 경로」 두 줄."""
        if self._locations is None:
            return
        path = self._locations.current()
        tip = f"{self.fullText()}\n{path}" if self._compact else path
        if self.toolTip() != tip:
            self.setToolTip(tip)

    def paintEvent(self, event) -> None:
        """글 모양은 펼침 버튼 그대로, 아이콘 모양은 상자 · 📁만 그린다."""
        if not self._compact:
            super().paintEvent(event)
            return
        self._paintIcon("folder").end()

    def _fillMenu(self) -> None:
        """메뉴를 지금 값으로 다시 채운다 — 열 때마다(없는 경로 · 도형 색이 늘 지금 값)."""
        menu = self.menuWidget
        menu.clear()
        locations = self._locations
        entries = locations.menu_entries()
        missing_text = self.tr("(missing)")
        # 메뉴 폭 상한 = 창 폭 − 24 — 긴 경로가 메뉴를 창보다 넓히지 않는다(프로토 PATH_MENU_MARGIN, inbox 71)
        limit = max(MENU_MIN_LIMIT, self.window().width() - MENU_WINDOW_MARGIN)
        metrics = menu.fontMetrics()
        headed: set[str] = set()
        for entry in entries:
            if entry.kind == "favorite" and "favorite" not in headed:
                menu.addHeader(self.tr("Favorites"))
                headed.add("favorite")
            if entry.kind == "recent" and "recent" not in headed:
                menu.addHeader(self.tr("Recent"))
                headed.add("recent")
            # 줄마다 「이름 + 흐린 경로」 — 이름은 즐겨찾기면 별칭(★), 아니면 폴더 이름(지금 위치가 즐겨찾기면
            # 그 별칭). 경로는 카드와 같은 표기. 툴팁은 경로가 말줄임으로 잘렸을 때만 전체 경로(ChoiceMenu)
            favorite = locations.favorite_for(entry.path)
            if entry.kind == "favorite":
                name = f"★ {entry.name}"
            else:
                name = favorite["name"] if favorite else folder_name(entry.path)
            name = metrics.elidedText(name, Qt.TextElideMode.ElideRight, limit // 2)
            suffix = f"  {missing_text}" if entry.missing else ""
            action = menu.addChoice(
                name, (entry.kind, entry.path), abbreviate_path(entry.path) + suffix, entry.path
            )
            action.setProperty("missing", entry.missing)
            action.setEnabled(not entry.missing or entry.kind == "current")
            action.triggered.connect(lambda _=False, p=entry.path: self._choose(p))
        menu.mark(("current", locations.current()))
        menu.addSeparator()
        # 동작 줄 — 아이콘 칸에 별 · 폴더 · 되돌리기(#536, 설계 §12.1)
        add = menu.addCommand(self.tr("Add current folder to favorites"), glyph="star")
        add.setObjectName("addFavoriteAction")
        add.setEnabled(locations.can_add_favorite())  # 이미 즐겨찾기 · 가득 차면 끔
        add.triggered.connect(self._addFavorite)
        find = menu.addCommand(self.tr("Find folder…"), glyph="folder")
        find.setObjectName("findFolderAction")
        find.triggered.connect(self._findFolder)
        default = locations.default()
        reset = menu.addCommand(
            self.tr("Default location"), abbreviate_path(default), default, glyph="undo"
        )
        reset.setObjectName("defaultLocationAction")
        reset.setEnabled(not locations.is_default())  # 이미 기본 위치면 끔(inbox 71)
        reset.triggered.connect(locations.reset)
        # 폭 = 버튼 폭 ~ 창 폭 − 24(설계 §12.1)
        menu.setMinimumWidth(min(self.width(), limit))
        menu.setMaximumWidth(limit)

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

    좁은 폭에서는 아이콘 모양이 된다(`setCompact` — #529, 설계 §2 「좁은 폭」): 같은 테두리 상자의
    정사각에 ⇅만 그리고, 기본 정렬(추가된 순서 오름차순)이 아니면 상자 오른쪽 위 안쪽에 강조색 점을 찍는다.
    글이 없으므로 툴팁 「정렬: <이름>」을 단다. 누르면 같은 메뉴가 열린다.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("", parent)
        self._order = None
        self._compact = False
        self.menuWidget = ChoiceMenu(self)
        self.menuWidget.anchor = self  # 버튼 왼쪽 끝 맞춤 · 조금 아래(#536)
        self.menuWidget.aboutToShow.connect(self._fillMenu)
        self.setMenu(self.menuWidget)

    def isCompact(self) -> bool:
        """아이콘 모양인지."""
        return self._compact

    def setCompact(self, compact: bool) -> None:
        """글 모양 ↔ 아이콘 모양을 바꾼다 — 폭 · 툴팁이 함께 바뀐다."""
        if compact == self._compact:
            return
        self._compact = compact
        self.applyWidth()
        self._refreshTip()
        self.update()

    def textWidth(self) -> int:
        """글 모양의 폭 — 가장 긴 이름이 들어가는 폭(지금 모양과 무관)."""
        return max(self.widthFor(name) for name in self.names().values())

    def iconWidth(self) -> int:
        """아이콘 모양의 폭 — 바 컨트롤 높이의 정사각(지금 모양과 무관, #536)."""
        return theme.METRICS["barControl"]

    def isDefaultSort(self) -> bool:
        """기본 정렬(추가된 순서 오름차순)인지 — 아니면 아이콘 모양에 점을 찍는다."""
        return self._order.sortBy() == SORT_KEYS[0] and not self._order.isDescending()

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
        """지금 모양의 폭을 건다 — 글 모양은 가장 긴 이름, 아이콘 모양은 정사각. 글꼴 · 번역이 바뀌면 다시 부른다."""
        self.setFixedWidth(self.iconWidth() if self._compact else self.textWidth())

    def _refresh(self) -> None:
        self.setFullText(self.names()[self._order.sortBy()])
        self._refreshTip()

    def _refreshTip(self) -> None:
        """아이콘 모양이면 「정렬: <이름>」 툴팁, 글 모양이면 없음(보이는 글을 되풀이하지 않는다)."""
        tip = self.tr("Sort: {0}").format(self.fullText()) if self._compact else ""
        if self.toolTip() != tip:
            self.setToolTip(tip)

    def paintEvent(self, event) -> None:
        """글 모양은 펼침 버튼 그대로, 아이콘 모양은 상자 · ⇅ · (기본 정렬이 아니면) 점을 그린다."""
        if not self._compact:
            super().paintEvent(event)
            return
        painter = self._paintIcon("sort")
        if self._order is not None and not self.isDefaultSort():
            tokens = theme.current_tokens()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(tokens["accent" if self.isEnabled() else "textDisabled"]))
            inset = theme.METRICS["sortDotInset"]
            radius = theme.METRICS["sortDot"]
            painter.drawEllipse(QPointF(self.width() - inset, inset), radius, radius)
        painter.end()

    def _fillMenu(self) -> None:
        menu = self.menuWidget
        menu.clear()
        order = self._order
        menu.addHeader(self.tr("Sort by"))
        for key, name in self.names().items():
            action = menu.addChoice(name, ("by", key))
            action.triggered.connect(lambda _=False, k=key: order.setSort(k, order.isDescending()))
        menu.addSeparator()
        menu.addHeader(self.tr("Direction"))
        for descending, name in zip((False, True), self.directionNames(order.sortBy())):
            action = menu.addChoice(name, ("direction", "desc" if descending else "asc"))
            action.triggered.connect(lambda _=False, d=descending: order.setSort(order.sortBy(), d))
        menu.mark(("by", order.sortBy()), ("direction", "desc" if order.isDescending() else "asc"))
        menu.setMinimumWidth(self.width())  # 폭 = 버튼 폭부터(설계 §12.1)

    def directionNames(self, by: str) -> tuple[str, str]:
        """정렬 방향 이름(오름 · 내림) — 기준마다 다르다(#536, 설계 §12.1 「드롭다운 메뉴」)."""
        names = {
            "added": (self.tr("Oldest added first"), self.tr("Newest added first")),
            "title": (self.tr("A to Z"), self.tr("Z to A")),
            "channel": (self.tr("A to Z"), self.tr("Z to A")),
            "upload": (self.tr("Newest first"), self.tr("Oldest first")),
            "state": (self.tr("Downloading first"), self.tr("Finished first")),
        }
        return names[by]


CHIP_COUNT_RESERVE = 999  # 칩 폭을 잡는 숫자 — 세 자리까지 칩 · 헤더바 · 창 최소폭이 그대로다(옛 카운트의 999/999 예약)
CHIP_GAP = 4  # 칩 사이 간격(px) — 좁은 폭의 드롭다운 ↔ 실패 칩도 같다


class FilterChips(QWidget):
    """헤더바 2줄 상태 필터 — 상태별 숫자 칩(전체 · 대기 · 진행 · 일시정지 · 완료 · 실패) (#495).

    고른 칩만 눌린 상태다. 고른 칩을 다시 누르면 전체로 돌아간다(설계 §2 — 뜻은 `CardOrder.setFilter`).
    실패 칩은 실패 카드가 있을 때 강조한다(`alert` 속성 → 전역 QSS). 칩 폭은 숫자 세 자리
    (`CHIP_COUNT_RESERVE`)가 들어가는 고정 폭이다 — 숫자가 바뀌어도 줄이 움직이지 않는다.

    좁은 폭에서는 드롭다운 모양이 된다(`setCompact` — #529, 설계 §2 「좁은 폭」): 칩 여섯 대신 고른 필터
    「이름 N」을 보이는 드롭다운 하나(`dropButton`)와, 실패 카드가 있을 때만 그 옆에 남는 실패 칩이다.
    실패 칩은 넓은 모양의 그 칩 그대로라 다시 누르면 전체로 돌아간다. 고른 필터는 뷰모델에 있어 모양이
    바뀌어도 그대로다. 숫자는 뷰모델이 세어 둔 값만 읽는다 — 카드 목록을 돌지 않는다.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._order = None
        self._compact = False
        self.chips: dict[str, QPushButton] = {}
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(CHIP_GAP)
        # 좁은 폭 드롭다운 — 칩 줄 맨 앞, 평소에는 숨는다
        self.dropButton = DropButton("", self)
        self.dropButton.setObjectName("filterDropButton")
        self.dropMenu = ChoiceMenu(self.dropButton)
        self.dropMenu.anchor = self.dropButton
        self.dropMenu.aboutToShow.connect(self._fillMenu)
        self.dropButton.setMenu(self.dropMenu)
        self.dropButton.hide()
        layout.addWidget(self.dropButton)
        for key in FILTER_KEYS:
            chip = QPushButton(self)
            chip.setObjectName(f"filterChip_{key}")
            chip.setProperty("role", "filter")
            chip.setProperty("alert", False)
            chip.setCheckable(True)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.clicked.connect(lambda _=False, k=key: self._choose(k))
            layout.addWidget(chip)
            self.chips[key] = chip

    def _choose(self, key: str) -> None:
        """칩을 누르면 필터를 고르고 눌림을 뷰모델 값으로 다시 맞춘다.

        눌림은 Qt가 클릭 때 먼저 뒤집는다. 고른 「전체」를 다시 누르면 필터가 그대로라 바뀜 알림이
        오지 않으므로, 알림을 기다리지 않고 여기서 맞춘다(#503 리뷰).
        """
        self._order.setFilter(key)
        self._refreshChecked()

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
        """칩마다 「이름 999」, 드롭다운에 가장 긴 「이름 999」가 들어가는 폭을 건다 — 글꼴 · 번역이 바뀌면 다시 부른다."""
        names = self.names()
        for key, name in names.items():
            chip = self.chips[key]
            shown = chip.text()
            chip.setText(f"{name} {CHIP_COUNT_RESERVE}")
            chip.ensurePolished()
            chip.setFixedWidth(chip.sizeHint().width())
            chip.setText(shown)
        self.dropButton.setFixedWidth(
            max(self.dropButton.widthFor(f"{name} {CHIP_COUNT_RESERVE}") for name in names.values())
        )

    def isCompact(self) -> bool:
        """드롭다운 모양인지."""
        return self._compact

    def setCompact(self, compact: bool) -> None:
        """칩 모양 ↔ 드롭다운 모양을 바꾼다."""
        if compact == self._compact:
            return
        self._compact = compact
        self._applyShape()

    def chipsWidth(self) -> int:
        """칩 모양의 폭 — 칩 여섯 고정 폭과 사이 간격(지금 모양과 무관)."""
        widths = [chip.minimumWidth() for chip in self.chips.values()]
        return sum(widths) + CHIP_GAP * (len(widths) - 1)

    def compactWidth(self, failed: bool) -> int:
        """드롭다운 모양의 폭 — 드롭다운과, `failed`면 실패 칩 · 간격(지금 모양과 무관)."""
        width = self.dropButton.minimumWidth()
        if failed:
            width += CHIP_GAP + self.chips["failed"].minimumWidth()
        return width

    def hasFailed(self) -> bool:
        """실패 카드가 있는지 — 좁은 폭에서 실패 칩이 남는지."""
        return self._order is not None and self._order.counts()["failed"] > 0

    def _applyShape(self) -> None:
        """지금 모양대로 드롭다운 · 칩을 보이거나 숨긴다."""
        compact = self._compact
        self.dropButton.setVisible(compact)
        for key, chip in self.chips.items():
            if key == "failed":
                chip.setVisible(not compact or self.hasFailed())
            else:
                chip.setVisible(not compact)

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
        self._refreshDrop()
        if self._compact:
            self._applyShape()  # 실패 0 ↔ N — 실패 칩이 나타나거나 숨는다

    def _refreshChecked(self) -> None:
        chosen = self._order.filterKey()
        for key, chip in self.chips.items():
            chip.setChecked(key == chosen)
        self._refreshDrop()

    def _refreshDrop(self) -> None:
        """드롭다운 닫힌 글 = 고른 필터 「이름 N」."""
        key = self._order.filterKey()
        self.dropButton.setFullText(f"{self.names()[key]} {self._order.counts()[key]}")

    def _fillMenu(self) -> None:
        """드롭다운 메뉴를 지금 숫자로 채운다 — 고른 필터에만 ✓. 고른 것을 다시 골라도 그대로 둔다."""
        menu = self.dropMenu
        menu.clear()
        order = self._order
        counts = order.counts()
        for key, name in self.names().items():
            action = menu.addChoice(f"{name} {counts[key]}", key)
            action.triggered.connect(lambda _=False, k=key: self._pick(k))
        menu.mark(order.filterKey())

    def _pick(self, key: str) -> None:
        """드롭다운에서 고른다 — 이미 고른 필터면 바꾸지 않는다(칩의 「다시 누르면 전체」와 다르다)."""
        if key != self._order.filterKey():
            self._order.setFilter(key)


SPIN_INTERVAL_MS = 80  # 도는 표시 한 칸의 시간(ms) — 한 바퀴 약 1초(12칸)
SPIN_STEP_DEGREES = 30  # 한 칸에 도는 각도


class BusyButton(QPushButton):
    """도는 표시가 있는 글 버튼 — 헤더바 「VOD 추가」(#495 — 설계 알림 자리).

    영상 정보를 불러오는 동안 글 왼쪽에 도는 호를 그린다(「불러오는 중」 글은 띄우지 않는다).
    호 자리는 늘 잡아 둔다(`reservedWidth`) — 돌기 시작해도 버튼 폭 · 글 자리가 그대로다. 호 색은
    그릴 때마다 theme 토큰을 새로 읽는다(실행 중 테마 전환 #247).
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._busy = False
        self._step = 0
        self._timer = QTimer(self)
        self._timer.setInterval(SPIN_INTERVAL_MS)
        self._timer.timeout.connect(self._advance)

    def isBusy(self) -> bool:
        """도는 표시를 그리는 중인지."""
        return self._busy

    def setBusy(self, busy: bool) -> None:
        """도는 표시를 켜고 끈다 — 폭은 바뀌지 않는다."""
        if busy == self._busy:
            return
        self._busy = busy
        if busy:
            self._step = 0
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def reservedWidth(self) -> int:
        """글 양옆에 호 자리(도형 + 간격)를 하나씩 더한 폭 — 글이 늘 가운데에 남는다."""
        self.ensurePolished()
        return self.sizeHint().width() + 2 * (theme.METRICS["actionGlyph"] + GLYPH_GAP)

    def _advance(self) -> None:
        self._step = (self._step + 1) % (360 // SPIN_STEP_DEGREES)
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self._busy:
            return
        size = theme.METRICS["actionGlyph"]
        text_left = (self.width() - self.fontMetrics().horizontalAdvance(self.text())) // 2
        rect = QRect(text_left - GLYPH_GAP - size, (self.height() - size) // 2, size, size)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(theme.current_tokens()["text"]), 1.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(rect.adjusted(1, 1, -1, -1), -self._step * SPIN_STEP_DEGREES * 16, 270 * 16)
        painter.end()
