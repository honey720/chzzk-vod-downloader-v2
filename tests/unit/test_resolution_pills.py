"""카드 3행 해상도 pill 게이트 (#244 3행 정리).

- 같은 높이 트랙이 둘인 매니페스트 → pill은 높이당 하나 (core/api/representations.py)
"""

import pytest
from PySide6.QtWidgets import QApplication

import main as main_module
import theme
from content.data import ContentItem
from content.widget import ContentItemWidget
from core.api.dash import parse_dash_manifest
from core.models.download_state import DownloadState
from tests.unit.card_helpers import hold_style


@pytest.fixture(autouse=True)
def _apply_production_qss(qapp):
    """실제 전역 QSS를 태운 상태에서 잰다(scope=function 유지 — test_widget_theme 참고)."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    class _FailingSession:
        def head(self, *a, **k):
            raise RuntimeError("network disabled in tests")

        def get(self, *a, **k):
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("content.widget.get_thread_session", lambda: _FailingSession())
    monkeypatch.setattr("content.widget._global_download_path", "C:/dl")


def make_waiting_widget(reps, width=900, path="C:/dl") -> ContentItemWidget:
    """대기 카드 — `reps`는 파서가 돌려주는 `[해상도, url]` 목록(또는 그 튜플)."""
    item = ContentItem(
        "https://chzzk.naver.com/video/1",
        {"title": "제목", "category": "", "channelName": "채널", "createdDate": "", "duration": 3600},
        [list(r) for r in reps], None, "", path, "video", None,
    )
    item.downloadState = DownloadState.WAITING
    item.total_size = "711.02 MB"
    widget = ContentItemWidget(item, 0)
    widget.addRepresentationButtons()
    widget.setData(item, 0)
    widget.resize(width, widget.sizeHint().height())
    widget.show()
    QApplication.processEvents()
    return widget


_DUPLICATE_HEIGHTS = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011">
  <Period><AdaptationSet mimeType="video/mp4">
    <Representation id="a" width="1920" height="1080" bandwidth="5000000"><BaseURL>https://v.invalid/1080_low.mp4</BaseURL></Representation>
    <Representation id="b" width="1920" height="1080" bandwidth="6000000"><BaseURL>https://v.invalid/1080_high.mp4</BaseURL></Representation>
    <Representation id="c" width="1280" height="720" bandwidth="3000000"><BaseURL>https://v.invalid/720.mp4</BaseURL></Representation>
    <Representation id="d" width="1280" height="720" bandwidth="2000000"><BaseURL>https://v.invalid/720_low.mp4</BaseURL></Representation>
  </AdaptationSet></Period>
</MPD>"""


class TestSameHeightTracksMakeOnePill:
    """같은 높이 트랙이 둘인 매니페스트에서 pill이 하나만 생긴다 — [3] 게이트."""

    def test_one_pill_per_height_from_a_manifest_with_duplicate_heights(self, qapp):
        reps, _, _ = parse_dash_manifest(_DUPLICATE_HEIGHTS)
        widget = make_waiting_widget(reps)
        texts = [b.text() for b in widget.buttons]
        assert texts == ["1080p", "720p"], f"높이당 pill 하나여야 한다: {texts}"
        # 남은 것은 비트레이트가 높은 트랙 — pill이 가리키는 URL로 확인
        assert widget.item.unique_reps[0][1] == "https://v.invalid/1080_high.mp4"
        assert widget.item.unique_reps[1][1] == "https://v.invalid/720.mp4"
