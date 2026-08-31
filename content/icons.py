"""카드 조작 아이콘 — 폰트 글리프 대신 QPainter로 직접 그린 도형 (#245).

왜 글리프를 버렸나: ①`‖`(U+2016)는 문장부호(DOUBLE VERTICAL LINE)라
일시정지로 읽히지 않고, ②`🗀`·`↻` 같은 문자의 모양은 플랫폼 폰트 스택이
정하므로 macOS·Linux 실기가 없는 한 어떻게 보이는지 확인할 길이 영영
없다. 도형을 직접 그리면 폰트·플랫폼에서 자유롭다 — 어디서나 같은 모양이다.

비용 통제 (카드 1000개 O(1) 삽입 보존):
- 아이콘은 **(이름, 색, 크기, 배율)별로 한 번만** 그려 모듈 캐시(`_CACHE`)에
  공유한다. 카드마다 페인트 객체(QIcon·QPixmap·이펙트)가 붙지 않는다 —
  `IconButton`은 인스턴스 속성으로 문자열 둘(아이콘 이름·호버 토큰)만 든다.
- 버튼은 `paintEvent`에서 공유 QPixmap을 `drawPixmap` 한 번 한다.

색은 `theme.py` 토큰 이름으로만 고른다(평소 `textMuted` / 호버 `text` 또는
버튼별 지정 / 비활성 `textDisabled`) — 이 파일에는 색 리터럴이 없다
(`tests/unit/test_theme.py`의 단일 정의 게이트). 배경·테두리(호버 표면)는
전역 QSS의 `QPushButton[role="icon"]` 규칙이 그대로 담당한다.
"""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QPushButton

import theme

#: 그릴 수 있는 아이콘 이름 — `IconButton.setIconName()`이 받는 어휘.
#: folder_dot = 폴더 + 우상단 점(강조색) — 카드 경로가 전역 설정과 다를 때
#: 아이콘만 남은 경로 자리에 "다르다"를 표시한다(#245).
ICON_NAMES = ("pause", "resume", "retry", "folder", "folder_dot", "delete")

_CACHE: dict[tuple[str, str, int, float, str], QPixmap] = {}


def action_pixmap(name: str, color: str, size: int, dpr: float = 1.0, accent: str = "") -> QPixmap:
    """조작 아이콘 도형을 `size`px 정사각 픽스맵으로 돌려준다 — 캐시 공유.

    같은 (이름, 색, 크기, 배율, 강조색)은 항상 같은 QPixmap 객체다. 배율(dpr)
    만큼 크게 그린 뒤 `setDevicePixelRatio`로 논리 크기를 맞춰 HiDPI에서도
    선명하다. `accent`는 두 색을 쓰는 도형(folder_dot의 점)만 읽는다.
    """
    if name not in ICON_NAMES:
        raise ValueError(f"알 수 없는 아이콘: {name!r} (허용: {ICON_NAMES})")
    key = (name, color, size, dpr, accent)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    pixmap = QPixmap(round(size * dpr), round(size * dpr))
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    _PAINTERS[name](painter, float(size), QColor(color), QColor(accent or color))
    painter.end()
    _CACHE[key] = pixmap
    return pixmap


def cache_size() -> int:
    """캐시에 든 픽스맵 수 — 공유가 실제로 일어나는지 테스트가 본다."""
    return len(_CACHE)


# ---- 도형 — 좌표는 0..s 정사각 안의 비율. 선 두께는 s에 비례한다 ----


def _fill(painter: QPainter, color: QColor) -> None:
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)


def _stroke(painter: QPainter, color: QColor, width: float) -> None:
    pen = QPen(color, width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)


def _paint_pause(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """일시정지 — 세로 막대 둘."""
    _fill(painter, color)
    radius = s * 0.08
    painter.drawRoundedRect(QRectF(s * 0.14, s * 0.06, s * 0.26, s * 0.88), radius, radius)
    painter.drawRoundedRect(QRectF(s * 0.60, s * 0.06, s * 0.26, s * 0.88), radius, radius)


def _paint_resume(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """재개 — 오른쪽을 향한 삼각형(재생)."""
    _fill(painter, color)
    painter.drawPolygon(
        QPolygonF(
            [
                QPointF(s * 0.18, s * 0.06),
                QPointF(s * 0.94, s * 0.50),
                QPointF(s * 0.18, s * 0.94),
            ]
        )
    )


def _paint_retry(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """재시도 — 3/4 원호 + 우상단 끝에 또렷한 화살촉(↻).

    작은 크기(12px)에서 원호만 그리면 'o'로 읽힌다 — 틈을 80°로 넉넉히
    벌리고 화살촉을 선 두께의 두 배쯤으로 키워 방향이 읽히게 한다(실기
    렌더로 크기를 맞췄다 — 더 크면 덩어리, 더 작으면 'C'로 보였다).
    """
    import math

    width = s * 0.17
    _stroke(painter, color, width)
    cx, cy, r = s * 0.50, s * 0.54, s * 0.32
    rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
    # Qt 각도는 1/16도 단위, 음수 span이 시계 방향. 50°(우상단)에서 시계
    # 방향으로 290° 돌아 120°(좌상단)에서 끝난다 → 위쪽 50°~120°가 틈이고,
    # 화살촉은 원호가 끝나는 120° 점에서 시계 방향(오른쪽 위, 틈 쪽)을
    # 가리킨다 — ↻과 같은 읽힘.
    painter.drawArc(rect, 40 * 16, -280 * 16)
    end_deg = math.radians(120)
    end = QPointF(cx + r * math.cos(end_deg), cy - r * math.sin(end_deg))
    # 시계 방향 접선(단위) — 각도가 줄어드는 방향
    dx, dy = math.sin(end_deg), math.cos(end_deg)
    nx, ny = -dy, dx  # 접선에 수직
    # 촉 — 밑변이 원호 끝에 놓이고 접선 방향으로 뻗는다. 선 두께의 두 배쯤이
    # 12px에서 방향이 읽히는 하한이고, 그보다 크면 덩어리로 보인다.
    length, half = s * 0.30, s * 0.19
    _fill(painter, color)
    painter.drawPolygon(
        QPolygonF(
            [
                QPointF(end.x() + dx * length, end.y() + dy * length),  # 촉 끝
                QPointF(end.x() + nx * half, end.y() + ny * half),
                QPointF(end.x() - nx * half, end.y() - ny * half),
            ]
        )
    )


def _paint_folder(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """폴더 열기 — 탭이 달린 폴더 몸통."""
    _fill(painter, color)
    radius = s * 0.10
    path = QPainterPath()
    path.addRoundedRect(QRectF(s * 0.04, s * 0.24, s * 0.92, s * 0.66), radius, radius)
    path.addRoundedRect(QRectF(s * 0.04, s * 0.12, s * 0.42, s * 0.24), radius * 0.6, radius * 0.6)
    painter.drawPath(path.simplified())


def _paint_folder_dot(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """폴더 + 우상단 점(강조색) — "이 카드의 경로는 전역과 다르다" 표시."""
    _paint_folder(painter, s, color, accent)
    _fill(painter, accent)
    radius = s * 0.17
    painter.drawEllipse(QPointF(s * 0.82, s * 0.20), radius, radius)


def _paint_delete(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """삭제 — 대각선 둘(✕)."""
    _stroke(painter, color, s * 0.16)
    painter.drawLine(QPointF(s * 0.22, s * 0.22), QPointF(s * 0.78, s * 0.78))
    painter.drawLine(QPointF(s * 0.78, s * 0.22), QPointF(s * 0.22, s * 0.78))


_PAINTERS = {
    "pause": _paint_pause,
    "resume": _paint_resume,
    "retry": _paint_retry,
    "folder": _paint_folder,
    "folder_dot": _paint_folder_dot,
    "delete": _paint_delete,
}


class IconButton(QPushButton):
    """도형 아이콘을 그리는 정사각 조작 버튼 (#245).

    배경·테두리(호버 표면)는 부모 `paintEvent`(전역 QSS `[role="icon"]`)가
    그리고, 그 위에 공유 캐시의 도형 픽스맵을 얹는다. 색 토큰은
    평소 `textMuted`, 호버 시 `hoverToken()`(기본 `text`, 삭제는 `stateFailed`),
    비활성 `textDisabled`. 인스턴스가 드는 것은 문자열 둘뿐이다.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._icon_name = ""
        self._idle_token = "textMuted"
        self._hover_token = "text"
        self._accent_token = ""
        # 호버 진입·이탈에 다시 그리게 한다(QSS :hover 규칙이 없어도)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def setIconName(self, name: str) -> None:
        """그릴 도형을 바꾼다 — 상태별 조작 전환(일시정지↔재개)에 쓴다."""
        if name and name not in ICON_NAMES:
            raise ValueError(f"알 수 없는 아이콘: {name!r} (허용: {ICON_NAMES})")
        if name != self._icon_name:
            self._icon_name = name
            self.update()

    def iconName(self) -> str:
        """지금 그리는 도형 이름 — 테스트·상태 매트릭스 확인용."""
        return self._icon_name

    def setHoverToken(self, token: str) -> None:
        """호버 시 도형 색 토큰(theme.py 키)을 지정한다 — 삭제는 `stateFailed`."""
        self._hover_token = token

    def hoverToken(self) -> str:
        """호버 시 도형 색 토큰 이름."""
        return self._hover_token

    def setIdleToken(self, token: str) -> None:
        """평소 도형 색 토큰(기본 textMuted) — 경로 아이콘이 "전역과 다름"을 밝게 알릴 때 쓴다."""
        if token != self._idle_token:
            self._idle_token = token
            self.update()

    def idleToken(self) -> str:
        """평소 도형 색 토큰 이름."""
        return self._idle_token

    def setAccentToken(self, token: str) -> None:
        """두 색 도형(folder_dot의 점)의 강조색 토큰. 빈 문자열이면 본체 색을 따른다."""
        if token != self._accent_token:
            self._accent_token = token
            self.update()

    def accentToken(self) -> str:
        """강조색 토큰 이름(없으면 빈 문자열)."""
        return self._accent_token

    def colorToken(self) -> str:
        """지금 도형에 쓸 색 토큰 — 비활성 > 호버 > 평소 순으로 정한다."""
        if not self.isEnabled():
            return "textDisabled"
        if self.underMouse():
            return self._hover_token
        return self._idle_token

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self._icon_name:
            return
        size = theme.METRICS["actionGlyph"]
        tokens = theme.current_tokens()
        color = tokens[self.colorToken()]
        accent = tokens[self._accent_token] if self._accent_token else ""
        pixmap = action_pixmap(self._icon_name, color, size, self.devicePixelRatioF(), accent)
        painter = QPainter(self)
        painter.drawPixmap((self.width() - size) // 2, (self.height() - size) // 2, pixmap)
        painter.end()
