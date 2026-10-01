"""받은 범위만 이어 붙인 부분 mp4 (#309).

구간 다운로드는 mp4의 머리(moov가 든 앞부분)와 구간에 필요한 범위만 받는다. 받은
바이트를 원래 위치에 쓰면 파일이 원본만큼 커진다 — 사이의 빈 자리를 실제로 채우는
파일 시스템이 있다(NTFS의 일반 파일, exFAT). 그래서 받은 범위를 빈틈없이 이어 쓰고,
moov의 청크 위치 표(stco/co64)를 이어 쓴 위치로 고쳐 ffmpeg가 읽을 수 있는 작은
mp4로 만든다. 디스크 사용량이 받은 바이트와 같고 파일 시스템에 기대지 않는다.

받지 않은 청크의 위치는 파일의 끝으로 적는다. ffmpeg가 그 자리를 읽으면 입력이 끝난
것으로 처리한다 — 0으로 채운 자리를 디코드하다 깨진 프레임을 내는 일이 없다.

이 모듈은 위치 계산과 bytes 고치기만 한다. 파일 입출력은 다운로더가 한다.
"""

import struct
from bisect import bisect_right
from collections.abc import Iterable
from dataclasses import dataclass

from core.api.mp4 import MP4_INVALID, MP4_UNSUPPORTED, Mp4Error, _boxes, _entry_count, _leaf_boxes
from core.models.mp4_index import Mp4Index


@dataclass(frozen=True)
class PartialLayout:
    """받을 범위와, 그 범위가 부분 파일의 어디에 놓이는지를 담는다."""

    # 받을 원본 바이트 범위 — (시작, 끝) 양 끝 포함, 오름차순이고 겹치거나 맞닿지 않는다.
    # 첫 범위는 파일의 머리(0부터 첫 샘플 직전까지)다
    ranges: tuple[tuple[int, int], ...]
    starts: tuple[int, ...]  # ranges와 같은 순서로, 그 범위가 부분 파일에서 시작하는 위치
    size: int  # 부분 파일의 크기(바이트) — 받을 바이트의 합

    def position(self, offset: int) -> int | None:
        """원본의 offset이 부분 파일에서 놓이는 위치. 받지 않는 자리면 None."""
        at = bisect_right(self.ranges, (offset, float("inf"))) - 1
        if at < 0 or offset > self.ranges[at][1]:
            return None
        return self.starts[at] + offset - self.ranges[at][0]


def merge_ranges(ranges: Iterable[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    """겹치거나 맞닿은 범위를 합쳐 오름차순으로 돌려준다. 범위는 (시작, 끝) 양 끝 포함이다."""
    merged: list[tuple[int, int]] = []
    for first, last in sorted(ranges):
        if merged and first <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], last))
        else:
            merged.append((first, last))
    return tuple(merged)


def plan_partial(index: Mp4Index, ranges: Iterable[tuple[int, int]]) -> PartialLayout:
    """파일의 머리와 주어진 범위를 받는 배치를 만든다.

    머리는 0부터 첫 샘플 직전까지다 — ftyp · moov · mdat의 머리가 든다. 겹치는 범위는
    합친다.

    Args:
        index: ``read_mp4_index`` · ``fetch_mp4_index``의 결과 — moov의 위치가 들어 있어야 한다
        ranges: 구간마다의 바이트 범위(양 끝 포함)

    Raises:
        Mp4Error: moov가 샘플보다 뒤에 있는 경우(``MP4_UNSUPPORTED``)
    """
    tracks = [index.video] + ([index.audio] if index.audio is not None else [])
    first_sample = min(min(track.offsets) for track in tracks if track.offsets)
    if index.moov_range is None or index.moov_range[1] >= first_sample:
        raise Mp4Error(MP4_UNSUPPORTED, "moov가 샘플보다 뒤에 있다")
    merged = merge_ranges([(0, first_sample - 1), *ranges])
    starts = []
    position = 0
    for first, last in merged:
        starts.append(position)
        position += last - first + 1
    return PartialLayout(ranges=merged, starts=tuple(starts), size=position)


def patch_head(head: bytes, layout: PartialLayout, moov_range: tuple[int, int]) -> bytes:
    """부분 파일의 머리를 부분 파일에 맞게 고친 bytes를 돌려준다. 길이는 바뀌지 않는다.

    - 모든 트랙의 stco/co64: 받은 청크는 부분 파일 안의 위치로, 받지 않은 청크는 파일의
      끝으로 바꾼다
    - mdat의 크기: 부분 파일의 끝까지로 줄인다

    Args:
        head: 원본의 0부터 읽은 머리 — ``layout.ranges[0]``의 바이트
        layout: ``plan_partial``의 결과
        moov_range: 원본에서 moov의 (시작, 끝) — 양 끝 포함

    Raises:
        Mp4Error: 머리가 moov를 다 담지 못했거나 상자가 손상된 경우(``MP4_INVALID``)
    """
    patched = bytearray(head)
    moov_start, moov_end = moov_range[0], moov_range[1] + 1
    if moov_end > len(patched):
        raise Mp4Error(MP4_INVALID, "머리에 moov가 다 들어 있지 않다")
    try:
        moov_boxes = list(_boxes(head, moov_start, moov_end))
        if len(moov_boxes) != 1 or moov_boxes[0][0] != b"moov":
            raise Mp4Error(MP4_INVALID, "moov 상자가 아니다")
        _, body, body_end = moov_boxes[0]
        for box_type, track_body, track_end in _boxes(head, body, body_end):
            if box_type == b"trak":
                _patch_chunk_offsets(patched, _leaf_boxes(head, track_body, track_end), layout)
        _shrink_mdat(patched, layout.size)
    except (struct.error, IndexError) as e:
        raise Mp4Error(MP4_INVALID, str(e)) from e
    return bytes(patched)


def _patch_chunk_offsets(
    data: bytearray, boxes: dict[bytes, tuple[int, int]], layout: PartialLayout
) -> None:
    """트랙 하나의 stco(32비트) 또는 co64(64비트)를 부분 파일의 위치로 고쳐 쓴다."""
    for name, code, width in ((b"stco", "I", 4), (b"co64", "Q", 8)):
        if name not in boxes:
            continue
        span = boxes[name]
        count = _entry_count(data, span, width)
        table = span[0] + 8
        offsets = struct.unpack_from(f">{count}{code}", data, table)
        moved = [layout.position(offset) for offset in offsets]
        struct.pack_into(
            f">{count}{code}",
            data,
            table,
            *(layout.size if position is None else position for position in moved),
        )


def _shrink_mdat(data: bytearray, total_size: int) -> None:
    """머리 끝에 걸친 상자(mdat)의 크기를 부분 파일의 끝까지로 줄인다.

    크기 0("파일 끝까지")은 그대로 둔다. 머리가 상자 경계에서 끝나면 고칠 것이 없다.
    """
    position = 0
    while position + 8 <= len(data):
        size, box_type = struct.unpack_from(">I4s", data, position)
        if size == 0:
            return
        large = size == 1
        if large:
            if position + 16 > len(data):
                return
            size = struct.unpack_from(">Q", data, position + 8)[0]
        if size < (16 if large else 8):
            raise Mp4Error(MP4_INVALID, f"상자 {box_type!r}의 크기 {size}")
        if position + size > len(data):
            if box_type == b"mdat":
                if large:
                    struct.pack_into(">Q", data, position + 8, total_size - position)
                else:
                    struct.pack_into(">I", data, position, total_size - position)
            return
        position += size
