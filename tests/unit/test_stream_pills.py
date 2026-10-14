"""해상도의 표시와 선택 — 버튼 · 카드 · 로그 · 헤드리스 목록 (#318).

버튼의 본 글자는 "<짧은 변>p"이고 원본이면 "(원본)"이 붙는다. 50fps 이상의 프레임률은
본 글자가 아니라 같은 버튼의 보조 글자다. 카드(다운로드 시작 이후)와 로그는 짧은 변이
같은 항목이 둘일 때 원본 쪽에만 "(원본)"을 붙인다 — 파일명과 같은 조건이다.
"""

import logging

import pytest
from PySide6.QtCore import QTranslator
from PySide6.QtGui import QColor, QFontMetrics
from PySide6.QtWidgets import QApplication

import config.config as config_module
import main as main_module
import app.theme as theme
from app.download_logger import DownloadLogger
from app.viewmodels.data import ContentItem
from app.widgets.widget import ContentItemWidget
from core.api.dash import parse_dash_manifest
from core.api.playback_tracks import list_streams, playback_tracks
from core.models.content import StreamKey
from core.models.download_state import DownloadState
from scripts.headless_download import _format_resolutions, _rep_label
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


def _secondary_texts(widget: ContentItemWidget) -> list[str]:
    for button in widget.buttons:
        assert button.isVisible()
    return [button.secondaryText() for button in widget.buttons]


_ENCODED_VOD = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" xmlns:nvod="urn:naver:vod:2020">
  <Period><AdaptationSet mimeType="video/mp4">
    <Representation id="a" width="1920" height="1080" bandwidth="8000000" frameRate="60"><nvod:Label kind="qualityId">1080P</nvod:Label><BaseURL>https://v.invalid/1080.mp4</BaseURL></Representation>
    <Representation id="b" width="256" height="144" bandwidth="160000" frameRate="30"><nvod:Label kind="qualityId">144P</nvod:Label><BaseURL>https://v.invalid/144.mp4</BaseURL></Representation>
    <Representation id="c" width="1280" height="720" bandwidth="3200000" frameRate="60"><nvod:Label kind="qualityId">720P</nvod:Label><BaseURL>https://v.invalid/720.mp4</BaseURL></Representation>
  </AdaptationSet></Period>
</MPD>"""


def _encoded_vod() -> list:
    return parse_dash_manifest(_ENCODED_VOD)[0]


@pytest.mark.parametrize(
    ("sample", "texts"),
    [
        (S1, ["1080p(source)", "720p", "480p", "360p", "144p"]),
        (S2, ["1080p(source)", "720p", "480p", "360p", "144p"]),
        (S4, ["720p(source)", "720p", "480p", "360p", "144p"]),
        (S11, ["1080p", "720p", "480p", "360p", "144p"]),
    ],
    ids=["S1", "S2", "S4", "S11"],
)
def test_replay_button_text_is_the_short_side_with_the_source_mark(qapp, sample, texts):
    """다시보기의 해상도 버튼의 본 글자는 높은 것부터 "<짧은 변>p"이고 원본에만 원본 표시가 붙어야 한다.

    주석의 표본마다 (번역기 없음 — 원본 표시는 원문 "(source)")
    -> 왼쪽부터의 버튼 글자. 짧은 변이 같으면 원본이 앞
    """
    widget = _card(_streams(sample))

    assert _texts(widget) == texts


@pytest.mark.parametrize(
    ("sample", "secondary"),
    [
        (S1, ["60fps", "60fps", "", "", ""]),
        (S2, ["", "60fps", "", "", ""]),  # 원본이 30fps다
        (S4, ["", "60fps", "", "", ""]),
        (S11, ["60fps", "60fps", "", "", ""]),
    ],
    ids=["S1", "S2", "S4", "S11"],
)
def test_replay_button_secondary_text_is_the_frame_rate_from_50fps(qapp, sample, secondary):
    """다시보기의 해상도 버튼의 보조 글자는 50fps 이상인 항목에만 "<fps>fps"여야 한다.

    주석의 표본마다
    -> 왼쪽부터의 보조 글자. 30fps 항목은 빈 문자열
    """
    widget = _card(_streams(sample))

    assert _secondary_texts(widget) == secondary


def test_encoded_vod_buttons_show_the_frame_rate_without_a_source_mark(qapp):
    """인코딩 완료 VOD의 해상도 버튼은 보조 글자에 fps만 있고 원본 표시는 없어야 한다.

    1080(60fps) · 720(60fps) · 144(30fps) 매니페스트
    -> 본 글자 ["1080p", "720p", "144p"], 보조 글자 ["60fps", "60fps", ""]
    """
    widget = _card(_encoded_vod(), content_type="video")

    assert _texts(widget) == ["1080p", "720p", "144p"]
    assert _secondary_texts(widget) == ["60fps", "60fps", ""]


def test_clip_buttons_show_only_the_number(qapp):
    """스트림 정보가 없는 목록(클립)의 해상도 버튼은 숫자만 보이고 보조 글자가 없어야 한다.

    [[480, 주소], [720, 주소]]
    -> 본 글자 ["720p", "480p"], 보조 글자 ["", ""]
    """
    widget = _card(
        [[480, "https://c.invalid/480.mp4"], [720, "https://c.invalid/720.mp4"]],
        content_type="clip",
    )

    assert _texts(widget) == ["720p", "480p"]
    assert _secondary_texts(widget) == ["", ""]


def test_button_with_secondary_text_is_wider_by_more_than_that_text(qapp):
    """보조 글자가 있는 버튼의 자연 폭은 없을 때보다 그 글자의 폭 넘게 넓어야 한다.

    S1의 둘째 버튼("720p" + "60fps"), 보조 글자를 지운 뒤의 자연 폭과 비교
    -> 차이 > 버튼의 글꼴(본 글자와 같은 크기)로 잰 "60fps"의 폭
    """
    button = _card(_streams(S1)).buttons[1]
    assert button.isVisible()
    with_secondary = button.sizeHint().width()
    text_width = QFontMetrics(button.font()).horizontalAdvance("60fps")

    button.setSecondaryText("")

    assert with_secondary - button.sizeHint().width() > text_width


def test_secondary_text_of_an_unselected_button_is_dimmer_than_its_text(qapp):
    """선택되지 않은 버튼의 보조 글자 색은 textDisabled 토큰의 색이고 본 글자 색과 달라야 한다.

    S1의 둘째 버튼(선택 아님, 보조 글자 "60fps"), 다크 테마. 본 글자 색은 QSS의 textMuted
    -> 보조 글자 색 == textDisabled, != textMuted
    """
    button = _card(_streams(S1)).buttons[1]
    tokens = theme.current_tokens()
    assert button.isVisible() and not button.isSelected()

    assert button.secondaryColor() == QColor(tokens["textDisabled"])
    assert button.secondaryColor() != QColor(tokens["textMuted"])


def test_secondary_text_of_the_selected_button_is_translucent_on_accent(qapp):
    """선택된 버튼의 보조 글자 색은 onAccent와 색이 같고 불투명하지 않되, 절반보다는 진해야 한다.

    S1의 첫 버튼(선택, 보조 글자 "60fps"), 다크 테마. 본 글자 색은 QSS의 onAccent(불투명)
    -> 보조 글자의 RGB == onAccent의 RGB, 0.5 < 불투명도 < 1
    """
    button = _card(_streams(S1)).buttons[0]
    on_accent = QColor(theme.current_tokens()["onAccent"])
    assert button.isVisible() and button.isSelected()

    color = button.secondaryColor()

    assert color.rgb() == on_accent.rgb()
    assert 0.5 < color.alphaF() < 1


@pytest.mark.parametrize(
    ("language", "translated"),
    [("ko_KR", "(원본)"), ("en_US", "(source)")],
)
def test_source_mark_is_translated_in_the_bundled_catalogs(qapp, language, translated):
    """동봉된 카탈로그는 언어마다 원본 표시의 번역을 갖고 있어야 한다.

    translations/<언어>.qm, 컨텍스트 ContentItemWidget, 원문 "(source)"
    -> ko_KR "(원본)", en_US "(source)". 항목이 없으면 빈 문자열이 온다
    """
    translator = QTranslator()
    assert translator.load(main_module.resource_path(f"translations/{language}.qm"))

    assert translator.translate("ContentItemWidget", "(source)") == translated


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

    S4, 둘째 버튼("720p", 보조 글자 "60fps")을 누름
    -> 둘째 버튼만 선택, 스트림 값은 60fps 변형, 해상도 720 그대로, 파일명 표시는 빈 문자열
    """
    widget = _card(_streams(S4))

    widget.buttons[1].click()
    QApplication.processEvents()

    assert [button.isSelected() for button in widget.buttons] == [False, True, False, False, False]
    assert widget.item.stream == StreamKey(720, 1280, 60.0, 3192000)
    assert (widget.item.resolution, widget.item.resolution_tag) == (720, "")


def _start(widget: ContentItemWidget) -> None:
    """고른 그대로 다운로드 중 상태로 넘긴다 — 크기 1.20 GB."""
    item = widget.item
    item.downloadState = DownloadState.RUNNING
    item.download_progress = 42
    item.download_speed = "5.0 MB/s"
    item.download_remain_time = "00:03:12"
    item.download_size = 1288490189  # 세그먼트 기반의 받은 양 — 1.20 GB
    item.total_size = "1.20 GB"  # 파일 기반의 총량
    widget.setData(item, 0)
    QApplication.processEvents()


def test_running_card_marks_the_original_when_two_entries_share_the_short_side(qapp):
    """짧은 변이 같은 항목이 둘이고 원본을 골랐으면 다운로드 중 카드의 해상도에 원본 표시가 붙어야 한다.

    S4, 기본 선택(원본 720p), 받은 양 1.20 GB (번역기 없음)
    -> "720p(source) · 1.20 GB"
    """
    widget = _card(_streams(S4))

    _start(widget)

    assert shown(widget.fileSizeLabel) == "720p(source) · 1.20 GB"


def test_running_card_shows_the_translated_source_mark(qapp):
    """한국어 카탈로그를 쓰면 다운로드 중 카드의 원본 표시는 "(원본)"이어야 한다.

    S4, 기본 선택(원본 720p), 받은 양 1.20 GB, translations/ko_KR.qm 설치
    -> "720p(원본) · 1.20 GB"
    """
    translator = QTranslator()
    assert translator.load(main_module.resource_path("translations/ko_KR.qm"))
    qapp.installTranslator(translator)
    try:
        widget = _card(_streams(S4))
        _start(widget)

        assert shown(widget.fileSizeLabel) == "720p(원본) · 1.20 GB"
    finally:
        qapp.removeTranslator(translator)


def test_running_card_of_the_other_entry_of_the_same_short_side_shows_only_the_number(qapp):
    """짧은 변이 같은 두 항목 중 원본이 아닌 쪽을 골랐으면 다운로드 중 카드의 해상도는 숫자만이어야 한다.

    S4, 둘째 버튼(다시 인코딩한 720p)을 누름, 받은 양 1.20 GB
    -> "720p · 1.20 GB"
    """
    widget = _card(_streams(S4))
    widget.buttons[1].click()
    QApplication.processEvents()

    _start(widget)

    assert shown(widget.fileSizeLabel) == "720p · 1.20 GB"


@pytest.mark.parametrize(
    ("reps", "content_type", "expected"),
    [
        (lambda: _streams(S1), "m3u8", "1080p · 1.20 GB"),  # 원본 60fps
        (lambda: _streams(S2), "m3u8", "1080p · 1.20 GB"),  # 원본 30fps
        # 인코딩 완료 VOD는 받은 크기 / 전체 크기다 (#309 — v2.9.6의 표기를 되살렸다)
        (_encoded_vod, "video", "1080p · 1.20 GB / 1.20 GB"),
    ],
    ids=["S1", "S2", "encoded"],
)
def test_running_card_of_a_landscape_video_shows_only_the_number(
    qapp, reps, content_type, expected
):
    """해상도마다 항목이 하나인 영상의 다운로드 중 카드는 원본을 골라도 해상도가 숫자만이어야 한다.

    주석의 표본마다 기본 선택(최고 해상도), 받은 크기 1.20 GB(인코딩 완료 VOD는 전체도 1.20 GB)
    -> 다시보기 "1080p · 1.20 GB", 인코딩 완료 VOD "1080p · 1.20 GB / 1.20 GB"
    """
    widget = _card(reps(), content_type=content_type)

    _start(widget)

    assert shown(widget.fileSizeLabel) == expected


def _logged_resolution(tmp_path, monkeypatch, item: ContentItem) -> list[str]:
    """아이템의 다운로드 정보를 로그 파일에 쓰고 resolution 줄의 메시지를 돌려준다."""
    monkeypatch.setattr(config_module, "CONFIG_DIR", str(tmp_path))
    logger = DownloadLogger(logging.INFO)
    logger.log_download_info(item)
    log_file = logger.log_file
    logger.save_and_close()
    with open(log_file, encoding="utf-8") as f:
        messages = [line.strip().rsplit(" - ", 1)[-1] for line in f]
    assert any(message.startswith("content_type: ") for message in messages)  # 로그가 쓰였다
    return [message for message in messages if message.startswith("resolution: ")]


def test_log_marks_the_original_when_two_entries_share_the_short_side(qapp, tmp_path, monkeypatch):
    """짧은 변이 같은 항목이 둘이고 원본을 골랐으면 로그의 resolution 줄에 "(원본)"이 붙어야 한다.

    S4, 기본 선택(원본 720p)
    -> ["resolution: 720(원본)"]
    """
    widget = _card(_streams(S4))

    assert _logged_resolution(tmp_path, monkeypatch, widget.item) == ["resolution: 720(원본)"]


def test_log_of_the_other_entry_of_the_same_short_side_is_only_the_number(
    qapp, tmp_path, monkeypatch
):
    """짧은 변이 같은 두 항목 중 원본이 아닌 쪽을 골랐으면 로그의 resolution 줄은 숫자만이어야 한다.

    S4, 둘째 버튼(다시 인코딩한 720p)을 누름
    -> ["resolution: 720"]
    """
    widget = _card(_streams(S4))
    widget.buttons[1].click()
    QApplication.processEvents()

    assert _logged_resolution(tmp_path, monkeypatch, widget.item) == ["resolution: 720"]


def test_log_of_a_landscape_video_is_only_the_number(qapp, tmp_path, monkeypatch):
    """해상도마다 항목이 하나인 영상은 원본을 골라도 로그의 resolution 줄이 숫자만이어야 한다.

    S1, 기본 선택(원본 1080p)
    -> ["resolution: 1080"]
    """
    widget = _card(_streams(S1))

    assert _logged_resolution(tmp_path, monkeypatch, widget.item) == ["resolution: 1080"]


@pytest.mark.parametrize(
    ("sample", "labels"),
    [
        (S1, ["144p", "360p", "480p", "720p · 60fps", "1080p(원본) · 60fps"]),
        (S4, ["144p", "360p", "480p", "720p · 60fps", "720p(원본)"]),
    ],
    ids=["S1", "S4"],
)
def test_headless_list_labels_match_the_button(sample, labels):
    """헤드리스 --list의 항목 글자는 "<짧은 변>p"에 원본이면 "(원본)", 50fps 이상이면 " · <fps>fps"가 붙어야 한다.

    주석의 표본마다 조회 결과의 목록 순서 그대로(낮은 것부터)
    -> 항목 글자
    """
    assert [_rep_label(rep) for rep in _streams(sample)] == labels


@pytest.mark.parametrize(
    ("sample", "line"),
    [
        (S1, "144p · 30fps, 360p · 30fps, 480p · 30fps, 720p · 60fps, 1080p(원본) · 60fps"),
        (S4, "144p · 30fps, 360p · 30fps, 480p · 30fps, 720p · 60fps, 720p(원본) · 30fps"),
    ],
    ids=["S1", "S4"],
)
def test_headless_list_line_shows_the_source_mark_and_every_declared_frame_rate(sample, line):
    """헤드리스 --list의 한 줄은 원본에 "(원본)"을 붙이고 항목이 든 프레임률을 전부 적어야 한다.

    주석의 표본마다 조회 결과의 목록, 매니페스트 프레임률 없음({})
    -> 주석의 한 줄
    """
    assert _format_resolutions(_streams(sample), {}) == line
