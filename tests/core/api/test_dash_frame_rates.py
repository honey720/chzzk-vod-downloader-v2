"""DASH 매니페스트의 프레임률 읽기(core/api/dash.py::parse_frame_rates) 단위 테스트 (#309).

핵심 계약:
- frameRate 속성을 정확한 비(Fraction)로 읽는다 — 분수 표기를 float로 바꾸지 않는다
- 결과의 키는 parse_dash_manifest · parse_sea_manifest가 base_url로 돌려주는 주소다
"""

from fractions import Fraction

from core.api.dash import parse_dash_manifest, parse_frame_rates, parse_sea_manifest

MPD_NS = 'xmlns="urn:mpeg:dash:schema:mpd:2011" xmlns:nvod="urn:naver:vod:2020"'


def _manifest(adaptation_attrs: str, representations: str) -> str:
    """AdaptationSet 하나짜리 최소 매니페스트."""
    return (
        f"<MPD {MPD_NS}><Period><AdaptationSet {adaptation_attrs}>"
        f"{representations}</AdaptationSet></Period></MPD>"
    )


def _representation(attrs: str, base_url: str) -> str:
    return f'<Representation width="1920" height="1080" {attrs}><BaseURL>{base_url}</BaseURL></Representation>'


def test_parse_frame_rates_matches_base_urls_of_plain_manifest(load_mock_response):
    """parse_frame_rates는 parse_dash_manifest가 돌려준 base_url마다 그 Representation의 프레임률을 돌려줘야 한다.

    픽스처 dash_manifest_audio_only_14158884.xml (144p frameRate="30", 720p · 1080p frameRate="60")
    -> {144: 30, 720: 60, 1080: 60}
    """
    xml = load_mock_response("dash_manifest_audio_only_14158884.xml")
    reps, _auto_resolution, _auto_url = parse_dash_manifest(xml)

    rates = parse_frame_rates(xml)

    assert {resolution: rates[url] for resolution, url in reps} == {
        144: Fraction(30),
        720: Fraction(60),
        1080: Fraction(60),
    }


def test_parse_frame_rates_matches_playlist_urls_of_sea_manifest(load_mock_response):
    """parse_frame_rates는 parse_sea_manifest가 돌려준 플레이리스트 주소마다 프레임률을 돌려줘야 한다.

    픽스처 dash_manifest_sea_13714380.xml (BaseURL 없이 nvod:m3u만 있는 Representation)
    -> {144: 30, 720: 60, 1080: 60}
    """
    xml = load_mock_response("dash_manifest_sea_13714380.xml")
    reps, _auto_resolution, _auto_url = parse_sea_manifest(xml)

    rates = parse_frame_rates(xml)

    assert {resolution: rates[url] for resolution, url in reps} == {
        144: Fraction(30),
        720: Fraction(60),
        1080: Fraction(60),
    }


def test_parse_frame_rates_keeps_fraction_notation_exact():
    """parse_frame_rates는 분수로 적힌 frameRate를 float로 바꾸지 않고 그 비 그대로 돌려줘야 한다.

    frameRate="30000/1001" · "60000/1001"
    -> Fraction(30000, 1001) · Fraction(60000, 1001)
    """
    xml = _manifest(
        'mimeType="video/mp4"',
        _representation('frameRate="30000/1001"', "https://example.invalid/a.mp4")
        + _representation('frameRate="60000/1001"', "https://example.invalid/b.mp4"),
    )

    rates = parse_frame_rates(xml)

    assert rates["https://example.invalid/a.mp4"] == Fraction(30000, 1001)
    assert rates["https://example.invalid/b.mp4"] == Fraction(60000, 1001)


def test_parse_frame_rates_inherits_from_adaptation_set():
    """parse_frame_rates는 Representation에 frameRate가 없으면 AdaptationSet의 값을 돌려줘야 한다.

    AdaptationSet frameRate="30", Representation에는 없음
    -> Fraction(30)
    """
    xml = _manifest('frameRate="30"', _representation("", "https://example.invalid/a.mp4"))

    assert parse_frame_rates(xml) == {"https://example.invalid/a.mp4": Fraction(30)}


def test_parse_frame_rates_skips_missing_or_unreadable_values():
    """parse_frame_rates는 frameRate가 없거나 수로 읽을 수 없거나 0 이하인 Representation을 결과에 넣지 않아야 한다.

    frameRate 없음 · "abc" · "0" · "30/0" · "60"
    -> "60"인 주소만
    """
    xml = _manifest(
        'mimeType="video/mp4"',
        _representation("", "https://example.invalid/none.mp4")
        + _representation('frameRate="abc"', "https://example.invalid/text.mp4")
        + _representation('frameRate="0"', "https://example.invalid/zero.mp4")
        + _representation('frameRate="30/0"', "https://example.invalid/div.mp4")
        + _representation('frameRate="60"', "https://example.invalid/ok.mp4"),
    )

    assert parse_frame_rates(xml) == {"https://example.invalid/ok.mp4": Fraction(60)}


def test_parse_frame_rates_returns_empty_for_manifest_without_frame_rate(load_mock_response):
    """parse_frame_rates는 frameRate 속성이 하나도 없는 매니페스트에서 빈 dict를 돌려줘야 한다.

    픽스처 dash_manifest.xml (frameRate 속성 없음)
    -> {}
    """
    assert parse_frame_rates(load_mock_response("dash_manifest.xml")) == {}
