"""Representation 목록의 공통 후처리 — 같은 해상도 트랙 합치기 (#244 3행 정리).

매니페스트(DASH·SEA·m3u8 JSON·클립)는 같은 높이의 비디오 트랙을 여러 개
가질 수 있다(비트레이트·코덱·프레임레이트만 다른 변형). 카드의 해상도
pill은 높이만 보여주므로, 그대로 두면 "1080p" pill이 둘 생겨 무엇이 다른지
유저가 알 수 없고 pill 개수도 예측할 수 없게 된다 — 3행 폭 설계의 전제가
흔들린다. 그래서 파서 네 곳이 전부 이 한 함수를 거쳐 **높이당 하나**만
남긴다(`unique_reps`라는 이름이 약속하던 동작).

어느 것을 남기나: **비트레이트가 높은 쪽**, 같거나 알 수 없으면 **매니페스트에
먼저 나온 쪽**.
- 높은 비트레이트: pill은 높이만 보여주므로 유저는 "그 높이에서 가장 좋은
  화질"을 기대한다. 기본 선택이 최고 해상도인 것과 같은 방향이다.
- 먼저 나온 쪽(동률·미상): m3u8 경로는 트랙별 URL이 없고 다운로드 시점에
  같은 규칙으로 트랙을 다시 골라 마스터 플레이리스트에서 그 트랙의 변형을 찾는다
  (app/network.py::get_video_m3u8_base_url, core/api/playback_tracks.py). 목록에
  남긴 트랙과 받는 트랙이 같은 규칙으로 정해지므로, pill이 가리키는 것과 받는
  것이 어긋나지 않는다. 비트레이트를 모르는 클립 경로도 같은 규칙으로 결정적이다.

인코딩 전 다시보기의 목록은 여기서 합치지 않는다 (#318). 마스터 플레이리스트의 변형마다
한 항목이고(`StreamEntry`), 짧은 변이 같은 변형도 둘 다 남는다 —
core/api/playback_tracks.py의 `list_streams`. 마스터 플레이리스트를 받지 못했을 때만
playback 정보의 트랙을 이 함수로 합치며, 그때의 해상도는 `track_resolution`의 값이다.
DASH · 암호화 VOD는 화질 이름(qualityId)이 같은 것만 합친다(core/api/dash.py).
"""

import re
from collections.abc import Iterable
from typing import Any

# 트랙 이름의 해상도 표기 — "1080p" · "1080P" · "1080"
_TRACK_NAME = re.compile(r"(\d+)[pP]?")


def track_resolution(name: object, width: int, height: int) -> int:
    """트랙의 해상도(목록·파일명에 쓰는 정수)를 정한다 (#318).

    매니페스트가 트랙에 붙인 이름이 ``<숫자>p``면 그 숫자를 쓴다. 이름이 없거나 그런
    형식이 아니면 짧은 변이다.

    짧은 변만 쓰면 크기가 같은 두 트랙이 한 값이 되어 하나가 목록에서 사라진다 —
    세로 방송의 720x1280 트랙 둘(다시 인코딩한 "720p"와 원본 "1080p")이 그렇다.
    가로 방송은 이름의 숫자와 짧은 변이 같아 값이 달라지지 않는다.

    Args:
        name: 매니페스트의 트랙 이름 (인코딩 전 다시보기의 encodingTrackId)
    """
    if isinstance(name, str):
        matched = _TRACK_NAME.fullmatch(name.strip())
        if matched and int(matched.group(1)) > 0:
            return int(matched.group(1))
    return min(width, height)


# 파일명에 붙이는 원본 표시 — 짧은 변이 같은 항목이 둘일 때 원본 쪽에만 붙인다
ORIGINAL_FILE_TAG = "(원본)"

# 이 값 이상의 프레임률만 표시한다 (fps). 30fps 이하는 표시하지 않는다
SHOWN_FRAME_RATE_FROM = 50


class StreamEntry(list):
    """해상도 목록의 한 항목 — ``[해상도, base_url]`` 그대로이고 그 스트림의 정체를 함께 든다 (#318).

    목록의 형식(리스트의 리스트)을 쓰는 쪽이 그대로 쓰도록 list를 잇는다. 해상도는 짧은
    변이다. 짧은 변이 같은 항목이 둘일 수 있다(세로 방송의 720x1280 두 변형) — 그 둘을
    가르는 값이 아래 속성이다.
    """

    def __init__(
        self,
        resolution: int,
        base_url: str | None,
        *,
        frame_rate: float | None = None,
        original: bool = False,
        stream: Any = None,
    ):
        super().__init__([resolution, base_url])
        self.frame_rate = frame_rate  # 선언된 프레임률(fps). 모르면 None
        self.original = original  # 다시 인코딩하지 않은 원본 스트림인지
        self.stream = stream  # 다운로드 때 같은 변형을 다시 찾는 값(StreamKey). 없으면 None


def shown_frame_rate(entry: list) -> int | None:
    """목록 항목에 표시할 프레임률 — 50fps 이상이면 반올림한 정수, 아니면 None."""
    frame_rate = getattr(entry, "frame_rate", None)
    if frame_rate is None or frame_rate < SHOWN_FRAME_RATE_FROM:
        return None
    return round(frame_rate)


def is_original(entry: list) -> bool:
    """목록 항목이 원본 스트림인지."""
    return bool(getattr(entry, "original", False))


def file_tag(entries: Iterable[list], entry: list) -> str:
    """파일명의 ``{해상도}p`` 뒤에 붙일 표시 — 짧은 변이 같은 항목이 또 있고 이 항목이 원본이면 ``(원본)``.

    그 밖에는 빈 문자열이다. 해상도마다 항목이 하나인 영상의 파일명은 달라지지 않는다.
    """
    if not is_original(entry):
        return ""
    same = sum(1 for other in entries if other[0] == entry[0])
    return ORIGINAL_FILE_TAG if same > 1 else ""


def dedupe_by_resolution(tracks: Iterable[tuple[int, Any, int]]) -> list[list]:
    """(해상도, base_url, 비트레이트) 트랙들을 해상도당 하나로 합쳐 오름차순 목록으로 돌려준다.

    Args:
        tracks: 매니페스트 등장 순서대로의 (해상도, base_url, 비트레이트). 비트레이트를
            모르면 0.

    Returns:
        list[list]: 해상도 오름차순의 `[해상도, base_url]` 목록 — 파서들의 기존 반환
            형식과 같다. 같은 해상도는 비트레이트가 높은 것, 동률이면 먼저 온 것만 남는다.
    """
    kept: dict[int, tuple[Any, int]] = {}
    for resolution, base_url, bandwidth in tracks:
        current = kept.get(resolution)
        # 엄격한 초과만 교체한다 — 동률이면 먼저 온 쪽이 남는다
        if current is None or bandwidth > current[1]:
            kept[resolution] = (base_url, bandwidth)
    return [[resolution, kept[resolution][0]] for resolution in sorted(kept)]
