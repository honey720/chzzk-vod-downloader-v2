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

"같은 해상도"의 해상도는 `track_resolution`의 값이다 (#318) — 매니페스트가 트랙에
붙인 이름의 숫자, 이름이 없으면 짧은 변. 크기가 같아도 이름이 다른 트랙은 합치지
않는다.
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
        name: 매니페스트의 트랙 이름 (m3u8: encodingTrackId, DASH: resolution 라벨)
    """
    if isinstance(name, str):
        matched = _TRACK_NAME.fullmatch(name.strip())
        if matched and int(matched.group(1)) > 0:
            return int(matched.group(1))
    return min(width, height)


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
