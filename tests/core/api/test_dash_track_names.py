"""DASH · 암호화 VOD 매니페스트의 목록 — 같은 화질은 하나로, 프레임률을 함께 (#318).

실제 매니페스트의 Representation은 ``<nvod:Label kind="qualityId">``로 화질 이름을 갖고,
한 화질이 전송 형식만 달라 두 번 나온다(test_dash.py의 실물 박제 픽스처). 여기서는 그
구조만 옮긴 합성 XML로 잰다. qualityId가 없는 매니페스트의 결과는 test_dash.py가 본다.
"""

import pytest

from core.api.dash import parse_dash_manifest, parse_sea_manifest
from core.api.representations import is_original, shown_frame_rate

_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" xmlns:nvod="urn:naver:vod:2020">\n'
)
_TAIL = "</MPD>"


def _rep(
    quality: str | None,
    width: int,
    height: int,
    bandwidth: int,
    url: str,
    frame_rate: str | None = None,
    *,
    encrypted: bool = False,
) -> str:
    """Representation 하나 — 화질 이름 · 크기 · bandwidth · 주소 · frameRate 속성."""
    label = f'<nvod:Label kind="qualityId">{quality}</nvod:Label>' if quality else ""
    rate = f' frameRate="{frame_rate}"' if frame_rate else ""
    if encrypted:
        return (
            f'<Representation id="{url}" width="{width}" height="{height}" bandwidth="{bandwidth}"'
            f'{rate} nvod:m3u="{url}">{label}'
            '<ContentProtection schemeIdUri="urn:mpeg:dash:sea:2012"/></Representation>\n'
        )
    return (
        f'<Representation id="{url}" width="{width}" height="{height}" bandwidth="{bandwidth}"'
        f"{rate}>{label}<BaseURL>{url}</BaseURL></Representation>\n"
    )


def _mpd(*reps: str, adaptation_frame_rate: str | None = None) -> str:
    rate = f' frameRate="{adaptation_frame_rate}"' if adaptation_frame_rate else ""
    return (
        f'{_HEAD}<Period><AdaptationSet mimeType="video/mp4"{rate}>\n'
        f"{''.join(reps)}</AdaptationSet></Period>{_TAIL}"
    )


def _listed(reps: list) -> list[tuple]:
    return [(rep[0], rep[1], shown_frame_rate(rep), is_original(rep)) for rep in reps]


def test_dash_lists_each_quality_once_with_its_frame_rate():
    """한 화질이 전송 형식만 달라 두 번 나오면 하나만 목록에 있고, 프레임률은 frameRate 속성이어야 한다.

    인코딩 완료 VOD의 구조 — 1080(60fps) · 144(30fps) · 720(60fps)이 주소만 다르게 두 번씩
    -> [(144, 첫째 주소, None, False), (720, 첫째 주소, 60, False), (1080, 첫째 주소, 60, False)]
    """
    xml = _mpd(
        _rep("1080P_1920_8000_192", 1920, 1080, 8000000, "https://v.invalid/1080.mp4", "60"),
        _rep("144P_256_128_64", 256, 144, 160000, "https://v.invalid/144.mp4", "30"),
        _rep("720P_1280_4000_192", 1280, 720, 3200000, "https://v.invalid/720.mp4", "60"),
        _rep("1080P_1920_8000_192", 1920, 1080, 8000000, "https://v.invalid/1080/x", "60"),
        _rep("144P_256_128_64", 256, 144, 160000, "https://v.invalid/144/x", "30"),
        _rep("720P_1280_4000_192", 1280, 720, 3200000, "https://v.invalid/720/x", "60"),
    )

    reps, auto_resolution, auto_base_url = parse_dash_manifest(xml)

    assert _listed(reps) == [
        (144, "https://v.invalid/144.mp4", None, False),
        (720, "https://v.invalid/720.mp4", 60, False),
        (1080, "https://v.invalid/1080.mp4", 60, False),
    ]
    assert (auto_resolution, auto_base_url) == (1080, "https://v.invalid/1080.mp4")


def test_dash_keeps_two_qualities_of_the_same_short_side():
    """짧은 변이 같아도 화질 이름이 다른 Representation은 둘 다 목록에 남아야 한다.

    720x1280 둘 — "720P_A"(60fps, bandwidth 3200000) · "720P_B"(30fps, bandwidth 2700000)
    -> 720이 둘 — bandwidth 오름차순(30fps 먼저)
    """
    xml = _mpd(
        _rep("720P_A", 720, 1280, 3200000, "https://v.invalid/a.mp4", "60"),
        _rep("720P_B", 720, 1280, 2700000, "https://v.invalid/b.mp4", "30"),
    )

    reps, _auto_resolution, _auto_base_url = parse_dash_manifest(xml)

    assert _listed(reps) == [
        (720, "https://v.invalid/b.mp4", None, False),
        (720, "https://v.invalid/a.mp4", 60, False),
    ]


@pytest.mark.parametrize(
    ("own", "adaptation", "shown"),
    [
        ("60", None, 60),
        ("60000/1001", None, 60),  # 59.94
        ("30000/1001", None, None),  # 29.97
        (None, "60", 60),  # 자기 속성이 없으면 감싼 AdaptationSet의 값
        ("30", "60", None),  # 자기 속성이 먼저
        (None, None, None),
        ("abc", None, None),  # 읽을 수 없는 값
    ],
)
def test_dash_frame_rate_comes_from_the_representation_then_the_adaptation_set(
    own, adaptation, shown
):
    """프레임률은 Representation의 frameRate, 없으면 감싼 AdaptationSet의 frameRate여야 한다.

    주석의 경우마다 (Representation의 값, AdaptationSet의 값)
    -> 표시할 fps
    """
    xml = _mpd(
        _rep("720P", 1280, 720, 3200000, "https://v.invalid/a.mp4", own),
        adaptation_frame_rate=adaptation,
    )

    reps, _auto_resolution, _auto_base_url = parse_dash_manifest(xml)

    assert shown_frame_rate(reps[0]) == shown


def test_sea_lists_each_quality_once_with_its_frame_rate():
    """암호화 VOD도 같은 화질은 하나만 목록에 있고, 프레임률이 함께 실려야 한다.

    1080(60fps) · 720(60fps) · 144(30fps), 1080이 bandwidth만 다르게 한 번 더(높은 쪽이 뒤)
    -> [(144, 주소, None, False), (720, 주소, 60, False), (1080, bandwidth가 높은 쪽 주소, 60, False)]
    """
    xml = _mpd(
        _rep("1080P", 1920, 1080, 8000000, "https://v.invalid/1080a.m3u8", "60", encrypted=True),
        _rep("720P", 1280, 720, 3200000, "https://v.invalid/720.m3u8", "60", encrypted=True),
        _rep("144P", 256, 144, 160000, "https://v.invalid/144.m3u8", "30", encrypted=True),
        _rep("1080P", 1920, 1080, 8100000, "https://v.invalid/1080b.m3u8", "60", encrypted=True),
    )

    reps, auto_resolution, auto_base_url = parse_sea_manifest(xml)

    assert _listed(reps) == [
        (144, "https://v.invalid/144.m3u8", None, False),
        (720, "https://v.invalid/720.m3u8", 60, False),
        (1080, "https://v.invalid/1080b.m3u8", 60, False),
    ]
    assert (auto_resolution, auto_base_url) == (1080, "https://v.invalid/1080b.m3u8")
