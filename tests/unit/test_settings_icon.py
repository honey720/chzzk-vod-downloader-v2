"""상단 바 설정 버튼의 톱니 아이콘 게이트.

이전에는 `QPushButton.setText("⚙")`(U+2699)이었다 — 어느 폰트가 그 문자를 받느냐에
따라 모양이 달라 Windows 11에서 설정 앱의 톱니와 다르게 보였다. 지금은
`IconButton`("settings")이다: Windows에서는 설정 앱과 같은 글리프(Segoe Fluent Icons /
Segoe MDL2 Assets의 U+E713), 그 폰트가 없는 OS에서는 같은 자세의 외곽선 톱니를 직접
그린다(`icons._paint_settings_drawn`).

게이트는 세 갈래다 — ① 버튼이 글리프 텍스트가 아니라 도형 버튼인가 ② 글리프 경로와
그린 경로가 **각각** 무언가를 칠하는가(폰트 유무를 테스트가 강제) ③ 폰트 선택이
알려진 두 가족 밖으로 새지 않는가. 이 파일은 폰트 유무에 답이 달린 단언을 두지 않는다
(3-OS CI 어디서든 같은 결과).
"""

import pytest
from PySide6.QtWidgets import QApplication

import app.theme as theme
from app.views.mainWindow import VodDownloader
from app.widgets import icons


@pytest.fixture
def window(qapp):
    win = VodDownloader()
    yield win
    win.deleteLater()
    QApplication.processEvents()


def _painted(name: str, size: int) -> int:
    image = icons.action_pixmap(name, theme.DARK["text"], size).toImage()
    return sum(
        1
        for x in range(image.width())
        for y in range(image.height())
        if image.pixelColor(x, y).alpha() > 0
    )


@pytest.fixture
def force_family(monkeypatch):
    """설정 톱니의 폰트 선택을 테스트가 정한다 — 캐시된 픽스맵이 답을 오염시키지 않게 비운다."""

    def apply(family):
        monkeypatch.setattr(icons, "_settings_family", family)
        icons._CACHE.clear()

    yield apply
    monkeypatch.setattr(icons, "_settings_family", None)
    icons._CACHE.clear()


class TestButton:
    def test_setting_button_is_a_drawn_icon_not_a_text_glyph(self, window):
        button = window.settingButton
        assert isinstance(button, icons.IconButton), "설정 버튼이 도형 버튼이 아니다"
        assert button.text() == "", "설정 버튼에 텍스트 글리프가 남아 있다"
        assert button.iconName() == "settings"
        assert button.property("role") == "icon", "호버 표면 QSS 규칙이 안 붙는다"

    def test_glyph_size_matches_the_header_metric(self, window):
        """32px 버튼에 카드용 12px 도형을 그리면 작아서 안 읽힌다 — 상단 바 크기를 쓴다."""
        assert window.settingButton.glyphSize() == theme.METRICS["headerGlyph"]
        assert theme.METRICS["headerGlyph"] > theme.METRICS["actionGlyph"]
        assert theme.METRICS["headerGlyph"] <= window.settingButton.minimumHeight()

    def test_glyph_size_default_is_the_card_metric(self, qapp):
        """설정 버튼 때문에 카드 조작 아이콘 기본이 바뀌면 안 된다."""
        button = icons.IconButton()
        try:
            assert button.glyphSize() == theme.METRICS["actionGlyph"]
            button.setGlyphSize(16)
            assert button.glyphSize() == 16
            button.setGlyphSize(0)
            assert button.glyphSize() == theme.METRICS["actionGlyph"]
        finally:
            button.deleteLater()


class TestPainters:
    def test_drawn_fallback_paints_without_the_font(self, qapp, force_family):
        force_family("")
        assert _painted("settings", theme.METRICS["headerGlyph"]) > 0, "그린 톱니가 비어 있다"

    def test_platform_glyph_paints_when_the_font_exists(self, qapp, force_family):
        """폰트가 있는 곳(Windows)에서만 잰다 — 없는 OS에서는 앞 테스트(그린 경로)가 답이다."""
        force_family(None)
        family = icons.settings_glyph_family()
        if not family:
            pytest.skip("설정 글리프 폰트가 없는 OS — 그린 경로가 쓰인다")
        assert _painted("settings", theme.METRICS["headerGlyph"]) > 0, f"{family} U+E713이 비어 있다"

    def test_family_choice_stays_within_the_known_list(self, qapp, force_family):
        force_family(None)
        assert icons.settings_glyph_family() in ("",) + icons._SETTINGS_GLYPH_FAMILIES
