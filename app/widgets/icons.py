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
(`tests/app/gui/test_theme.py`의 단일 정의 게이트). 배경·테두리(호버 표면)는
전역 QSS의 `QPushButton[role="icon"]` 규칙이 그대로 담당한다.
"""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import QPushButton

import app.theme as theme

#: 그릴 수 있는 아이콘 이름 — `IconButton.setIconName()`이 받는 어휘.
#: folder = 완료 카드의 폴더 열기.
#: folder_edit = 폴더 + 우하단 연필 — 카드 위 줄의 저장 위치 버튼(위치를 바꾼다). 폴더
#: 열기와 하는 일이 달라 모양을 나눈다(#309).
#: folder_edit_dot = 거기에 우상단 점(강조색) — 카드 경로가 전역 설정과 다를 때
#: 저장 위치 버튼이 "다르다"를 표시한다(#245, #309).
#: scissors = 카드 위 줄의 구간 편집 버튼 — 가위(자를 구간을 정한다, #309).
#: check = 하나만 고르는 메뉴의 고른 항목 표시(✓) — 헤더바 저장 위치 · 정렬 메뉴(#495).
#: stop = 하단바의 중지(■) — 받는 중 · 일시정지 카드를 대기로 되돌린다(#504).
#: sort = 좁은 폭 헤더바의 정렬 아이콘(⇅) — 위 화살표 · 아래 화살표 나란히(#529).
#: star · undo = 드롭다운 메뉴 동작 줄의 아이콘 — 「지금 위치를 즐겨찾기에」 · 「기본 위치로」(#536).
#: plus = 헤더바 등록(+), chevron_down = 펼침 버튼의 ⌄, folder_outline = 메뉴 「폴더 찾기…」(테두리 폴더),
#: link · memory · disk = 하단바 연결 · 메모리 · 여유 칸 — 프로토 그림을 옮겼다(#536).
ICON_NAMES = (
    "plus",
    "chevron_down",
    "folder_outline",
    "link",
    "memory",
    "disk",
    "star",
    "undo",
    "pause",
    "resume",
    "retry",
    "folder",
    "folder_edit",
    "folder_edit_dot",
    "delete",
    "scissors",
    "check",
    "stop",
    "sort",
)

_CACHE: dict[tuple[str, str, int, float, str], QPixmap] = {}


def action_pixmap(name: str, color: str, size: int, dpr: float = 1.0, accent: str = "") -> QPixmap:
    """조작 아이콘 도형을 `size`px 정사각 픽스맵으로 돌려준다 — 캐시 공유.

    같은 (이름, 색, 크기, 배율, 강조색)은 항상 같은 QPixmap 객체다. 배율(dpr)
    만큼 크게 그린 뒤 `setDevicePixelRatio`로 논리 크기를 맞춰 HiDPI에서도
    선명하다. `accent`는 두 색을 쓰는 도형(folder_edit_dot의 점)만 읽는다.
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


def _paint_folder_edit(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """저장 위치 변경 — 폴더 몸통의 우하단에 비스듬한 연필.

    폴더는 면으로 칠하므로 같은 색의 연필은 그 위에서 보이지 않는다. 연필 둘레를 먼저 투명하게
    파내고 그 안에 연필을 그린다 — 삭제(✕) · 가위와 같은 굵기 계열의 선이다.
    """
    _paint_folder(painter, s, color, accent)
    tip, top = QPointF(s * 0.50, s * 0.94), QPointF(s * 0.90, s * 0.54)
    painter.save()
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
    _stroke(painter, color, s * 0.36)
    painter.drawLine(tip, top)
    painter.restore()
    _stroke(painter, color, s * 0.14)
    painter.drawLine(QPointF(s * 0.60, s * 0.84), top)
    _fill(painter, color)
    painter.drawPolygon(
        QPolygonF(
            [
                QPointF(s * 0.44, s * 1.00),
                QPointF(s * 0.49, s * 0.83),
                QPointF(s * 0.61, s * 0.95),
            ]
        )
    )


def _paint_folder_edit_dot(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """저장 위치 변경 + 우상단 점(강조색) — "이 카드의 경로는 전역과 다르다" 표시."""
    _paint_folder_edit(painter, s, color, accent)
    _fill(painter, accent)
    radius = s * 0.17
    painter.drawEllipse(QPointF(s * 0.82, s * 0.20), radius, radius)


def _paint_scissors(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """구간 편집 — 가위. 엇갈린 날 둘과 아래의 손잡이 고리 둘. 삭제(✕)와 같은 굵기 계열의 선이다."""
    _stroke(painter, color, s * 0.12)
    painter.drawLine(QPointF(s * 0.34, s * 0.60), QPointF(s * 0.84, s * 0.12))
    painter.drawLine(QPointF(s * 0.66, s * 0.60), QPointF(s * 0.16, s * 0.12))
    ring = s * 0.14
    painter.drawEllipse(QPointF(s * 0.25, s * 0.76), ring, ring)
    painter.drawEllipse(QPointF(s * 0.75, s * 0.76), ring, ring)


def _paint_delete(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """삭제 — 대각선 둘(✕)."""
    _stroke(painter, color, s * 0.16)
    painter.drawLine(QPointF(s * 0.22, s * 0.22), QPointF(s * 0.78, s * 0.78))
    painter.drawLine(QPointF(s * 0.78, s * 0.22), QPointF(s * 0.22, s * 0.78))


def _paint_check(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """고른 항목 — 꺾인 선 하나(✓). 삭제(✕)와 같은 굵기 계열의 선이다."""
    _stroke(painter, color, s * 0.16)
    painter.drawPolyline(
        QPolygonF(
            [QPointF(s * 0.16, s * 0.54), QPointF(s * 0.40, s * 0.78), QPointF(s * 0.86, s * 0.24)]
        )
    )


def _paint_stop(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """중지 — 꽉 찬 정사각형(■). 일시정지 막대 둘과 같은 높이로 맞춘다."""
    _fill(painter, color)
    radius = s * 0.08
    painter.drawRoundedRect(QRectF(s * 0.10, s * 0.10, s * 0.80, s * 0.80), radius, radius)


def _paint_sort(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """정렬 — 왼쪽은 위로, 오른쪽은 아래로 가는 화살표(⇅). 좌표는 프로토 정렬 그림(16 격자)이다."""
    _stroke(painter, color, s * 0.11)
    unit = s / 16
    painter.drawLine(QPointF(5.2 * unit, 13 * unit), QPointF(5.2 * unit, 3.4 * unit))
    painter.drawPolyline(
        QPolygonF(
            [
                QPointF(2.6 * unit, 6 * unit),
                QPointF(5.2 * unit, 3.4 * unit),
                QPointF(7.8 * unit, 6 * unit),
            ]
        )
    )
    painter.drawLine(QPointF(10.8 * unit, 3 * unit), QPointF(10.8 * unit, 12.6 * unit))
    painter.drawPolyline(
        QPolygonF(
            [
                QPointF(8.2 * unit, 10 * unit),
                QPointF(10.8 * unit, 12.6 * unit),
                QPointF(13.4 * unit, 10 * unit),
            ]
        )
    )


def _paint_star(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """별 — 꼭짓점 다섯의 윤곽선. 좌표는 프로토 메뉴 별 그림(16 격자)이다."""
    _stroke(painter, color, s * 0.094)
    unit = s / 16
    points = [
        (8, 2.2), (9.8, 6), (13.9, 6.4), (10.8, 9.1), (11.7, 13.2),
        (8, 11.1), (4.3, 13.2), (5.2, 9.1), (2.1, 6.4), (6.2, 6),
    ]  # fmt: skip
    painter.drawPolygon(QPolygonF([QPointF(x * unit, y * unit) for x, y in points]))


def _paint_undo(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """되돌리기 — 왼쪽을 가리키는 꺾쇠와 아래로 돌아오는 고리. 좌표는 프로토 메뉴 되돌리기 그림(16 격자)이다."""
    _stroke(painter, color, s * 0.1)
    unit = s / 16
    painter.drawPolyline(
        QPolygonF(
            [
                QPointF(5.4 * unit, 3.2 * unit),
                QPointF(2.6 * unit, 6 * unit),
                QPointF(5.4 * unit, 8.8 * unit),
            ]
        )
    )
    path = QPainterPath(QPointF(2.8 * unit, 6 * unit))
    path.lineTo(9.2 * unit, 6 * unit)
    path.arcTo(QRectF(5.6 * unit, 6 * unit, 7.2 * unit, 7.2 * unit), 90, -180)
    path.lineTo(6.4 * unit, 13.2 * unit)
    painter.drawPath(path)


LINE_WIDTH = 0.11  # 선 아이콘 굵기 — 한 변에 대한 비율(프로토 11v 새 아이콘 선 · 폴더 윤곽과 같다)


def _paint_plus(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """더하기 — 헤더바 등록 버튼. 좌표는 프로토 등록 + 그림이다."""
    _stroke(painter, color, s * LINE_WIDTH)
    painter.drawLine(QPointF(s * 0.5, s * 0.18), QPointF(s * 0.5, s * 0.82))
    painter.drawLine(QPointF(s * 0.18, s * 0.5), QPointF(s * 0.82, s * 0.5))


def _paint_chevron_down(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """아래 꺾쇠(⌄) — 펼침 버튼의 펼침 표시. 좌표는 프로토 ▾ SVG(10 격자, 선 1.6)이다."""
    _stroke(painter, color, s * 0.16)
    unit = s / 10
    painter.drawPolyline(
        QPolygonF(
            [
                QPointF(1.8 * unit, 3.6 * unit),
                QPointF(5 * unit, 6.8 * unit),
                QPointF(8.2 * unit, 3.6 * unit),
            ]
        )
    )


def _paint_folder_outline(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """닫힌 폴더 윤곽 — 탭이 있는 상자 하나, 선만. 좌표는 프로토 폴더 윤곽 그림이다."""
    _stroke(painter, color, s * LINE_WIDTH)
    points = [(0.12, 0.80), (0.12, 0.24), (0.38, 0.24), (0.46, 0.32), (0.88, 0.32), (0.88, 0.80)]
    painter.drawPolygon(QPolygonF([QPointF(s * x, s * y) for x, y in points]))


def _paint_link(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """연결 — 서로 걸친 둥근 고리 둘(하단바 연결 수). 좌표는 프로토 연결 그림이다."""
    _stroke(painter, color, s * LINE_WIDTH)
    painter.save()
    painter.translate(s * 0.5, s * 0.5)
    painter.rotate(-45)
    width, height, radius = s * 0.46, s * 0.26, s * 0.13
    painter.drawRoundedRect(QRectF(-width + s * 0.06, -height / 2, width, height), radius, radius)
    painter.drawRoundedRect(QRectF(-s * 0.06, -height / 2, width, height), radius, radius)
    painter.restore()


def _paint_memory(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """메모리 — 칩 상자와 위아래 다리 셋씩(하단바 메모리). 좌표는 프로토 메모리 그림이다."""
    _stroke(painter, color, s * LINE_WIDTH)
    painter.drawRoundedRect(QRectF(s * 0.24, s * 0.24, s * 0.52, s * 0.52), s * 0.06, s * 0.06)
    for x in (0.36, 0.5, 0.64):
        painter.drawLine(QPointF(s * x, s * 0.08), QPointF(s * x, s * 0.2))
        painter.drawLine(QPointF(s * x, s * 0.8), QPointF(s * x, s * 0.92))


def _paint_disk(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """디스크 — 납작한 상자와 오른쪽 점(하단바 여유 공간). 좌표는 프로토 여유 공간 그림이다."""
    _stroke(painter, color, s * LINE_WIDTH)
    painter.drawRoundedRect(QRectF(s * 0.1, s * 0.3, s * 0.8, s * 0.4), s * 0.08, s * 0.08)
    _fill(painter, color)
    painter.drawEllipse(QPointF(s * 0.72, s * 0.5), s * 0.06, s * 0.06)


_PAINTERS = {
    "plus": _paint_plus,
    "chevron_down": _paint_chevron_down,
    "folder_outline": _paint_folder_outline,
    "link": _paint_link,
    "memory": _paint_memory,
    "disk": _paint_disk,
    "star": _paint_star,
    "undo": _paint_undo,
    "sort": _paint_sort,
    "pause": _paint_pause,
    "resume": _paint_resume,
    "retry": _paint_retry,
    "folder": _paint_folder,
    "folder_edit": _paint_folder_edit,
    "folder_edit_dot": _paint_folder_edit_dot,
    "delete": _paint_delete,
    "scissors": _paint_scissors,
    "check": _paint_check,
    "stop": _paint_stop,
}


#: 메뉴 상태 버블의 상태 — 우선순위 차례(위가 먼저, 설계 §2). 「없음」은 버블을 숨긴다(#523).
BUBBLE_STATES = ("running", "paused", "all_failed", "some_failed", "done")

#: 버블 상태 → 원 색 토큰 — 진행 막대 · 상태 글과 같은 상태 색(설계 §12.1). 일부 실패는 경고 색.
BUBBLE_FILL_TOKENS = {
    "running": "stateRunning",
    "paused": "statePaused",
    "all_failed": "stateFailed",
    "some_failed": "stateWarning",
    "done": "stateFinished",
}

_BUBBLE_CACHE: dict[tuple[str, str, str, int, float], QPixmap] = {}


def bubble_pixmap(state: str, fill: str, glyph: str, size: int, dpr: float = 1.0) -> QPixmap:
    """메뉴 상태 버블 — `fill` 색 원 안에 `glyph` 색 기호를 그린 `size`px 정사각 픽스맵(#523).

    기호는 글자(↓ ‖ ✕ ! ✓)가 아니라 도형이다 — 글꼴에 따라 모양 · 자리가 달라지지 않는다(설계 §2).
    같은 (상태, 색, 크기, 배율)은 같은 QPixmap 객체다. OS 알림 아이콘도 이 그림을 크기마다 쓴다.
    """
    if state not in BUBBLE_STATES:
        raise ValueError(f"알 수 없는 버블 상태: {state!r} (허용: {BUBBLE_STATES})")
    key = (state, fill, glyph, size, dpr)
    cached = _BUBBLE_CACHE.get(key)
    if cached is not None:
        return cached
    pixmap = QPixmap(round(size * dpr), round(size * dpr))
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    _fill(painter, QColor(fill))
    painter.drawEllipse(QRectF(0, 0, size, size))
    painter.scale(size / _BUBBLE_GRID, size / _BUBBLE_GRID)
    _BUBBLE_GLYPHS[state](painter, QColor(glyph))
    painter.end()
    _BUBBLE_CACHE[key] = pixmap
    return pixmap


#: 버블 기호를 그리는 격자 한 변 — 좌표는 0..16 정사각 안(원 지름 = 16).
_BUBBLE_GRID = 16.0


def _glyph_down(painter: QPainter, color: QColor) -> None:
    """받는 중 — 아래 화살표(↓). 화살촉이 아래에 몰려 있어 전체를 0.8 내려 원 가운데에 맞춘다."""
    _stroke(painter, color, 1.8)
    painter.drawLine(QPointF(8, 4.6), QPointF(8, 11.2))
    painter.drawPolyline(QPolygonF([QPointF(5, 8.4), QPointF(8, 11.4), QPointF(11, 8.4)]))


def _glyph_pause(painter: QPainter, color: QColor) -> None:
    """일시정지 — 세로 막대 둘(‖)."""
    _stroke(painter, color, 1.9)
    painter.drawLine(QPointF(6.2, 5), QPointF(6.2, 11))
    painter.drawLine(QPointF(9.8, 5), QPointF(9.8, 11))


def _glyph_cross(painter: QPainter, color: QColor) -> None:
    """전부 실패 — 엇갈린 선 둘(✕)."""
    _stroke(painter, color, 1.8)
    painter.drawLine(QPointF(5.3, 5.3), QPointF(10.7, 10.7))
    painter.drawLine(QPointF(10.7, 5.3), QPointF(5.3, 10.7))


def _glyph_bang(painter: QPainter, color: QColor) -> None:
    """일부 실패 — 세로 선과 점(!)."""
    _stroke(painter, color, 1.9)
    painter.drawLine(QPointF(8, 4.2), QPointF(8, 8.9))
    _fill(painter, color)
    painter.drawEllipse(QPointF(8, 11.5), 1.1, 1.1)


def _glyph_tick(painter: QPainter, color: QColor) -> None:
    """완료 — 꺾인 선 하나(✓)."""
    _stroke(painter, color, 1.8)
    painter.drawPolyline(QPolygonF([QPointF(4.5, 8.3), QPointF(6.8, 10.5), QPointF(11.5, 5.7)]))


_BUBBLE_GLYPHS = {
    "running": _glyph_down,
    "paused": _glyph_pause,
    "all_failed": _glyph_cross,
    "some_failed": _glyph_bang,
    "done": _glyph_tick,
}


class IconButton(QPushButton):
    """도형 아이콘을 그리는 정사각 조작 버튼 (#245).

    배경·테두리(호버 표면)는 부모 `paintEvent`(전역 QSS `[role="icon"]`)가
    그리고, 그 위에 공유 캐시의 도형 픽스맵을 얹는다. 색 토큰은
    평소 `textMuted`, 호버 시 `hoverToken()`(기본 `text`, 삭제는 `stateFailed`),
    비활성 `textDisabled`. 인스턴스가 드는 것은 문자열 둘뿐이다.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._icon_name = ""
        self._idle_token = "textMuted"
        self._hover_token = "text"
        self._accent_token = ""
        self._glyph_metric = "actionGlyph"  # 도형 한 변의 크기 토큰(theme.METRICS 키)
        self._interactive = True
        self.setProperty("interactive", True)
        # 호버 진입·이탈에 다시 그리게 한다(QSS :hover 규칙이 없어도)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def setInteractive(self, interactive: bool) -> None:
        """조작 표면(호버 강조)을 켜고 끈다 — 눌러도 아무 일도 없는 상태에서는 끈다 (#244 P-5).

        도형 색은 그대로(정보는 남는다) 두고 호버 반응만 없앤다: 도형은 colorToken이
        호버 토큰을 건너뛰고, 배경·테두리는 전역 QSS `[role="icon"][interactive="false"]:hover`가
        이 속성을 보고 지운다. 경로 아이콘이 대기 밖에서 "누르면 뭔가 된다"고 말하던 칸.
        """
        if interactive == self._interactive:
            return
        self._interactive = interactive
        self.setProperty("interactive", interactive)
        theme.repolish(self)

    def isInteractive(self) -> bool:
        """호버 강조가 켜져 있는가(눌러서 되는 상태인가)."""
        return self._interactive

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
        """두 색 도형(folder_edit_dot의 점)의 강조색 토큰. 빈 문자열이면 본체 색을 따른다."""
        if token != self._accent_token:
            self._accent_token = token
            self.update()

    def accentToken(self) -> str:
        """강조색 토큰 이름(없으면 빈 문자열)."""
        return self._accent_token

    def setGlyphMetric(self, key: str) -> None:
        """도형 한 변의 크기 토큰(theme.METRICS 키)을 정한다 — 기본은 카드 조작의 `actionGlyph`.

        하단바 버튼(#504)은 카드 버튼보다 큰 `barGlyph`를 쓴다.
        """
        if key not in theme.METRICS:
            raise ValueError(f"알 수 없는 크기 토큰: {key!r}")
        if key != self._glyph_metric:
            self._glyph_metric = key
            self.update()

    def glyphSize(self) -> int:
        """지금 그리는 도형 한 변(px)."""
        return theme.METRICS[self._glyph_metric]

    def colorToken(self) -> str:
        """지금 도형에 쓸 색 토큰 — 비활성 > 호버 > 평소 순으로 정한다."""
        if not self.isEnabled():
            return "textDisabled"
        if self.underMouse() and self._interactive:
            return self._hover_token
        return self._idle_token

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self._icon_name:
            return
        size = self.glyphSize()
        tokens = theme.current_tokens()
        color = tokens[self.colorToken()]
        accent = tokens[self._accent_token] if self._accent_token else ""
        pixmap = action_pixmap(self._icon_name, color, size, self.devicePixelRatioF(), accent)
        painter = QPainter(self)
        painter.drawPixmap((self.width() - size) // 2, (self.height() - size) // 2, pixmap)
        painter.end()
