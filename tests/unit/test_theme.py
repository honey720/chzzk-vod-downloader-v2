"""전역 테마(#227) 단위 테스트 — 토큰 단일 정의·번들 포함·상태색.

이 파일이 지키는 것은 셋이다.

1. **번들 포함** — `.qss`가 `resource_path()` 기준으로 실제로 존재해야 한다.
   `.js`가 `app/resources/`에서 빠진 채 릴리스로 나갔던 사고(#216)와 같은
   부류를 소스 단계에서 잡는다.
2. **조용한 무시 방지** — QSS는 모르는 값을 만나면 그 규칙을 **에러 없이**
   버린다. `@토큰` 오타는 그래서 "적용했는데 안 보인다"로만 드러난다.
   여기서 KeyError로 먼저 터뜨린다.
3. **색의 단일 정의** — 색 리터럴이 theme.py 밖으로 새면 나중에 테마를
   붙일 때 갈라진다. 소스 트리를 스캔해 그 재발을 막는다.
"""

import re
from pathlib import Path

import pytest

import main
import theme

ROOT_DIR = Path(main.__file__).resolve().parent
HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}\b")
# 미치환 자리표시자 — 주석에 한글로 쓴 "@토큰"(설명)은 토큰 문법이 아니라 잡지 않는다
UNRESOLVED_TOKEN = re.compile(r"@[A-Za-z]")


class TestStylesheetIsBundled:
    def test_qss_exists_at_resource_path(self):
        """`resource_path()`가 가리키는 자리에 실제 파일이 있어야 한다."""
        assert Path(main.resource_path(theme.QSS_RELATIVE_PATH)).is_file()

    def test_qss_lives_under_resources_dir(self):
        """`resources/` 아래여야 릴리스 워크플로 수정 없이 번들된다.

        release.yml은 `--include-data-dir=resources=resources`로 이 폴더를
        통째로 넣는다(그 폴더는 보호 구역이라 에이전트가 못 고친다). 경로를
        `resources/` 밖으로 옮기면 그 순간 배포본에서 스타일이 사라진다.
        """
        assert theme.QSS_RELATIVE_PATH.replace("\\", "/").startswith("resources/")


class TestTokenSubstitution:
    def test_every_token_in_qss_is_defined(self):
        """`.qss`가 쓰는 모든 `@토큰`이 theme에 정의돼 있어야 한다."""
        theme.load_stylesheet(main.resource_path(theme.QSS_RELATIVE_PATH))  # KeyError면 실패

    def test_no_placeholder_survives_substitution(self):
        loaded = theme.load_stylesheet(main.resource_path(theme.QSS_RELATIVE_PATH))
        assert not UNRESOLVED_TOKEN.search(loaded)

    def test_unknown_token_raises(self):
        with pytest.raises(KeyError):
            theme.substitute("QWidget { color: @nope; }")

    def test_substitute_uses_given_table(self):
        assert theme.substitute("a: @x;", {"x": "1px"}) == "a: 1px;"


class TestCardStateStyles:
    @pytest.mark.parametrize("state", theme.CARD_STATES)
    def test_each_state_paints_its_own_colour(self, state):
        css = theme.card_style(state)
        assert theme.DARK["state" + state.capitalize()] in css
        assert not UNRESOLVED_TOKEN.search(css)

    def test_states_are_all_distinct(self):
        styles = {theme.card_style(s) for s in theme.CARD_STATES}
        assert len(styles) == len(theme.CARD_STATES)

    def test_unknown_state_raises(self):
        with pytest.raises(ValueError):
            theme.card_style("bogus")


class TestColoursAreDefinedOnlyOnce:
    """색 리터럴은 theme.py에만 있어야 한다.

    카드 완료·실패 색이 `content/view.py`에, 카드 배경이
    `ui/contentItemWidget.ui`에 직접 박혀 있던 게 #227 착수 전 모습이다.
    그 상태로 테마가 하나 더 붙으면 값이 여러 곳에서 갈린다.
    """

    SCAN_DIRS = ("application", "config", "content", "download", "ui")

    def _sources(self):
        files = [ROOT_DIR / "main.py"]
        for name in self.SCAN_DIRS:
            files.extend(sorted((ROOT_DIR / name).rglob("*.py")))
            files.extend(sorted((ROOT_DIR / name).rglob("*.ui")))
        return files

    def test_no_hex_colour_outside_theme(self):
        offenders = []
        for path in self._sources():
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if HEX_COLOR.search(line):
                    offenders.append(f"{path.relative_to(ROOT_DIR)}:{n}: {line.strip()}")
        assert not offenders, "색 리터럴은 theme.py에서만 정의한다. 발견:\n" + "\n".join(offenders)

    def test_qss_file_holds_no_hex_colour(self):
        text = Path(main.resource_path(theme.QSS_RELATIVE_PATH)).read_text(encoding="utf-8")
        assert not HEX_COLOR.search(text), ".qss는 값이 아니라 `@토큰`만 써야 한다"


class TestPalette:
    def test_palette_uses_theme_tokens(self, qapp):
        from PySide6.QtGui import QColor, QPalette

        palette = theme.build_palette()
        assert palette.color(QPalette.ColorRole.Window) == QColor(theme.DARK["windowBg"])
        assert palette.color(QPalette.ColorRole.WindowText) == QColor(theme.DARK["text"])
        assert palette.color(QPalette.ColorRole.Highlight) == QColor(theme.DARK["accent"])


class TestLightTokenTable:
    """LIGHT이 DARK와 나란히 존재하되 실제로 다른 값을 낸다는 것을 고정한다 (#227 회귀 수정).

    v2.9.6까지 네이티브 스타일이 공짜로 해 주던 OS 라이트/다크 추종이,
    #227의 Fusion 고정 + 항상-DARK 팔레트로 조용히 사라졌었다(PR #234 리뷰에서
    오너 실기로 발견). 이 클래스는 "토큰 표 자체가 스킴에 따라 갈린다"는
    전제를, 아래 TestColorSchemeSelection은 "감지 결과가 실제로 그 표를
    고른다"는 배선을 고정한다 — 둘 다 있어야 회귀가 다시 나면 여기서 잡힌다.
    """

    def test_light_and_dark_share_the_same_keys(self):
        """`.qss`와 `card_style()`은 두 표를 구분 없이 조회한다 — 키가 어긋나면 KeyError."""
        assert set(theme.LIGHT.keys()) == set(theme.DARK.keys())

    def test_light_backgrounds_are_not_just_dark_reused(self):
        for key in ("windowBg", "surface", "surfaceAlt", "cardBg"):
            assert theme.LIGHT[key] != theme.DARK[key], f"LIGHT[{key}]가 DARK와 같다"

    @pytest.mark.parametrize("state", theme.CARD_STATES)
    def test_light_state_colours_paint_light_cards(self, state):
        """`current_tokens()`가 LIGHT를 고른 동안 `card_style()`이 LIGHT 상태색을 쓰는지."""
        theme.set_color_scheme("light")
        try:
            css = theme.card_style(state)
            assert theme.LIGHT["state" + state.capitalize()] in css
        finally:
            theme.set_color_scheme("dark")

    @pytest.mark.parametrize("state", ("running", "finished", "failed"))
    def test_light_state_colours_differ_from_dark_where_contrast_needed(self, state):
        """대기색은 원래도 흰 배경에서 대비가 충분해 재사용하지만, 나머지 셋은 대비 때문에 갈아 끼웠다."""
        theme.set_color_scheme("light")
        try:
            css = theme.card_style(state)
            assert theme.DARK["state" + state.capitalize()] not in css
        finally:
            theme.set_color_scheme("dark")

    def test_light_state_colours_are_still_mutually_distinct(self):
        """상태색은 장식이 아니라 정보를 나른다 — 라이트 배경에서도 4종이 서로 달라야 한다."""
        colours = [theme.LIGHT["state" + s.capitalize()] for s in theme.CARD_STATES]
        assert len(set(colours)) == len(colours)


class TestColorSchemeSelection:
    """`set_color_scheme()`으로 넣은 값이 `current_tokens()`가 돌려주는 표를 바꾸는지.

    실제 OS 감지(`detect_color_scheme()`)는 실 OS 설정에 기대므로 3-OS CI에서
    안정적으로 못 돈다 — 그래서 여기서는 감지값을 흉내 낸 문자열을
    `set_color_scheme()`으로 직접 주입해 그 뒤의 배선만 고정한다. 실제 감지
    로직 자체는 아래 TestDetectColorScheme가 가짜 `styleHints()`로 고정한다.
    """

    def teardown_method(self):
        # 다른 테스트 파일이 기본값(dark)을 전제하므로 항상 되돌린다
        theme.set_color_scheme("dark")

    def test_default_scheme_is_dark(self):
        """아무도 set_color_scheme()을 안 부르면 예전처럼 항상 DARK — 기존 테스트 전제."""
        assert theme.current_tokens() is theme.DARK

    def test_set_color_scheme_light_switches_current_tokens(self):
        theme.set_color_scheme("light")
        assert theme.current_tokens() is theme.LIGHT

    def test_set_color_scheme_back_to_dark_switches_back(self):
        theme.set_color_scheme("light")
        theme.set_color_scheme("dark")
        assert theme.current_tokens() is theme.DARK

    def test_unknown_scheme_raises_and_does_not_change_current(self):
        theme.set_color_scheme("light")
        with pytest.raises(ValueError):
            theme.set_color_scheme("sepia")
        assert theme.current_tokens() is theme.LIGHT  # 실패한 호출이 상태를 안 건드렸는지


class _FakeStyleHints:
    def __init__(self, scheme):
        self._scheme = scheme

    def colorScheme(self):
        return self._scheme


class _FakeApp:
    """`detect_color_scheme()`이 보는 건 `styleHints()`와 `palette()`뿐이라 이걸로 충분하다."""

    def __init__(self, scheme, palette=None):
        self._hints = _FakeStyleHints(scheme)
        self._palette = palette

    def styleHints(self):
        return self._hints

    def palette(self):
        return self._palette


class TestDetectColorScheme:
    """`detect_color_scheme()`의 두 경로(Qt API·팔레트 폴백)를 가짜 앱으로 고정한다.

    실제 QGuiApplication 대신 최소 가짜(`_FakeApp`)를 주입한다 — 진짜 OS
    다크모드를 이 프로세스에서 흉내 낼 방법이 없고(오프스크린 QPA에는 OS
    테마 개념이 없다), 그렇다고 이 함수를 통째로 안 재는 것도 안 된다.
    """

    def test_qt_api_reports_dark(self):
        from PySide6.QtCore import Qt

        assert theme.detect_color_scheme(_FakeApp(Qt.ColorScheme.Dark)) == "dark"

    def test_qt_api_reports_light(self):
        from PySide6.QtCore import Qt

        assert theme.detect_color_scheme(_FakeApp(Qt.ColorScheme.Light)) == "light"

    def test_unknown_falls_back_to_dark_palette_lightness(self):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QColor, QPalette

        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor("#101010"))
        fake = _FakeApp(Qt.ColorScheme.Unknown, palette=palette)
        assert theme.detect_color_scheme(fake) == "dark"

    def test_unknown_falls_back_to_light_palette_lightness(self):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QColor, QPalette

        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor("#f5f5f5"))
        fake = _FakeApp(Qt.ColorScheme.Unknown, palette=palette)
        assert theme.detect_color_scheme(fake) == "light"

    def test_offscreen_qpa_has_no_os_theme_and_uses_palette_fallback(self, qapp):
        """오프스크린 QPA(테스트 환경)는 `colorScheme()`이 Unknown을 준다 — 실측 확인.

        그래서 여기서 나오는 값은 "OS가 라이트/다크다"가 아니라 "오프스크린
        기본 팔레트가 밝다/어둡다"일 뿐이다 — 실기 OS 라이트/다크 각각에서의
        실제 추종 여부는 이 테스트가 아니라 오너의 실렌더 스크린샷 확인
        대상이다.
        """
        from PySide6.QtCore import Qt

        assert qapp.styleHints().colorScheme() == Qt.ColorScheme.Unknown
        # 오프스크린 기본 팔레트 밝기로 결정된 값 — 어느 쪽이든 예외 없이 문자열로 나와야 한다
        assert theme.detect_color_scheme(qapp) in ("light", "dark")


class TestApplyThemeWiring:
    """`main.apply_theme()`이 감지 결과를 실제로 팔레트에 반영하는지.

    #227 회귀의 정확한 모양이 이거였다 — Fusion 고정 자체가 문제가 아니라,
    감지와 무관하게 `theme.build_palette()`가 항상 DARK를 굳혀 썼던 게
    문제였다. `theme.py` 안의 배선(위 TestColorSchemeSelection)만으로는
    `main.py`가 그 배선을 실제로 호출하는지까지는 못 잡는다 — 여기서 그
    간극을 닫는다.
    """

    def teardown_method(self):
        theme.set_color_scheme("dark")

    def test_apply_theme_builds_a_light_palette_when_light_is_detected(self, qapp, monkeypatch):
        from PySide6.QtGui import QColor, QPalette

        monkeypatch.setattr(theme, "detect_color_scheme", lambda app: "light")
        main.apply_theme(qapp)
        assert qapp.palette().color(QPalette.ColorRole.Window) == QColor(theme.LIGHT["windowBg"])

    def test_apply_theme_builds_a_dark_palette_when_dark_is_detected(self, qapp, monkeypatch):
        from PySide6.QtGui import QColor, QPalette

        monkeypatch.setattr(theme, "detect_color_scheme", lambda app: "dark")
        main.apply_theme(qapp)
        assert qapp.palette().color(QPalette.ColorRole.Window) == QColor(theme.DARK["windowBg"])
