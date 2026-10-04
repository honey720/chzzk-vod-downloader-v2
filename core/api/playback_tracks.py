"""인코딩 전 다시보기의 트랙 — playback 정보의 트랙을 읽고 마스터 플레이리스트의 변형과 짝짓는다 (#318).

playback 정보(JSON)의 ``encodingTrack``이 트랙 목록이고, 받을 주소는 마스터
플레이리스트의 ``#EXT-X-STREAM-INF``에 있다. 둘을 잇는 값은 트랙의 정체(크기 ·
프레임률 · 비트레이트)다.

해상도 숫자 하나로는 이을 수 없다. 세로 방송은 ``RESOLUTION=가로x세로``의 세로값이
목록의 해상도와 다르고, 크기가 같은 트랙이 둘(다시 인코딩한 트랙과 원본 트랙)일 수 있다.

HTTP는 하지 않는다 — 텍스트를 받아 고르기만 한다. 요청·쿠키는 호출부의 몫이다.
"""

import json
import re
from dataclasses import dataclass

from core.api.hls import _parse_attributes
from core.api.representations import StreamEntry, track_resolution
from core.models.content import StreamKey

# 실패 키 — 고른 해상도의 스트림을 마스터 플레이리스트에서 하나로 정하지 못했다
STREAM_NOT_FOUND = "Stream for the selected resolution not found"

_FRAME_RATE_TOLERANCE = 0.01  # fps. "60.0"과 "60.00"처럼 표기만 다른 값을 같게 본다


class StreamSelectionError(ValueError):
    """고른 해상도의 스트림을 정하지 못했을 때 — 맞는 변형이 없거나 둘 이상이다.

    ``message_key``는 번역하지 않은 i18n 키 원문이다. 표시 계층이 번역한다.
    전에 같은 자리에서 내던 ValueError를 잡는 쪽이 그대로 잡도록 ValueError를 잇는다.
    """

    def __init__(self, detail: str):
        super().__init__(detail)
        self.message_key = STREAM_NOT_FOUND


@dataclass(frozen=True)
class PlaybackTrack:
    """playback 정보의 트랙 하나."""

    name: str | None  # encodingTrackId (예: "1080p"). 없으면 None
    resolution: int  # 목록·파일명에 쓰는 해상도 — track_resolution의 값
    width: int  # 가로 픽셀
    height: int  # 세로 픽셀
    frame_rate: float | None  # fps. 없거나 읽을 수 없으면 None
    video_bitrate: int  # bps. 모르면 0
    audio_bitrate: int  # bps. 모르면 0
    original: bool = False  # 다시 인코딩하지 않은 원본 트랙인지 (avoidReencoding)


def _int_or_zero(value) -> int:
    """정수로 읽는다. 없거나 숫자가 아니면 0."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float_or_none(value) -> float | None:
    """양수 실수로 읽는다. 없거나 숫자가 아니거나 0 이하면 None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def playback_tracks(json_str: str) -> list[PlaybackTrack]:
    """playback 정보의 첫 media에서 트랙을 등장 순서대로 읽는다.

    Raises:
        TypeError, ValueError: 트랙의 가로·세로가 없거나 숫자가 아닌 경우(전과 같다)
    """
    media = json.loads(json_str).get("media", [])
    tracks = []
    for encoding in media[0].get("encodingTrack", []):
        width = int(encoding.get("videoWidth"))
        height = int(encoding.get("videoHeight"))
        name = encoding.get("encodingTrackId")
        tracks.append(
            PlaybackTrack(
                name=name if isinstance(name, str) else None,
                resolution=track_resolution(name, width, height),
                width=width,
                height=height,
                frame_rate=_float_or_none(encoding.get("videoFrameRate")),
                video_bitrate=_int_or_zero(encoding.get("videoBitRate")),
                audio_bitrate=_int_or_zero(encoding.get("audioBitRate")),
                original=encoding.get("avoidReencoding") is True,
            )
        )
    return tracks


def track_for_resolution(tracks: list[PlaybackTrack], resolution: int) -> PlaybackTrack | None:
    """목록의 그 해상도가 가리키는 트랙을 고른다. 그런 트랙이 없으면 None.

    해상도가 같은 트랙이 여럿이면 목록을 만들 때와 같은 규칙으로 고른다
    (``dedupe_by_resolution`` — 영상 비트레이트가 높은 쪽, 같으면 먼저 나온 쪽).
    """
    picked = None
    for track in tracks:
        if track.resolution != resolution:
            continue
        if picked is None or track.video_bitrate > picked.video_bitrate:
            picked = track
    return picked


@dataclass(frozen=True)
class _Variant:
    """마스터 플레이리스트의 ``#EXT-X-STREAM-INF`` 하나."""

    uri: str  # 태그 다음 줄 — 그 변형의 미디어 플레이리스트 주소(상대일 수 있다)
    size: str  # RESOLUTION 값 그대로("가로x세로"). 없으면 빈 문자열
    frame_rate: float | None  # FRAME-RATE. 없으면 None
    bandwidth: int | None  # BANDWIDTH(bps). 없으면 None


def _variants(master_text: str) -> list[_Variant]:
    lines = master_text.splitlines()
    variants = []
    for number, line in enumerate(lines[:-1]):
        line = line.strip()
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        attributes = _parse_attributes(line.split(":", 1)[1])
        bandwidth = attributes.get("BANDWIDTH", "")
        variants.append(
            _Variant(
                uri=lines[number + 1].strip(),
                size=attributes.get("RESOLUTION", "").strip(),
                frame_rate=_float_or_none(attributes.get("FRAME-RATE")),
                bandwidth=int(bandwidth) if bandwidth.isdigit() else None,
            )
        )
    return variants


def select_variant(master_text: str, track: PlaybackTrack) -> str:
    """마스터 플레이리스트에서 그 트랙의 변형을 골라 주소(태그 다음 줄)를 돌려준다.

    크기(가로x세로)가 같은 변형을 찾는다. 둘 이상이면 프레임률, 그래도 둘 이상이면
    BANDWIDTH(영상 + 오디오 비트레이트)로 좁힌다. 좁히는 값은 양쪽이 모두 선언했을
    때만 견준다. 끝에 하나가 남아야 한다 — 아무거나 고르지 않는다.

    Raises:
        StreamSelectionError: 맞는 변형이 없거나 하나로 정해지지 않는 경우
    """
    wanted = f"{track.width}x{track.height}"
    candidates = [variant for variant in _variants(master_text) if variant.size == wanted]
    if not candidates:
        raise StreamSelectionError(
            f"{track.resolution} 해상도 스트림을 찾을 수 없습니다. (크기 {wanted}인 변형 없음)"
        )

    candidates = _narrow(candidates, track.frame_rate, track.video_bitrate + track.audio_bitrate)

    if len(candidates) != 1:
        raise StreamSelectionError(
            f"{track.resolution} 해상도 스트림을 하나로 정할 수 없습니다. "
            f"(크기 {wanted}, 남은 변형 {len(candidates)}개)"
        )
    return candidates[0].uri


_LEGACY_SIZE = r"RESOLUTION=\d+x{resolution}"  # 트랙 정보가 없을 때의 규칙 — 세로값으로 찾는다


def select_variant_by_height(master_text: str, resolution: int) -> str:
    """세로값이 그 해상도인 첫 변형의 주소를 돌려준다 — 트랙 정보가 없을 때만 쓴다.

    playback 정보에 그 해상도의 트랙이 없으면 정체를 알 수 없다. 그때는 전과 같이
    ``RESOLUTION=가로x세로``의 세로값으로 찾는다.

    Raises:
        StreamSelectionError: 맞는 줄이 없는 경우
    """
    pattern = re.compile(_LEGACY_SIZE.format(resolution=resolution))
    lines = master_text.splitlines()
    for number, line in enumerate(lines):
        if pattern.search(line):
            return lines[number + 1].strip()
    raise StreamSelectionError(f"{resolution} 해상도 스트림을 찾을 수 없습니다.")


# ================================================================ 마스터 플레이리스트 기준의 목록


def _size(variant: _Variant) -> tuple[int, int] | None:
    """RESOLUTION 값을 (가로, 세로)로. 없거나 형식이 다르면 None."""
    matched = re.fullmatch(r"(\d+)x(\d+)", variant.size)
    return (int(matched.group(1)), int(matched.group(2))) if matched else None


def _narrow(
    candidates: list[_Variant], frame_rate: float | None, bandwidth: int | None
) -> list[_Variant]:
    """크기가 같은 변형이 둘 이상일 때 프레임률, 그다음 BANDWIDTH로 좁힌다.

    좁히는 값은 양쪽이 모두 선언했을 때만 견준다.
    """
    if (
        len(candidates) > 1
        and frame_rate is not None
        and all(variant.frame_rate is not None for variant in candidates)
    ):
        candidates = [
            variant
            for variant in candidates
            if abs(variant.frame_rate - frame_rate) < _FRAME_RATE_TOLERANCE
        ]
    if (
        len(candidates) > 1
        and bandwidth
        and all(variant.bandwidth is not None for variant in candidates)
    ):
        candidates = [variant for variant in candidates if variant.bandwidth == bandwidth]
    return candidates


def _original_variant(variants: list[_Variant], tracks: list[PlaybackTrack]) -> _Variant | None:
    """playback 정보의 원본 트랙과 짝인 변형. 하나로 정해지지 않으면 None.

    playback 정보는 영상과 어긋날 수 있다 — 원본 트랙의 프레임률·비트레이트가 0으로 오거나,
    트랙의 크기가 마스터 플레이리스트에 없는 경우가 있다. 크기가 같은 변형이 하나면 그것이고,
    둘 이상이면 선언된 프레임률, 그다음 BANDWIDTH로 좁힌다. 하나가 남지 않으면 원본을
    표시하지 않는다(틀린 변형에 원본을 붙이지 않는다).
    """
    originals = [track for track in tracks if track.original]
    if len(originals) != 1:
        return None
    track = originals[0]
    wanted = f"{track.width}x{track.height}"
    total = track.video_bitrate + track.audio_bitrate if track.video_bitrate else 0
    candidates = _narrow(
        [variant for variant in variants if variant.size == wanted], track.frame_rate, total
    )
    return candidates[0] if len(candidates) == 1 else None


def list_streams(master_text: str, tracks: list[PlaybackTrack]) -> list[StreamEntry]:
    """마스터 플레이리스트의 변형으로 해상도 목록을 만든다 (#318).

    목록의 원천은 마스터 플레이리스트다 — 실제로 받는 것이 그 변형이기 때문이다. playback
    정보의 트랙은 원본 여부를 아는 데만 쓴다. 해상도는 짧은 변이고, 짧은 변이 같은 변형은
    합치지 않는다(둘 다 남는다).

    Returns:
        해상도 오름차순의 목록. 짧은 변이 같으면 원본이 뒤다 — 마지막 항목이 기본 선택
        (짧은 변이 가장 큰 것, 같으면 원본)이다. RESOLUTION이 없는 변형은 들어 있지 않다.
    """
    variants = [variant for variant in _variants(master_text) if _size(variant) is not None]
    original = _original_variant(variants, tracks)
    entries = []
    for variant in variants:
        width, height = _size(variant)
        entries.append(
            StreamEntry(
                min(width, height),
                None,
                frame_rate=variant.frame_rate,
                original=variant is original,
                stream=StreamKey(width, height, variant.frame_rate, variant.bandwidth),
            )
        )
    entries.sort(key=lambda entry: (entry[0], entry.original, entry.stream.bandwidth or 0))
    return entries


def select_stream(master_text: str, key: StreamKey) -> str:
    """목록에서 고른 변형을 마스터 플레이리스트에서 다시 찾아 주소(태그 다음 줄)를 돌려준다.

    크기(가로x세로)가 같은 변형을 찾고, 둘 이상이면 프레임률, 그다음 BANDWIDTH로 좁힌다.

    Raises:
        StreamSelectionError: 맞는 변형이 없거나 하나로 정해지지 않는 경우
    """
    wanted = f"{key.width}x{key.height}"
    resolution = min(key.width, key.height)
    candidates = [variant for variant in _variants(master_text) if variant.size == wanted]
    if not candidates:
        raise StreamSelectionError(
            f"{resolution} 해상도 스트림을 찾을 수 없습니다. (크기 {wanted}인 변형 없음)"
        )
    candidates = _narrow(candidates, key.frame_rate, key.bandwidth)
    if len(candidates) != 1:
        raise StreamSelectionError(
            f"{resolution} 해상도 스트림을 하나로 정할 수 없습니다. "
            f"(크기 {wanted}, 남은 변형 {len(candidates)}개)"
        )
    return candidates[0].uri
