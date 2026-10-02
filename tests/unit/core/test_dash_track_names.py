"""DASH · 암호화 VOD 매니페스트의 해상도 — Representation에 붙은 이름을 따른다 (#318).

실제 매니페스트의 Representation은 ``<nvod:Label kind="resolution">``으로 해상도 이름을
갖는다(test_dash.py의 실물 박제 픽스처). 여기서는 그 구조만 옮긴 합성 XML로, 크기가 같은
두 트랙이 이름으로 갈리는 것을 잰다. 이름이 없는 매니페스트의 결과는 test_dash.py가 본다.
"""

from core.api.dash import parse_dash_manifest, parse_sea_manifest

_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" xmlns:nvod="urn:naver:vod:2020">\n'
    '  <Period><AdaptationSet mimeType="video/mp4">\n'
)
_TAIL = "  </AdaptationSet></Period>\n</MPD>"


def _dash_rep(label: str | None, width: int, height: int, bandwidth: int, url: str) -> str:
    name = f'<nvod:Label kind="resolution">{label}</nvod:Label>' if label is not None else ""
    return (
        f'    <Representation id="{url}" width="{width}" height="{height}" bandwidth="{bandwidth}">'
        f'<nvod:Label kind="fps">30</nvod:Label>{name}<BaseURL>{url}</BaseURL></Representation>\n'
    )


def _sea_rep(label: str | None, width: int, height: int, bandwidth: int, url: str) -> str:
    name = f'<nvod:Label kind="resolution">{label}</nvod:Label>' if label is not None else ""
    return (
        f'    <Representation id="{url}" width="{width}" height="{height}" bandwidth="{bandwidth}"'
        f' nvod:m3u="{url}">{name}'
        '<ContentProtection schemeIdUri="urn:mpeg:dash:sea:2012"/></Representation>\n'
    )


def test_dash_lists_same_size_tracks_separately_by_their_resolution_label():
    """DASH에서 크기가 같은 두 Representation의 해상도 이름이 다르면 둘 다 목록에 나와야 한다.

    720x1280 둘 — 이름 "720"(bandwidth 3000000) · "1080"(bandwidth 2500000)
    -> [[720, 첫째 주소], [1080, 둘째 주소]], 자동 (1080, 둘째 주소)
    """
    xml = (
        _HEAD
        + _dash_rep("720", 720, 1280, 3000000, "https://v.invalid/a.mp4")
        + _dash_rep("1080", 720, 1280, 2500000, "https://v.invalid/b.mp4")
        + _TAIL
    )

    reps, auto_resolution, auto_base_url = parse_dash_manifest(xml)

    assert reps == [[720, "https://v.invalid/a.mp4"], [1080, "https://v.invalid/b.mp4"]]
    assert (auto_resolution, auto_base_url) == (1080, "https://v.invalid/b.mp4")


def test_dash_uses_the_short_side_when_the_label_is_missing_or_not_a_number():
    """DASH에서 해상도 이름이 없거나 숫자가 아니면 짧은 변이 해상도여야 한다.

    720x1280(이름 없음) · 480x852(이름 "auto")
    -> [[480, 둘째 주소], [720, 첫째 주소]]
    """
    xml = (
        _HEAD
        + _dash_rep(None, 720, 1280, 3000000, "https://v.invalid/a.mp4")
        + _dash_rep("auto", 480, 852, 1500000, "https://v.invalid/b.mp4")
        + _TAIL
    )

    reps, _auto_resolution, _auto_base_url = parse_dash_manifest(xml)

    assert reps == [[480, "https://v.invalid/b.mp4"], [720, "https://v.invalid/a.mp4"]]


def test_dash_still_merges_tracks_with_the_same_resolution_label():
    """DASH에서 해상도 이름이 같은 Representation은 bandwidth가 높은 것 하나만 남아야 한다.

    720x1280 둘 — 둘 다 이름 "720", bandwidth 2500000 · 3000000
    -> [[720, 둘째 주소]]
    """
    xml = (
        _HEAD
        + _dash_rep("720", 720, 1280, 2500000, "https://v.invalid/a.mp4")
        + _dash_rep("720", 720, 1280, 3000000, "https://v.invalid/b.mp4")
        + _TAIL
    )

    reps, _auto_resolution, _auto_base_url = parse_dash_manifest(xml)

    assert reps == [[720, "https://v.invalid/b.mp4"]]


def test_sea_lists_same_size_tracks_separately_by_their_resolution_label():
    """암호화 VOD에서 크기가 같은 두 Representation의 해상도 이름이 다르면 둘 다 목록에 나와야 한다.

    720x1280 둘 — 이름 "720"(bandwidth 3000000) · "1080"(bandwidth 2500000)
    -> [[720, 첫째 주소], [1080, 둘째 주소]], 자동 (1080, 둘째 주소)
    """
    xml = (
        _HEAD
        + _sea_rep("720", 720, 1280, 3000000, "https://v.invalid/a.m3u8")
        + _sea_rep("1080", 720, 1280, 2500000, "https://v.invalid/b.m3u8")
        + _TAIL
    )

    reps, auto_resolution, auto_base_url = parse_sea_manifest(xml)

    assert reps == [[720, "https://v.invalid/a.m3u8"], [1080, "https://v.invalid/b.m3u8"]]
    assert (auto_resolution, auto_base_url) == (1080, "https://v.invalid/b.m3u8")
