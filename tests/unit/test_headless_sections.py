"""헤드리스 스크립트의 구간 옵션과 해상도 목록(scripts/headless_download.py) 단위 테스트 (#309).

옵션 해석과 출력 문자열을 본다 — 조회·다운로드는 대역으로 바꾼다.
"""

import logging
import os
from fractions import Fraction
from types import SimpleNamespace

import pytest

import app.section_basis as section_basis
import scripts.headless_download as headless
from core.api.fmp4 import parse_init_segment, parse_media_segment
from core.api.hls import parse_media_playlist
from core.models.fmp4_index import Fmp4Head
from core.models.content import VideoInfo
from core.models.plan import TimeRange
from core.utils.paths import build_section_output_paths, release_output_paths
from core.utils.timecode import TIMECODE_FRAME_OUT_OF_RANGE, TIMECODE_INVALID_FORMAT, TimecodeError
from tests.unit.core.fmp4_builder import (
    KEY,
    NON_KEY,
    Fragment,
    InitTrack,
    Run,
    Sample,
    Traf,
    init_segment,
    media_segment,
)
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


@pytest.mark.parametrize("content_type", ["clip", "m3u8"])
def test_fetch_frame_rates_does_not_query_types_without_a_dash_manifest(monkeypatch, content_type):
    """_fetch_frame_rates는 클립과 인코딩 전 다시보기면 조회하지 않고 빈 결과를 돌려줘야 한다.

    content_type "clip" · "m3u8"
    -> {}, get_video_info 호출 0회
    """
    calls = []
    monkeypatch.setattr(headless.NetworkManager, "get_video_info", lambda *a: calls.append(a))

    assert _fetch_frame_rates("https://chzzk.naver.com/video/123", {}, content_type) == {}
    assert calls == []


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


# ================================================================ 엔진에 넘기기


def test_runner_hands_sections_paths_and_moov_to_the_engine(monkeypatch, tmp_path):
    """러너는 구간, 배정한 구간 파일 경로, 구간을 해석하며 받은 것을 제출하는 Content에 실어야 한다.

    구간 둘과 moov 대역 · fMP4 대역을 받은 러너, DownloadService · 로거 · 태스크는 대역
    -> 제출된 content의 selections == 구간 둘, selection_paths == "제목 1080p_1.mp4" · "_2.mp4",
       mp4_head is 넘긴 moov, fmp4_head is 넘긴 fMP4 대역
    """
    submitted = []

    class FakeService:
        def __init__(self, **kwargs):
            pass

        def submit(self, content, **kwargs):
            submitted.append(content)
            return SimpleNamespace(wait=lambda timeout=None: True)

    monkeypatch.setattr(headless, "DownloadService", FakeService)
    monkeypatch.setattr(headless, "DownloadLogger", lambda: SimpleNamespace())
    monkeypatch.setattr(
        headless, "DownloadTask", lambda data, item, log: SimpleNamespace(start=lambda: None)
    )
    item = SimpleNamespace(
        base_url="https://example.invalid/video.mp4",
        vod_url="https://chzzk.naver.com/video/123",
        output_path=str(tmp_path / "제목 1080p.mp4"),
        resolution=1080,
        content_type="video",
        title="제목",
        download_path=str(tmp_path),
    )
    selections = (TimeRange(1.0, 2.0), TimeRange(5.0, 6.0))
    moov = SimpleNamespace(index=None, data=b"")
    fmp4 = SimpleNamespace(playlist=None)

    runner = headless._HeadlessRunner(item, 60, selections, moov, fmp4)
    runner.run()
    release_output_paths(runner.section_paths)

    assert len(submitted) == 1
    assert submitted[0].selections == selections
    assert [os.path.basename(path) for path in submitted[0].selection_paths] == [
        "제목 1080p_1.mp4",
        "제목 1080p_2.mp4",
    ]
    assert submitted[0].mp4_head is moov
    assert submitted[0].fmp4_head is fmp4


# ================================================================ 구간 옵션을 받는 타입


def _main_with(monkeypatch, tmp_path, content_type: str, calls: list):
    """main을 대역으로 감싼다 — 조회 결과는 content_type이고, 해석·러너 호출을 calls에 남긴다.

    암호화 VOD의 해석("ts")은 받은 선언 프레임률과 함께 남기고, 러너가 받은 ts_head는
    ("ts_head", 값)으로 따로 남긴다(None이면 남기지 않는다).
    """
    result = (
        "https://chzzk.naver.com/video/123",
        {"title": "제목"},
        [[1080, "u1080"]],
        1080,
        "u1080",
    )
    result += (str(tmp_path), None)
    monkeypatch.setattr(headless, "setup_logging", lambda level: None)
    monkeypatch.setattr(headless, "_load_cookies", lambda: {})
    monkeypatch.setattr(headless, "_fetch", lambda url, cookies, path: (result, content_type))
    selections = (TimeRange(1.0, 2.0),)

    def mp4(item, texts):
        calls.append(("mp4", tuple(texts)))
        return selections, "moov"

    def fmp4(item, texts, segment_dir=None):
        calls.append(("fmp4", tuple(texts)))
        return selections, "fmp4"

    def ts(item, texts, segment_dir=None, declared=None):
        calls.append(("ts", tuple(texts), declared))
        return selections, "ts"

    class FakeRunner:
        def __init__(
            self,
            item,
            timeout,
            given=(),
            mp4_head=None,
            fmp4_head=None,
            section_paths=(),
            ts_head=None,
        ):
            calls.append(("run", given, mp4_head, fmp4_head))
            if ts_head is not None:
                calls.append(("ts_head", ts_head))
            self.section_paths = section_paths

        def run(self) -> int:
            release_output_paths(self.section_paths)  # 엔진이 끝날 때 푸는 예약 — 대역이 대신 푼다
            return 0

    monkeypatch.setattr(headless, "_resolve_sections", mp4)
    monkeypatch.setattr(headless, "_resolve_fmp4_sections", fmp4)
    monkeypatch.setattr(headless, "_resolve_ts_sections", ts)
    # 고른 해상도(u1080)와 다른 해상도의 선언값을 함께 준다 — 고른 것의 값이 넘어가야 한다
    monkeypatch.setattr(
        headless,
        "_fetch_frame_rates",
        lambda url, cookies, kind: {"u720": Fraction(30), "u1080": Fraction(60000, 1001)},
    )
    monkeypatch.setattr(headless, "_HeadlessRunner", FakeRunner)
    return headless.main(
        ["https://chzzk.naver.com/video/123", "--output", str(tmp_path)]
        + ["--section", "00:00:01:00-00:00:02:00"]
    ), selections


def test_section_option_on_unencoded_replay_resolves_fmp4_and_hands_it_to_the_runner(
    monkeypatch, tmp_path
):
    """인코딩 전 다시보기(m3u8)에 --section을 주면 fMP4로 구간을 해석하고 받은 것을 러너에 넘겨야 한다.

    조회 결과의 content_type "m3u8", --section 하나
    -> 종료 코드 0, fMP4 해석 1회(mp4 해석 0회), 러너에 (구간, mp4_head None, fmp4_head "fmp4")
    """
    calls: list = []

    code, selections = _main_with(monkeypatch, tmp_path, "m3u8", calls)

    assert code == 0
    assert calls == [
        ("fmp4", ("00:00:01:00-00:00:02:00",)),
        ("run", selections, None, "fmp4"),
    ]


def test_section_option_on_encoded_vod_resolves_mp4(monkeypatch, tmp_path):
    """인코딩이 끝난 VOD(video)에 --section을 주면 mp4로 구간을 해석하고 받은 moov를 러너에 넘겨야 한다.

    조회 결과의 content_type "video", --section 하나
    -> 종료 코드 0, mp4 해석 1회, 러너에 (구간, mp4_head "moov", fmp4_head None)
    """
    calls: list = []

    code, selections = _main_with(monkeypatch, tmp_path, "video", calls)

    assert code == 0
    assert calls == [("mp4", ("00:00:01:00-00:00:02:00",)), ("run", selections, "moov", None)]


def test_section_option_on_encrypted_vod_resolves_ts_with_the_declared_frame_rate(
    monkeypatch, tmp_path
):
    """암호화 VOD(hls_aes)에 --section을 주면 고른 해상도의 선언 프레임률로 TS 구간을 해석하고 받은 것을 러너에 넘겨야 한다.

    조회 결과의 content_type "hls_aes", 고른 해상도의 주소 "u1080",
    매니페스트의 프레임률 {u720: 30, u1080: 60000/1001}, --section 하나
    -> 종료 코드 0, TS 해석 1회(선언값 60000/1001), 러너에 (구간, mp4_head None, fmp4_head None)와 ts_head "ts"
    """
    calls: list = []

    code, selections = _main_with(monkeypatch, tmp_path, "hls_aes", calls)

    assert code == 0
    assert calls == [
        ("ts", ("00:00:01:00-00:00:02:00",), Fraction(60000, 1001)),
        ("run", selections, None, None),
        ("ts_head", "ts"),
    ]


def test_section_option_is_refused_for_clip(monkeypatch, tmp_path):
    """클립에 --section을 주면 해석도 다운로드도 하지 않고 2로 끝나야 한다.

    조회 결과의 content_type "clip", --section 하나
    -> 종료 코드 2, 해석·러너 호출 0건
    """
    calls: list = []

    code, _selections = _main_with(monkeypatch, tmp_path, "clip", calls)

    assert code == 2
    assert calls == []


# ================================================================ 인코딩 전 다시보기의 프레임률


def _fmp4_head(durations: list[int]):
    """영상 샘플 길이가 durations(ms)인 세그먼트 하나짜리 다시보기 — (Fmp4Head, 그 세그먼트)."""
    init_data = init_segment([InitTrack(1, b"vide", 1000, trex=(0, 10, NON_KEY))])
    init = parse_init_segment(init_data)
    frames = [
        Sample(duration=d, size=10, flags=KEY if n == 0 else NON_KEY)
        for n, d in enumerate(durations)
    ]
    segment = parse_media_segment(media_segment([Fragment([Traf(1, [Run(frames)])])]), init)
    playlist = parse_media_playlist(
        "\n".join(
            [
                "#EXTM3U",
                '#EXT-X-MAP:URI="init.mp4"',
                "#EXTINF:4.000000,",
                "seg-0.m4s",
                "#EXT-X-ENDLIST",
            ]
        )
    )
    return Fmp4Head(playlist=playlist, init_data=init_data, init=init, segments={0: segment})


@pytest.mark.parametrize(
    ("declared", "rate", "source"),
    [
        (Fraction(2997, 50), Fraction(2997, 50), "①"),
        (None, Fraction(60), "②"),
    ],
    ids=["declared", "measured-standard"],
)
def test_fmp4_sections_log_the_frame_rate_and_hand_it_to_the_engine(
    monkeypatch, caplog, declared, rate, source
):
    """인코딩 전 다시보기의 구간 해석은 정한 프레임률과 경로를 로그로 남기고, 받은 것에 실어 엔진으로 넘겨야 한다.

    프레임 간격 17 · 17 · 16ms인 4초 세그먼트 하나, 마스터 플레이리스트의 FRAME-RATE는 주석의 값.
    --section 00:00:01:00-00:00:01:59
    -> head.frame_rate == 기대값, "프레임률:" 로그에 그 값과 경로 번호, 구간의 끝 == 1 + 59 ÷ 프레임률
    """
    head = _fmp4_head([17, 17, 16] * 80)
    # 조회 순서는 app/section_basis.py에 있다 — 대역도 거기에 건다
    monkeypatch.setattr(
        section_basis,
        "resolve_m3u8_variant",
        lambda content: ("https://x.invalid/p.m3u8", declared),
    )
    monkeypatch.setattr(section_basis, "fetch_fmp4_head", lambda url, segment_dir=None: head)
    item = SimpleNamespace(vod_url="https://chzzk.naver.com/video/1", resolution=1080)

    with caplog.at_level(logging.INFO, logger="headless"):
        resolved = headless._resolve_fmp4_sections(item, ["00:00:01:00-00:00:01:59"])

    assert resolved is not None
    selections, handed = resolved
    assert handed is head
    assert head.frame_rate == rate
    assert selections[0].end == pytest.approx(float(1 + Fraction(59) / rate))
    messages = [r.getMessage() for r in caplog.records if r.name == "headless"]
    line = next(message for message in messages if message.startswith("프레임률:"))
    assert str(rate) in line
    assert source in line


def test_failed_fmp4_resolution_releases_names_and_removes_the_segment_folder(
    monkeypatch, tmp_path
):
    """인코딩 전 다시보기의 구간 해석이 실패하면 배정한 구간 파일명을 풀고, 세그먼트를 받아 둔 폴더를 지우고, 2로 끝나야 한다.

    구간 해석 대역이 넘겨받은 폴더에 파일 하나를 쓰고 None을 돌려줌(해석 실패)
    -> 종료 코드 2, 그 폴더가 없다, 같은 이름을 다시 배정받을 수 있다(`_1`)
    """
    result = ("https://chzzk.naver.com/video/123", {"title": "제목"}, [[1080, "u"]], 1080, "u")
    result += (str(tmp_path), None)
    monkeypatch.setattr(headless, "setup_logging", lambda level: None)
    monkeypatch.setattr(headless, "_load_cookies", lambda: {})
    monkeypatch.setattr(headless, "_fetch", lambda url, cookies, path: (result, "m3u8"))
    folders = []

    def failing(item, texts, segment_dir=None):
        os.makedirs(segment_dir)
        with open(os.path.join(segment_dir, "1.m4v"), "wb") as f:
            f.write(b"segment")
        folders.append(segment_dir)
        return None

    monkeypatch.setattr(headless, "_resolve_fmp4_sections", failing)

    code = headless.main(
        ["https://chzzk.naver.com/video/123", "--output", str(tmp_path)]
        + ["--section", "00:00:01:00-00:00:02:00"]
    )

    assert code == 2
    assert len(folders) == 1 and not os.path.exists(folders[0])
    again = build_section_output_paths(str(tmp_path), "제목", 1080, 1)
    release_output_paths(again)
    assert os.path.basename(again[0]) == "제목 1080p_1.mp4"
