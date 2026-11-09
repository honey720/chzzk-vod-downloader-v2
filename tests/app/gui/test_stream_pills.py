"""해상도의 차례 · 선택 · 표시 — 카드 · 로그 · 헤드리스 목록 (#318).

카드는 해상도 목록을 높은 것부터 정렬하고 맨 앞을 기본으로 고른다. 다른 항목은 카드 편집 창의
해상도 드롭다운에서 고른다(#550). 카드의 정보 글과 로그는 짧은 변이 같은 항목이 둘일 때 원본
쪽에만 "(원본)"을 붙인다 — 파일명과 같은 조건이다.
"""

import logging
from collections.abc import Iterator

import pytest
from PySide6.QtCore import QTranslator
from PySide6.QtWidgets import QApplication

import app.system.settings as settings_module
import CVDv2 as main_module
import app.theme as theme
from app.download.logger import DownloadLogger
from app.viewmodels.data import ContentItem
from app.views.mainWindow import VodDownloader
from app.widgets.widget import ContentItemWidget
from core.media.dash import parse_dash_manifest
from core.chzzk.playback_tracks import list_streams, playback_tracks
from core.models.content import StreamKey
from core.models.download_state import DownloadState
from scripts.headless_download import _format_resolutions, _rep_label
from tests.app.gui.card_helpers import drop_new_top_levels, hold_style, shown, snapshot_top_levels
from tests.app.gui.test_section_editor import _pump, open_window
from tests.support.stream_samples import S1, S2, S4, S11, Sample
from typing import Never

WIDE = 1600  # 정보 글이 줄지 않는 카드 폭


@pytest.fixture(autouse=True)
def _apply_production_qss(qapp) -> None:
    """실제 전역 QSS를 태운 상태에서 잰다(scope=function 유지 — test_widget_theme 참고)."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))


@pytest.fixture(autouse=True)
def _drop_windows(qapp) -> Iterator[None]:
    """이 파일의 테스트가 띄운 창을 테스트 끝에 확실히 파괴한다(card_helpers.drop_new_top_levels)."""
    before = snapshot_top_levels()
    yield
    drop_new_top_levels(before)


@pytest.fixture(autouse=True)
def no_network(monkeypatch) -> None:
    class _FailingSession:
        def head(self, *a, **k) -> Never:
            raise RuntimeError("network disabled in tests")

        def get(self, *a, **k) -> Never:
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("app.widgets.widget.get_thread_session", lambda: _FailingSession())
    monkeypatch.setattr("app.widgets.widget._global_download_path", "C:/dl")


def _item(reps: list, content_type: str = "m3u8") -> ContentItem:
    """조회 결과의 목록 그대로 대기 아이템을 만든다 — 기본 선택은 제품과 같이 목록의 마지막 항목."""
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
    return item


def _card(reps: list, content_type: str = "m3u8") -> ContentItemWidget:
    """조회 결과의 목록 그대로 대기 카드를 만든다."""
    item = _item(reps, content_type)
    widget = ContentItemWidget(item, 0)
    widget.loadRepresentations()
    widget.setData(item, 0)
    widget.resize(WIDE, widget.sizeHint().height())
    widget.show()
    QApplication.processEvents()
    return widget


def _streams(sample: Sample) -> list:
    return list_streams(sample.master, playback_tracks(sample.playback))


def _window_card(tmp_path, reps: list) -> tuple[VodDownloader, ContentItemWidget]:
    """실제 메인 창에 대기 카드를 넣고 그 카드를 돌려준다."""
    item = _item(reps)
    win = open_window(tmp_path, item)
    card = win.listView.widgetFor(item)
    assert card is not None, "전제: 목록이 카드를 만들었다"
    return win, card


def _choose(win: VodDownloader, item: ContentItem, index: int) -> None:
    """카드 편집 창의 해상도 드롭다운에서 해상도 목록의 `index`번 줄을 골라 저장한다."""
    win.onCardEdit(item)
    _pump()
    dialog = win._editDialog
    assert dialog is not None, "전제: 카드 편집 창이 열렸다"
    combo = dialog.resolutionCombo
    row = next(r for r in range(combo.count()) if combo.itemData(r) == index)
    combo.setCurrentIndex(row)
    dialog.saveButton.click()
    _pump()


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
    ("sample", "resolutions"),
    [
        (S1, [1080, 720, 480, 360, 144]),
        (S2, [1080, 720, 480, 360, 144]),
        (S4, [720, 720, 480, 360, 144]),
        (S11, [1080, 720, 480, 360, 144]),
    ],
    ids=["S1", "S2", "S4", "S11"],
)
def test_replay_resolutions_are_listed_from_the_highest(qapp, sample, resolutions) -> None:
    """다시보기 카드의 해상도 목록은 높은 해상도부터 놓여야 한다.

    주석의 표본마다 대기 카드(loadRepresentations)
    -> item.unique_reps의 해상도 차례
    """
    widget = _card(_streams(sample))

    assert [rep[0] for rep in widget.item.unique_reps] == resolutions


def test_clip_resolutions_are_listed_from_the_highest(qapp) -> None:
    """스트림 정보가 없는 목록(클립)도 해상도가 높은 것부터 놓이고 기본 선택은 맨 앞이어야 한다.

    [[480, 주소], [720, 주소]]
    -> 해상도 차례 [720, 480], pickedIndex 0, 아이템 해상도 720
    """
    widget = _card(
        [[480, "https://c.invalid/480.mp4"], [720, "https://c.invalid/720.mp4"]],
        content_type="clip",
    )

    assert [rep[0] for rep in widget.item.unique_reps] == [720, 480]
    assert widget.pickedIndex() == 0
    assert widget.item.resolution == 720


@pytest.mark.parametrize(
    ("language", "translated"),
    [("ko_KR", "(원본)"), ("en_US", "(source)")],
)
def test_source_mark_is_translated_in_the_bundled_catalogs(qapp, language, translated) -> None:
    """동봉된 카탈로그는 언어마다 원본 표시의 번역을 갖고 있어야 한다.

    translations/<언어>.qm, 컨텍스트 ContentItemWidget, 원문 "(source)"
    -> ko_KR "(원본)", en_US "(source)". 항목이 없으면 빈 문자열이 온다
    """
    translator = QTranslator()
    assert translator.load(main_module.resource_path(f"translations/{language}.qm"))

    assert translator.translate("ContentItemWidget", "(source)") == translated


def test_first_entry_of_the_same_short_side_is_picked_and_is_the_original(qapp) -> None:
    """짧은 변이 같은 두 항목이 있으면 기본 선택은 맨 앞 항목이고 그것이 원본이어야 한다.

    S4
    -> pickedIndex 0, 아이템의 스트림 값은 30fps 변형, 파일명 표시는 "(원본)"
    """
    widget = _card(_streams(S4))

    assert widget.pickedIndex() == 0
    assert widget.item.stream == StreamKey(720, 1280, 30.0, 2692000)
    assert (widget.item.resolution, widget.item.resolution_tag) == (720, "(원본)")


def test_choosing_the_other_entry_of_the_same_short_side_selects_that_stream(
    qapp, tmp_path
) -> None:
    """편집 창에서 짧은 변이 같은 다른 항목을 고르면 아이템의 스트림 값이 그 항목의 것으로 바뀌어야 한다.

    S4, 메인 창의 카드 편집 창에서 해상도 목록 1번(다시 인코딩한 720p 60fps)을 골라 저장
    -> 스트림 값은 60fps 변형, 해상도 720 그대로, 파일명 표시는 빈 문자열, 카드가 고른 번호 1
    """
    win, widget = _window_card(tmp_path, _streams(S4))

    _choose(win, widget.item, 1)

    assert widget.item.stream == StreamKey(720, 1280, 60.0, 3192000)
    assert (widget.item.resolution, widget.item.resolution_tag) == (720, "")
    # m3u8 항목은 주소가 비어 있다 — 스트림까지 봐야 1번으로 찾는다(#550)
    assert widget.pickedIndex() == 1


# 짧은 변이 720인 Representation 둘(화질 이름이 다르다, 둘 다 원본 아님)과 144 하나 (#336)
_TWO_720 = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" xmlns:nvod="urn:naver:vod:2020">
  <Period><AdaptationSet mimeType="video/mp4">
    <Representation id="a" width="1280" height="720" bandwidth="3200000" frameRate="60"><nvod:Label kind="qualityId">720P_A</nvod:Label><BaseURL>https://v.invalid/720-60.mp4</BaseURL></Representation>
    <Representation id="b" width="1280" height="720" bandwidth="1600000" frameRate="30"><nvod:Label kind="qualityId">720P_B</nvod:Label><BaseURL>https://v.invalid/720-30.mp4</BaseURL></Representation>
    <Representation id="c" width="256" height="144" bandwidth="160000" frameRate="30"><nvod:Label kind="qualityId">144P</nvod:Label><BaseURL>https://v.invalid/144.mp4</BaseURL></Representation>
  </AdaptationSet></Period>
</MPD>"""


def test_higher_bandwidth_of_the_same_short_side_is_the_first_and_default_button(qapp) -> None:
    """해상도와 원본 여부가 같은 두 항목이면 대역폭이 높은 쪽이 맨 앞이고 기본 선택이어야 한다 (#336).

    _TWO_720(720 60fps 3200000 · 720 30fps 1600000 · 144, 모두 원본 아님)
    -> 목록 차례 720-60 · 720-30 · 144, pickedIndex 0, 아이템의 주소는 60fps 쪽(조회의 기본 선택)
    """
    reps = parse_dash_manifest(_TWO_720)[0]
    assert reps[-1][1] == "https://v.invalid/720-60.mp4"  # 조회의 기본 선택(목록의 마지막)

    widget = _card(reps, content_type="video")

    assert [rep[1] for rep in widget.item.unique_reps] == [
        "https://v.invalid/720-60.mp4",
        "https://v.invalid/720-30.mp4",
        "https://v.invalid/144.mp4",
    ]
    assert widget.pickedIndex() == 0
    assert (widget.item.resolution, widget.item.base_url) == (720, "https://v.invalid/720-60.mp4")


def test_late_size_of_another_button_does_not_move_the_default(qapp) -> None:
    """다른 항목의 크기 조회가 먼저 끝나도 기본 선택이 옮겨 가지 않아야 한다 (#336).

    _TWO_720, 1번 항목(720 30fps)의 크기 "1.00 GB"가 도착 → 다음 0번 항목(720 60fps)의 "2.00 GB"
    -> pickedIndex 0, 아이템의 주소는 60fps 쪽, 아이템의 크기는 "2.00 GB"
    """
    widget = _card(parse_dash_manifest(_TWO_720)[0], content_type="video")

    widget._repSizeFetched.emit(1, "1.00 GB")
    widget._repSizeFetched.emit(0, "2.00 GB")
    QApplication.processEvents()

    assert widget.pickedIndex() == 0
    assert widget.item.base_url == "https://v.invalid/720-60.mp4"
    assert widget.item.total_size == "2.00 GB"


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


def test_running_card_marks_the_original_when_two_entries_share_the_short_side(qapp) -> None:
    """짧은 변이 같은 항목이 둘이고 원본을 골랐으면 다운로드 중 카드의 해상도에 원본 표시가 붙어야 한다.

    S4, 기본 선택(원본 720p), 받은 양 1.20 GB (번역기 없음)
    -> "720p(source) · 1.20 GB"
    """
    widget = _card(_streams(S4))

    _start(widget)

    assert shown(widget.fileSizeLabel) == "720p(source) · 1.20 GB"


def test_running_card_shows_the_translated_source_mark(qapp) -> None:
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


def test_running_card_of_the_other_entry_of_the_same_short_side_shows_only_the_number(
    qapp, tmp_path
) -> None:
    """짧은 변이 같은 두 항목 중 원본이 아닌 쪽을 골랐으면 다운로드 중 카드의 해상도는 숫자만이어야 한다.

    S4, 카드 편집 창에서 해상도 목록 1번(다시 인코딩한 720p)을 골라 저장, 받은 양 1.20 GB
    -> "720p · 1.20 GB"
    """
    win, widget = _window_card(tmp_path, _streams(S4))
    _choose(win, widget.item, 1)

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
) -> None:
    """해상도마다 항목이 하나인 영상의 다운로드 중 카드는 원본을 골라도 해상도가 숫자만이어야 한다.

    주석의 표본마다 기본 선택(최고 해상도), 받은 크기 1.20 GB(인코딩 완료 VOD는 전체도 1.20 GB)
    -> 다시보기 "1080p · 1.20 GB", 인코딩 완료 VOD "1080p · 1.20 GB / 1.20 GB"
    """
    widget = _card(reps(), content_type=content_type)

    _start(widget)

    assert shown(widget.fileSizeLabel) == expected


def _logged_resolution(tmp_path, monkeypatch, item: ContentItem) -> list[str]:
    """아이템의 다운로드 정보를 로그 파일에 쓰고 resolution 줄의 메시지를 돌려준다."""
    monkeypatch.setattr(settings_module, "DATA_DIR", str(tmp_path))
    logger = DownloadLogger(logging.INFO)
    logger.log_download_info(item)
    log_file = logger.log_file
    logger.save_and_close()
    with open(log_file, encoding="utf-8") as f:
        messages = [line.strip().rsplit(" - ", 1)[-1] for line in f]
    assert any(message.startswith("content_type: ") for message in messages)  # 로그가 쓰였다
    return [message for message in messages if message.startswith("resolution: ")]


def test_log_marks_the_original_when_two_entries_share_the_short_side(
    qapp, tmp_path, monkeypatch
) -> None:
    """짧은 변이 같은 항목이 둘이고 원본을 골랐으면 로그의 resolution 줄에 "(원본)"이 붙어야 한다.

    S4, 기본 선택(원본 720p)
    -> ["resolution: 720(원본)"]
    """
    widget = _card(_streams(S4))

    assert _logged_resolution(tmp_path, monkeypatch, widget.item) == ["resolution: 720(원본)"]


def test_log_of_the_other_entry_of_the_same_short_side_is_only_the_number(
    qapp, tmp_path, monkeypatch
) -> None:
    """짧은 변이 같은 두 항목 중 원본이 아닌 쪽을 골랐으면 로그의 resolution 줄은 숫자만이어야 한다.

    S4, 카드 편집 창에서 해상도 목록 1번(다시 인코딩한 720p)을 골라 저장
    -> ["resolution: 720"]
    """
    win, widget = _window_card(tmp_path, _streams(S4))
    _choose(win, widget.item, 1)

    assert _logged_resolution(tmp_path, monkeypatch, widget.item) == ["resolution: 720"]


def test_log_of_a_landscape_video_is_only_the_number(qapp, tmp_path, monkeypatch) -> None:
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
def test_headless_list_labels_match_the_button(sample, labels) -> None:
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
def test_headless_list_line_shows_the_source_mark_and_every_declared_frame_rate(
    sample, line
) -> None:
    """헤드리스 --list의 한 줄은 원본에 "(원본)"을 붙이고 항목이 든 프레임률을 전부 적어야 한다.

    주석의 표본마다 조회 결과의 목록, 매니페스트 프레임률 없음({})
    -> 주석의 한 줄
    """
    assert _format_resolutions(_streams(sample), {}) == line
