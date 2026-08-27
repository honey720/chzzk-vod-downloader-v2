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
