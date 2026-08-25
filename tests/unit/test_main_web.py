"""main_web.py 배경색 회귀 테스트 (#217).

리사이즈 중 번쩍임은 webview.create_window의 background_color와 CSS
body 배경이 서로 다른 값을 쓸 때 생긴다 — 이 둘이 항상 같은 값을
쓰도록 단일 정의 지점(_BACKGROUND_COLOR)을 강제한다.
"""
import re
from unittest.mock import patch

import main_web


class TestBackgroundColorSingleSource:
    def test_css_body_background_matches_constant(self):
        match = re.search(r"body\s*\{[^}]*background:\s*(#[0-9a-fA-F]{3,8})", main_web._PLACEHOLDER_HTML)
        assert match is not None
        assert match.group(1) == main_web._BACKGROUND_COLOR

    def test_create_window_receives_matching_background_color(self):
        with (
            patch.object(main_web, "setup_logging"),
            patch.object(main_web.webview, "create_window") as mock_create_window,
            patch.object(main_web.webview, "start"),
        ):
            main_web.main()

        _, kwargs = mock_create_window.call_args
        assert kwargs["background_color"] == main_web._BACKGROUND_COLOR
