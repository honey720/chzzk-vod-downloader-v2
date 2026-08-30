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
    """카드 상태 신호 (#240 2단계 → #244 — 신호를 테두리에서 상태 아이콘으로 이관).

    카드 테두리(`#contentFrame`)는 #244부터 항상 중립색이고 더는 `state`를
    안 본다 — 상태 신호는 이제 `#stateIconLabel[state="..."]`(아이콘 색)과
    `QProgressBar[state="..."]`(진행바 색) 둘로 줄었다(오너 확정: "테두리·
    진행바·텍스트" 3중 반복 → "아이콘·진행바" 2가지). 진행바와 같은
    패턴으로 그 규칙이 전역 `.qss`에 실제로 있는지, 서로 다른 토큰으로
    갈리는지를 **실제 QSS 파일 소스**를 읽어 확인한다(만든 문자열이 아니라
    프로덕션이 읽는 바로 그 파일).

    "알 수 없는 state를 주면 ValueError"라는 옛 검증은 대응하는 게 없다 —
    `card_style()`이라는 함수 자체가 사라져 임의 문자열을 검증할 진입점이
    없다(`content/widget.py::_cardState()`가 항상 `theme.CARD_STATES` 안의
    값만 반환하므로 검증이 필요한 지점 자체가 없어졌다). 위젯이 실제로
    옳은 색을 입는지는 `test_widget_theme.py`(실렌더 픽셀 확인)가 본다.
    """

    @pytest.mark.parametrize("state", theme.CARD_STATES)
    def test_each_state_rule_exists_and_resolves_to_its_token(self, state):
        block = _rule_block(_raw_qss(), f'#stateIconLabel[state="{state}"]')
        assert f"@state{state.capitalize()}" in block

    def test_states_resolve_to_distinct_colours(self):
        loaded = theme.load_stylesheet(main.resource_path(theme.QSS_RELATIVE_PATH))
        colours = {
            _rule_block(loaded, f'#stateIconLabel[state="{state}"]').strip()
            for state in theme.CARD_STATES
        }
        assert len(colours) == len(theme.CARD_STATES)

    def test_base_rule_has_zero_horizontal_padding(self):
        """상태와 무관한 공통 규칙 — padding은 세로만 있고 가로는 0이어야
        한다(#227 PR #234 3-OS 폭 회귀 재발 방지, theme.py 주석 참고)."""
        block = _rule_block(_raw_qss(), "#contentFrame")
        padding = [ln for ln in block.splitlines() if "padding" in ln]
        assert padding, "카드 규칙에 padding 선언이 사라졌다 — 이 테스트의 전제를 확인할 것"
        assert padding[0].strip().rstrip(";").split(":")[1].split()[-1] == "0px"

    def test_card_frame_border_is_always_neutral(self):
        """카드 테두리는 상태 선택자를 전혀 안 쓴다 — #244의 핵심 불변식."""
        block = _rule_block(_raw_qss(), "#contentFrame")
        assert "@border" in block
        assert "@state" not in block
        for state in theme.CARD_STATES:
            assert f'#contentFrame[state="{state}"]' not in _raw_qss()


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


def _style_hint_for_real_combo(style, combo, hint) -> int:
    """`QComboBox.initStyleOption()`으로 실제 옵션을 채워 질의한다.

    `option=None`으로 질의하면 `SH_ComboBox_Popup`는 스타일과 무관하게
    항상 0을 낸다(실측 확인 — Fusion 자체도 0으로 나옴) — Qt가 내부적으로
    팝업 배치를 결정할 때는 항상 채워진 `QStyleOptionComboBox`를 넘기고,
    그 경우에만 Fusion이 실제로 1(겹침)을 낸다. 옵션 없이 질의하는 테스트는
    아무 스타일에 대해서나 통과해버리는 무의미한 검증이 된다(#241 후속
    개발 중 고장 주입으로 직접 확인한 함정) — 항상 이 헬퍼를 거칠 것.
    """
    from PySide6.QtWidgets import QStyleOptionComboBox

    opt = QStyleOptionComboBox()
    combo.initStyleOption(opt)
    return style.styleHint(hint, opt, combo)


class TestComboBoxDropDownStyle:
    """#241 후속(오너 실기 확인) — Fusion의 콤보 팝업 겹침 배치를 끄고
    v2.9.6(네이티브 스타일)의 드롭다운 배치를 되살린다.

    `SH_ComboBox_Popup`가 켜져 있으면 팝업이 currentIndex 항목을 콤보
    라벨에 맞춰 겹쳐 뜬다 — v2.9.6은 스타일을 지정하지 않아 네이티브
    스타일(드롭다운, 이 힌트 꺼짐)을 썼다(main.py에 `app.setStyle()`
    호출 자체가 없었음, `git show v2.9.6:main.py`로 확인).
    """

    def test_plain_fusion_uses_the_overlay_popup(self, qapp):
        """되돌릴 회귀가 실제로 존재한다는 것부터 고정한다 — Fusion 자체는
        이 힌트를 켠 채로 둔다(#227이 조용히 들여온 배치 변경의 근거)."""
        from PySide6.QtWidgets import QComboBox, QStyle, QStyleFactory

        fusion = QStyleFactory.create("Fusion")
        combo = QComboBox()
        combo.setStyle(fusion)
        assert _style_hint_for_real_combo(fusion, combo, QStyle.StyleHint.SH_ComboBox_Popup) == 1

    def test_style_hint_disables_combobox_overlay_popup(self, qapp):
        from PySide6.QtWidgets import QComboBox, QStyle

        style = theme.build_style()
        combo = QComboBox()
        combo.setStyle(style)
        assert _style_hint_for_real_combo(style, combo, QStyle.StyleHint.SH_ComboBox_Popup) == 0

    def test_other_style_hints_still_delegate_to_fusion(self, qapp):
        """오버라이드 안 한 힌트는 그대로 Fusion 값을 내야 한다 — 프록시가
        SH_ComboBox_Popup 하나만 가로채고 나머지는 안 건드리는지 확인."""
        from PySide6.QtWidgets import QStyle, QStyleFactory

        style = theme.build_style()
        fusion = QStyleFactory.create("Fusion")
        hint = QStyle.StyleHint.SH_EtchDisabledText
        assert style.styleHint(hint) == fusion.styleHint(hint)

    def test_list_mouse_tracking_hint_is_intentionally_left_untouched(self, qapp):
        """`SH_ComboBox_ListMouseTracking`을 같이 끄면 호버 오염(선택이 마우스를
        따라가는 것)도 같이 사라질 것 같지만, 실측해보니 Fusion은 이 힌트
        하나에 "마우스 추적 켜짐"과 "호버 페인트"를 함께 묶어놔서 끄면 호버
        시각 피드백 자체가 통째로 사라지고 `viewport().setMouseTracking(True)`로
        되살릴 수도 없다(native Windows는 이 힌트와 무관한 별도 경로로 호버를
        그려서 안 겪는 문제) — 그래서 이 힌트는 일부러 안 건드린다.
        `config.dialog._ComboBoxPopupHighlightResync`가 오염을 막는 대신
        사후 복원하는 이유가 이것이다. 여기서는 우리 스타일이 실제로 이
        힌트를 안 건드린다는 것만 고정한다(Fusion 원래 값 그대로 나와야 함)."""
        from PySide6.QtWidgets import QComboBox, QStyle, QStyleFactory

        style = theme.build_style()
        fusion = QStyleFactory.create("Fusion")
        combo = QComboBox()
        combo.setStyle(style)
        combo_fusion_ref = QComboBox()
        combo_fusion_ref.setStyle(fusion)

        hint = QStyle.StyleHint.SH_ComboBox_ListMouseTracking
        ours = _style_hint_for_real_combo(style, combo, hint)
        plain = _style_hint_for_real_combo(fusion, combo_fusion_ref, hint)
        assert ours == plain == 1

    def test_popup_drops_below_the_combo_regardless_of_current_index(self, qapp):
        """실제 QComboBox로 배치 자체를 확인 — 겹침 배치였다면 팝업 top이
        콤보 bottom보다 위(작은 y)에 온다. 드롭다운이면 정확히 콤보 bottom."""
        from PySide6.QtWidgets import QComboBox
        from PySide6.QtTest import QTest

        qapp.setStyle(theme.build_style())
        combo = QComboBox()
        combo.addItems(["Alpha", "Beta", "Gamma", "Delta", "Epsilon"])
        combo.setCurrentIndex(3)  # 콤보 라벨과 안 겹치면 티가 나는 임의의 값
        combo.move(50, 300)
        combo.show()
        QTest.qWaitForWindowExposed(combo)

        combo_bottom = combo.mapToGlobal(combo.rect().bottomLeft())
        combo.showPopup()
        popup_pos = combo.view().window().pos()
        combo.hidePopup()

        assert popup_pos == combo_bottom

    def test_popup_placement_is_stable_across_a_stray_hover_and_reopen(self, qapp):
        """드롭다운으로 고정하면 배치가 currentIndex를 아예 안 보므로,
        호버로 뷰의 currentIndex가 옮겨가도 팝업 위치 자체는 안 흔들려야
        한다 — 강조 복원(_ComboBoxPopupHighlightResync)과 별개로, 배치
        문제 자체가 currentIndex 오염에 애초에 영향을 안 받는지 확인."""
        from PySide6.QtWidgets import QComboBox
        from PySide6.QtTest import QTest

        qapp.setStyle(theme.build_style())
        combo = QComboBox()
        combo.addItems(["Alpha", "Beta", "Gamma", "Delta", "Epsilon"])
        combo.setCurrentIndex(3)
        combo.move(50, 300)
        combo.show()
        QTest.qWaitForWindowExposed(combo)
        combo_bottom = combo.mapToGlobal(combo.rect().bottomLeft())

        combo.showPopup()
        view = combo.view()
        viewport = view.viewport()
        QTest.mouseMove(viewport, viewport.rect().topLeft())
        rect0 = view.visualRect(view.model().index(0, 0))
        QTest.mouseMove(viewport, rect0.center())  # currentIndex를 row0로 오염
        combo.hidePopup()

        combo.showPopup()
        popup_pos = combo.view().window().pos()
        combo.hidePopup()

        assert popup_pos == combo_bottom


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
        """LIGHT 토큰으로 치환한 실제 `.qss`(`theme.card_style()` 폐지 후속,
        `main.apply_theme()`이 쓰는 것과 같은 `load_stylesheet()` 경로)에
        LIGHT 상태색이 들어가는지."""
        loaded = theme.load_stylesheet(main.resource_path(theme.QSS_RELATIVE_PATH), theme.LIGHT)
        block = _rule_block(loaded, f'#stateIconLabel[state="{state}"]')
        assert theme.LIGHT["state" + state.capitalize()] in block

    @pytest.mark.parametrize("state", ("running", "finished", "failed"))
    def test_light_state_colours_differ_from_dark_where_contrast_needed(self, state):
        """대기색은 원래도 흰 배경에서 대비가 충분해 재사용하지만, 나머지 셋은 대비 때문에 갈아 끼웠다."""
        loaded = theme.load_stylesheet(main.resource_path(theme.QSS_RELATIVE_PATH), theme.LIGHT)
        block = _rule_block(loaded, f'#stateIconLabel[state="{state}"]')
        assert theme.DARK["state" + state.capitalize()] not in block

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

    def test_apply_theme_disables_the_combobox_overlay_popup(self, qapp, monkeypatch):
        """#241 후속 — 순정 `"Fusion"` 문자열이 아니라 `theme.build_style()`로
        감싼 스타일을 실제로 앱에 건다는 것까지 확인한다(간극을 닫는다,
        위 클래스 docstring과 같은 이유).

        `option=None`으로 질의하면 이 힌트는 스타일과 무관하게 항상 0이라
        (`TestComboBoxDropDownStyle` 참고) 아무 배선 없이도 통과해버리는
        가짜 통과였다 — 실제 `QComboBox`로 배치 자체를 재는 쪽으로 바꿨다."""
        from PySide6.QtWidgets import QComboBox
        from PySide6.QtTest import QTest

        monkeypatch.setattr(theme, "detect_color_scheme", lambda app: "dark")
        main.apply_theme(qapp)

        combo = QComboBox()
        combo.addItems(["Alpha", "Beta", "Gamma", "Delta", "Epsilon"])
        combo.setCurrentIndex(3)
        combo.move(50, 300)
        combo.show()
        QTest.qWaitForWindowExposed(combo)
        combo_bottom = combo.mapToGlobal(combo.rect().bottomLeft())

        combo.showPopup()
        popup_pos = combo.view().window().pos()
        combo.hidePopup()

        assert popup_pos == combo_bottom


def _raw_qss() -> str:
    return Path(main.resource_path(theme.QSS_RELATIVE_PATH)).read_text(encoding="utf-8")


def _rule_block(text: str, selector: str) -> str:
    """선택자와 정확히 일치하는(바로 뒤에 공백만 두고 `{`가 오는) 규칙 블록 하나를 뽑는다.

    `re.escape(selector) + r"\\s*\\{"`로 앵커를 걸어 `QComboBox`가 `QComboBox::drop-down`·
    `QComboBox QAbstractItemView`처럼 더 긴 선택자의 접두사로 걸리는 걸 막는다 —
    셋 다 문자열로는 "QComboBox"를 포함하지만 그 뒤에 바로 `{`가 오는 건 순정
    `QComboBox { ... }` 블록 하나뿐이다.
    """
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", text)
    assert m, f"{selector!r} 규칙을 QSS에서 못 찾았다"
    return m.group(1)


class TestComboBoxSubcontrols:
    """#240 감사 항목 1·2 — QComboBox 서브컨트롤(닫힌 화살표·팝업 선택 강조).

    QComboBox 자체를 QSS로 스타일하는 순간 Fusion 기본 서브컨트롤 렌더링이
    전부 꺼진다(주석·실측 확인) — `::drop-down`/`::down-arrow`를 명시적으로
    채우지 않으면 화살표 버튼만 네이티브로 남는다.
    """

    def test_drop_down_subcontrol_is_styled(self):
        _rule_block(_raw_qss(), "QComboBox::drop-down")  # 못 찾으면 자체 assert로 실패

    def test_down_arrow_subcontrol_is_styled(self):
        _rule_block(_raw_qss(), "QComboBox::down-arrow")  # 못 찾으면 자체 assert로 실패

    def test_down_arrow_does_not_use_the_transparent_keyword(self):
        """실측 확인: Qt QSS는 `border-color: transparent`로 삼각형 모서리를 안
        마이터링한다 — 사각형이 그대로 찍힌다. 배경과 같은 실색을 써야 삼각형이
        나온다(육안으로는 똑같이 안 보이면서 도형은 제대로 그려진다)."""
        block = _rule_block(_raw_qss(), "QComboBox::down-arrow")
        assert "transparent" not in block

    def test_down_arrow_border_colours_track_the_drop_down_background(self):
        """정지 상태 화살표의 좌우 border 색이 `::drop-down`의 배경 토큰과
        같아야 이음매가 안 보인다."""
        raw = _raw_qss()
        arrow_block = _rule_block(raw, "QComboBox::down-arrow")
        drop_down_block = _rule_block(raw, "QComboBox::drop-down")
        assert "@surfaceAlt" in arrow_block
        assert "background-color: @surfaceAlt" in drop_down_block

    def test_selection_colours_are_declared_on_combobox_itself(self):
        """`selection-background-color`/`selection-color`는 QComboBox 자신에 둬야
        팝업에 실제로 먹는다 — `QAbstractItemView`·`::item:selected`에 두면 안
        먹는 걸 실측으로 확인했다(고장 주입으로도 재확인, 아래 완료 보고 참고)."""
        block = _rule_block(_raw_qss(), "QComboBox")
        assert "selection-background-color: @accent" in block
        assert "selection-color: @onAccent" in block

    def test_selection_colours_resolve_to_accent_tokens(self):
        loaded = theme.load_stylesheet(main.resource_path(theme.QSS_RELATIVE_PATH))
        block = _rule_block(loaded, "QComboBox")
        assert theme.DARK["accent"] in block
        assert theme.DARK["onAccent"] in block


class TestPopupWindowsAreSquareCornered:
    """#240 감사 항목 3 — 최상위 윈도우(툴팁·콤보 팝업)는 `border-radius`를 빼야 한다.

    위젯 페인팅만 둥글고 윈도우 합성 경계 자체는 사각형이라 `border-radius`를
    주면 귀퉁이가 어긋난다(오너 실기 확인). `WA_TranslucentBackground` 우회는
    쓰지 않는다 — `QToolTip`의 실제 위젯(`QTipLabel`)은 공개 API로 못 잡고,
    투명 배경은 플랫폼마다 동작이 달라 SPEC §8.4의 플랫폼 전제 코드가 된다.
    """

    def test_tooltip_has_no_border_radius(self):
        block = _rule_block(_raw_qss(), "QToolTip")
        assert "border-radius" not in block

    def test_combobox_popup_has_no_border_radius(self):
        block = _rule_block(_raw_qss(), "QComboBox QAbstractItemView")
        assert "border-radius" not in block
