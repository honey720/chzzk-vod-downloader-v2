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
from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QProxyStyle, QStyleFactory

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
    "sectionDeletePadY": 5,  # 구간 행 ✕ 상자 위아래 안쪽 여백(px) — 상자 높이 = 글 한 줄 + 2 × 이 값 + 테두리(#586)
    "sectionDeletePadX": 10,  # 구간 행 ✕ 상자 좌우 안쪽 여백(px) — 상자 폭 = 도형 한 변 + 2 × 이 값 + 테두리(#586)
    # ---- 카드 ----
    # 카드 행 안쪽 좌우 여백(px) — 창 바깥 좌우 여백(outerMargin)과 같아 썸네일 왼쪽 끝이 헤더 URL 칸 왼쪽 끝과 같은
    # x다(설계 §13, #611). 오른쪽은 스크롤바가 보이면 그 폭만큼 줄어 글 끝이 바 안 내용 끝과 같은 x다(view.py)
    "cardPadding": 12,
    # 카드 행 안쪽 위아래 여백(px) — 행 80 = 9 + 세 줄(62) + 9, 그 밑 구분선 1(설계 §13, #611)
    "cardPadY": 9,
    "cardSeparator": 1,  # 카드 행 사이 구분선 두께(px) — 행 아래끝(카드 면의 아래 테두리, 설계 §13)
    "cardRowSpacing": 4,  # 카드 안 행 간격(4의 배수 권장) — 줄이면 카드가 낮아져 밀도↑
    "cardRadius": 0,  # 카드 모서리 둥글기 — 띠 목록이라 0(설계 §13, #611 — §11.7의 카드 8을 대체)
    "cardThumbWidth": 110,  # 카드 썸네일 폭(px) — 글 묶음 높이(62)에 맞춘 16:9(설계 §11.1, #550)
    "cardThumbHeight": 62,  # 카드 썸네일 높이(px) — 1줄 위끝 ~ 3줄 아래끝
    "cardThumbCompactWidth": 68,  # 썸네일 줄이기의 폭(px) — 위 두 줄 높이(38)에 맞춘 16:9(설계 §12.1, #554)
    "cardThumbCompactHeight": 38,  # 썸네일 줄이기의 높이(px) — 1줄 위끝 ~ 2줄 아래끝(16 + 4 + 18)
    "cardThumbGap": 12,  # 썸네일 ↔ 글(px) — 프로토 THUMB_GAP(#550)
    "cardRow1Height": 16,  # 카드 1줄(채널 그림 · 채널명 · 날짜) 높이(px)
    "cardRow2Height": 18,  # 카드 2줄(제목) 높이(px)
    "cardRow3Height": 20,  # 카드 3줄(상태 · 정보) 높이(px) — 설계 §11.3 「3줄 20」
    # 카드 사이(px) — 띠 목록이라 0이다. 행은 구분선(cardSeparator) 하나로 나눈다(설계 §13, #611). 카드 목록
    # 위아래 여백도 이 값이다(app/widgets/view.py)
    "cardGap": 0,
    # 카드 버튼 한 변(px) — 그림 칸 16(actionGlyph) 둘레 4씩. 1줄(16)보다 커서 상자 세로 가운데를 1줄 가운데에
    # 맞춰 위아래로 4씩 넘친다 — 2줄 제목을 덮지 않는다(widget.py::_placeCardButtons, 176j)
    "cardButton": 24,
    "cardStatusIcon": 16,  # 3줄 상태 원 아이콘 지름(px) — 메뉴 버블과 같은 그림
    # 고른 행 왼쪽 끝 강조색 세로 띠 폭(px) — 체크 원과 함께 고름을 알린다(바탕은 그대로, #611 176p). 카드 면과
    # progress bar가 그린다(띠가 진행 막대 위) — 줄 배치는 그대로다. 3 · 4 · 5 비교 뒤 4(오너 2026-11-15)
    "cardSelectBar": 4,
    # 선택 모드의 체크 원(#561) — 썸네일 왼쪽 위에 얹는다. 썸네일 위라 카드 폭 · 줄 배치는 그대로다
    "cardPickMark": 20,  # 체크 원 지름(px) — 카드 버튼(24)이 커질 때 그대로 뒀다(176j — 20 · 24 비교 그림)
    "cardPickMarkCompact": 16,  # 썸네일 줄이기의 체크 원 지름(px) — 썸네일 높이 38에서 그림을 덜 가린다
    "cardPickMarkInset": 4,  # 썸네일 왼쪽 · 위 끝 ↔ 체크 원(px)
    # 카드의 키보드 focus ring(#561) — 목록이 초점 행 안쪽 가장자리에 하나만 그린다(app/widgets/focus_ring.py,
    # 설계 §13 · #611). 행끼리 붙어 바깥에 띄울 자리가 없다
    "focusRingWidth": 2,  # focus ring 굵기(px)
    # ---- 선택 줄(#561 — 설계 §12.1 「선택 모드」, 프로토 shell.py SELECT_*) ----
    "selectBarGap": 4,  # 헤더바 아래끝 ↔ 선택 줄 위끝 — 말풍선 ↔ 대상(tipGap)과 같은 떠 있는 것 간격
    "selectBarPad": 8,  # 선택 줄 왼쪽 안쪽 여백 — 「N개 선택」 앞(§11.2 8)
    "selectCheckBox": 16,  # 세 상태 체크박스 네모 한 변 — 아이콘 도형(16)과 같은 크기
    "selectCheckWidth": 24,  # 세 상태 체크박스 위젯 폭 — 네모 16 + 좌우 4(누르는 자리)
    "selectSepHeight": 16,  # 선택 줄의 글 ↔ 버튼 묶음 구분선 높이 — 버튼 안 도형(16)과 같은 높이
    "thumbRadius": 8,  # 썸네일 모서리 — 카드보다 작아야 안쪽이 자연스럽다
    "barHeight": 4,  # 하단 진행바 두께(진행분이 있을 때 — 진행·일시정지 — 보임)
    # 카드 조작 아이콘(편집 · 구간 · 일시정지 · 재개 · 재시도 · 폴더 · 삭제)과 구간 창 ✕ 도형 한 변 — cardButton 상자
    # 안에 그려진다. 바 안 드롭다운의 📁는 글 옆이면 menuIcon · 아이콘만이면 narrowIcon을 쓴다(176k)
    "actionGlyph": 16,
    "barGlyph": 20,  # 바 아이콘 칸(하단바 ▶ ‖ ■ 등) 도형 한 변 — 상자 32 안, narrowIcon과 같다(176j)
    # ---- 메뉴바 상태 버블(#523, 설계 §2) — 메뉴 버튼 오른쪽 안쪽 여백에 늘 잡아 두는 자리 ----
    # 수치는 프로토(shell.py BADGE_SIDE 16 · 버블 자리 여백 29 · 버튼 오른끝 ↔ 버블 7)를 따른다(오너 실기 2026-11-07)
    "menuBubble": 16,  # 버블(원) 지름 — 기호는 16 격자에 그려 이 크기에서 프로토와 같다
    "menuBubbleGap": 7,  # 메뉴 이름 글 ↔ 버블 사이
    "menuBubbleInset": 7,  # 버튼 오른쪽 바깥 끝(테두리 포함) ↔ 버블
    # ---- 메뉴바 탭 — 밑줄 탭(#611, 176d 그림 ③): 면 없이 글, 고른 탭만 아래 강조색 밑줄 ----
    "menuTabPadX": 5,  # 탭 왼쪽 안쪽 여백 — 투명 테두리 1 + 5 = 탭 왼쪽 끝 ↔ 글 6(176d 그림 ③과 같다)
    "menuUnderline": 2,  # 고른 탭 밑줄 굵기 — 탭 아래끝에 탭 폭 그대로(왼쪽 끝 = 탭 왼쪽 끝)
    "menuFocusPadX": 4,  # 키보드 초점 상자 ↔ 글 좌우
    "menuFocusPadY": 2,  # 탭 위끝 ↔ 초점 상자 · 초점 상자 ↔ 밑줄 자리
    # ---- 상단·하단 바 & 창 ----
    "framePadding": 8,  # 상단·하단 바 안쪽 여백 — cardPadding과 같으면 정렬선이 관통
    "frameRadius": 8,  # 상단·하단 바 모서리
    # 창 가장자리 ↔ 바 안 내용 · 카드 바깥 끝의 좌우 여백 — 메뉴바 · 헤더바 · 하단바의 안쪽 가로
    # 여백과 카드 목록이 이 값 하나를 읽는다(§11.1). 16은 상단 버튼이 가운데로 몰려 보였다(오너 실기
    # 2026-11-11, #561 · #580). 카드 목록의 겹친 스크롤바는 행 안쪽 오른쪽 여백(cardPadding) 자리에 그려진다
    "outerMargin": 12,
    # ---- 스크롤바(style.qss QScrollBar — 카드 목록 밖의 세로 · 가로 스크롤바가 함께 쓴다) ----
    "scrollBarWidth": 8,  # 세로 스크롤바 폭 · 가로 스크롤바 높이
    "scrollHandleRadius": 4,  # 손잡이 모서리 — 폭의 절반이라 손잡이 양끝이 반원이다
    # 카드 목록의 겹친 스크롤바(app/widgets/overlay_scrollbar.py, #611) — 행 오른쪽 안쪽 여백 12 자리 가운데에
    # 놓인다. 굵음 8이면 막대 ↔ 행 안 버튼 2 · 막대 ↔ 창 끝 2(굵음 10은 버튼과 1px라 붙어 보였다 — 176c 비교 그림)
    "overlayBarThin": 6,  # 평소 굵기
    "overlayBarThick": 8,  # 막대 자리에 포인터를 대거나 끄는 동안의 굵기 — scrollBarWidth와 같다
    "initialWidthMax": 800,  # 첫 실행 초기 창 폭의 상한 — 콘텐츠 최대폭이 아니다(#253, 유저는 얼마든지 넓힐 수 있다)
    # ---- 셸 바 컨트롤(#529, 설계 §11.2 · §11.3 · §11.7) ----
    "barControl": 32,  # 바 컨트롤(메뉴 버튼 등) 높이
    "controlRadius": 6,  # 바 컨트롤 모서리
    "barPadY": 8,  # 메뉴바 안쪽 여백 세로 — 창 위끝 ↔ 메뉴 버튼
    # 바 아이콘 도형 한 변 — 톱니바퀴 · 선택 · 「+」 · 정렬 ⇅ · 저장 위치 📁(아이콘만) · 하단 칸 아이콘. 상자 32 안에서
    # 둘레 6(176j). 메뉴 팝업 줄 아이콘 · 글 옆 드롭다운 📁(menuIcon)는 16이다(176k)
    "narrowIcon": 20,
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
    # URL 입력창 안쪽 좌우 여백 — 펼침 버튼(app/widgets/header_controls.py BUTTON_PADDING)과 같은 값(#611)
    "fieldPadX": 10,
    "fieldPadY": 5,  # 입력창 · 콤보 안쪽 위아래 여백 — 테두리가 네 변 1px라 초점 때도 그대로다(#611)
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
    "surface": "#24262b",  # 떠 있는 패널 — 드롭다운 메뉴 · 콤보 팝업 · 설정 창 그룹 상자
    # 메뉴바 · 헤더바 · 하단바 면 — 176q 카드 면(#2b2d33)에서 한 단계(ΔE00 약 3.0) 어둡게 정했다. 176r에서 카드 면을
    # 내린 뒤 거리는 1.65다. 헤더가 목록 첫 행처럼 읽히지 않게 바를 목록과 다른 면으로 둔다(#611 176i — 176h 바 면
    # (A)안)
    "barBg": "#222329",
    "surfaceAlt": "#2f3239",  # 기본 버튼 · 꺼진 ▶ · 고른 메뉴 바탕
    # 입력 칸(URL 칸 · 펼침 칸 · QComboBox) · 고른 칩 · 켠 선택 버튼 면 — 다크는 surfaceAlt와 같다(#611 176i · 176j)
    "fieldBg": "#2f3239",
    "surfaceHover": "#383c44",
    "surfacePressed": "#282b31",
    # 고른 칩 · 켠 선택 버튼 면 — 176k ~ 176u의 바 버튼 호버 면 값이다. 그 위 한 단계 더 간 #60636c는 너무
    # 밝았다(오너 칩 실기 2026-11-17, #611 176v). 바 버튼 호버 · 눌림 면(barHover · barPressed)은 이 값과
    # 바 면에서 파생한다(`_bar_faces`)
    "barSelected": "#4c5159",
    "menuHover": "#2c2f35",  # 드롭다운 메뉴 호버 면 — 바 면(surface)에서 한 단계(ΔE00 약 2.9, #536)
    "rowHoverBg": "#242629",  # 창 바탕 위 목록 행(구간 행) 호버 띠 — 창 바탕에서 밝게 한 단계(ΔE00 약 3.2, #586)
    "shadow": "#000000",  # 말풍선 그림자 — 그릴 때 불투명도를 낮춰 쓴다(#536)
    # 등록 오류 말풍선 면 — 실패 빨강의 색상을 옅게 띤 어두운 면. 글(text) 11.67 · 테두리(stateFailed) 5.10(#536)
    "errorTipBg": "#3d2326",
    # 말풍선(툴팁) 떠 있는 면 — 카드(cardBg)보다 한 단계 밝게(프로토 demoFloat 대비 1.25 규칙, 흐린 글 4.5를 지키게
    # 1.23). 바 면과 1.36 · 글 9.10 · 흐린 글(단축키) 4.56(#536)
    "tipBg": "#393b43",
    # 썸네일 자리표시 바탕 — 176r에서 카드 평소 면을 내린 만큼(OKLab 밝기, ΔE00 약 1.5) 같이 내려 카드 면과의 거리를
    # 176q 수준(ΔE00 약 3.85)으로 되돌렸다(#611 176s)
    "thumbBg": "#1a1c21",
    # ---- 선·글자 ----
    "border": "#3a3d45",  # 구분선 · 툴팁 · 그룹 상자 · 기본 버튼 테두리
    "borderStrong": "#4c515b",
    # 입력 칸(URL 칸 · 펼침 칸 · QComboBox) 네 변 테두리 — 세 후보 가운데 (나)로 정함: 칸 면과 2.40(#611 176i).
    # 호버(`fieldEdgeHover`)는 이 값에서 파생한다 — 이 값 하나만 바꾸면 된다
    "fieldEdge": "#676b72",
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
    # 카드 평소 면 — 176q 값(#2b2d33)에서 OKLab 밝기만 ΔE00 약 1.5 내렸다. 평소 · 선택 구분이 약했다(오너 재실기
    # 2026-11-17, #611 176r). 바 면(barBg)과 1.65 — 바 면보다 밝다
    "cardBg": "#26282e",
    # 고른 카드 면 — 강조색을 섞지 않는 중립 면이다(Windows 11 목록 방식, #611 176q). 그 위 글 4.5에 맞춘 값이라
    # 176r에서 평소 면만 내리고 이 값은 그대로 뒀다(평소 면과 ΔE00 4.57). 호버 면(cardHoverBg)은 평소 면 ·
    # 이 값 사이에서 파생한다(`_card_hover`)
    "cardSelectedBg": "#35363c",
    # ---- 상태색 (상태 아이콘·상태 텍스트·진행바 공용) ----
    # running/failed는 기존 값을 그대로 승계한다 — 실패 빨강(#FF6969)은
    # tests/app/gui/test_failure_display.py가 상태 표시에서 직접 확인한다.
    # 상태 글은 같은 색상에서 카드 바탕 위 4.5 이상인 가장 가까운 밝기(#512 — 대기 2.76 · 일시정지 4.13이었다).
    # 대기 · 일시정지는 고른 카드 면(cardSelectedBg) 위에서도 4.5가 되게 OKLab 밝기만 더 올렸다(3.94였다,
    # #611 176m)
    "stateWaiting": "#9a9fa6",  # 대기 — 회색
    "stateRunning": "#55B5FF",  # 진행 — 파랑
    # 일시정지 — 진행 파랑의 채도를 뺀 강철빛 회색. 진행바가 그대로 남되
    # "돌고 있지 않다"가 색으로 읽혀야 한다(#245). 대기 회색과는 달라야
    # 한다(상태색은 정보를 나른다 — tests/app/gui/test_theme.py 상호 구별 게이트).
    "statePaused": "#91a0b3",
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
#: 짙게 눌렀다. accent/onAccent도 그대로 재사용한다 —
#: 이 값이 칠하는 곳은 항상 accent 배경 위(버튼 자체) 아니면 accent 배경 위
#: 흰 글자라 창 바탕 밝기와 무관하다.
LIGHT = {
    # ---- 바탕 ----
    # 창 배경 — 바 면(barBg)에서 OKLab 밝기만 반 단계(ΔE00 약 1.43) 밝힌 푸른 회색, 그 위 흰 카드가 떠 있다(Windows 11
    # 설정 앱 방식, #611 176m). 바와 같은 값이면 상하단 바와 빈 목록이 한 면으로 읽혔다(오너 칩 실기 2026-11-17,
    # 176v). 대화상자 바탕도 같다.
    # 라이트 바탕 넷(windowBg · barBg · cardSelectedBg · rowHoverBg)은 border의 OKLCH 색상각에 border 채도의 절반을
    # 준 푸른 회색이다(#611 176o). 바 면 밝기는 Windows 11 바탕 #f3f3f3에 맞춘 안(오너 2026-11-17, 176p)
    "windowBg": "#f9fafd",
    "surface": "#ffffff",
    "barBg": "#f2f3f6",  # 바 면 — 흰 카드 면에서 어둡게(ΔE00 약 2.9, #611 176i · 176o · 176p)
    "surfaceAlt": "#eceef1",
    # 입력 칸 · 고른 칩 · 켠 선택 버튼 면 — 회색 바 위 흰 면(Fluent 2 · Windows 11). surfaceAlt로 두면 바 면과
    # 1.02라 면이 안 보인다(#611 176i · 176j)
    "fieldBg": "#ffffff",
    "surfaceHover": "#e1e4e9",
    "surfacePressed": "#d5d9df",
    # 고른 칩 · 켠 선택 버튼 면 — 다크와 같은 규칙: 176k ~ 176u의 바 버튼 호버 면 값. 그 위 한 단계 더 간
    # #aaacb2는 너무 어두웠다(오너 칩 실기 2026-11-17, #611 176v)
    "barSelected": "#c2c5c9",
    "menuHover": "#f0f1f3",  # 드롭다운 메뉴 호버 면 — 바 면(surface)에서 한 단계(ΔE00 약 3.0, #536)
    # 창 바탕 위 목록 행(구간 행) 호버 띠 — 창 바탕에서 어둡게 한 단계(ΔE00 약 3.3, #586). 176v에서 창 바탕을 밝힌
    # 만큼 OKLab 밝기만 올렸다(#e4e4e7이면 4.80)
    "rowHoverBg": "#eaeaed",
    "shadow": "#000000",  # 말풍선 그림자 — 그릴 때 불투명도를 낮춰 쓴다(#536)
    # 등록 오류 말풍선 면 — 실패 빨강의 색상을 옅게 띤 밝은 면. 글(text) 14.61 · 테두리(stateFailed) 4.59(#536)
    "errorTipBg": "#fdecec",
    # 말풍선(툴팁) 떠 있는 면 — 라이트는 바 · 카드가 흰색이라 더 밝은 면이 없다: 흰 면 + 진한 테두리(borderStrong)로
    # 띄운다(프로토 「흰 카드」 경우와 같다). 글 16.69(#536)
    "tipBg": "#ffffff",
    "thumbBg": "#e6e8ec",
    # ---- 선·글자 ----
    "border": "#d5d8de",  # 구분선 · 툴팁 · 그룹 상자 · 기본 버튼 테두리
    "borderStrong": "#b9bec7",
    "fieldEdge": "#989ba0",  # 입력 칸 네 변 테두리 — (나)로 정함(#611 176i)
    "text": "#1c1e22",
    # 흐린 글 — 호버한 실패 카드 면(cardTintFailedHover) 위에서도 4.5가 되게 OKLab 밝기만 낮췄다(4.12였다, #611 176m).
    # 바 · 설정 창 · 메뉴의 흐린 글도 이 값이다
    "textMuted": "#555a63",
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
    # 고른 카드 면 — 바 면(= 창 배경)에서 어둡게 한 단계(ΔE00 약 3.2, 176p). 카드 면에서 한 단계면 바 면과
    # 같아져 고른 면이 바탕에 묻힌다(카드 면에서는 ΔE00 약 5.8, #611 176m). 176p까지의 호버 면 값 그대로다 —
    # 호버 면(cardHoverBg)은 이 값에서 반 단계로 파생한다(#611 176q)
    "cardSelectedBg": "#e4e4e7",
    # ---- 상태색 (상태 아이콘·상태 텍스트·진행바 공용) — 흰 배경 대비용으로 짙게 눌렀다 ----
    # 대기 · 받는 중 · 일시정지는 고른 카드 면(cardSelectedBg) 위에서도 4.5가 되게 같은 색상에서 OKLab 밝기만
    # 낮췄다(3.85 · 3.77 · 3.51이었다, #611 176m)
    "stateWaiting": "#60656c",
    "stateRunning": "#0a61c7",
    "statePaused": "#596679",  # 일시정지 — 흰 배경에서 진행 파랑과 구별되는 무채색 강철빛
    "stateFinished": "#288654",  # #512 — 카드 바탕 위 4.5(3.39였다)
    "stateFailed": "#d02727",  # #512 — 카드 · 헤더바 · 버튼 면(실패 칩) 위 모두 4.5(버튼 면 3.99였다)
    # #523 — 주황 #f0a33c의 색상각 그대로 밝기만 낮춤(흰 기호 4.5 · 바탕 3.0, 화면 색 범위 안으로 채도만 조금 줄어듦)
    "stateWarning": "#a66904",
    "stateInfo": "#0a61c7",  # 정보 — 진행 색과 같은 값(설계 §11.5, #611 176n)
    # ---- 진행바 ----
    "barTrack": "#d5d8de",  # = border — 진행 막대 트랙은 카드 테두리 색(설계 §12.1, #512)
    "scrollHandle": "#c3c7cd",
    "scrollHandleHover": "#aeb3bb",
}

# ---- 카드 색조(#554 — 설계 §8의 6 · §12.1) ----
# 완료 · 실패 색조는 리터럴로 적지 않고 카드 면 · 상태 색에서 셈한다. 색상은 상태 색의
# OKLCH 색상각 그대로, 밝기는 카드 면과 같게 두고 채도로 세기(카드 면과의 ΔE00)를 맞춘다 — 투명도로
# 섞으면 카드 면의 기운 때문에 색상이 상태 색에서 어긋난다. 그 위 글(본문 · 보조 · 상태 글)은 4.5를 지킨다.
TINT_DE00 = {
    "dark": 12.0,
    "light": 10.0,
}  # 색조 ↔ 카드 면 ΔE00 목표 — 설계 §8의 6(오너 2026-11-02)
TINT_TEXT_CONTRAST = 4.5  # 색조 위 글의 대비 하한 — 설계 §11.5
# 카드 호버 면 ↔ 평소 면 ΔE00 — 선택 면(한 단계, 약 3.0)의 반 단계(Windows 11 목록 방식, #611 176q). 라이트만 쓴다
CARD_HOVER_DE00 = 1.5
# 호버 면을 평소 면 · 선택 면의 OKLab 가운데로 두는 스킴 — 다크는 평소 면을 내려 선택 면과 4.57이 됐으므로
# 반 단계 대신 가운데로 둔다(#611 176r)
CARD_HOVER_MIDPOINT = {"dark": True, "light": False}
# 색조 채도를 셈하는 기준 면 — 176r에서 다크 평소 면만 내리고 색조 선택 면을 그대로 두려고, 채도는 176q 평소
# 면에서 셈하고 밝기만 옮긴다. None이면 지금 평소 면(cardBg)에서 셈한다
TINT_CHROMA_BASE = {"dark": "#2b2d33", "light": None}
# 입력 칸 테두리 한 단계 ΔE00 — border → borderStrong 한 단계와 같은 크기(#611 176g · 176h)
FIELD_STEP_DE00 = 6.7
# 입력 칸 호버 테두리 ↔ 평소 테두리 ΔE00 — 두 단계. 한 단계로는 호버가 잘 안 보였다(오너 재실기 2026-11-17,
# #611 176s)
FIELD_HOVER_DE00 = 2 * FIELD_STEP_DE00
# 바 버튼 호버 면(barHover) ↔ 고른 면(barSelected) ΔE00 — 반 단계. 카드처럼 「평소 < 호버 < 선택」이고, 호버는
# 고른 면 쪽에 붙는다(오너 칩 실기 2026-11-17, #611 176v — 176t의 한 단계는 고른 면이 너무 진했다)
BAR_HOVER_DE00 = FIELD_STEP_DE00 / 2
# 바 버튼 눌림 면(barPressed)의 자리 — 호버 면 → 고른 면 OKLab 직선 위 비율(0.5 = 가운데, #611 176v)
BAR_PRESSED_SHARE = 0.5

# ---- 선택 모드의 체크 원(#561) ----
# 정수 px가 아닌 값이라 METRICS(QSS에 "Npx"로 들어간다)에 두지 않는다
PICK_MARK_RING_WIDTH = 1.5  # 체크 원 테두리 두께(px) — 1은 밝은 썸네일에 묻힌다
PICK_MARK_SCRIM_ALPHA = 0.5  # 안 고른 원 바탕의 불투명도 — 흰 썸네일 위에서도 테두리와 3.0 이상이다


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
    hue_from: str,
    target: float,
    texts: tuple[str, ...],
) -> str:
    """`base`에서 `hue_from`의 색상각으로 채도만 올려 ΔE00 `target`이 되는 색 — 글 대비를 지키는 만큼만.

    밝은 면(흰 카드)은 밝기를 그대로 두고 채도만 올리면 sRGB 밖으로 나가 잘리므로, 채도를 올린 만큼의
    절반을 밝기에서 뺀다. 이분 탐색으로 정하고, `texts`의 글이 4.5 아래로 내려가면 그 아래 세기에서 멈춘다.
    """
    big_l, _a, _b = _oklab(base)

    def color_at(amount: float) -> str:
        _l, ha, hb = _oklab(hue_from)
        hue = math.atan2(hb, ha)
        lightness = big_l - amount / 2 if big_l > 0.85 else big_l
        return _from_oklab((lightness, amount * math.cos(hue), amount * math.sin(hue)))

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


def _hover_of(tint: str, card: str, hover: str) -> str:
    """색조 면의 호버 · 선택 면 — 카드 면 → `hover`와 같은 OKLab 밝기 차를 색조에 옮긴다(설계 §12.1 카드 호버)."""
    big_l, a, b = _oklab(tint)
    return _from_oklab((big_l + _oklab(hover)[0] - _oklab(card)[0], a, b))


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


def _field_hover(edge: str, face: str) -> str:
    """입력 칸 호버 테두리 — 평소 테두리에서 칸 면과 멀어지는 쪽으로 밝기만 ΔE00 `FIELD_HOVER_DE00` 옮긴 색.

    평소 색(`fieldEdge`) 하나만 바꾸면 호버가 따라오게 파생한다(#611 176h). 옮기는 거리는 두 단계다 — 다크는
    밝게, 라이트는 어둡게(글 색 쪽) 간다(#611 176s).
    """
    big_l, a, b = _oklab(edge)
    direction = 1.0 if _oklab(face)[0] < 0.6 else -1.0
    step = 0.0
    moved = edge
    while step < 1.0 and delta_e00(edge, moved) < FIELD_HOVER_DE00:
        step += 0.001
        moved = _from_oklab((big_l + direction * step, a, b))
    return moved


def _bar_faces(table: dict) -> None:
    """바 버튼 호버 · 눌림 면(`barHover` · `barPressed`)을 바 면 · 고른 면에서 셈해 표에 더한다.

    호버 면은 바 면 → 고른 면 OKLab 직선 위에서 고른 면과 ΔE00 `BAR_HOVER_DE00`(반 단계) 떨어진 첫 지점이다
    — 고른 면에서 바 면 쪽으로 걸어 처음 닿는 자리. 눌림 면은 호버 면 → 고른 면 직선의 `BAR_PRESSED_SHARE`
    지점이다. 바 면 · 고른 면이 바뀌면 따라온다(#611 176v).
    """
    bar, chosen = _oklab(table["barBg"]), _oklab(table["barSelected"])

    def along(share: float) -> str:
        return _from_oklab(tuple(b + (c - b) * share for b, c in zip(bar, chosen, strict=True)))

    share = 1.0
    hover = table["barSelected"]
    while share > 0.0 and delta_e00(table["barSelected"], hover) < BAR_HOVER_DE00:
        share -= 0.0005
        hover = along(share)
    table["barHover"] = hover
    near = _oklab(hover)
    table["barPressed"] = _from_oklab(
        tuple(h + (c - h) * BAR_PRESSED_SHARE for h, c in zip(near, chosen, strict=True))
    )


def _card_hover(table: dict, scheme: str) -> None:
    """카드 호버 면(`cardHoverBg`)을 표에 더한다 — 평소 면 → 선택 면 OKLab 직선 위의 한 지점.

    라이트는 ΔE00 `CARD_HOVER_DE00` 지점, 다크는 가운데(`CARD_HOVER_MIDPOINT`, #611 176r)다. 선택 면 쪽으로
    가므로 라이트의 푸른 회색 기운도 선택 면과 같은 쪽이다. 선택 면보다 옅어 호버 ≠ 선택이고 그 위 글 대비는
    평소 면 · 선택 면 사이에 든다(#611 176q).
    """
    rest, goal = _oklab(table["cardBg"]), _oklab(table["cardSelectedBg"])
    if CARD_HOVER_MIDPOINT[scheme]:
        table["cardHoverBg"] = _from_oklab(
            tuple((r + g) / 2 for r, g in zip(rest, goal, strict=True))
        )
        return
    moved = table["cardBg"]
    for step in range(1, 1001):
        share = step / 1000
        moved = _from_oklab(tuple(r + (g - r) * share for r, g in zip(rest, goal, strict=True)))
        if delta_e00(table["cardBg"], moved) >= CARD_HOVER_DE00:
            break
    table["cardHoverBg"] = moved


def _card_tints(table: dict, scheme: str) -> None:
    """완료 · 실패 색조(+ 호버 · 선택 면 · 그 위 상태 글 색)를 표에 더한다.

    색조 세기는 목표(ΔE00)를 먼저 정한다(설계 §8의 6). 본문 · 보조 글은 그 세기에서 4.5를 지키는지
    본다. 호버 · 선택 면은 중립 카드의 평소 → 호버 · 선택 밝기 차를 색조에 옮긴다(#611 176q). 상태 글(완료
    초록 · 실패 빨강)은 색조 세 면 위에서 4.5가 되게 같은 색상의 밝기만 옮긴 색을 따로 둔다(`state…OnTint`) —
    라이트의 완료 초록은 흰 카드에서 이미 4.5라 색조가 조금만 생겨도 4.5 아래로 내려간다.

    채도는 `TINT_CHROMA_BASE` 면에서 셈하고, 평소 · 호버 · 선택 면은 그 면 → 각 중립 면의 밝기 차만 옮긴다 —
    다크는 평소 면을 내려도 색조 선택 면이 그대로다(#611 176r).
    """
    card, target = table["cardBg"], TINT_DE00[scheme]
    base = TINT_CHROMA_BASE[scheme] or card
    texts = (table["text"], table["textMuted"])
    for name, state in (("Finished", "stateFinished"), ("Failed", "stateFailed")):
        tint_at_base = _tinted(base, table[state], target, texts)
        tint = _hover_of(tint_at_base, base, card) if base != card else tint_at_base
        table[f"cardTint{name}"] = tint
        table[f"cardTint{name}Hover"] = _hover_of(tint_at_base, base, table["cardHoverBg"])
        table[f"cardTint{name}Selected"] = _hover_of(tint_at_base, base, table["cardSelectedBg"])
        grounds = (tint, table[f"cardTint{name}Hover"], table[f"cardTint{name}Selected"])
        table[f"{state}OnTint"] = _readable_on(table[state], grounds)


# 메뉴 버튼 오른쪽 안쪽 여백(px) = 바깥 끝 ↔ 버블 + 버블 + 글 ↔ 버블 − 테두리 1px(style.qss 메뉴바 절)
# — 버블이 숨어도 자리를 둔다(#523, 설계 §2). 초점 상자 자리(app/widgets/menu_button.py)도 이 값을 읽는다
MENU_BUTTON_PAD_RIGHT = (
    METRICS["menuBubbleInset"] + METRICS["menuBubble"] + METRICS["menuBubbleGap"] - 1
)

# METRICS(정수)와 FONTS(문자열)를 두 색 표에 자동 주입한다 — .qss는
# @cardPadding·@fontSizeTitle 같은 이름으로 참조하고, 파이썬 코드는
# theme.METRICS["cardPadding"] 정수를 직접 쓴다. 정의 지점은 위 한 곳이다.
# 파생 토큰: barRadius(=barHeight/2).
for _table in (DARK, LIGHT):
    _table.update({_name: f"{_value}px" for _name, _value in METRICS.items()})
    _table.update(FONTS)
    _table["barRadius"] = f"{METRICS['barHeight'] // 2}px"
    _table["menuButtonPadRight"] = f"{MENU_BUTTON_PAD_RIGHT}px"
_card_hover(DARK, "dark")
_card_hover(LIGHT, "light")
_card_tints(DARK, "dark")
_card_tints(LIGHT, "light")
DARK["fieldEdgeHover"] = _field_hover(DARK["fieldEdge"], DARK["fieldBg"])
LIGHT["fieldEdgeHover"] = _field_hover(LIGHT["fieldEdge"], LIGHT["fieldBg"])
_bar_faces(DARK)
_bar_faces(LIGHT)
# 대화상자 버튼(Windows 11 표준, #622) 호버 · 눌림 면 — 평소 면(fieldBg)에서 한 단계 · 두 단계. 다크는 평소 면이
# surfaceAlt와 같아 기본 버튼 단계(surfaceHover · surfacePressed)를 쓰고, 라이트는 흰 면에서 회색 단계를 하나씩 내려간다
DARK["dialogButtonHover"] = DARK["surfaceHover"]
DARK["dialogButtonPressed"] = DARK["surfacePressed"]
LIGHT["dialogButtonHover"] = LIGHT["surfaceAlt"]
LIGHT["dialogButtonPressed"] = LIGHT["surfaceHover"]

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
    """앱 전체 스타일 — Fusion을 감싼 `QProxyStyle`이다 (`build_style`).

    예전에는 Qt 콤보 목록의 두 동작(`SH_ComboBox_Popup` 0 — 목록을 칸 아래로 · macOS
    `SH_Menu_FlashTriggeredItem` — 목록 닫기 지연)을 바꿨다(#241 · #246). 콤보박스가 모두
    `ChoiceCombo`(펼치면 Qt 목록 대신 메뉴)로 바뀌어 두 동작이 걸릴 Qt 목록이 앱에 없으므로 걷어냈다(#620 · #622).
    지금은 오버라이드가 없다 — 앱 스타일을 한 곳에서 만드는 자리로 남긴다.
    """


def build_style() -> QProxyStyle:
    """Fusion을 감싼 앱 전체 스타일 객체를 만든다.

    `QProxyStyle`은 오버라이드 안 한 모든 호출을 감싼 베이스 스타일로 넘긴다 — 그리기는 전부 Fusion 그대로다.
    """
    return _DropDownComboBoxStyle(QStyleFactory.create("Fusion"))


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
