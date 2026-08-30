"""앱 전역 테마의 단일 정의 지점 — 색·모서리·간격 토큰과 QSS 로더 (#227).

**왜 파이썬에 토큰을 두고 QSS를 치환하는가.**
#227의 요구는 "색상값을 한 곳에서만 정의"다. 그런데 색이 필요한 곳이
두 군데로 갈린다 — (a) 앱 전역 스타일시트(`resources/qss/style.qss`),
(b) 카드 상태별 테두리처럼 파이썬이 런타임에 골라 적용해야 하는 값
(`content/widget.py`). QSS 파일에 색을 직접 박으면 (b)가 파이썬 상수로
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
호출부(`main.py`, `content/widget.py`)는 토큰 *이름*만 알기 때문에 이 변경에
손대지 않아도 됐다 — 설계했던 대로다. 실제 OS 감지는 `detect_color_scheme()`이
한다(`main.py`가 시작 시점에 호출해 `set_color_scheme()`에 넘긴다). `current_tokens()`
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
import re

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QProxyStyle, QStyle, QStyleFactory

logger = logging.getLogger(__name__)

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
    # 카드 안쪽 여백은 **세로만** 준다 — 가로는 0이어야 한다.
    # 이유는 아래 _CARD_TEMPLATE 주석에 있다(3-OS 폰트 메트릭 회귀의 원인이었다).
    "cardPaddingV": "4px",
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
    "thumbBg": "#e6e8ec",
    # ---- 선·글자 ----
    "border": "#d5d8de",
    "borderStrong": "#b9bec7",
    "text": "#1c1e22",
    "textMuted": "#5b6069",
    "textDisabled": "#9a9fa8",
    # ---- 강조(주 동작 버튼) ----
    "accent": "#3d8bfd",
    "accentHover": "#559bff",
    "accentPressed": "#2f74d8",
    "onAccent": "#ffffff",
    # ---- 카드 ----
    "cardBg": "#ffffff",
    "cardRadius": "14px",
    "cardPaddingV": "4px",
    # ---- 상태색 (카드 테두리·진행바 공용) — 흰 배경 대비용으로 짙게 눌렀다 ----
    "stateWaiting": "#6b7078",
    "stateRunning": "#1f6fd6",
    "stateFinished": "#2f9e63",
    "stateFailed": "#d93636",
    # ---- 진행바 ----
    "barTrack": "#e3e5e9",
    "barHeight": "6px",
    "barRadius": "3px",
    # ---- 공통 형태 ----
    "radius": "8px",
    "pillRadius": "15px",
    "scrollHandle": "#c3c7cd",
    "scrollHandleHover": "#aeb3bb",
    # ---- 글자 크기 ----
    "fontSize": "14px",
}

#: 카드 상태 이름 — 파이썬(`setProperty`)과 QSS(`[state="..."]`)가 공유하는 어휘.
#: 상태 아이콘·진행바 둘 다 이 값을 그대로 `state` 동적 속성에 써서
#: `resources/qss/style.qss`의 `#stateIconLabel[state="..."]`/
#: `QProgressBar[state="..."]` 규칙과 맞춘다 (#227, #240, #244 후속 —
#: 카드 테두리는 상태 신호에서 빠지고 상태 아이콘이 그 역할을 이어받았다).
CARD_STATES = ("waiting", "running", "finished", "failed")


#: 지금 활성 스킴("dark"/"light"). 기본값 "dark"는 의도적이다 — 아무도
#: `set_color_scheme()`을 부르지 않은 환경(기존 테스트 스위트 전체,
#: `theme.py`를 단독 임포트하는 스크립트)에서 예전과 똑같이 항상 DARK가
#: 나오게 한다. 실제 OS 감지는 `main.py`가 시작 시점에
#: `set_color_scheme(detect_color_scheme(app))`으로 흘려 넣는다.
_current_scheme = "dark"


def set_color_scheme(scheme: str) -> None:
    """활성 스킴을 명시적으로 정한다 — `current_tokens()`가 그다음부터 이걸 본다.

    `current_tokens()` 자체는 OS를 들여다보지 않는다(부수효과 없는 순수
    조회로 남긴다). 감지는 `detect_color_scheme()`이 따로 하고, 그 결과를
    여기로 넘기는 건 호출부(`main.py`) 책임이다 — 그래야 테스트가 OS 설정에
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
        result, window_lightness,
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
    """

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.StyleHint.SH_ComboBox_Popup:
            return 0
        return super().styleHint(hint, option, widget, returnData)


def build_style() -> QProxyStyle:
    """Fusion을 감싸 콤보 팝업 배치만 v2.9.6(드롭다운)으로 되돌린 스타일 객체.

    나머지 그리기는 전부 Fusion 그대로다 — `QProxyStyle`은 오버라이드
    안 한 모든 호출을 감싼 베이스 스타일로 넘긴다.
    """
    return _DropDownComboBoxStyle(QStyleFactory.create("Fusion"))


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
