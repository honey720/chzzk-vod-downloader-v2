"""앱 전역 테마의 단일 정의 지점 — 색·모서리·간격 토큰과 QSS 로더 (#227).

**왜 파이썬에 토큰을 두고 QSS를 치환하는가.**
#227의 요구는 "색상값을 한 곳에서만 정의"다. 그런데 색이 필요한 곳이
두 군데로 갈린다 — (a) 앱 전역 스타일시트(`resources/qss/style.qss`),
(b) 카드 상태별 테두리처럼 파이썬이 런타임에 골라 적용해야 하는 값
(`app/widgets/widget.py`). QSS 파일에 색을 직접 박으면 (b)가 파이썬 상수로
복제되고, 파이썬에만 두면 QSS가 못 읽는다. 그래서 **정의는 여기 한 곳**
(`DARK`)에 두고, `.qss`는 `@토큰` 자리표시자만 쓰는 "규칙 파일"로
남긴다 — 로드 시점에 `load_stylesheet()`가 치환한다.

**이 파일 하나가 그 이음매였다 — 그리고 실제로 그 이음매에서 회귀가 났다.**
#227이 `app.setStyle("Fusion")` + 고정 다크 `QPalette`를 들이면서, v2.9.6까지
네이티브 스타일이 OS 라이트/다크 설정을 그대로 따라가던 동작이 조용히
사라졌다(PR #234 리뷰에서 오너 실기로 발견). "기능 무변화" 게이트가 이
동작을 보존 대상으로 명시하지 않았던 것이 원인이다.

그래서 이제 `LIGHT = {...}` 딕셔너리가 `DARK` 옆에 있고, `current_tokens()`가
`set_color_scheme()`으로 설정된 값을 보고 고른다. `resources/qss/style.qss`와
호출부(`CVDv2.py`, `app/widgets/widget.py`)는 토큰 *이름*만 알기 때문에 이 변경에
손대지 않아도 됐다 — 설계했던 대로다. 실제 OS 감지는 `detect_color_scheme()`이
한다(`CVDv2.py`가 시작 시점에 호출해 `set_color_scheme()`에 넘긴다). `current_tokens()`
자체는 감지를 하지 않고 마지막으로 설정된 값만 본다 — 그래야 테스트가 실제
OS 설정에 기대지 않고 `set_color_scheme()`으로 원하는 테마를 주입할 수 있고,
그 값을 아무도 설정하지 않은 채 호출되는 기존 테스트들(`test_theme.py`,
`test_widget_theme.py`)은 모듈 기본값(`"dark"`)으로 예전과 동일하게 동작한다.

**QSS는 지원하지 않는 것을 조용히 무시한다** — 에러도 경고도 없다.
그래서 이 파일이 쓰는 것은 전부 순정 QSS가 지원하는 속성뿐이고,
지원하지 않는 것(`box-shadow`, `.class` 선택자, `transition`)은 아예
쓰지 않는다. 상태 구분은 QSS가 지원하는 **동적 속성 선택자**
(`setProperty("state", ...)` + `[state="..."]`)로 낸다 — 이건 파이썬과
QSS 양쪽을 같이 손봐야 동작한다(파이썬에서 속성만 바꾸면 이미 계산된
스타일이 갱신되지 않아 `repolish()`도 함께 필요하다).
"""

import logging
import math
import re
import sys
from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QComboBox, QProxyStyle, QStyle, QStyleFactory

logger = logging.getLogger(__name__)

# .qss 자리표시자 문법: @토큰이름 (SCSS 변수처럼 읽힌다).
# `{}`를 쓰는 str.format은 QSS 중괄호와 충돌해 전부 이스케이프해야 하므로 쓰지 않는다.
_TOKEN_RE = re.compile(r"@([A-Za-z][A-Za-z0-9_]*)")

# 앱 전역 스타일시트 위치 — 호출부(CVDv2.py)가 resource_path()로 절대경로를
# 만들어 넘긴다. resources/ 전체가 이미 Nuitka `--include-data-dir=resources=resources`로
# 번들되므로 이 하위 경로는 배포 워크플로 수정 없이 그대로 따라 들어간다
# (#216의 `.js` 누락은 `resources/` 밖의 별도 폴더였던 게 원인이다).
QSS_RELATIVE_PATH = "resources/qss/style.qss"

# ═══════════════ 오너 튜닝 구역 — 크기·간격·글자 위계 ═══════════════
# 여기 숫자를 바꾸고 `uv run python CVDv2.py`로 바로 확인할 수 있다.
# 색은 아래 DARK/LIGHT 표에서 바꾼다(다크/라이트가 서로 달라야 해서 분리).
# METRICS의 정수는 파이썬 코드가 직접 쓰고, 동시에 "이름+px" 문자열로
# DARK/LIGHT 표에 자동 주입되어 .qss의 @토큰으로도 쓰인다(아래 주입 루프).

#: 크기·간격(px 정수). 다크/라이트 공통.
METRICS = {
    # ---- 구간 창 ----
    "sectionRowGap": 4,  # 구간 행 안 손잡이 · 번호 · 칸 · 길이 · ✕ 사이(px) — 프로토 구간 행(#586)
    # 구간 행 호버 띠 ↔ 입력칸 줄 위아래 여백(px). 행 위에 이만큼 빈 자리를 두어 띠가 이웃 행의
    # 입력칸 · 안내 글에 닿지 않는다(#586)
    "sectionRowHoverPad": 4,
    # ---- 카드 ----
    "cardPadding": 8,  # 카드 안쪽 여백 — 썸네일·글이 테두리에서 떨어지는 거리
    "cardRowSpacing": 4,  # 카드 안 행 간격(4의 배수 권장) — 줄이면 카드가 낮아져 밀도↑
    "cardRadius": 8,  # 카드 모서리 둥글기 — 카드 · 메뉴 창 8(설계 §11.7, #550)
    "cardThumbWidth": 110,  # 카드 썸네일 폭(px) — 글 묶음 높이(62)에 맞춘 16:9(설계 §11.1, #550)
    "cardThumbHeight": 62,  # 카드 썸네일 높이(px) — 1줄 위끝 ~ 3줄 아래끝
    "cardThumbCompactWidth": 68,  # 썸네일 줄이기의 폭(px) — 위 두 줄 높이(38)에 맞춘 16:9(설계 §12.1, #554)
    "cardThumbCompactHeight": 38,  # 썸네일 줄이기의 높이(px) — 1줄 위끝 ~ 2줄 아래끝(16 + 4 + 18)
    "cardThumbGap": 12,  # 썸네일 ↔ 글(px) — 프로토 THUMB_GAP(#550)
    "cardRow1Height": 16,  # 카드 1줄(채널 그림 · 채널명 · 날짜) 높이(px)
    "cardRow2Height": 18,  # 카드 2줄(제목) 높이(px)
    "cardRow3Height": 20,  # 카드 3줄(상태 · 정보) 높이(px) — 설계 §11.3 「3줄 20」
    "cardGap": 8,  # 카드 사이(px) — 설계 §11.2(#550)
    "cardButton": 20,  # 카드 버튼 한 변(px) — 지금 앱과 같은 20×20(설계 §12.1 카드 배치)
    "cardStatusIcon": 16,  # 3줄 상태 원 아이콘 지름(px) — 메뉴 버블과 같은 그림
    # 고른 카드 테두리 두께(px) — 테두리 1px 자리를 강조색으로 덮고 안쪽으로 1px 더(프로토 SELECT_BORDER, #561).
    # 카드 면이 그린다 — QSS 테두리 폭을 바꾸면 안쪽 자리가 줄어 줄 배치가 1px 움직인다
    "cardSelectBorder": 2,
    # 선택 모드의 체크 원(#561) — 썸네일 왼쪽 위에 얹는다. 썸네일 위라 카드 폭 · 줄 배치는 그대로다
    "cardPickMark": 20,  # 체크 원 지름(px) — 카드 버튼(cardButton)과 같은 크기
    "cardPickMarkCompact": 16,  # 썸네일 줄이기의 체크 원 지름(px) — 썸네일 높이 38에서 그림을 덜 가린다
    "cardPickMarkInset": 4,  # 썸네일 왼쪽 · 위 끝 ↔ 체크 원(px)
    # 카드의 키보드 focus ring(#561) — 목록이 초점 카드 바깥에 하나만 그린다(app/widgets/focus_ring.py).
    # 띄움 + 굵기 = 4 — 카드 사이(cardGap 8)의 절반을 쓴다
    "focusRingGap": 2,  # 카드 테두리 바깥 끝 ↔ focus ring(px)
    "focusRingWidth": 2,  # focus ring 굵기(px)
    # ---- 선택 줄(#561 — 설계 §12.1 「선택 모드」, 프로토 shell.py SELECT_*) ----
    "selectBarGap": 4,  # 헤더바 아래끝 ↔ 선택 줄 위끝 — 말풍선 ↔ 대상(tipGap)과 같은 떠 있는 것 간격
    "selectBarPad": 8,  # 선택 줄 왼쪽 안쪽 여백 — 「N개 선택」 앞(§11.2 8)
    "selectCheckBox": 16,  # 세 상태 체크박스 네모 한 변 — 아이콘 도형(16)과 같은 크기
    "selectCheckWidth": 24,  # 세 상태 체크박스 위젯 폭 — 네모 16 + 좌우 4(누르는 자리)
    "selectSepHeight": 16,  # 선택 줄의 글 ↔ 버튼 묶음 구분선 높이 — 버튼 안 도형(16)과 같은 높이
    "thumbRadius": 8,  # 썸네일 모서리 — 카드보다 작아야 안쪽이 자연스럽다
    "barHeight": 4,  # 하단 진행바 두께(진행분이 있을 때 — 진행·일시정지 — 보임)
    "actionGlyph": 12,  # 조작 아이콘(일시정지·재개·재시도·폴더·삭제) 도형 한 변 — cardButton 버튼 안에 그려진다
    "barGlyph": 14,  # 하단바 아이콘 버튼(▶ ‖ ■) 도형 한 변 — 버튼은 글 버튼 높이의 정사각이다(#504)
    # ---- 메뉴바 상태 버블(#523, 설계 §2) — 메뉴 버튼 오른쪽 안쪽 여백에 늘 잡아 두는 자리 ----
    # 수치는 프로토(shell.py BADGE_SIDE 16 · 버블 자리 여백 29 · 버튼 오른끝 ↔ 버블 7)를 따른다(오너 실기 2026-11-07)
    "menuBubble": 16,  # 버블(원) 지름 — 기호는 16 격자에 그려 이 크기에서 프로토와 같다
    "menuBubbleGap": 7,  # 메뉴 이름 글 ↔ 버블 사이
    "menuBubbleInset": 7,  # 버튼 오른쪽 바깥 끝(테두리 포함) ↔ 버블
    # ---- 상단·하단 바 & 창 ----
    "framePadding": 8,  # 상단·하단 바 안쪽 여백 — cardPadding과 같으면 정렬선이 관통
    "frameRadius": 8,  # 상단·하단 바 모서리
    # 창 가장자리 ↔ 바 안 내용 · 카드 바깥 끝의 좌우 여백 — 메뉴바 · 헤더바 · 하단바의 안쪽 가로
    # 여백과 카드 목록이 이 값 하나를 읽는다(§11.1). 12: 스크롤바(scrollBarWidth 8)가 보일 때 카드 ↔
    # 스크롤바가 4라 focus ring(띄움 2 + 굵기 2)이 네 변 모두 같은 모양으로 들어간다. 16은 상단 버튼이
    # 가운데로 몰려 보였다(오너 실기 2026-11-11, #561 · #580)
    "outerMargin": 12,
    # ---- 스크롤바(style.qss QScrollBar — 앱 전체의 세로 · 가로 스크롤바가 함께 쓴다) ----
    "scrollBarWidth": 8,  # 세로 스크롤바 폭 · 가로 스크롤바 높이 — 카드 목록 오른쪽 여백(outerMargin) 안에 든다
    "scrollHandleRadius": 4,  # 손잡이 모서리 — 폭의 절반이라 손잡이 양끝이 반원이다
    "initialWidthMax": 800,  # 첫 실행 초기 창 폭의 상한 — 콘텐츠 최대폭이 아니다(#253, 유저는 얼마든지 넓힐 수 있다)
    # ---- 셸 바 컨트롤(#529, 설계 §11.2 · §11.3 · §11.7) ----
    "barControl": 32,  # 바 컨트롤(메뉴 버튼 등) 높이
    "controlRadius": 6,  # 바 컨트롤 모서리
    "barPadY": 8,  # 메뉴바 안쪽 여백 세로 — 창 위끝 ↔ 메뉴 버튼
    "narrowIcon": 16,  # 좁은 폭 아이콘 모양(정렬 ⇅ · 저장 위치 📁) 도형 한 변 — 아이콘 한 벌 크기(§11.3)
    "sortDot": 3,  # 기본 정렬이 아닐 때 정렬 아이콘 상자 안 점의 반지름(프로토 SORT_DOT)
    "sortDotInset": 7,  # 상자 오른쪽 · 위 끝 ↔ 점 가운데(프로토 SortIconButton)
    # ---- 드롭다운 메뉴 D(#536, 설계 §12.1 「드롭다운 메뉴」 · 프로토 shell.py MENU_*) ----
    "menuRadius": 8,  # 메뉴 창 모서리(떠 있는 것 8 — §11.7)
    "menuVPad": 4,  # 메뉴 위아래 안쪽 여백
    "menuRowInset": 4,  # 호버 면 ↔ 메뉴 테두리
    "menuRowPad": 8,  # 줄 안 좌우 · 위아래 여백 — 아이콘 칸은 이 뒤에서 시작한다
    "menuIcon": 16,  # 모든 줄의 아이콘 칸(✓ · 별 · 폴더 · 되돌리기) 한 변
    "menuIconGap": 8,  # 아이콘 칸 ↔ 글
    "menuHoverRadius": 4,  # 호버 면 모서리
    "menuOffset": 4,  # 메뉴를 버튼 아래 이만큼 띄운다
    "menuNamePx": 14,  # 항목 이름 글 크기
    "menuDetailPx": 12,  # 흐린 경로 · 머리글 글 크기
    # ---- 말풍선(툴팁, #536 — 설계 §2 「말풍선」 · 프로토 card.py TIP_*) ----
    "tipGap": 4,  # 꼬리 끝 ↔ 대상
    "tipTail": 6,  # 꼬리 길이
    "tipTailWidth": 12,  # 꼬리 밑변
    "tipRadius": 8,  # 말풍선 모서리
    "tipPadX": 8,  # 안쪽 여백 가로
    "tipPadY": 4,  # 안쪽 여백 세로
    "tipShadow": 6,  # 그림자 자리 — 꼬리 쪽에는 두지 않는다
    "tipFontPx": 12,  # 말풍선 글 크기(보조 글 — §11.4)
    "tipMaxWidth": 360,  # 이보다 긴 글은 접는다
    # ---- 공통 형태 ----
    "radius": 8,  # 버튼·입력창 등 일반 모서리
    "buttonPadX": 14,  # 기본 버튼 좌우 안쪽 여백
}

#: 글자 위계 — 위(제목)에서 아래(경로)로 무게가 내려간다. 값은 QSS 문자열.
FONTS = {
    "fontSizeTitle": "14px",  # 카드 제목 — 굵게(설계 §11.4 「크기 둘 14 · 12」, #550)
    "fontSizeBase": "14px",  # 앱 기본(입력창·버튼 등)
    "fontSizeMeta": "12px",  # 카드의 제목 밖 글(채널명 · 날짜 · 상태 · 저장 위치 · 정보) — #550
}

#: 다크 테마 토큰 — 이 저장소의 모든 색·모서리·간격 값의 유일한 정의 지점.
DARK = {
    # ---- 바탕 ----
    "windowBg": "#1b1c1f",  # 창 배경
    "surface": "#24262b",  # 헤더·하단 바 같은 패널
    "surfaceAlt": "#2f3239",  # 기본 버튼·입력창 바탕
    "surfaceHover": "#383c44",
    "surfacePressed": "#282b31",
    "menuHover": "#2c2f35",  # 드롭다운 메뉴 호버 면 — 바 면(surface)에서 한 단계(ΔE00 약 2.9, #536)
    "rowHoverBg": "#242629",  # 창 바탕 위 목록 행(구간 행) 호버 띠 — 창 바탕에서 밝게 한 단계(ΔE00 약 3.2, #586)
    "shadow": "#000000",  # 말풍선 그림자 — 그릴 때 불투명도를 낮춰 쓴다(#536)
    # 등록 오류 말풍선 면 — 실패 빨강의 색상을 옅게 띤 어두운 면. 글(text) 11.67 · 테두리(stateFailed) 5.10(#536)
    "errorTipBg": "#3d2326",
    # 말풍선(툴팁) 떠 있는 면 — 카드(cardBg)보다 한 단계 밝게(프로토 demoFloat 대비 1.25 규칙, 흐린 글 4.5를 지키게
    # 1.23). 바 면과 1.36 · 글 9.10 · 흐린 글(단축키) 4.56(#536)
    "tipBg": "#393b43",
    "thumbBg": "#1f2126",  # 썸네일 자리표시 바탕
    # ---- 선·글자 ----
    "border": "#3a3d45",
    "borderStrong": "#4c515b",
    "text": "#e6e8ec",
    "textMuted": "#a0a6b0",
    "textDisabled": "#6b7078",
    # ---- 강조(주 동작 버튼) — 채움 색 규칙(#512, 설계 §12.1): 다크는 채움이 밝은 톤, 그 안 글자 · 그림은
    # 어둡게. 채움 안 글자 ↔ 채움 · 호버 · 눌림 모두 4.5 이상(tests/app/gui/test_theme_contrast.py) ----
    "accent": "#3b8cff",
    "accentHover": "#559bff",
    "accentPressed": "#3f84e9",
    "onAccent": "#1b1d22",
    # 강조색 채움 버튼의 키보드 focus ring — 채움 · 바 면 모두와 3.0 이상(#536). 카드 바깥의 focus ring도
    # 이 색이다 — 창 바탕(카드 사이) · 고름 테두리(강조색) 모두와 3.0 이상(#561)
    "focusOnAccent": "#ffffff",
    # ---- 카드 ----
    # 선택 모드 체크 원의 테두리 — 썸네일 그림 위라 두 테마가 같은 흰색이다. 강조색 채움과 3.0 이상(#561)
    "pickMarkRing": "#ffffff",
    # 안 고른 체크 원의 바탕 — 그릴 때 불투명도(PICK_MARK_SCRIM_ALPHA)를 낮춰 쓴다. 밝은 썸네일 위에서도
    # 흰 테두리가 보인다(#561)
    "pickMarkScrim": "#000000",
    "cardBg": "#2b2d33",
    "cardHoverBg": "#35363c",  # 카드 호버 바탕 — 카드 면에서 밝게 한 단계(ΔE00 약 3.0, 설계 §12.1, #550)
    # ---- 상태색 (상태 아이콘·상태 텍스트·진행바 공용) ----
    # running/failed는 기존 값을 그대로 승계한다 — 실패 빨강(#FF6969)은
    # tests/app/gui/test_failure_display.py가 상태 표시에서 직접 확인한다.
    # 상태 글은 같은 색상에서 카드 바탕 위 4.5 이상인 가장 가까운 밝기(#512 — 대기 2.76 · 일시정지 4.13이었다)
    "stateWaiting": "#8f949b",  # 대기 — 회색
    "stateRunning": "#55B5FF",  # 진행 — 파랑
    # 일시정지 — 진행 파랑의 채도를 뺀 강철빛 회색. 진행바가 그대로 남되
    # "돌고 있지 않다"가 색으로 읽혀야 한다(#245). 대기 회색과는 달라야
    # 한다(상태색은 정보를 나른다 — tests/app/gui/test_theme.py 상호 구별 게이트).
    "statePaused": "#8695a8",
    "stateFinished": "#4CC38A",  # 완료 — 초록
    "stateFailed": "#FF6969",  # 실패 — 빨강
    # 경고 · 정보(#523, 설계 §12.1 상태 색) — 원래 색의 OKLCH 색상각 그대로 밝기만 옮긴 값: 기호(onAccent) ↔ 원
    # 4.5, 원 ↔ 평소 바탕(메뉴바 windowBg · 고른 메뉴 surfaceAlt · OS 알림 바탕 #2c2c2c) 3.0을 지키는 가장
    # 가까운 밝기. 다크는 원래 값이 그대로 통과한다(tests/app/gui/test_theme_contrast.py)
    "stateWarning": "#f0a33c",  # 경고 — 주황(일부 실패 버블)
    "stateInfo": "#55b5ff",  # 정보 — 진행 색(설계 §11.5)
    # ---- 진행바 ----
    "barTrack": "#3a3d45",
    "scrollHandle": "#4c515b",
    "scrollHandleHover": "#5e646f",
}

#: 라이트 테마 토큰 — 키 집합은 DARK와 반드시 같아야 한다(.qss와 카드 규칙이
#: 두 표를 구분 없이 참조한다). 상태색 중 running/finished/failed는 DARK의
#: 파스텔 톤을 그대로 쓰면 흰 배경 위에서 대비가 낮아 거의 안 보인다(카드
#: 테두리 1px, 진행바 칸 채우기 둘 다 실측 확인) — 그래서 이 세 값만 더
#: 짙게 눌렀다. stateWaiting(#6b7078)은 원래도 중간 회색이라 흰 배경에서도
#: 대비가 충분해 그대로 재사용한다. accent/onAccent도 그대로 재사용한다 —
#: 이 값이 칠하는 곳은 항상 accent 배경 위(버튼 자체) 아니면 accent 배경 위
#: 흰 글자라 창 바탕 밝기와 무관하다.
LIGHT = {
    # ---- 바탕 ----
    "windowBg": "#f2f3f5",
    "surface": "#ffffff",
    "surfaceAlt": "#eceef1",
    "surfaceHover": "#e1e4e9",
    "surfacePressed": "#d5d9df",
    "menuHover": "#f0f1f3",  # 드롭다운 메뉴 호버 면 — 바 면(surface)에서 한 단계(ΔE00 약 3.0, #536)
    "rowHoverBg": "#e4e5e6",  # 창 바탕 위 목록 행(구간 행) 호버 띠 — 창 바탕에서 어둡게 한 단계(ΔE00 약 3.0, #586)
    "shadow": "#000000",  # 말풍선 그림자 — 그릴 때 불투명도를 낮춰 쓴다(#536)
    # 등록 오류 말풍선 면 — 실패 빨강의 색상을 옅게 띤 밝은 면. 글(text) 14.61 · 테두리(stateFailed) 4.59(#536)
    "errorTipBg": "#fdecec",
    # 말풍선(툴팁) 떠 있는 면 — 라이트는 바 · 카드가 흰색이라 더 밝은 면이 없다: 흰 면 + 진한 테두리(borderStrong)로
    # 띄운다(프로토 「흰 카드」 경우와 같다). 글 16.69(#536)
    "tipBg": "#ffffff",
    "thumbBg": "#e6e8ec",
    # ---- 선·글자 ----
    "border": "#d5d8de",
    "borderStrong": "#b9bec7",
    "text": "#1c1e22",
    "textMuted": "#5b6069",
    "textDisabled": "#9a9fa8",
    # ---- 강조(주 동작 버튼) ----
    # 채움 색 규칙(#512): 라이트는 채움이 어두운 톤, 그 안 글자 · 그림은 흰색
    "accent": "#0062ec",
    "accentHover": "#146df8",
    "accentPressed": "#044fbf",
    "onAccent": "#ffffff",
    # 강조색 채움 버튼의 키보드 focus ring — 채움 · 바 면 모두와 3.0 이상(#536). 카드 바깥의 focus ring도
    # 이 색이다 — 창 바탕(카드 사이) · 고름 테두리(강조색) 모두와 3.0 이상(#561)
    "focusOnAccent": "#1b1d22",
    # ---- 카드 ----
    # 선택 모드 체크 원의 테두리 — 썸네일 그림 위라 두 테마가 같은 흰색이다. 강조색 채움과 3.0 이상(#561)
    "pickMarkRing": "#ffffff",
    # 안 고른 체크 원의 바탕 — 그릴 때 불투명도(PICK_MARK_SCRIM_ALPHA)를 낮춰 쓴다. 밝은 썸네일 위에서도
    # 흰 테두리가 보인다(#561)
    "pickMarkScrim": "#000000",
    "cardBg": "#ffffff",
    "cardHoverBg": "#f0f0f0",  # 카드 호버 바탕 — 카드 면에서 어둡게 한 단계(ΔE00 약 3.0, 설계 §12.1, #550)
    # ---- 상태색 (상태 아이콘·상태 텍스트·진행바 공용) — 흰 배경 대비용으로 짙게 눌렀다 ----
    "stateWaiting": "#6b7078",
    "stateRunning": "#1f6fd6",
    "statePaused": "#6a778b",  # 일시정지 — 흰 배경에서 진행 파랑과 구별되는 무채색 강철빛
    "stateFinished": "#288654",  # #512 — 카드 바탕 위 4.5(3.39였다)
    "stateFailed": "#d02727",  # #512 — 카드 · 헤더바 · 버튼 면(실패 칩) 위 모두 4.5(버튼 면 3.99였다)
    # #523 — 주황 #f0a33c의 색상각 그대로 밝기만 낮춤(흰 기호 4.5 · 바탕 3.0, 화면 색 범위 안으로 채도만 조금 줄어듦)
    "stateWarning": "#a66904",
    "stateInfo": "#1f6fd6",  # 정보 — 진행 색(설계 §11.5)
    # ---- 진행바 ----
    "barTrack": "#d5d8de",  # = border — 진행 막대 트랙은 카드 테두리 색(설계 §12.1, #512)
    "scrollHandle": "#c3c7cd",
    "scrollHandleHover": "#aeb3bb",
}

# ---- 카드 색조 · 채움 색(#554 — 설계 §8의 6 · §12.1) ----
# 완료 · 실패 색조와 배경 채움 색은 리터럴로 적지 않고 카드 면 · 상태 색에서 셈한다. 색상은 상태 색의
# OKLCH 색상각 그대로, 밝기는 카드 면과 같게 두고 채도로 세기(카드 면과의 ΔE00)를 맞춘다 — 투명도로
# 섞으면 카드 면의 기운 때문에 색상이 상태 색에서 어긋난다. 그 위 글(본문 · 보조 · 상태 글)은 4.5를 지킨다.
TINT_DE00 = {
    "dark": 12.0,
    "light": 10.0,
}  # 색조 · 채움 ↔ 카드 면 ΔE00 목표 — 설계 §8의 6(오너 2026-11-02)
TINT_TEXT_CONTRAST = 4.5  # 색조 · 채움 위 글의 대비 하한 — 설계 §11.5
# 눌린 카드 바탕 ↔ 호버 바탕 ΔE00 목표 — 카드 면 → 호버와 같은 크기로 한 단계 더(#561)
PRESS_DE00 = 3.0
SRGB_EPSILON = 1e-6  # sRGB 범위 판정 허용 오차 — 0 · 1 끝 채널이 셈 오차로 밖이 되지 않게

# ---- 선택 모드의 체크 원(#561) ----
# 정수 px가 아닌 값이라 METRICS(QSS에 "Npx"로 들어간다)에 두지 않는다
PICK_MARK_RING_WIDTH = 1.5  # 체크 원 테두리 두께(px) — 1은 밝은 썸네일에 묻힌다
PICK_MARK_SCRIM_ALPHA = 0.5  # 안 고른 원 바탕의 불투명도 — 흰 썸네일 위에서도 테두리와 3.0 이상이다
PICK_MARK_CHECK_WIDTH = 2.0  # ✓ 선 굵기(px) — 지름 20일 때. 지름에 비례해 줄인다


def _linear(value: float) -> float:
    """sRGB 0~1 → 선형."""
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _gamma(value: float) -> float:
    """선형 → sRGB 0~1."""
    return 12.92 * value if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055


def _rgb(color: str) -> tuple[float, float, float]:
    """ "#rrggbb" → 선형 RGB."""
    return tuple(_linear(int(color[i : i + 2], 16) / 255) for i in (1, 3, 5))


def _hex(rgb: tuple[float, float, float]) -> str:
    """선형 RGB → "#rrggbb"(범위 밖은 자른다)."""
    return "#" + "".join(
        f"{round(min(max(_gamma(min(max(v, 0.0), 1.0)), 0.0), 1.0) * 255):02x}" for v in rgb
    )


def _oklab(color: str) -> tuple[float, float, float]:
    """ "#rrggbb" → OKLab (L, a, b)."""
    r, g, b = _rgb(color)
    lms = (
        (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3),
        (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3),
        (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3),
    )
    return (
        0.2104542553 * lms[0] + 0.7936177850 * lms[1] - 0.0040720468 * lms[2],
        1.9779984951 * lms[0] - 2.4285922050 * lms[1] + 0.4505937099 * lms[2],
        0.0259040371 * lms[0] + 0.7827717662 * lms[1] - 0.8086757660 * lms[2],
    )


def _oklab_to_linear(lab: tuple[float, float, float]) -> tuple[float, float, float]:
    """OKLab → 선형 RGB — sRGB 범위(0 ~ 1) 밖의 값을 자르지 않고 돌려준다."""
    big_l, a, b = lab
    l_ = (big_l + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (big_l - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (big_l - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return (
        4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
        -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
        -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_,
    )


def _in_srgb(lab: tuple[float, float, float]) -> bool:
    """OKLab 색이 sRGB 범위 안인지 — 채널을 자르지 않고 그대로 나타낼 수 있는지."""
    return all(-SRGB_EPSILON <= value <= 1 + SRGB_EPSILON for value in _oklab_to_linear(lab))


def _from_oklab(lab: tuple[float, float, float]) -> str:
    """OKLab → "#rrggbb"(sRGB 범위 밖의 채널은 자른다)."""
    return _hex(_oklab_to_linear(lab))


def _cielab(color: str) -> tuple[float, float, float]:
    """ "#rrggbb" → CIELAB(D65)."""
    r, g, b = _rgb(color)
    xyz = (
        (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / 0.95047,
        0.2126729 * r + 0.7151522 * g + 0.0721750 * b,
        (0.0193339 * r + 0.1191920 * g + 0.9503041 * b) / 1.08883,
    )
    f = [t ** (1 / 3) if t > 216 / 24389 else (24389 / 27 * t + 16) / 116 for t in xyz]
    return 116 * f[1] - 16, 500 * (f[0] - f[1]), 200 * (f[1] - f[2])


def delta_e00(first: str, second: str) -> float:
    """두 색의 CIEDE2000 색 차이."""
    l1, a1, b1 = _cielab(first)
    l2, a2, b2 = _cielab(second)
    c_bar = (math.hypot(a1, b1) + math.hypot(a2, b2)) / 2
    g = 0.5 * (1 - math.sqrt(c_bar**7 / (c_bar**7 + 25**7)))
    a1p, a2p = a1 * (1 + g), a2 * (1 + g)
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360 if c1p else 0.0
    h2p = math.degrees(math.atan2(b2, a2p)) % 360 if c2p else 0.0
    dl, dc = l2 - l1, c2p - c1p
    dh = 0.0
    if c1p * c2p:
        dh = h2p - h1p
        dh = dh - 360 if dh > 180 else dh + 360 if dh < -180 else dh
    big_dh = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(dh / 2))
    l_bar, cp_bar = (l1 + l2) / 2, (c1p + c2p) / 2
    if c1p * c2p == 0:
        hp_bar = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        hp_bar = (h1p + h2p) / 2
    else:
        hp_bar = (h1p + h2p + 360) / 2 if h1p + h2p < 360 else (h1p + h2p - 360) / 2
    t = (
        1
        - 0.17 * math.cos(math.radians(hp_bar - 30))
        + 0.24 * math.cos(math.radians(2 * hp_bar))
        + 0.32 * math.cos(math.radians(3 * hp_bar + 6))
        - 0.20 * math.cos(math.radians(4 * hp_bar - 63))
    )
    sl = 1 + 0.015 * (l_bar - 50) ** 2 / math.sqrt(20 + (l_bar - 50) ** 2)
    sc, sh = 1 + 0.045 * cp_bar, 1 + 0.015 * cp_bar * t
    rt = (
        -2
        * math.sqrt(cp_bar**7 / (cp_bar**7 + 25**7))
        * math.sin(math.radians(60 * math.exp(-(((hp_bar - 275) / 25) ** 2))))
    )
    return math.sqrt(
        (dl / sl) ** 2 + (dc / sc) ** 2 + (big_dh / sh) ** 2 + rt * (dc / sc) * (big_dh / sh)
    )


def contrast_ratio(first: str, second: str) -> float:
    """WCAG 대비."""

    def luminance(color: str) -> float:
        r, g, b = _rgb(color)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    light, dark = sorted((luminance(first), luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def _tinted(
    base: str,
    hue_from: str | None,
    target: float,
    texts: tuple[str, ...],
    keep_hue: bool = False,
) -> str:
    """`base`에서 `hue_from`의 색상각으로 채도만 올려 ΔE00 `target`이 되는 색 — 글 대비를 지키는 만큼만.

    `hue_from`이 None이면 무채색 그대로 밝기만 옮긴다(일시정지 회색 채움 — 어두운 면이면 밝게, 밝은
    면이면 어둡게). 밝은 면(흰 카드)은 밝기를 그대로 두고 채도만 올리면 sRGB 밖으로 나가 잘리므로, 채도를
    올린 만큼의 절반을 밝기에서 뺀다. 이분 탐색으로 정하고, `texts`의 글이 4.5 아래로 내려가면 그 아래
    세기에서 멈춘다.

    `keep_hue`면 그래도 sRGB 밖으로 나가는 색은 채널을 자르지 않고 채도를 그 색상각에서 들어가는 만큼으로
    줄인다 — 채널을 자르면 색상각이 밀린다(라이트 받는 중 채움이 하늘색 쪽으로 41° 밀렸다, #582). 지금은
    받는 중 채움만 쓴다 — 다른 색조에 넣으면 라이트 실패 색조가 ΔE00 1 넘게 바뀐다.
    """
    big_l, _a, _b = _oklab(base)

    def color_at(amount: float) -> str:
        if hue_from is None:
            step = amount if big_l < 0.6 else -amount
            return _from_oklab((big_l + step, 0.0, 0.0))
        _l, ha, hb = _oklab(hue_from)
        hue = math.atan2(hb, ha)
        lightness = big_l - amount / 2 if big_l > 0.85 else big_l
        chroma = _chroma_in_srgb(lightness, hue, amount) if keep_hue else amount
        return _from_oklab((lightness, chroma * math.cos(hue), chroma * math.sin(hue)))

    def readable(color: str) -> bool:
        return all(contrast_ratio(text, color) >= TINT_TEXT_CONTRAST for text in texts)

    low, high = 0.0, 0.4
    for _ in range(40):
        middle = (low + high) / 2
        color = color_at(middle)
        if delta_e00(base, color) < target and readable(color):
            low = middle
        else:
            high = middle
    return color_at(low)


def _chroma_in_srgb(lightness: float, hue: float, chroma: float) -> float:
    """그 밝기 · 색상각(라디안)에서 `chroma`를 넘지 않고 sRGB 안에 드는 가장 큰 채도 — 이분 탐색."""

    def fits(value: float) -> bool:
        return _in_srgb((lightness, value * math.cos(hue), value * math.sin(hue)))

    if fits(chroma):
        return chroma
    low, high = 0.0, chroma
    for _ in range(40):
        middle = (low + high) / 2
        if fits(middle):
            low = middle
        else:
            high = middle
    return low


def _hover_of(tint: str, card: str, hover: str) -> str:
    """색조 면의 호버 — 카드 면 → 호버 바탕과 같은 OKLab 밝기 차를 색조에 옮긴다(설계 §12.1 카드 호버)."""
    big_l, a, b = _oklab(tint)
    return _from_oklab((big_l + _oklab(hover)[0] - _oklab(card)[0], a, b))


def _pressed_of(card: str, hover: str) -> str:
    """눌린 카드 바탕 — 호버 바탕에서 카드 면 → 호버와 같은 쪽으로 밝기만 더 옮겨 ΔE00 `PRESS_DE00`이 되는 색.

    다크는 더 밝게, 라이트는 더 어둡게 간다(#561). 색상 · 채도는 호버 바탕 그대로다.
    """
    big_l, a, b = _oklab(hover)
    direction = 1.0 if big_l >= _oklab(card)[0] else -1.0
    low, high = 0.0, 0.2
    for _ in range(40):
        middle = (low + high) / 2
        if delta_e00(hover, _from_oklab((big_l + direction * middle, a, b))) < PRESS_DE00:
            low = middle
        else:
            high = middle
    return _from_oklab((big_l + direction * low, a, b))


def _readable_on(color: str, grounds: tuple[str, ...]) -> str:
    """`color`의 색상 · 채도는 두고 OKLab 밝기만 옮겨 모든 `grounds` 위 4.5가 되는 가장 가까운 색.

    이미 4.5면 그대로다. 어두운 면이면 밝게, 밝은 면이면 어둡게 옮긴다.
    """
    if all(contrast_ratio(color, ground) >= TINT_TEXT_CONTRAST for ground in grounds):
        return color
    big_l, a, b = _oklab(color)
    direction = 1.0 if _oklab(grounds[0])[0] < 0.6 else -1.0
    step = 0.0
    while step < 1.0:
        step += 0.005
        moved = _from_oklab((big_l + direction * step, a, b))
        if all(contrast_ratio(moved, ground) >= TINT_TEXT_CONTRAST for ground in grounds):
            return moved
    return color


def _card_tints(table: dict, scheme: str) -> None:
    """완료 · 실패 색조(+ 호버 · 그 위 상태 글 색)와 받는 중 · 일시정지 채움 색을 표에 더한다.

    색조 세기는 목표(ΔE00)를 먼저 정한다(설계 §8의 6). 본문 · 보조 글은 그 세기에서 4.5를 지키는지
    본다. 상태 글(완료 초록 · 실패 빨강)은 색조 위에서 4.5가 되게 같은 색상의 밝기만 옮긴 색을 따로 둔다
    (`state…OnTint`) — 라이트의 완료 초록은 흰 카드에서 이미 4.5라 색조가 조금만 생겨도 4.5 아래로 내려간다.

    눌린 바탕(`…Pressed`, #561)은 호버에서 한 단계 더 간 색이다 — 색조는 카드 면 → 눌린 바탕의 밝기 차를,
    채움은 호버 → 눌린 바탕의 밝기 차를 옮긴다(채움은 호버에서 바뀌지 않아 한 단계만 간다). 누르는 동안만
    보이는 면이라 세기(`PRESS_DE00`)를 먼저 지킨다 — 본문 글은 4.5를 지키고 보조 글은 그 아래로 내려간다
    (tests/app/gui/test_theme_contrast.py가 하한을 잰다).
    """
    card, hover, target = table["cardBg"], table["cardHoverBg"], TINT_DE00[scheme]
    pressed = _pressed_of(card, hover)
    table["cardPressedBg"] = pressed
    texts = (table["text"], table["textMuted"])
    for name, state in (("Finished", "stateFinished"), ("Failed", "stateFailed")):
        tint = _tinted(card, table[state], target, texts)
        table[f"cardTint{name}"] = tint
        table[f"cardTint{name}Hover"] = _hover_of(tint, card, hover)
        table[f"cardTint{name}Pressed"] = _hover_of(tint, card, pressed)
        table[f"{state}OnTint"] = _readable_on(table[state], (tint, table[f"cardTint{name}Hover"]))
    # 배경 채움 위의 상태 색(상태 원 · 상태 글)은 대비 조건 밖이다 — 설정에서 고르는 선택의 예외(설계 §12.1
    # 상태 색). 본문 · 보조 글만 4.5를 지킨다
    # 받는 중 채움은 강조색의 색상각이다 — 상태 글 · 막대(stateRunning)의 색상각이면 다크가 청록 쪽으로
    # 보였다. 라이트는 sRGB 밖으로 나가므로 채널을 자르지 않고 채도를 줄여 색상각을 지킨다(오너 2026-11-12, #582)
    table["cardFillRunning"] = _tinted(card, table["accent"], target, texts, keep_hue=True)
    table["cardFillPaused"] = _tinted(card, None, target, texts)
    for name in ("Running", "Paused"):
        table[f"cardFill{name}Pressed"] = _hover_of(table[f"cardFill{name}"], hover, pressed)


# METRICS(정수)와 FONTS(문자열)를 두 색 표에 자동 주입한다 — .qss는
# @cardPadding·@fontSizeTitle 같은 이름으로 참조하고, 파이썬 코드는
# theme.METRICS["cardPadding"] 정수를 직접 쓴다. 정의 지점은 위 한 곳이다.
# 파생 토큰: barRadius(=barHeight/2).
for _table in (DARK, LIGHT):
    _table.update({_name: f"{_value}px" for _name, _value in METRICS.items()})
    _table.update(FONTS)
    _table["barRadius"] = f"{METRICS['barHeight'] // 2}px"
    # 메뉴 버튼 오른쪽 안쪽 여백 = 바깥 끝 ↔ 버블 + 버블 + 글 ↔ 버블 − 테두리 1px(style.qss QPushButton)
    # — 버블이 숨어도 자리를 둔다(#523, 설계 §2)
    _table["menuButtonPadRight"] = (
        f"{METRICS['menuBubbleInset'] + METRICS['menuBubble'] + METRICS['menuBubbleGap'] - 1}px"
    )
_card_tints(DARK, "dark")
_card_tints(LIGHT, "light")

#: 카드 상태 이름 — 파이썬(`setProperty`)과 QSS(`[state="..."]`)가 공유하는 어휘.
#: 상태 아이콘·진행바 둘 다 이 값을 그대로 `state` 동적 속성에 써서
#: `resources/qss/style.qss`의 `#stateIconLabel[state="..."]`/
#: `QProgressBar[state="..."]` 규칙과 맞춘다 (#227, #240, #244 후속 —
#: 카드 테두리는 상태 신호에서 빠지고 상태 아이콘이 그 역할을 이어받았다).
CARD_STATES = ("waiting", "running", "paused", "finished", "failed")


#: 지금 활성 스킴("dark"/"light"). 기본값 "dark"는 의도적이다 — 아무도
#: `set_color_scheme()`을 부르지 않은 환경(기존 테스트 스위트 전체,
#: `theme.py`를 단독 임포트하는 스크립트)에서 예전과 똑같이 항상 DARK가
#: 나오게 한다. 실제 OS 감지는 `CVDv2.py`가 시작 시점에
#: `set_color_scheme(detect_color_scheme(app))`으로 흘려 넣는다.
_current_scheme = "dark"


def set_color_scheme(scheme: str) -> None:
    """활성 스킴을 명시적으로 정한다 — `current_tokens()`가 그다음부터 이걸 본다.

    `current_tokens()` 자체는 OS를 들여다보지 않는다(부수효과 없는 순수
    조회로 남긴다). 감지는 `detect_color_scheme()`이 따로 하고, 그 결과를
    여기로 넘기는 건 호출부(`CVDv2.py`) 책임이다 — 그래야 테스트가 OS 설정에
    기대지 않고 이 함수 하나로 원하는 스킴을 주입할 수 있다.
    """
    global _current_scheme
    if scheme not in ("dark", "light"):
        raise ValueError(f"알 수 없는 색 스킴: {scheme!r} (가능한 값: 'dark', 'light')")
    _current_scheme = scheme


def detect_color_scheme(app=None) -> str:
    """OS의 라이트/다크 설정을 감지해 `"light"`/`"dark"`를 반환한다.

    Qt 6.5+에서는 `QGuiApplication.styleHints().colorScheme()`로 OS 설정을
    직접 읽는다(이 저장소는 PySide6 6.11을 쓰므로 이 경로가 정상 경로다).
    이 API가 없거나 `Unknown`을 돌려주는 경우 — 오프스크린 QPA는 OS 테마
    개념 자체가 없어 거의 항상 여기로 빠진다 — 팔레트의 `Window` 색 밝기로
    판정하는 폴백을 쓴다. 어느 경로를 탔는지는 항상 로그로 남긴다(오프스크린
    환경에서 "왜 라이트가 나왔지"를 나중에 추적할 수 있게).
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication

    app = app or QGuiApplication.instance()

    if app is not None:
        hints = app.styleHints()
        scheme = hints.colorScheme()
        if scheme == Qt.ColorScheme.Dark:
            logger.info("theme scheme detected via styleHints().colorScheme(): dark")
            return "dark"
        if scheme == Qt.ColorScheme.Light:
            logger.info("theme scheme detected via styleHints().colorScheme(): light")
            return "light"
        logger.info(
            "styleHints().colorScheme() returned Unknown "
            "(offscreen QPA has no OS theme) — falling back to palette lightness"
        )

    palette = app.palette() if app is not None else QPalette()
    window_lightness = palette.color(QPalette.ColorRole.Window).lightness()
    result = "light" if window_lightness >= 128 else "dark"
    logger.info(
        "theme scheme detected via palette fallback: %s (Window lightness=%d)",
        result,
        window_lightness,
    )
    return result


def current_tokens() -> dict:
    """지금 쓸 토큰 표를 반환한다.

    `set_color_scheme()`으로 마지막에 설정된 스킴을 그대로 본다 — 여기서
    직접 OS를 감지하지 않는다. 호출부는 전부 이 함수를 거친다.
    """
    return LIGHT if _current_scheme == "light" else DARK


def substitute(text: str, tokens: dict | None = None) -> str:
    """`@토큰` 자리표시자를 값으로 치환한다.

    정의되지 않은 토큰은 조용히 넘기지 않고 KeyError로 터뜨린다 — QSS는
    잘못된 값을 만나면 그 규칙만 조용히 버리기 때문에, 오타를 여기서
    잡지 않으면 "적용했는데 안 보인다"로만 드러난다.
    """
    table = current_tokens() if tokens is None else tokens

    def _replace(match: re.Match) -> str:
        name = match.group(1)
        if name not in table:
            raise KeyError(f"정의되지 않은 테마 토큰: @{name}")
        return table[name]

    return _TOKEN_RE.sub(_replace, text)


def load_stylesheet(path: str, tokens: dict | None = None) -> str:
    """`.qss` 파일을 읽어 토큰을 치환한 전역 스타일시트 문자열을 반환한다."""
    with open(path, encoding="utf-8") as f:
        return substitute(f.read(), tokens)


def build_palette(tokens: dict | None = None) -> QPalette:
    """토큰으로 다크 QPalette를 만든다 (#227).

    **QSS만으로는 부족하다.** 스타일시트는 우리가 명시적으로 규칙을 쓴
    것만 칠한다 — `QScrollArea`의 뷰포트, 컨텍스트 메뉴, 입력창의 clear
    버튼처럼 스타일이 직접 그리는 부분은 팔레트를 본다. 팔레트를 안 주면
    창은 어두운데 카드 목록 배경만 시스템 기본 밝은 회색으로 남는다
    (실측으로 확인 — 이 함수가 생긴 이유다).

    색은 여기서도 새로 정하지 않는다. 전부 위 토큰 표에서 가져온다.
    """
    table = current_tokens() if tokens is None else tokens
    c = lambda name: QColor(table[name])  # noqa: E731 — 표 조회 축약

    p = QPalette()
    p.setColor(QPalette.ColorRole.Window, c("windowBg"))
    p.setColor(QPalette.ColorRole.WindowText, c("text"))
    p.setColor(QPalette.ColorRole.Base, c("surfaceAlt"))
    p.setColor(QPalette.ColorRole.AlternateBase, c("surface"))
    p.setColor(QPalette.ColorRole.Text, c("text"))
    p.setColor(QPalette.ColorRole.PlaceholderText, c("textDisabled"))
    p.setColor(QPalette.ColorRole.Button, c("surfaceAlt"))
    p.setColor(QPalette.ColorRole.ButtonText, c("text"))
    p.setColor(QPalette.ColorRole.ToolTipBase, c("surface"))
    p.setColor(QPalette.ColorRole.ToolTipText, c("text"))
    p.setColor(QPalette.ColorRole.Highlight, c("accent"))
    p.setColor(QPalette.ColorRole.HighlightedText, c("onAccent"))
    p.setColor(QPalette.ColorRole.Link, c("accent"))

    disabled = QPalette.ColorGroup.Disabled
    p.setColor(disabled, QPalette.ColorRole.WindowText, c("textDisabled"))
    p.setColor(disabled, QPalette.ColorRole.Text, c("textDisabled"))
    p.setColor(disabled, QPalette.ColorRole.ButtonText, c("textDisabled"))
    p.setColor(disabled, QPalette.ColorRole.Base, c("surface"))
    p.setColor(disabled, QPalette.ColorRole.Button, c("surface"))
    return p


class _DropDownComboBoxStyle(QProxyStyle):
    """Fusion의 콤보 팝업 겹침 배치를 끄고 v2.9.6(네이티브 스타일)의 드롭다운
    배치를 되살린다 (#241 후속 — 오너 실기 확인).

    `QStyle.SH_ComboBox_Popup` 힌트가 켜져 있으면 팝업이 열릴 때 콤보의
    현재 선택 항목이 콤보 라벨 위치에 오도록 콤보 위로 겹쳐 뜬다 — Fusion은
    이 힌트가 켜져 있고, 네이티브 Windows 스타일(windowsvista·windows11)은
    꺼져 있어 항상 콤보 아래로 떨어진다(실측 대조로 확인). v2.9.6은
    `app.setStyle()`을 아예 안 불러 네이티브 스타일을 썼으므로 드롭다운
    배치가 원래 동작이었다 — `#227`이 Fusion을 고정하며 조용히 바뀐
    회귀다. 판단이 갈리는 지점이라 기존(v2.9.6) 동작에 맞춘다.

    이 힌트를 끄면 팝업 위치 계산 자체가 currentIndex를 안 본다 — 그래서
    "마지막 호버로 옮겨간 currentIndex가 배치 기준으로 쓰인다"는 문제도
    같이 없어진다. `config.dialog._ComboBoxPopupHighlightResync`(팝업 안에서
    어떤 항목이 강조되는지)와는 역할이 겹치지 않는다 — 저건 팝업이 뜬
    *다음* 내용을, 이건 팝업 창 자체가 뜨는 *위치*를 다룬다.

    **`deferred_popup_hide` — 콤보 팝업 닫기를 한 턴 뒤로 미룬다 (macOS 전용,
    #240 실기 후속).** macOS에서 팝업을 열고 Enter로 고르면 설정 창까지 닫히던
    회귀의 경로(Qt 6.11.1 소스 추적 + Windows에서 같은 배달 순서를 재현해 확인,
    오너 macOS 실기로 수정 확인):

    1. Enter의 `ShortcutOverride`가 팝업 뷰에 닿으면 Qt의
       `QComboBoxPrivateContainer::eventFilter`가 `hidePopup()`을 부르고
       항목을 선택한 뒤 이벤트를 accept한다. 그러면 Qt는 이어서 같은 키의
       `KeyPress`를 **창 단위로** 배달한다.
    2. 그 `KeyPress`가 어느 창으로 가는지가 OS마다 다르다. Windows QPA는
       팝업이 키보드를 grab하므로(`QWindowsKeyMapper::m_keyGrabber`) 팝업
       창으로 간다 → 이미 닫힌 팝업의 뷰에서 무시되고 끝. macOS QPA는
       키보드 grab이 없고(`QCocoaWindow::setKeyboardGrabEnabled`는
       QTBUG-106597로 항상 false) 팝업 NSPanel은 key window가 될 수 없어
       (`QNSPanel canBecomeKeyWindow`) **다이얼로그 창**으로 간다 →
       `QComboBox::keyPressEvent`는 Enter를 ignore → `QDialog::keyPressEvent`
       가 기본 버튼(OK)을 누른다.
    3. v2.9.6(QMacStyle)에서 안 그랬던 이유: QMacStyle은
       `SH_Menu_FlashTriggeredItem`이 켜져 있어 `hidePopup()`이 실제 숨김을
       `QTimer::singleShot(0→60→20ms)`로 미룬다. 그래서 2의 `KeyPress`가
       도착할 때 팝업이 아직 활성 팝업이라 뷰로 배달되고 거기서 끝났다.
       Fusion은 이 힌트가 꺼져 있어(`QCommonStyle` 기본값) 숨김이 즉시
       일어난다 — 이것이 v2.9.6과 유일하게 다른 지점이다.

    수정은 3의 힌트를 QComboBox에 한해(QMenu는 건드리지 않음) 되살려
    v2.9.6 macOS와 같은 지연 숨김을 만드는 것이다. 대가도 v2.9.6 macOS와 같다 —
    닫힐 때 선택 항목이 약 80ms 깜빡이고(deselect→reselect) 그동안 팝업이 떠
    있는데, 오너 macOS 실기에서 지각되지 않았다. **macOS에서만 켠다**
    (`_deferred_popup_hide_default()`): Windows 실기에서 켜 보니 v2.9.6
    Windows(네이티브 스타일, 이 힌트 0)에는 없던 깜빡임이 그대로 생겼다(선택 행
    픽셀이 강조색→바탕색→강조색으로 바뀌는 것을 시간축으로 계측). Windows는
    2의 키보드 grab 덕에 회귀 자체가 없으므로 켤 이유도 없다. 새어 들어온
    `KeyPress`를 위젯 쪽에서 삼키는 방식은 쓸 수 없다 — macOS에서 그 `KeyPress`는
    콤보를 거치지 않고 다이얼로그 창에 직행한다(오너 macOS 실기로 확인).
    """

    def __init__(self, base_style, *, deferred_popup_hide: bool = False) -> None:
        super().__init__(base_style)
        self._deferred_popup_hide = deferred_popup_hide

    def styleHint(self, hint, option=None, widget=None, returnData=None) -> int:
        if hint == QStyle.StyleHint.SH_ComboBox_Popup:
            return 0
        if (
            hint == QStyle.StyleHint.SH_Menu_FlashTriggeredItem
            and self._deferred_popup_hide
            and isinstance(widget, QComboBox)
        ):
            return 1
        return super().styleHint(hint, option, widget, returnData)


def _deferred_popup_hide_default() -> bool:
    """콤보 팝업의 지연 닫기(`_DropDownComboBoxStyle`)를 기본으로 켤 플랫폼인지 — macOS만.

    플랫폼 분기는 "어느 플랫폼에서도 같은 코드가 돈다"는 성질을 잃게 하므로
    조건을 한 곳에 모으고, `tests/app/gui/test_theme.py`가 플랫폼 값을 바꿔 가며
    이 분기를 명시적으로 잰다(호출 시점의 `sys.platform`을 읽는 이유).
    """
    return sys.platform == "darwin"


def build_style(*, deferred_popup_hide: bool | None = None) -> QProxyStyle:
    """Fusion을 감싸 콤보 팝업 배치만 v2.9.6(드롭다운)으로 되돌린 스타일 객체.

    나머지 그리기는 전부 Fusion 그대로다 — `QProxyStyle`은 오버라이드
    안 한 모든 호출을 감싼 베이스 스타일로 넘긴다.

    `deferred_popup_hide`를 생략하면 `_deferred_popup_hide_default()`(macOS에서만
    켬)를 따른다. 메커니즘 테스트는 플랫폼과 무관하게 명시해서 넘긴다.
    """
    if deferred_popup_hide is None:
        deferred_popup_hide = _deferred_popup_hide_default()
    return _DropDownComboBoxStyle(
        QStyleFactory.create("Fusion"), deferred_popup_hide=deferred_popup_hide
    )


def scheme_name(scheme) -> str | None:
    """`Qt.ColorScheme` 값을 `"dark"`/`"light"`로 바꾼다. `Unknown`이면 None."""
    from PySide6.QtCore import Qt

    if scheme == Qt.ColorScheme.Dark:
        return "dark"
    if scheme == Qt.ColorScheme.Light:
        return "light"
    return None


def tokens_for(scheme: str) -> dict:
    """스킴 이름 → 토큰 표. 알 수 없는 이름은 ValueError — 아무것도 바꾸기 전에 걸러낸다."""
    if scheme == "dark":
        return DARK
    if scheme == "light":
        return LIGHT
    raise ValueError(f"알 수 없는 색 스킴: {scheme!r} (가능한 값: 'dark', 'light')")


def apply_color_scheme(app, scheme: str, qss_path: str) -> None:
    """스킴 하나를 앱 전역에 (재)적용한다 — **시작 시점과 실행 중 전환이 같은 경로**.

    **원자적이다.** 새 스타일시트와 팔레트를 먼저 만들어 두고, 둘 다 준비된
    뒤에야 색을 커밋한다(스킴 확정 → 팔레트 → QSS). 스타일시트 로드가
    `OSError`/`KeyError`로 실패하면 **아무것도 바뀌지 않는다** — 옛 테마가
    팔레트·QSS·토큰 전부에서 그대로 일관되게 남는다. 팔레트만 새 테마로 가고
    QSS가 옛 테마로 남는 "반쪽" 상태(SPEC §9 — 일관되게 안 따라가는 것보다
    나쁘다)는 이 함수가 만들 수 없다. 실패 시 `setStyleSheet("")`로 비우지도
    않는다 — 스타일을 벗기면 반쪽보다 나쁘다.

    `app.setStyleSheet()`은 Qt가 모든 위젯을 다시 polish하게 만들므로(실측 —
    `tests/app/gui/test_theme_switch.py`가 "시작부터 라이트"와 "다크에서 라이트로
    전환" 두 화면의 렌더 픽셀이 같은지로 잰다) 동적 속성 선택자
    (`[state="..."]`)도, 파이썬이 paint 시점에 토큰을 읽는 `IconButton`도 이
    한 번의 재로드로 함께 갈아입는다. 별도의 `repolish()` 순회는 필요 없었다
    — 있으면 좋아 보여도 효과가 없는 코드는 두지 않는다(같은 게이트에서
    빼 보고 확인).

    예외는 그대로 올라온다 — 호출부(`CVDv2.apply_theme`)가 시작 시점의 폴백을
    정하고, 실행 중 전환(`follow_os_color_scheme`)은 로그만 남기고 옛 테마를 지킨다.
    """
    tokens = tokens_for(scheme)
    stylesheet = load_stylesheet(qss_path, tokens)  # 실패하면 여기서 끝 — 아직 아무것도 안 바뀜
    palette = build_palette(tokens)
    set_color_scheme(scheme)
    app.setPalette(palette)
    app.setStyleSheet(stylesheet)
    logger.info("color scheme applied: %s (%s)", scheme, qss_path)


#: `follow_os_color_scheme()`가 건 슬롯 — 앱당 하나. 파이썬 참조가 죽으면 연결도
#: 사라지므로 붙들고, 다시 부르면 이전 것을 떼고 교체한다(테스트가 `CVDv2.apply_theme()`를
#: 여러 번 불러도 팔로워가 누적되지 않게 — 누적되면 후행 테스트의 신호 하나에 여러 번 반응한다).
_SCHEME_FOLLOWERS: dict = {}


def follow_os_color_scheme(app, qss_path: str) -> Callable[[Qt.ColorScheme], None]:
    """OS 라이트/다크 설정이 바뀌면 `apply_color_scheme()`을 다시 태운다.

    `QStyleHints.colorSchemeChanged`는 OS가 보내는 신호다 — offscreen QPA와
    CI에서는 오지 않으므로 테스트는 `QStyleHints.setColorScheme()`(Qt 6.8+,
    같은 신호를 낸다)으로 주입한다. 실제로 따라가는지는 오너가 실기에서
    시스템 테마를 바꿔 확인한다.

    #227 때 넣었다가 뺐던 기능이다(SPEC §8.5) — 카드가 위젯별 `setStyleSheet`이라
    전역 재적용으로는 안 바뀌어 화면이 반쪽만 갈아입었다. #242로 카드가 전역
    `.qss`로 옮겨 그 원인이 사라졌고, 이번에 "반쪽 없음"을 게이트로 재서 재도입한다.
    """
    hints = app.styleHints()
    previous = _SCHEME_FOLLOWERS.pop(id(app), None)
    if previous is not None:
        hints.colorSchemeChanged.disconnect(previous)

    def _on_changed(scheme) -> None:
        name = scheme_name(scheme) or detect_color_scheme(app)
        try:
            apply_color_scheme(app, name, qss_path)
        except (OSError, KeyError) as e:
            logger.warning("color scheme switch failed (%s): %s", qss_path, e)

    hints.colorSchemeChanged.connect(_on_changed)
    _SCHEME_FOLLOWERS[id(app)] = _on_changed
    return _on_changed


def unfollow_os_color_scheme(app, slot) -> None:
    """`follow_os_color_scheme()`이 돌려준 슬롯을 뗀다 — 테스트가 전역 배선을 남기지 않기 위한 짝."""
    if _SCHEME_FOLLOWERS.get(id(app)) is slot:
        del _SCHEME_FOLLOWERS[id(app)]
        app.styleHints().colorSchemeChanged.disconnect(slot)


def repolish(widget) -> None:
    """동적 속성을 바꾼 뒤 스타일을 다시 계산시킨다.

    `setProperty()`만으로는 이미 계산된 스타일이 갱신되지 않는다 —
    QSS의 `[state="..."]` 선택자가 새 값을 반영하려면 unpolish/polish를
    거쳐야 한다. 빠뜨리면 "QSS가 무시된 것처럼" 보인다.
    """
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()
