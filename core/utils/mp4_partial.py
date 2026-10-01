"""받은 범위만 이어 붙인 부분 mp4 (#309).

구간 다운로드는 mp4에서 구간에 필요한 범위만 받는다. 받은 바이트를 원래 위치에 쓰면
파일이 원본만큼 커진다 — 사이의 빈 자리를 실제로 채우는 파일 시스템이 있다(NTFS의
일반 파일, exFAT). 그래서 받은 범위를 빈틈없이 이어 쓰고, moov의 청크 위치 표
(stco/co64)를 이어 쓴 위치로 고쳐 ffmpeg가 읽을 수 있는 작은 mp4로 만든다. 디스크
사용량이 파일 시스템에 기대지 않는다.

부분 파일의 모양:

    [머리: 원본의 0 ~ moov 끝(위치 표를 고친 것) · mdat 머리 · 채움][받은 범위 1][받은 범위 2] …

머리는 받지 않고 만든다 — moov는 구간을 정하려고 이미 받았다(``Mp4Head``). 머리의
길이는 원본에서 첫 샘플이 놓인 위치와 같다. 원본의 moov 끝과 첫 샘플 사이(mdat의
머리가 있던 자리)는 받지 않았으므로, 그 자리에 mdat 머리를 새로 쓰고 남는 자리는
0으로 채운다.

받지 않은 청크의 위치는 파일의 끝으로 적는다. ffmpeg가 그 자리를 읽으면 입력이 끝난
것으로 처리한다 — 0으로 채운 자리를 디코드하다 깨진 프레임을 내는 일이 없다.

이 모듈은 위치 계산과 bytes 만들기만 한다. 파일 입출력은 다운로더가 한다.
"""

import struct
from bisect import bisect_right
from collections.abc import Iterable
from dataclasses import dataclass

from core.api.mp4 import MP4_INVALID, MP4_UNSUPPORTED, Mp4Error, _boxes, _entry_count, _leaf_boxes
from core.models.mp4_index import Mp4Index

_BOX_HEADER_BYTES = 8  # 상자 머리 — 크기(4) + 종류(4)
# 32비트 크기 칸에 적을 수 있는 가장 큰 상자 크기. 이보다 큰 mdat는 크기를 0("파일
# 끝까지")으로 적는다 — 8바이트 머리로 끝난다
_MAX_BOX_SIZE_32 = 0xFFFFFFFF


@dataclass(frozen=True)
class PartialLayout:
    """받을 범위와, 그 범위가 부분 파일의 어디에 놓이는지를 담는다."""

    head_size: int  # 부분 파일의 머리 길이(바이트) — 원본에서 첫 샘플이 놓인 위치와 같다
    # 받을 원본 바이트 범위 — (시작, 끝) 양 끝 포함, 오름차순이고 겹치거나 맞닿지 않는다
    ranges: tuple[tuple[int, int], ...]
    starts: tuple[int, ...]  # ranges와 같은 순서로, 그 범위가 부분 파일에서 시작하는 위치
    size: int  # 부분 파일의 크기(바이트) — 머리 + 받을 바이트의 합

    @property
    def download_size(self) -> int:
        """받을 바이트의 합 — 머리는 받지 않으므로 빠진다."""
        return self.size - self.head_size

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
    """주어진 범위를 받아 머리 뒤에 이어 놓는 배치를 만든다. 겹치는 범위는 합친다.

    Args:
        index: ``read_mp4_head`` · ``fetch_mp4_head``의 색인 — moov의 위치가 들어 있어야 한다
        ranges: 구간마다의 바이트 범위(양 끝 포함) — 샘플이 놓인 자리다

    Raises:
        Mp4Error: moov가 샘플보다 뒤에 있거나, moov와 첫 샘플 사이에 mdat 머리가 들어갈
            자리가 없는 경우(``MP4_UNSUPPORTED``)
    """
    tracks = [index.video] + ([index.audio] if index.audio is not None else [])
    first_sample = min(min(track.offsets) for track in tracks if track.offsets)
    if index.moov_range is None or index.moov_range[1] >= first_sample:
        raise Mp4Error(MP4_UNSUPPORTED, "moov가 샘플보다 뒤에 있다")
    if first_sample - (index.moov_range[1] + 1) < _BOX_HEADER_BYTES:
        raise Mp4Error(MP4_UNSUPPORTED, "moov와 첫 샘플 사이에 mdat 머리가 들어갈 자리가 없다")
    merged = merge_ranges(ranges)
    starts = []
    position = first_sample
    for first, last in merged:
        starts.append(position)
        position += last - first + 1
    return PartialLayout(head_size=first_sample, ranges=merged, starts=tuple(starts), size=position)


def build_head(data: bytes, layout: PartialLayout, moov_range: tuple[int, int]) -> bytes:
    """부분 파일의 머리를 만든다. 길이는 ``layout.head_size``다.

    - 원본의 0부터 moov 끝까지를 그대로 쓰되, 모든 트랙의 stco/co64를 고친다 — 받은
      청크는 부분 파일 안의 위치로, 받지 않은 청크는 파일의 끝으로
    - moov 바로 뒤에 mdat 머리를 쓴다. 크기는 부분 파일의 끝까지다(32비트에 담기지
      않으면 0 = "파일 끝까지")
    - 남는 자리(원본에서 moov와 첫 샘플 사이에 있던 것의 나머지)는 0으로 채운다. mdat
      안이고 어느 샘플도 가리키지 않는다

    Args:
        data: 원본의 0부터 moov의 마지막 바이트까지 — ``Mp4Head.data``
        layout: ``plan_partial``의 결과
        moov_range: 원본에서 moov의 (시작, 끝) — 양 끝 포함

    Raises:
        Mp4Error: data가 moov를 다 담지 못했거나 상자가 손상된 경우(``MP4_INVALID``)
    """
    moov_start, moov_end = moov_range[0], moov_range[1] + 1
    if len(data) < moov_end:
        raise Mp4Error(MP4_INVALID, "머리에 moov가 다 들어 있지 않다")
    patched = bytearray(data[:moov_end])
    try:
        moov_boxes = list(_boxes(patched, moov_start, moov_end))
        if len(moov_boxes) != 1 or moov_boxes[0][0] != b"moov":
            raise Mp4Error(MP4_INVALID, "moov 상자가 아니다")
        _, body, body_end = moov_boxes[0]
        for box_type, track_body, track_end in list(_boxes(patched, body, body_end)):
            if box_type == b"trak":
                _patch_chunk_offsets(patched, _leaf_boxes(patched, track_body, track_end), layout)
    except (struct.error, IndexError) as e:
        raise Mp4Error(MP4_INVALID, str(e)) from e

    mdat_size = layout.size - moov_end
    header = struct.pack(">I4s", mdat_size if mdat_size <= _MAX_BOX_SIZE_32 else 0, b"mdat")
    padding = bytes(layout.head_size - moov_end - len(header))
    return bytes(patched) + header + padding


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
