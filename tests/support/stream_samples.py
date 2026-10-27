"""인코딩 전 다시보기의 합성 표본 — playback 정보와 마스터 플레이리스트 (#318).

실제 영상에서 본 구조만 옮겼다. 비트레이트 · 주소는 실제 값이 아니다.

playback 정보의 트랙과 마스터 플레이리스트의 변형을 따로 적는다 — 둘은 어긋날 수 있다
(원본 트랙의 값이 0으로 오거나, 다른 방송의 트랙이 오는 경우).
"""

import json
from dataclasses import dataclass

MASTER_URL = "https://example.invalid/live/master.m3u8"

# 트랙: (encodingTrackId, 가로, 세로, videoBitRate, audioBitRate, videoFrameRate, avoidReencoding)
# 변형: (가로, 세로, BANDWIDTH, FRAME-RATE)
_LANDSCAPE_LADDER = [
    ("720p", 1280, 720, 3000000, 192000, "60.0", False),
    ("480p", 852, 480, 1500000, 192000, "30.0", False),
    ("360p", 640, 360, 600000, 96000, "30.0", False),
    ("144p", 256, 144, 128000, 64000, "30.0", False),
]
_PORTRAIT_LADDER = [(n, h, w, v, a, f, o) for n, w, h, v, a, f, o in _LANDSCAPE_LADDER]


@dataclass(frozen=True)
class Sample:
    """합성 표본 하나."""

    tracks: list[tuple]
    variants: list[tuple]

    @property
    def playback(self) -> str:
        """playback 정보(JSON 문자열)."""
        return json.dumps(
            {
                "media": [
                    {
                        "path": MASTER_URL,
                        "encodingTrack": [
                            {
                                "encodingTrackId": name,
                                "videoWidth": width,
                                "videoHeight": height,
                                "videoBitRate": video,
                                "audioBitRate": audio,
                                "videoFrameRate": fps,
                                "avoidReencoding": original,
                            }
                            for name, width, height, video, audio, fps, original in self.tracks
                        ],
                    }
                ]
            }
        )

    @property
    def master(self) -> str:
        """마스터 플레이리스트 — n번째 변형의 주소는 ``v<n>/chunklist.m3u8``."""
        lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-INDEPENDENT-SEGMENTS"]
        for number, (width, height, bandwidth, fps) in enumerate(self.variants):
            lines.append(
                f"#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},"
                f'CODECS="avc1.640028,mp4a.40.2",RESOLUTION={width}x{height},'
                f"FRAME-RATE={fps:.2f}"
            )
            lines.append(f"v{number}/chunklist.m3u8")
        return "\n".join(lines) + "\n"


def variant_url(number: int) -> str:
    """n번째 변형을 고르면 나와야 하는 주소."""
    return f"https://example.invalid/live/v{number}/chunklist.m3u8"


def _variants(tracks: list[tuple]) -> list[tuple]:
    """트랙과 그대로 맞는 변형 — BANDWIDTH는 영상 + 오디오 비트레이트."""
    return [(w, h, v + a, float(f)) for _n, w, h, v, a, f, _o in tracks]


def _sample(tracks: list[tuple], variants: list[tuple] | None = None) -> Sample:
    return Sample(tracks, _variants(tracks) if variants is None else variants)


# S1 — 가로 방송, 원본 1920x1080 60fps
S1 = _sample(_LANDSCAPE_LADDER + [("1080p", 1920, 1080, 6000000, 192000, "60.0", True)])

# S2 — 가로 방송, 원본 1920x1080 30fps (720p 트랙은 60fps다)
S2 = _sample(_LANDSCAPE_LADDER + [("1080p", 1920, 1080, 2500000, 192000, "30.0", True)])

# S4 — 세로 방송, 원본 720x1280 30fps. 다시 인코딩한 720p(60fps)와 크기가 같다
S4 = _sample(_PORTRAIT_LADDER + [("1080p", 720, 1280, 2500000, 192000, "30.0", True)])

# S5 — 세로 방송, 원본 1080x1920 30fps
S5 = _sample(_PORTRAIT_LADDER + [("1080p", 1080, 1920, 6000000, 192000, "30.0", True)])

# S9 — 가로 방송. playback의 원본 트랙은 프레임률 · 영상 비트레이트가 0이고 마스터는 60fps다
S9 = _sample(
    _LANDSCAPE_LADDER + [("1080p", 1920, 1080, 0, 192000, "0.0", True)],
    _variants(_LANDSCAPE_LADDER) + [(1920, 1080, 8100000, 60.0)],
)

# S11 — playback은 다른 방송(세로)의 트랙이고, 마스터에는 가로 변형만 있다
S11 = _sample(
    S5.tracks,
    _variants(_LANDSCAPE_LADDER) + [(1920, 1080, 8300000, 60.0)],
)
