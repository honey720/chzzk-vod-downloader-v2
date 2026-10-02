"""해상도 버튼의 글자와 선택 — 원본 · 프레임률 표시, 짧은 변이 같은 두 항목 (#318).

버튼 글자는 임시 연결(A안 — 전부 버튼 글자에)이다. 표시 방식이 정해지면 글자를 재는
단언은 그 방식에 맞게 바뀐다. 선택이 아이템에 싣는 값(스트림 · 파일명 표시)은 표시
방식과 무관하다.
"""

import pytest
from PySide6.QtCore import QTranslator
from PySide6.QtWidgets import QApplication

import main as main_module
import app.theme as theme
from app.viewmodels.data import ContentItem
from app.widgets.widget import ContentItemWidget
from core.api.dash import parse_dash_manifest
from core.api.playback_tracks import list_streams, playback_tracks
from core.models.content import StreamKey
from core.models.download_state import DownloadState
from tests.unit.card_helpers import drop_new_top_levels, hold_style, shown, snapshot_top_levels
from tests.unit.stream_samples import S1, S2, S4, S11, Sample

WIDE = 1600  # pill이 접히지 않는 폭 — 버튼을 누르면 바로 선택된다


@pytest.fixture(autouse=True)
def _apply_production_qss(qapp):
    """실제 전역 QSS를 태운 상태에서 잰다(scope=function 유지 — test_widget_theme 참고)."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))


@pytest.fixture(autouse=True)
def _drop_windows(qapp):
    """이 파일의 테스트가 띄운 창을 테스트 끝에 확실히 파괴한다(card_helpers.drop_new_top_levels)."""
    before = snapshot_top_levels()
    yield
    drop_new_top_levels(before)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    class _FailingSession:
        def head(self, *a, **k):
            raise RuntimeError("network disabled in tests")

        def get(self, *a, **k):
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("app.widgets.widget.get_thread_session", lambda: _FailingSession())
    monkeypatch.setattr("app.widgets.widget._global_download_path", "C:/dl")


def _card(reps: list, content_type: str = "m3u8") -> ContentItemWidget:
    """조회 결과의 목록 그대로 대기 카드를 만든다 — 기본 선택은 제품과 같이 목록의 마지막 항목."""
    item = ContentItem(
        "https://chzzk.naver.com/video/1",
        {
            "title": "제목",
            "category": "",
            "channelName": "채널",
            "createdDate": "",
            "duration": 3600,
        },
        reps,
        reps[-1][0],
        reps[-1][1],
        "C:/dl",
        content_type,
        None,
    )
    item.downloadState = DownloadState.WAITING
    widget = ContentItemWidget(item, 0)
    widget.addRepresentationButtons()
    widget.setData(item, 0)
    widget.resize(WIDE, widget.sizeHint().height())
    widget.show()
    QApplication.processEvents()
    return widget


def _streams(sample: Sample) -> list:
    return list_streams(sample.master, playback_tracks(sample.playback))


def _texts(widget: ContentItemWidget) -> list[str]:
    return [shown(button) for button in widget.buttons]


_ENCODED_VOD = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" xmlns:nvod="urn:naver:vod:2020">
  <Period><AdaptationSet mimeType="video/mp4">
    <Representation id="a" width="1920" height="1080" bandwidth="8000000" frameRate="60"><nvod:Label kind="qualityId">1080P</nvod:Label><BaseURL>https://v.invalid/1080.mp4</BaseURL></Representation>
    <Representation id="b" width="256" height="144" bandwidth="160000" frameRate="30"><nvod:Label kind="qualityId">144P</nvod:Label><BaseURL>https://v.invalid/144.mp4</BaseURL></Representation>
    <Representation id="c" width="1280" height="720" bandwidth="3200000" frameRate="60"><nvod:Label kind="qualityId">720P</nvod:Label><BaseURL>https://v.invalid/720.mp4</BaseURL></Representation>
  </AdaptationSet></Period>
</MPD>"""


@pytest.mark.parametrize(
    ("sample", "texts"),
    [
        (S1, ["1080p(source) 60fps", "720p 60fps", "480p", "360p", "144p"]),
        (S2, ["1080p(source)", "720p 60fps", "480p", "360p", "144p"]),
        (S4, ["720p(source)", "720p 60fps", "480p", "360p", "144p"]),
        (S11, ["1080p 60fps", "720p 60fps", "480p", "360p", "144p"]),
    ],
    ids=["S1", "S2", "S4", "S11"],
)
def test_replay_buttons_show_the_source_mark_and_the_frame_rate(qapp, sample, texts):
    """다시보기의 해상도 버튼은 높은 것부터이고, 원본이면 원본 표시가, 50fps 이상이면 fps가 붙어야 한다.

    주석의 표본마다 (번역기 없음 — 원본 표시는 원문 "(source)")
    -> 왼쪽부터의 버튼 글자. 짧은 변이 같으면 원본이 앞
    """
    widget = _card(_streams(sample))

    assert _texts(widget) == texts


def test_encoded_vod_buttons_show_the_frame_rate_without_a_source_mark(qapp):
    """인코딩 완료 VOD의 해상도 버튼은 fps만 붙고 원본 표시는 없어야 한다.

    1080(60fps) · 720(60fps) · 144(30fps) 매니페스트
    -> ["1080p 60fps", "720p 60fps", "144p"]
    """
    reps, _auto_resolution, _auto_base_url = parse_dash_manifest(_ENCODED_VOD)

    widget = _card(reps, content_type="video")

    assert _texts(widget) == ["1080p 60fps", "720p 60fps", "144p"]


def test_clip_buttons_show_only_the_number(qapp):
    """스트림 정보가 없는 목록(클립)의 해상도 버튼은 숫자만 보여야 한다.

    [[480, 주소], [720, 주소]]
    -> ["720p", "480p"]
    """
    widget = _card(
        [[480, "https://c.invalid/480.mp4"], [720, "https://c.invalid/720.mp4"]],
        content_type="clip",
    )

    assert _texts(widget) == ["720p", "480p"]


def test_source_mark_is_translated_in_the_bundled_korean_catalog(qapp):
    """동봉된 한국어 카탈로그는 원본 표시를 "(원본)"으로 돌려줘야 한다.

    translations/ko_KR.qm, 컨텍스트 ContentItemWidget, 원문 "(source)"
    -> "(원본)"
    """
    translator = QTranslator()
    assert translator.load(main_module.resource_path("translations/ko_KR.qm"))

    assert translator.translate("ContentItemWidget", "(source)") == "(원본)"


def test_first_button_of_the_same_short_side_is_selected_and_is_the_original(qapp):
    """짧은 변이 같은 두 항목이 있으면 기본 선택은 맨 앞 버튼이고 그것이 원본이어야 한다.

    S4
    -> 첫 버튼만 선택, 아이템의 스트림 값은 30fps 변형, 파일명 표시는 "(원본)"
    """
    widget = _card(_streams(S4))

    assert [button.isSelected() for button in widget.buttons] == [True, False, False, False, False]
    assert widget.item.stream == StreamKey(720, 1280, 30.0, 2692000)
    assert (widget.item.resolution, widget.item.resolution_tag) == (720, "(원본)")


def test_clicking_the_other_button_of_the_same_short_side_selects_that_stream(qapp):
    """짧은 변이 같은 다른 버튼을 누르면 아이템의 스트림 값이 그 항목의 것으로 바뀌어야 한다.

    S4, 둘째 버튼("720p 60fps")을 누름
    -> 둘째 버튼만 선택, 스트림 값은 60fps 변형, 해상도 720 그대로, 파일명 표시는 빈 문자열
    """
    widget = _card(_streams(S4))

    widget.buttons[1].click()
    QApplication.processEvents()

    assert [button.isSelected() for button in widget.buttons] == [False, True, False, False, False]
    assert widget.item.stream == StreamKey(720, 1280, 60.0, 3192000)
    assert (widget.item.resolution, widget.item.resolution_tag) == (720, "")
