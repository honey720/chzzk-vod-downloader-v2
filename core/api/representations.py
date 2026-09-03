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
  마스터 플레이리스트에서 `RESOLUTION=WxH`가 **처음** 맞는 변형을 고른다
  (content/network.py::get_video_m3u8_base_url). 그 경로에서 실제로 받는 것이
  "먼저 나온 트랙"이므로, pill이 가리키는 것과 받는 것이 어긋나지 않는다.
  비트레이트를 모르는 클립 경로도 같은 규칙으로 결정적이다.
"""

from collections.abc import Iterable
from typing import Any


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
