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

선 굵기 · 선 끝 · 그림 칸 규칙은 아래 「아이콘 규칙」 절의 상수가 정한다(inbox 177).

색은 `theme.py` 토큰 이름으로만 고른다(평소 `textMuted` / 호버 `text` 또는
버튼별 지정 / 비활성 `textDisabled`) — 이 파일에는 색 리터럴이 없다
(`tests/app/gui/test_theme.py`의 단일 정의 게이트). 배경·테두리(호버 표면)는
전역 QSS의 `QPushButton[role="icon"]` 규칙이 그대로 담당한다.
"""

import math

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
#: folder = 완료 카드의 폴더 열기 · 헤더 저장 위치 버튼(열린 폴더 윤곽).
#: scissors = 카드 위 줄의 구간 편집 버튼 — 가위(자를 구간을 정한다, #309).
#: check = 하나만 고르는 메뉴의 고른 항목 표시(✓) — 헤더바 저장 위치 · 정렬 메뉴(#495).
#: stop = 하단바의 중지(■) — 받는 중 · 일시정지 카드를 대기로 되돌린다(#504).
#: sort = 좁은 폭 헤더바의 정렬 아이콘(⇅) — 위 화살표 · 아래 화살표 나란히(#529).
#: star · undo = 드롭다운 메뉴 동작 줄의 아이콘 — 「지금 위치를 즐겨찾기에」 · 「기본 위치로」(#536).
#: plus = 헤더바 등록(+), chevron_down = 펼침 버튼의 ⌄, folder_outline = 메뉴 「폴더 찾기…」(테두리 폴더),
#: link · memory · disk = 하단바 연결 · 메모리 · 여유 칸 — 프로토 그림을 옮겼다(#536).
#: download = 하단바 전체 속도 칸의 ↓ — 글자 대신 그린다(inbox 177).
#: edit = 대기 카드의 카드 편집 창 열기(✎, #545).
#: select = 헤더바 선택 버튼(체크박스 — 둥근 상자 + ✓), trash = 선택 줄 지우기(휴지통) — 프로토 그림(#561).
#: gear = 메뉴바 오른쪽 끝 설정 버튼(톱니바퀴 — 바퀴 · 축 구멍 · 톱니 여섯, #611).
ICON_NAMES = (
    "gear",
    "select",
    "trash",
    "edit",
    "plus",
    "chevron_down",
    "folder_outline",
    "link",
    "memory",
    "disk",
    "download",
    "star",
    "undo",
    "pause",
    "resume",
    "retry",
    "folder",
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
    선명하다. `accent`는 두 색을 쓰는 도형의 둘째 색이다(지금은 쓰는 도형이 없다).
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


# ---- 아이콘 규칙 (inbox 177) ----
#
# 동작 아이콘(누르는 것 · 버튼 안)은 모두 선 그림이다 — 선 하나의 굵기(`STROKE`) · 둥근 끝 · 둥근 이음.
# 굵기는 그림 칸 크기와 무관한 논리 px다: 칸에 비례하게 두면 카드(12)와 바(16)의 같은 선이 다른 굵기로 보인다.
# 면으로 칠할 수 있는 것은 지름이 선 굵기를 넘지 않는 점(`_dot`)뿐이다.
# 그림 칸은 둘 — 카드 버튼(상자 20) 안 `actionGlyph` 12, 바 버튼(상자 32) · 메뉴 줄 · 하단 칸의 16. 상자가
# 다르니 칸도 다르다: 20 상자에 16을 넣으면 호버 면과 그림 사이가 2px밖에 남지 않는다.
# 잉크(선 굵기까지 친 그림의 범위)의 긴 변은 칸의 `INK_MIN` ~ `INK_MAX`다 — 칸 안에서 차지하는 크기를 맞춘다.
# 상태 아이콘(상태 원 · 메뉴 버블 · 체크 원)은 상태 색 원 + 안쪽 기호이고, 안쪽 기호의 선은 16 격자에서
# 동작 아이콘과 같은 `STROKE`다(원 지름에 비례 — 체크 원 20은 2.1875).

# 동작 아이콘 선 굵기(논리 px) — 칸 크기와 무관하게 하나. 1.5는 배율 1에서 두 화소를 75%씩만 칠해 흐려
# 보였다(inbox 177). 1.75는 87.5%이고 지금 헤더 아이콘(16 × 0.11 = 1.76)과 같은 무게다
STROKE = 1.75
STATUS_STROKE = (
    STROKE  # 상태 아이콘 안쪽 기호의 선 굵기 — 원 지름 16 격자 단위(지름에 비례해 커진다)
)
STATUS_GRID = 16.0  # 상태 아이콘 기호를 그리는 격자 한 변 — 좌표는 0..16(원 지름 = 16)
DOT_MAX = STROKE  # 면으로 칠하는 점의 지름 상한(px) — 이보다 크면 면 그림이다
INK_MIN = 0.72  # 잉크 긴 변 / 그림 칸 하한 — 이보다 작으면 옆 아이콘보다 작아 보인다
INK_MAX = 0.95  # 잉크 긴 변 / 그림 칸 상한 — 이보다 크면 버튼 호버 면에 닿아 보인다
# 아이콘 그림 칸 크기 토큰(theme.METRICS 키) — 카드 버튼 12(actionGlyph), 바 · 메뉴 줄 · 하단 칸 16(나머지)
GLYPH_METRICS = ("actionGlyph", "barGlyph", "narrowIcon", "menuIcon")


def _stroke(painter: QPainter, color: QColor) -> None:
    """선 그림 펜을 건다 — 굵기 `STROKE` · 둥근 끝 · 둥근 이음, 면 없음. 동작 아이콘의 선은 모두 이것으로 긋는다."""
    pen = QPen(color, STROKE)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)


def _dot(painter: QPainter, color: QColor, center: QPointF, diameter: float) -> None:
    """작은 점 하나를 면으로 칠한다 — 지름은 `DOT_MAX` 이하(그보다 크면 선 그림 규칙을 벗어난다)."""
    if diameter > DOT_MAX + 1e-9:
        raise ValueError(f"점 지름 {diameter} > {DOT_MAX}")
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    radius = diameter / 2
    painter.drawEllipse(center, radius, radius)
    painter.restore()


def _points(s: float, coords: list[tuple[float, float]]) -> QPolygonF:
    """칸 비율 좌표 목록 → 한 변 s의 점 목록."""
    return QPolygonF([QPointF(s * x, s * y) for x, y in coords])


def _paint_pause(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """일시정지 — 세로 선 둘."""
    _stroke(painter, color)
    painter.drawLine(QPointF(s * 0.34, s * 0.18), QPointF(s * 0.34, s * 0.82))
    painter.drawLine(QPointF(s * 0.66, s * 0.18), QPointF(s * 0.66, s * 0.82))


def _paint_resume(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """재개 · 다운로드 — 오른쪽을 향한 삼각형 윤곽(재생)."""
    _stroke(painter, color)
    painter.drawPolygon(_points(s, [(0.26, 0.16), (0.84, 0.50), (0.26, 0.84)]))


def _paint_stop(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """중지 — 둥근 정사각형 윤곽(■). 일시정지 선 둘과 같은 높이다."""
    _stroke(painter, color)
    radius = s * 0.12
    painter.drawRoundedRect(QRectF(s * 0.18, s * 0.18, s * 0.64, s * 0.64), radius, radius)


def _paint_retry(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """재시도 — 오른쪽에서 시계 방향으로 도는 원호와 우상단의 ㄱ자 화살촉(↻). 좌표는 24 격자다.

    원호는 오른쪽(0°)에서 시계 방향으로 315° 돌아 우상단(45°)에서 끝나고, 짧은 선이 화살촉 모서리로 잇는다.
    화살촉은 면이 아니라 모서리에서 위 · 왼쪽으로 뻗는 선 둘이다 — 원호 끝에 꺾쇠를 붙이면 12px에서 「U」로
    읽혔다(inbox 177 contact sheet).
    """
    _stroke(painter, color)
    unit = s / 24
    path = QPainterPath(QPointF(21 * unit, 12 * unit))
    # Qt 각도는 반시계가 양수 — 음수 span이 시계 방향이다
    path.arcTo(QRectF(3 * unit, 3 * unit, 18 * unit, 18 * unit), 0, -315)
    path.lineTo(21 * unit, 8 * unit)
    painter.drawPath(path)
    painter.drawPolyline(
        QPolygonF(
            [
                QPointF(21 * unit, 3 * unit),
                QPointF(21 * unit, 8 * unit),
                QPointF(16 * unit, 8 * unit),
            ]
        )
    )


def _paint_folder(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """폴더 열기 — 뒤판(탭 달린)과 앞으로 기운 덮개의 윤곽(열린 폴더). 닫힌 폴더(`folder_outline`)와 모양을 가른다."""
    _stroke(painter, color)
    path = QPainterPath(QPointF(s * 0.78, s * 0.40))
    for x, y in (
        (0.78, 0.28),
        (0.46, 0.28),
        (0.38, 0.20),
        (0.12, 0.20),
        (0.12, 0.80),
        (0.76, 0.80),
        (0.88, 0.44),
        (0.27, 0.44),
        (0.12, 0.80),
    ):
        path.lineTo(s * x, s * y)
    painter.drawPath(path)


def _paint_scissors(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """구간 편집 — 가위. 엇갈린 날 둘과 아래의 손잡이 고리 둘."""
    _stroke(painter, color)
    painter.drawLine(QPointF(s * 0.34, s * 0.60), QPointF(s * 0.82, s * 0.14))
    painter.drawLine(QPointF(s * 0.66, s * 0.60), QPointF(s * 0.18, s * 0.14))
    ring = s * 0.13
    painter.drawEllipse(QPointF(s * 0.26, s * 0.74), ring, ring)
    painter.drawEllipse(QPointF(s * 0.74, s * 0.74), ring, ring)


def _paint_delete(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """삭제 — 대각선 둘(✕)."""
    _stroke(painter, color)
    painter.drawLine(QPointF(s * 0.18, s * 0.18), QPointF(s * 0.82, s * 0.82))
    painter.drawLine(QPointF(s * 0.82, s * 0.18), QPointF(s * 0.18, s * 0.82))


def _paint_check(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """고른 항목 — 꺾인 선 하나(✓). 꺾는 자리는 프로토 메뉴 ✓ SVG(16 격자, #536)를 가운데 기준 1.15배로 키웠다.

    색은 부르는 쪽이 본문 글 색(text)으로 준다 — 프로토도 같은 역할의 글 색이다.
    """
    _stroke(painter, color)
    unit = s / 16
    points = ((3.2, 8.4), (6.4, 11.4), (12.8, 4.8))
    painter.drawPolyline(
        QPolygonF(
            [QPointF((8 + (x - 8) * 1.15) * unit, (8 + (y - 8) * 1.15) * unit) for x, y in points]
        )
    )


def _paint_sort(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """정렬 — 왼쪽은 위로, 오른쪽은 아래로 가는 화살표(⇅). 좌표는 프로토 정렬 그림(16 격자)이다."""
    _stroke(painter, color)
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
    _stroke(painter, color)
    unit = s / 16
    points = [
        (8, 2.2), (9.8, 6), (13.9, 6.4), (10.8, 9.1), (11.7, 13.2),
        (8, 11.1), (4.3, 13.2), (5.2, 9.1), (2.1, 6.4), (6.2, 6),
    ]  # fmt: skip
    painter.drawPolygon(QPolygonF([QPointF(x * unit, y * unit) for x, y in points]))


def _paint_undo(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """되돌리기 — 왼쪽을 가리키는 꺾쇠와 아래로 돌아오는 고리. 좌표는 프로토 메뉴 되돌리기 그림(16 격자)이다."""
    _stroke(painter, color)
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


def _paint_plus(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """더하기 — 헤더바 등록 버튼."""
    _stroke(painter, color)
    painter.drawLine(QPointF(s * 0.5, s * 0.16), QPointF(s * 0.5, s * 0.84))
    painter.drawLine(QPointF(s * 0.16, s * 0.5), QPointF(s * 0.84, s * 0.5))


def _paint_chevron_down(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """아래 꺾쇠(⌄) — 펼침 버튼의 펼침 표시. 좌표는 프로토 ▾ SVG(10 격자)이다."""
    _stroke(painter, color)
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
    _stroke(painter, color)
    painter.drawPolygon(
        _points(
            s, [(0.12, 0.80), (0.12, 0.22), (0.38, 0.22), (0.46, 0.30), (0.88, 0.30), (0.88, 0.80)]
        )
    )


def _paint_link(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """연결 — 서로 걸친 둥근 고리 둘(하단바 연결 수). 좌표는 프로토 연결 그림이다."""
    _stroke(painter, color)
    painter.save()
    painter.translate(s * 0.5, s * 0.5)
    painter.rotate(-45)
    width, height, radius = s * 0.46, s * 0.26, s * 0.13
    painter.drawRoundedRect(QRectF(-width + s * 0.06, -height / 2, width, height), radius, radius)
    painter.drawRoundedRect(QRectF(-s * 0.06, -height / 2, width, height), radius, radius)
    painter.restore()


def _paint_memory(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """메모리 — 칩 상자와 위아래 다리 셋씩(하단바 메모리). 좌표는 프로토 메모리 그림이다."""
    _stroke(painter, color)
    painter.drawRoundedRect(QRectF(s * 0.24, s * 0.26, s * 0.52, s * 0.48), s * 0.06, s * 0.06)
    for x in (0.36, 0.5, 0.64):
        painter.drawLine(QPointF(s * x, s * 0.12), QPointF(s * x, s * 0.22))
        painter.drawLine(QPointF(s * x, s * 0.78), QPointF(s * x, s * 0.88))


def _paint_disk(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """디스크 — 납작한 상자와 오른쪽 점(하단바 여유 공간). 좌표는 프로토 여유 공간 그림이다."""
    _stroke(painter, color)
    painter.drawRoundedRect(QRectF(s * 0.12, s * 0.3, s * 0.76, s * 0.4), s * 0.08, s * 0.08)
    _dot(painter, color, QPointF(s * 0.70, s * 0.5), DOT_MAX)


def _paint_download(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """받기 — 아래 화살표(하단바 전체 속도 칸). 글자 「↓」 대신 그린다(inbox 177)."""
    _stroke(painter, color)
    painter.drawLine(QPointF(s * 0.5, s * 0.14), QPointF(s * 0.5, s * 0.84))
    painter.drawPolyline(_points(s, [(0.24, 0.58), (0.5, 0.84), (0.76, 0.58)]))


def _paint_edit(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """편집 — 연필. 왼쪽 아래 촉에서 오른쪽 위로 기운 막대 하나와 지우개 쪽 금 하나(#545)."""
    _stroke(painter, color)
    path = QPainterPath()
    path.moveTo(s * 0.16, s * 0.84)  # 촉 끝
    path.lineTo(s * 0.22, s * 0.60)
    path.lineTo(s * 0.66, s * 0.16)
    path.lineTo(s * 0.84, s * 0.34)
    path.lineTo(s * 0.40, s * 0.78)
    path.closeSubpath()
    painter.drawPath(path)
    painter.drawLine(QPointF(s * 0.56, s * 0.26), QPointF(s * 0.74, s * 0.44))


def _paint_gear(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """톱니바퀴 — 톱니 여섯을 두른 바퀴 윤곽 한 줄과 축 구멍(설정, #611).

    톱니 여덟 · 작은 축 구멍은 16px에서 선이 몰려 다른 아이콘보다 무거워 보였다(오너 실기 2026-11-16). 톱니를
    여섯으로 줄이고 짧게 해 골 사이를 넓히고, 축 구멍을 키웠다 — 잉크 긴 변은 칸의 0.78(16) · 0.83(12)이다.
    """
    _stroke(painter, color)
    center = s / 2
    outer, inner = s * 0.35, s * 0.28  # 톱니 끝 · 골 반지름 — 톱니 높이 0.07(16에서 1.1px)
    tip, root = math.radians(14), math.radians(22)  # 톱니 끝 · 뿌리의 반각 — 톱니 사이 60°
    points = []
    for k in range(6):
        middle = math.pi / 3 * k - math.pi / 2  # 맨 위에 톱니 하나
        for radius, angle in (
            (inner, middle - root),
            (outer, middle - tip),
            (outer, middle + tip),
            (inner, middle + root),
        ):
            points.append(
                QPointF(center + radius * math.cos(angle), center + radius * math.sin(angle))
            )
    painter.drawPolygon(QPolygonF(points))
    painter.drawEllipse(QPointF(center, center), s * 0.13, s * 0.13)


def _paint_select(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """선택 — 둥근 상자 하나와 그 안의 ✓(#561). 좌표는 프로토 선택 버튼 그림이다."""
    _stroke(painter, color)
    painter.drawRoundedRect(QRectF(s * 0.14, s * 0.14, s * 0.72, s * 0.72), s * 0.12, s * 0.12)
    painter.drawPolyline(_points(s, [(0.32, 0.5), (0.45, 0.63), (0.68, 0.38)]))


def _paint_trash(painter: QPainter, s: float, color: QColor, accent: QColor) -> None:
    """휴지통 — 뚜껑 · 손잡이 · 아래로 좁아지는 통 · 세로 줄 둘(#561). 좌표는 프로토 선택 줄 지우기 그림이다."""
    _stroke(painter, color)
    painter.drawLine(QPointF(s * 0.16, s * 0.28), QPointF(s * 0.84, s * 0.28))
    painter.drawPolyline(_points(s, [(0.40, 0.28), (0.40, 0.14), (0.60, 0.14), (0.60, 0.28)]))
    painter.drawPolyline(_points(s, [(0.26, 0.28), (0.31, 0.86), (0.69, 0.86), (0.74, 0.28)]))
    painter.drawLine(QPointF(s * 0.44, s * 0.42), QPointF(s * 0.45, s * 0.72))
    painter.drawLine(QPointF(s * 0.56, s * 0.42), QPointF(s * 0.55, s * 0.72))


_PAINTERS = {
    "gear": _paint_gear,
    "select": _paint_select,
    "trash": _paint_trash,
    "edit": _paint_edit,
    "plus": _paint_plus,
    "chevron_down": _paint_chevron_down,
    "folder_outline": _paint_folder_outline,
    "link": _paint_link,
    "memory": _paint_memory,
    "disk": _paint_disk,
    "download": _paint_download,
    "star": _paint_star,
    "undo": _paint_undo,
    "sort": _paint_sort,
    "pause": _paint_pause,
    "resume": _paint_resume,
    "retry": _paint_retry,
    "folder": _paint_folder,
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
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(fill))
    painter.drawEllipse(QRectF(0, 0, size, size))
    painter.scale(size / STATUS_GRID, size / STATUS_GRID)
    _BUBBLE_GLYPHS[state](painter, QColor(glyph))
    painter.end()
    _BUBBLE_CACHE[key] = pixmap
    return pixmap


def _status_pen(painter: QPainter, color: QColor) -> None:
    """상태 아이콘 안쪽 기호의 펜 — 16 격자에서 `STATUS_STROKE` · 둥근 끝 · 둥근 이음(동작 아이콘과 같은 선)."""
    pen = QPen(color, STATUS_STROKE)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)


def _glyph_down(painter: QPainter, color: QColor) -> None:
    """받는 중 — 아래 화살표(↓). 화살촉이 아래에 몰려 있어 전체를 0.8 내려 원 가운데에 맞춘다."""
    _status_pen(painter, color)
    painter.drawLine(QPointF(8, 4.6), QPointF(8, 11.2))
    painter.drawPolyline(QPolygonF([QPointF(5, 8.4), QPointF(8, 11.4), QPointF(11, 8.4)]))


def _glyph_pause(painter: QPainter, color: QColor) -> None:
    """일시정지 — 세로 막대 둘(‖)."""
    _status_pen(painter, color)
    painter.drawLine(QPointF(6.2, 5), QPointF(6.2, 11))
    painter.drawLine(QPointF(9.8, 5), QPointF(9.8, 11))


def _glyph_cross(painter: QPainter, color: QColor) -> None:
    """전부 실패 — 엇갈린 선 둘(✕)."""
    _status_pen(painter, color)
    painter.drawLine(QPointF(5.3, 5.3), QPointF(10.7, 10.7))
    painter.drawLine(QPointF(10.7, 5.3), QPointF(5.3, 10.7))


def _glyph_bang(painter: QPainter, color: QColor) -> None:
    """일부 실패 — 세로 선과 점(!). 점 지름은 선보다 조금 크다(2.2) — 선과 같으면 점이 선 끝처럼 보인다."""
    _status_pen(painter, color)
    painter.drawLine(QPointF(8, 4.2), QPointF(8, 8.9))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawEllipse(QPointF(8, 11.5), 1.1, 1.1)


def _glyph_tick(painter: QPainter, color: QColor) -> None:
    """완료 — 꺾인 선 하나(✓)."""
    _status_pen(painter, color)
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
        """두 색 도형의 강조색 토큰. 빈 문자열이면 본체 색을 따른다."""
        if token != self._accent_token:
            self._accent_token = token
            self.update()

    def accentToken(self) -> str:
        """강조색 토큰 이름(없으면 빈 문자열)."""
        return self._accent_token

    def setGlyphMetric(self, key: str) -> None:
        """도형 한 변의 크기 토큰(theme.METRICS 키)을 정한다 — 기본은 카드 조작의 `actionGlyph`.

        하단바 버튼(#504)은 카드 버튼보다 큰 `barGlyph`를 쓴다. 아이콘 그림 칸 토큰(`GLYPH_METRICS`) 밖은
        받지 않는다 — 그림 칸은 카드 12 · 바 16 둘이다(inbox 177).
        """
        if key not in GLYPH_METRICS:
            raise ValueError(f"아이콘 그림 칸 토큰이 아니다: {key!r} (허용: {GLYPH_METRICS})")
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
