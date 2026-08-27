"""앱 전역 테마의 단일 정의 지점 — 색·모서리·간격 토큰과 QSS 로더 (#227).

**왜 파이썬에 토큰을 두고 QSS를 치환하는가.**
#227의 요구는 "색상값을 한 곳에서만 정의"다. 그런데 색이 필요한 곳이
두 군데로 갈린다 — (a) 앱 전역 스타일시트(`resources/qss/style.qss`),
(b) 카드 상태별 테두리처럼 파이썬이 런타임에 골라 적용해야 하는 값
(`content/widget.py`). QSS 파일에 색을 직접 박으면 (b)가 파이썬 상수로
복제되고, 파이썬에만 두면 QSS가 못 읽는다. 그래서 **정의는 여기 한 곳**
(`DARK`)에 두고, `.qss`는 `@토큰` 자리표시자만 쓰는 "규칙 파일"로
남긴다 — 로드 시점에 `load_stylesheet()`가 치환한다.

**나중에 테마가 붙을 때 갈라질 이음매는 이 파일 하나다** (#217 조사대로
이 저장소에는 아직 테마 개념 자체가 없어 지금은 다크 하나만 둔다).
라이트 테마가 필요해지면 `LIGHT = {...}` 딕셔너리를 옆에 추가하고
`current_tokens()`가 설정값을 보고 고르게 만들면 된다 —
`resources/qss/style.qss`와 호출부(`main.py`, `content/widget.py`)는
토큰 *이름*만 알기 때문에 손대지 않아도 된다. 반대로 색을 QSS에 박아
두면 그때 파일을 통째로 복제해야 한다. 그게 이 구조의 값이다.

**QSS는 지원하지 않는 것을 조용히 무시한다** — 에러도 경고도 없다.
그래서 이 파일이 쓰는 것은 전부 순정 QSS가 지원하는 속성뿐이고,
지원하지 않는 것(`box-shadow`, `.class` 선택자, `transition`)은 아예
쓰지 않는다. 상태 구분은 QSS가 지원하는 **동적 속성 선택자**
(`setProperty("state", ...)` + `[state="..."]`)로 낸다 — 이건 파이썬과
QSS 양쪽을 같이 손봐야 동작한다(파이썬에서 속성만 바꾸면 이미 계산된
스타일이 갱신되지 않아 `repolish()`도 함께 필요하다).
"""

import re

from PySide6.QtGui import QColor, QPalette

# .qss 자리표시자 문법: @토큰이름 (SCSS 변수처럼 읽힌다).
# `{}`를 쓰는 str.format은 QSS 중괄호와 충돌해 전부 이스케이프해야 하므로 쓰지 않는다.
_TOKEN_RE = re.compile(r"@([A-Za-z][A-Za-z0-9_]*)")

# 앱 전역 스타일시트 위치 — 호출부(main.py)가 resource_path()로 절대경로를
# 만들어 넘긴다. resources/ 전체가 이미 Nuitka `--include-data-dir=resources=resources`로
# 번들되므로 이 하위 경로는 배포 워크플로 수정 없이 그대로 따라 들어간다
# (#216의 `.js` 누락은 `resources/` 밖의 별도 폴더였던 게 원인이다).
QSS_RELATIVE_PATH = "resources/qss/style.qss"

#: 다크 테마 토큰 — 이 저장소의 모든 색·모서리·간격 값의 유일한 정의 지점.
DARK = {
    # ---- 바탕 ----
    "windowBg": "#1b1c1f",       # 창 배경
    "surface": "#24262b",        # 헤더·하단 바 같은 패널
    "surfaceAlt": "#2f3239",     # 기본 버튼·입력창 바탕
    "surfaceHover": "#383c44",
    "surfacePressed": "#282b31",
    "thumbBg": "#1f2126",        # 썸네일 자리표시 바탕
    # ---- 선·글자 ----
    "border": "#3a3d45",
    "borderStrong": "#4c515b",
    "text": "#e6e8ec",
    "textMuted": "#a0a6b0",
    "textDisabled": "#6b7078",
    # ---- 강조(주 동작 버튼) ----
    "accent": "#3d8bfd",
    "accentHover": "#559bff",
    "accentPressed": "#2f74d8",
    "onAccent": "#ffffff",
    # ---- 카드 ----
    "cardBg": "#2b2d33",
    "cardRadius": "14px",
    "cardPadding": "10px",
    # ---- 상태색 (카드 테두리·진행바 공용) ----
    # running/failed는 기존 값을 그대로 승계한다 — 실패 빨강(#FF6969)은
    # tests/unit/test_failure_display.py가 카드 스타일시트에서 직접 확인한다.
    "stateWaiting": "#6b7078",   # 대기 — 회색
    "stateRunning": "#55B5FF",   # 진행 — 파랑
    "stateFinished": "#4CC38A",  # 완료 — 초록
    "stateFailed": "#FF6969",    # 실패 — 빨강
    # ---- 진행바 ----
    "barTrack": "#3a3d45",
    "barHeight": "6px",
    "barRadius": "3px",
    # ---- 공통 형태 ----
    "radius": "8px",
    "pillRadius": "15px",        # 해상도 버튼(고정 높이 30px)의 절반
    "scrollHandle": "#4c515b",
    "scrollHandleHover": "#5e646f",
    # ---- 글자 크기 ----
    # 카드 라벨들은 ui/contentItemWidget.ui에 인라인 `font-size: 14px`가
    # 그대로 남아 있다(일부러 건드리지 않았다 — 그 값이 폭 계산에 직접
    # 들어가고, 3-OS 폰트 메트릭에 민감한 회귀 테스트가 그 위에 서 있다).
    # 전역 값을 같은 14px로 맞춰 앱 전체가 한 크기로 보이게 한다.
    "fontSize": "14px",
}

#: 카드 상태 이름 — 파이썬(`setProperty`)과 QSS(`[state="..."]`)가 공유하는 어휘.
CARD_STATES = ("waiting", "running", "finished", "failed")

# 카드 프레임 규칙. 전역 .qss가 아니라 여기 있는 이유: 상태에 따라 값이
# 달라져 파이썬이 런타임에 위젯별로 적용해야 한다(`setStyleSheet`).
# 규칙 전체를 한 번에 내보내 전역 규칙과의 병합 순서에 기대지 않는다.
_CARD_TEMPLATE = """\
#contentFrame {
    background-color: @cardBg;
    border: 1px solid %(stateColor)s;
    border-radius: @cardRadius;
    padding: @cardPadding;
}
"""


def current_tokens() -> dict:
    """지금 쓸 토큰 표를 반환한다.

    테마가 하나뿐인 동안은 항상 다크다. 테마 선택이 생기면 이 함수만
    설정을 읽도록 바꾸면 된다 — 호출부는 전부 이 함수를 거친다.
    """
    return DARK


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


def card_style(state: str, tokens: dict | None = None) -> str:
    """카드 프레임(`#contentFrame`)에 붙일 상태별 스타일시트를 만든다."""
    if state not in CARD_STATES:
        raise ValueError(f"알 수 없는 카드 상태: {state!r} (가능한 값: {CARD_STATES})")
    table = current_tokens() if tokens is None else tokens
    state_color = table["state" + state.capitalize()]
    return substitute(_CARD_TEMPLATE % {"stateColor": state_color}, table)


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
