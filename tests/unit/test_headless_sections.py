"""헤드리스 스크립트의 구간 옵션과 해상도 목록(scripts/headless_download.py) 단위 테스트 (#309).

옵션 해석과 출력 문자열을 본다 — 조회·다운로드는 대역으로 바꾼다.
"""

import logging
from fractions import Fraction

import pytest

import scripts.headless_download as headless
from core.models.content import VideoInfo
from core.utils.timecode import TIMECODE_FRAME_OUT_OF_RANGE, TIMECODE_INVALID_FORMAT, TimecodeError
from scripts.headless_download import (
    _fetch_frame_rates,
    _format_fps,
    _format_resolutions,
    _parse_args,
    _parse_sections,
)


def test_section_option_can_be_given_several_times():
    """--section을 여러 번 주면 준 순서대로 모아야 한다.

    URL 하나와 --section 둘
    -> args.section == [첫째, 둘째]
    """
    args = _parse_args(
        ["https://chzzk.naver.com/video/1", "--section", "00:10:05:00-00:10:16:00"]
        + ["--section", "00:42:00:12-00:42:31:00"]
    )

    assert args.section == ["00:10:05:00-00:10:16:00", "00:42:00:12-00:42:31:00"]


def test_section_option_defaults_to_whole_download():
    """--section을 주지 않으면 구간이 없어야 한다(전체 다운로드).

    URL 하나
    -> args.section == []
    """
    assert _parse_args(["https://chzzk.naver.com/video/1"]).section == []


def test_parse_sections_turns_timecodes_into_seconds():
    """_parse_sections는 `시작-끝` 타임코드를 그 프레임률의 초 쌍으로 바꿔야 한다.

    "00:00:10:15-00:01:00:00", 30fps (15프레임 = 0.5초)
    -> [(10.5, 60.0)]
    """
    assert _parse_sections(["00:00:10:15-00:01:00:00"], Fraction(30)) == [(10.5, 60.0)]


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("00:00:10:00", TIMECODE_INVALID_FORMAT),  # 끝이 없다
        ("00:00:10:00-abc", TIMECODE_INVALID_FORMAT),  # 끝이 타임코드가 아니다
        ("00:00:10:30-00:00:20:00", TIMECODE_FRAME_OUT_OF_RANGE),  # 30fps에서 프레임 칸 30
    ],
)
def test_parse_sections_rejects_malformed_section(text, key):
    """_parse_sections는 형식에 맞지 않는 구간을 타임코드 키로 거부해야 한다.

    주석의 경우마다 구간 문자열, 30fps
    -> TimecodeError(message_key == 그 키)
    """
    with pytest.raises(TimecodeError) as info:
        _parse_sections([text], Fraction(30))

    assert info.value.message_key == key


# ================================================================ --list


@pytest.mark.parametrize(
    ("rate", "expected"),
    [
        (Fraction(60), "60fps"),
        (Fraction(30), "30fps"),
        (Fraction(30000, 1001), "29.97fps"),
        (Fraction(2997, 100), "29.97fps"),
        (Fraction(60000, 1001), "59.94fps"),
        (Fraction(25, 2), "12.5fps"),
        (None, "fps 모름"),
    ],
)
def test_format_fps_shows_integers_bare_and_fractions_to_two_places(rate, expected):
    """_format_fps는 정수 프레임률은 그대로, 분수는 소수 둘째 자리까지, 없으면 "fps 모름"으로 적어야 한다.

    주석의 경우마다 프레임률
    -> 표시 문자열
    """
    assert _format_fps(rate) == expected


def test_format_resolutions_attaches_frame_rate_to_each_resolution():
    """_format_resolutions는 해상도마다 그 base_url의 프레임률을 붙이고, 없는 것은 "fps 모름"으로 적어야 한다.

    해상도 144 · 720 · 1080, 프레임률은 144(30) · 1080(60)만 있음
    -> "144p · 30fps, 720p · fps 모름, 1080p · 60fps"
    """
    reps = [[144, "u144"], [720, "u720"], [1080, "u1080"]]
    rates = {"u144": Fraction(30), "u1080": Fraction(60)}

    assert _format_resolutions(reps, rates) == "144p · 30fps, 720p · fps 모름, 1080p · 60fps"


def _video_info() -> VideoInfo:
    return VideoInfo(
        video_id="vid",
        in_key="key",
        adult=False,
        vod_status=None,
        live_rewind_playback_json=None,
        membership_benefit_type=None,
        encryption_type=None,
        metadata={},
    )


def test_fetch_frame_rates_reads_the_manifest_of_an_encoded_vod(monkeypatch):
    """_fetch_frame_rates는 인코딩 완료 VOD면 영상 정보의 video_id · in_key로 매니페스트의 프레임률을 읽어야 한다.

    content_type "video", get_video_info가 (vid, key)를 돌려줌
    -> get_video_frame_rates("vid", "key", 쿠키)의 결과
    """
    calls = []
    cookies = {"NID_AUT": "", "NID_SES": ""}
    monkeypatch.setattr(headless.NetworkManager, "get_video_info", lambda no, c: _video_info())

    def frame_rates(video_id, in_key, given):
        calls.append((video_id, in_key, given))
        return {"u1080": Fraction(60)}

    monkeypatch.setattr(headless.NetworkManager, "get_video_frame_rates", frame_rates)

    rates = _fetch_frame_rates("https://chzzk.naver.com/video/123", cookies, "video")

    assert rates == {"u1080": Fraction(60)}
    assert calls == [("vid", "key", cookies)]


@pytest.mark.parametrize("content_type", ["m3u8", "clip"])
def test_fetch_frame_rates_does_not_query_types_without_a_dash_manifest(monkeypatch, content_type):
    """_fetch_frame_rates는 다시보기·클립이면 조회하지 않고 빈 결과를 돌려줘야 한다.

    content_type "m3u8" · "clip"
    -> {} (get_video_info를 부르지 않는다)
    """

    def unexpected(*args):
        raise AssertionError("조회하면 안 된다")

    monkeypatch.setattr(headless.NetworkManager, "get_video_info", unexpected)

    assert _fetch_frame_rates("https://chzzk.naver.com/video/123", {}, content_type) == {}


def test_list_option_logs_frame_rate_next_to_each_resolution(monkeypatch, tmp_path, caplog):
    """--list는 해상도마다 프레임률을 붙여 한 줄로 찍고 다운로드 없이 0으로 끝나야 한다.

    조회 결과 해상도 720(base u720) · 1080(base u1080), 프레임률 {u720: 60, u1080: 2997/100}
    -> 종료 코드 0, 로그에 "사용 가능한 해상도: 720p · 60fps, 1080p · 29.97fps"
    """
    result = ("url", {}, [[720, "u720"], [1080, "u1080"]], 1080, "u1080", str(tmp_path), None)
    monkeypatch.setattr(headless, "setup_logging", lambda level: None)
    monkeypatch.setattr(headless, "_load_cookies", lambda: {})
    monkeypatch.setattr(headless, "_fetch", lambda url, cookies, path: (result, "video"))
    monkeypatch.setattr(
        headless,
        "_fetch_frame_rates",
        lambda url, cookies, content_type: {"u720": Fraction(60), "u1080": Fraction(2997, 100)},
    )

    with caplog.at_level(logging.INFO, logger="headless"):
        code = headless.main(
            ["https://chzzk.naver.com/video/123", "--list", "--output", str(tmp_path)]
        )

    assert code == 0
    messages = [record.getMessage() for record in caplog.records if record.name == "headless"]
    assert "사용 가능한 해상도: 720p · 60fps, 1080p · 29.97fps" in messages
