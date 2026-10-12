"""구간 파일을 직접 쓴다 — 배치 · 대응표 · moov (#309).

구간 다운로드의 산출물(구간 파일)을 ffmpeg의 잇기 없이 만든다. 원본에서 옮길 수 있는
바이트는 받는 대로 구간 파일의 제자리에 쓰고, 다시 인코딩해야 하는 경계 GOP만 따로 만들어
파일 끝에 붙인 뒤 moov를 써 넣는다. 구간 길이에 비례해 다시 쓰는 일이 없다.

구간 파일의 모양:

    [ftyp][moov · free — 자리는 받기 전에 정한다][mdat: 옮긴 바이트(원본 순서) · 머리 GOP · 꼬리 GOP]

- **옮긴 바이트**: 경계 GOP를 뺀 영상 샘플과 구간의 오디오 샘플. 원본 파일 안의 순서 그대로
  빈틈없이 이어 쓴다. 어느 원본 범위가 구간 파일의 어디로 가는지가 대응표(``MovedRun``)다
- **경계 GOP**: 구간 시작이 키프레임이 아니면 그 프레임부터 다음 키프레임 직전까지(머리),
  구간 끝이 영상의 마지막 프레임이 아니면 마지막 키프레임부터 끝 프레임까지(꼬리)를 다시
  인코딩한다. 시작과 끝이 한 GOP 안이면 구간 전체가 조각 하나다
- **moov의 자리**: 옮긴 샘플의 표는 원본 moov의 표를 잘라 온 것이라 받기 전에 크기가
  정해진다. 경계 GOP의 표는 인코딩한 뒤에 정해지므로 상한으로 자리를 잡고, 남는 곳은
  ``free`` 상자로 채운다
- **영상 설정(SPS · PPS)**: 다시 인코딩한 조각은 원본과 다른 번호의 SPS · PPS를 써야 한다
  (인코더에 번호를 준다). 영상 샘플 설명 하나의 avcC에 원본 것과 조각의 것을 함께 넣는다.
  번호가 겹치면 원본 샘플이 조각의 설정으로 디코드돼 깨지므로 거부한다
- **오디오**: 다시 인코딩하지 않는다. 구간 시작에 걸친 샘플부터 넣고, 앞의 남는 부분은
  편집 목록이 가린다. 0초에서 시작하는 구간은 원본이 가린 첫 샘플(프라이밍)을 넣지 않는다
- 영상 트랙의 편집 목록이 구간의 첫 프레임을 0에 놓는다

같은 구간을 하이브리드 컷으로 자른 것과 영상 패킷 수 · PTS · DTS · 디코드한 프레임이 같음을
실제 파일로 확인했다(#309).

이 모듈은 위치 계산과 bytes 만들기만 한다. 파일 입출력 · 받기 · 인코딩은 다운로더가 한다.
"""

import array
import struct
import sys
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from itertools import accumulate, compress, groupby, repeat
from operator import add, le, mul, ne, sub

from core.api.mp4 import (
    MP4_INVALID,
    MP4_UNSUPPORTED,
    Mp4Error,
    _boxes,
    _entry_count,
    _leaf_boxes,
    _read_table,
    _sample_offsets,
    _sample_sizes,
    _timescale,
)
from core.models.cut import CutPiece
from core.models.mp4_index import Mp4Index, Mp4Track

# 구간 파일의 ftyp — ffmpeg가 mp4에 쓰는 것과 같은 상표다
SECTION_FILE_FTYP = struct.pack(">I4s4sI", 32, b"ftyp", b"isom", 0x200) + b"isomiso2avc1mp41"
MDAT_HEADER_BYTES = 16  # mdat 머리 — 64비트 크기 칸으로 쓴다(구간 파일은 4GiB를 넘을 수 있다)

# moov 자리의 상한에 더하는 값 (바이트)
RESERVE_PER_ENCODED_FRAME = 24  # 다시 인코딩한 프레임 하나 — stts 8 + ctts 8 + stsz 4 + stss 4
RESERVE_PER_ENCODED_PIECE = 276  # 조각 하나 — 청크 하나(stsc 12 + co64 8) + SPS · PPS 256
RESERVE_SLACK = 64  # 표의 이음매에서 늘 수 있는 줄
_FREE_HEADER_BYTES = 8  # free 상자의 머리 — 남는 자리가 이보다 작으면 상자를 쓸 수 없다

_TIME_SLACK = 1e-9  # 초 — 구간 경계와 오디오 샘플 시각을 견줄 때의 float 오차
_U32_LIMIT = 1 << 32


@dataclass(frozen=True)
class MovedRun:
    """대응표의 한 줄 — 원본의 이어진 바이트 범위가 구간 파일의 어디로 가는지."""

    source_offset: int  # 원본 파일 안의 시작 위치
    length: int  # 바이트 수
    target_offset: int  # 구간 파일 안의 시작 위치


@dataclass(frozen=True)
class EncodedPiece:
    """다시 인코딩한 조각 하나 — 인코더가 쓴 작은 mp4에서 읽은 영상 샘플이다."""

    timescale: int  # 영상 트랙의 초당 틱 수 — 원본과 같아야 한다
    stsd: bytes = field(repr=False)  # 영상 샘플 설명 상자의 본문 — SPS · PPS를 꺼낸다
    decode_ticks: tuple[int, ...]  # 샘플별 DTS(틱), 디코드 순서
    composition: tuple[int, ...]  # 샘플별 (PTS − DTS)
    sizes: tuple[int, ...]  # 샘플별 크기(바이트)
    sync_samples: tuple[int, ...]  # 키프레임인 샘플의 번호(0부터)
    last_delta: int  # 마지막 샘플의 길이(틱)
    payload: bytes = field(repr=False)  # 샘플 바이트를 디코드 순서로 이은 것


@dataclass(frozen=True, eq=False)
class _TrackPart:
    """트랙 하나에서 구간 파일로 옮기는 부분 — 원본 상자의 본문과 잘라 온 표."""

    timescale: int
    tkhd: bytes
    mdhd: bytes
    hdlr: bytes
    minf_other: bytes  # minf 안의 stbl이 아닌 상자들(vmhd · smhd · dinf) — 머리째로
    stsd: bytes  # 샘플 설명 상자의 본문
    first_dts: int  # 옮기는 첫 샘플의 DTS(틱). 옮기는 샘플이 없으면 0
    delta_counts: array.array  # stts의 (개수, 길이) 구간 — 옮기는 샘플의 것
    delta_values: array.array
    composition_counts: array.array  # ctts의 (개수, 값) 구간. 표가 없으면 비어 있다
    composition_values: array.array
    sizes: array.array  # 샘플별 크기
    sync_samples: array.array | None  # 옮기는 샘플 안에서의 키프레임 번호(0부터). 표가 없으면 None
    chunk_positions: array.array  # 청크마다의 위치 — mdat 본문의 시작부터 센 바이트
    chunk_counts: array.array  # 청크마다의 샘플 수


@dataclass(frozen=True, eq=False)
class SectionFilePlan:
    """구간 하나의 구간 파일을 어떻게 쓸지 — 받기 전에 정한다."""

    first_frame: int  # 구간의 첫 프레임(표시 순서의 번호)
    last_frame: int  # 구간의 끝 프레임 — 구간에 든다
    # 구간을 나눈 조각 — head · tail · whole은 다시 인코딩하고 mid는 원본에서 옮긴다
    pieces: tuple[CutPiece, ...]
    runs: tuple[MovedRun, ...]  # 대응표 — 원본 위치 순서
    moov_offset: int  # 구간 파일 안에서 moov가 시작하는 위치
    moov_reserved: int  # moov와 free가 함께 차지하는 바이트 — 받기 전에 정한 상한
    body_offset: int  # mdat 본문이 시작하는 위치 — 옮긴 바이트가 여기서부터 놓인다
    moved_bytes: int  # 원본에서 옮기는 바이트 수
    start_time: float  # 구간이 시작하는 시각(초) — 첫 프레임의 표시 시각
    end_time: float  # 구간이 끝나는 시각(초) — 끝 프레임이 끝나는 시각
    _movie_timescale: int = field(repr=False, default=0)
    _mvhd: bytes = field(repr=False, default=b"")
    _video: _TrackPart | None = field(repr=False, default=None)
    _audio: _TrackPart | None = field(repr=False, default=None)
    _audio_lead: int = field(
        repr=False, default=0
    )  # 오디오 첫 샘플이 구간 시작보다 앞선 틱(음수면 늦다)
    # 다시 인코딩하는 조각마다, 그 조각의 첫 프레임이 원본에서 갖는 PTS(틱)
    _piece_pts: tuple[int, ...] = field(repr=False, default=())
    _first_pts: int = field(repr=False, default=0)  # 구간의 첫 프레임이 원본에서 갖는 PTS(틱)

    @property
    def encoded_pieces(self) -> tuple[CutPiece, ...]:
        """다시 인코딩할 조각 — 구간 파일에 놓이는 순서."""
        return tuple(piece for piece in self.pieces if piece.reencoded)

    @property
    def encoded_offset(self) -> int:
        """다시 인코딩한 조각의 샘플이 놓이기 시작하는 위치 — 옮긴 바이트 바로 뒤."""
        return self.body_offset + self.moved_bytes


# ================================================================ 조각 나누기


def covers_whole_video(index: Mp4Index, first_frame: int, last_frame: int) -> bool:
    """구간이 영상의 첫 프레임부터 마지막 프레임까지인지 — 그러면 원본을 그대로 쓴다."""
    return first_frame == 0 and last_frame == len(index.frame_pts) - 1


def section_pieces(index: Mp4Index, first_frame: int, last_frame: int) -> tuple[CutPiece, ...]:
    """구간 [first_frame, last_frame]을 조각으로 나눈다. 두 프레임 모두 구간에 든다.

    - 시작이 키프레임이면 머리를 만들지 않는다
    - 끝이 영상의 마지막 프레임이면 꼬리를 만들지 않는다 — 마지막 GOP까지 옮긴다
    - 옮길 키프레임이 구간 안에 없으면 구간 전체를 다시 인코딩하는 조각 하나(whole)다

    Raises:
        ValueError: 프레임 번호가 범위를 벗어났거나 first_frame 앞에 키프레임이 없는 경우
    """
    count = len(index.frame_pts)
    if not 0 <= first_frame <= last_frame < count:
        raise ValueError(
            f"구간 [{first_frame}, {last_frame}]이 프레임 범위 [0, {count - 1}]를 벗어난다"
        )
    keys = index.keyframes
    before = bisect_right(keys, first_frame) - 1
    if before < 0:
        raise ValueError(f"프레임 {first_frame}보다 앞에 키프레임이 없다")
    starts_on_key = keys[before] == first_frame
    if starts_on_key:
        copy_from = first_frame
    else:
        copy_from = keys[before + 1] if before + 1 < len(keys) else None
    ends_video = last_frame == count - 1
    copy_until = count if ends_video else keys[bisect_right(keys, last_frame) - 1]

    if copy_from is None or copy_from > last_frame:
        return (CutPiece("whole", first_frame, last_frame + 1),)
    pieces = []
    if not starts_on_key:
        pieces.append(CutPiece("head", first_frame, copy_from))
    if copy_until > copy_from:
        pieces.append(CutPiece("mid", copy_from, copy_until))
    if not ends_video:
        pieces.append(CutPiece("tail", copy_until, last_frame + 1))
    if len(pieces) == 1 and pieces[0].reencoded:
        return (CutPiece("whole", first_frame, last_frame + 1),)
    return tuple(pieces)


# ================================================================ 배치 · 대응표


def plan_section_file(
    index: Mp4Index, moov: bytes, first_frame: int, last_frame: int
) -> SectionFilePlan:
    """구간 하나의 구간 파일 배치와 대응표를 정한다. 받기 전에 부른다.

    Args:
        index: moov를 해석한 색인
        moov: 그 moov 상자 전체(머리 포함) — ``Mp4Raw.moov``
        first_frame: 구간의 첫 프레임(``index.frame_pts``의 번호)
        last_frame: 구간의 끝 프레임 — 구간에 든다

    Raises:
        ValueError: 프레임 번호가 범위를 벗어난 경우
        Mp4Error: 옮길 영상이 닫힌 GOP가 아니거나(``MP4_UNSUPPORTED``), 표가 손상된
            경우(``MP4_INVALID``)
    """
    pieces = section_pieces(index, first_frame, last_frame)
    try:
        return _plan(index, moov, first_frame, last_frame, pieces)
    except (struct.error, IndexError) as e:
        raise Mp4Error(MP4_INVALID, str(e)) from e


def _plan(
    index: Mp4Index, moov: bytes, first_frame: int, last_frame: int, pieces: tuple[CutPiece, ...]
) -> SectionFilePlan:
    movie_timescale, mvhd, video_boxes, audio_boxes = _source_boxes(moov)
    samples = index.frame_samples

    mid = next((piece for piece in pieces if piece.kind == "mid"), None)
    low = high = 0
    if mid is not None:
        moved = samples[mid.first : mid.end]
        low, high = min(moved), max(moved) + 1
        if high - low != len(moved) or samples[mid.first] != low:
            # 표시 순서의 프레임 묶음이 디코드 순서의 이어진 샘플 묶음이 아니다 — 다음 GOP의
            # 프레임이 이 GOP의 프레임보다 먼저 표시된다(열린 GOP). 옮긴 것만으로 디코드할 수 없다
            raise Mp4Error(MP4_UNSUPPORTED, "옮길 영상이 닫힌 GOP가 아니다")

    last_sample = samples[last_frame]
    start_time = index.frame_pts[first_frame]
    end_time = index.frame_pts[last_frame] + index.video.durations[last_sample]

    video_stts = _run_table(moov, video_boxes.leaf[b"stts"], signed=False)
    video_ctts = (
        _run_table(moov, video_boxes.leaf[b"ctts"], signed=True)
        if b"ctts" in video_boxes.leaf
        else None
    )

    def source_pts(frame: int) -> int:
        sample = samples[frame]
        return _ticks_before(video_stts, sample) + (
            _value_at(video_ctts, sample) if video_ctts else 0
        )

    encoded = [piece for piece in pieces if piece.reencoded]
    video = _track_part(
        moov, video_boxes, index.video, low, high, video_stts, video_ctts, with_sync=True
    )

    audio = None
    audio_lead = 0
    audio_low = audio_high = 0
    if index.audio is not None and audio_boxes is not None:
        times = index.audio.times
        audio_low = max(bisect_right(times, start_time + _TIME_SLACK) - 1, 0)
        # 원본이 가린 샘플(프라이밍)은 넣지 않는다 — 0초에서 시작하는 구간이 여기에 걸린다
        while audio_low + 1 < len(times) and times[audio_low] < 0:
            audio_low += 1
        audio_high = max(bisect_left(times, end_time - _TIME_SLACK), audio_low)
        if audio_high > audio_low:
            audio_stts = _run_table(moov, audio_boxes.leaf[b"stts"], signed=False)
            audio_ctts = (
                _run_table(moov, audio_boxes.leaf[b"ctts"], signed=True)
                if b"ctts" in audio_boxes.leaf
                else None
            )
            audio = _track_part(
                moov, audio_boxes, index.audio, audio_low, audio_high, audio_stts, audio_ctts,
                with_sync=False,
            )  # fmt: skip
            audio_lead = round((start_time - times[audio_low]) * audio.timescale)

    runs, moved_bytes = _place(
        (index.video, low, high, video), (index.audio, audio_low, audio_high, audio)
    )

    draft = SectionFilePlan(
        first_frame=first_frame,
        last_frame=last_frame,
        pieces=pieces,
        runs=(),
        moov_offset=len(SECTION_FILE_FTYP),
        moov_reserved=0,
        body_offset=0,
        moved_bytes=moved_bytes,
        start_time=start_time,
        end_time=end_time,
        _movie_timescale=movie_timescale,
        _mvhd=mvhd,
        _video=video,
        _audio=audio,
        _audio_lead=audio_lead,
        _piece_pts=tuple(source_pts(piece.first) for piece in encoded),
        _first_pts=source_pts(first_frame),
    )
    # 옮기는 샘플만으로 쓴 moov의 길이는 정확하다 — 위치 값은 폭이 고정이라 길이를 바꾸지 않는다
    exact = len(_moov(draft, ()))
    encoded_frames = sum(piece.end - piece.first for piece in encoded)
    reserved = (
        exact
        + encoded_frames * RESERVE_PER_ENCODED_FRAME
        + len(encoded) * RESERVE_PER_ENCODED_PIECE
        + RESERVE_SLACK
        + _FREE_HEADER_BYTES
    )
    body_offset = len(SECTION_FILE_FTYP) + reserved + MDAT_HEADER_BYTES
    return replace(
        draft,
        runs=tuple(
            MovedRun(source, length, body_offset + target) for source, length, target in runs
        ),
        moov_reserved=reserved,
        body_offset=body_offset,
    )


@dataclass(frozen=True)
class _SourceTrack:
    leaf: dict[bytes, tuple[int, int]]  # 트랙 안의 상자 — {종류: (본문 시작, 본문 끝)}
    minf_other: bytes


def _source_boxes(moov: bytes) -> tuple[int, bytes, _SourceTrack, _SourceTrack | None]:
    """moov에서 (무비 timescale, mvhd 본문, 영상 트랙, 오디오 트랙)을 찾는다. 종류마다 첫 트랙이다."""
    top = list(_boxes(moov, 0, len(moov)))
    if len(top) != 1 or top[0][0] != b"moov":
        raise Mp4Error(MP4_INVALID, "moov 상자가 아니다")
    _, moov_body, moov_end = top[0]
    mvhd = b""
    tracks: dict[bytes, _SourceTrack] = {}
    for box_type, body, body_end in _boxes(moov, moov_body, moov_end):
        if box_type == b"mvhd":
            mvhd = moov[body:body_end]
        elif box_type == b"trak":
            leaf = _leaf_boxes(moov, body, body_end)
            if b"hdlr" not in leaf or b"mdhd" not in leaf or b"tkhd" not in leaf:
                continue
            handler = moov[leaf[b"hdlr"][0] + 8 : leaf[b"hdlr"][0] + 12]
            if handler in (b"vide", b"soun") and handler not in tracks:
                tracks[handler] = _SourceTrack(leaf, _minf_other(moov, body, body_end))
    if not mvhd or b"vide" not in tracks:
        raise Mp4Error(MP4_INVALID, "mvhd나 영상 트랙이 없다")
    return _timescale(mvhd, 0), mvhd, tracks[b"vide"], tracks.get(b"soun")


def _minf_other(moov: bytes, start: int, end: int) -> bytes:
    """trak 안 minf의, stbl이 아닌 상자들을 머리째로 이어 돌려준다."""
    for box_type, body, body_end in _boxes(moov, start, end):
        if box_type == b"mdia":
            for inner_type, inner, inner_end in _boxes(moov, body, body_end):
                if inner_type == b"minf":
                    return b"".join(
                        _box(kind, moov[kind_body:kind_end])
                        for kind, kind_body, kind_end in _boxes(moov, inner, inner_end)
                        if kind != b"stbl"
                    )
    return b""


def _run_table(data: bytes, span: tuple[int, int], *, signed: bool) -> tuple[array.array, array.array, list[int]]:  # fmt: skip
    """stts · ctts — (개수들, 값들, 개수의 누적합). 값은 signed면 부호 있는 32비트로 읽는다."""
    runs = _entry_count(data, span, 8)
    counts = _read_table(data, span[0] + 8, 2 * runs, "I")[0::2]
    values = _read_table(data, span[0] + 8, 2 * runs, "i" if signed else "I")[1::2]
    return counts, values, list(accumulate(counts))


def _run_at(table: tuple[array.array, array.array, list[int]], sample: int) -> int:
    """sample이 든 구간의 번호."""
    run = bisect_right(table[2], sample)
    if run >= len(table[0]):
        raise Mp4Error(MP4_INVALID, f"표에 샘플 {sample}이 없다")
    return run


def _value_at(table: tuple[array.array, array.array, list[int]], sample: int) -> int:
    return table[1][_run_at(table, sample)]


def _ticks_before(table: tuple[array.array, array.array, list[int]], sample: int) -> int:
    """sample 앞의 샘플들의 길이를 더한 값 — 그 샘플의 DTS(틱)."""
    counts, values, ends = table
    if sample == 0:
        return 0
    run = _run_at(table, sample)
    start = ends[run - 1] if run else 0
    return sum(map(mul, counts[:run], values[:run])) + (sample - start) * values[run]


def _slice_runs(
    table: tuple[array.array, array.array, list[int]], low: int, high: int
) -> tuple[array.array, array.array]:
    """샘플 [low, high)의 (개수, 값) 구간."""
    counts, values, ends = table
    out_counts, out_values = array.array("Q"), array.array("q")
    if high <= low:
        return out_counts, out_values
    first, last = _run_at(table, low), _run_at(table, high - 1)
    out_counts.extend(array.array("Q", counts[first : last + 1]))
    out_values.extend(array.array("q", values[first : last + 1]))
    out_counts[0] -= low - (ends[first - 1] if first else 0)
    out_counts[-1] -= ends[last] - high
    return out_counts, out_values


def _track_part(
    moov: bytes,
    boxes: _SourceTrack,
    track: Mp4Track,
    low: int,
    high: int,
    stts: tuple[array.array, array.array, list[int]],
    ctts: tuple[array.array, array.array, list[int]] | None,
    *,
    with_sync: bool,
) -> _TrackPart:
    leaf = boxes.leaf
    delta_counts, delta_values = _slice_runs(stts, low, high)
    composition_counts, composition_values = (
        _slice_runs(ctts, low, high) if ctts else (array.array("Q"), array.array("q"))
    )
    sync = None
    if with_sync:
        if b"stss" in leaf:
            source = track.sync_samples
            picked = source[bisect_left(source, low) : bisect_left(source, high)]
            sync = array.array("Q", map(add, picked, repeat(-low)))
        else:
            sync = array.array("Q", range(high - low))
    return _TrackPart(
        timescale=_timescale(moov, leaf[b"mdhd"][0]),
        tkhd=moov[slice(*leaf[b"tkhd"])],
        mdhd=moov[slice(*leaf[b"mdhd"])],
        hdlr=moov[slice(*leaf[b"hdlr"])],
        minf_other=boxes.minf_other,
        stsd=moov[slice(*leaf[b"stsd"])] if b"stsd" in leaf else b"",
        first_dts=_ticks_before(stts, low) if high > low else 0,
        delta_counts=delta_counts,
        delta_values=delta_values,
        composition_counts=composition_counts,
        composition_values=composition_values,
        sizes=array.array("I", track.sizes[low:high]),
        sync_samples=sync,
        chunk_positions=array.array("Q"),
        chunk_counts=array.array("I"),
    )


def _chunk_pieces(
    track: Mp4Track, low: int, high: int
) -> tuple[array.array, array.array, array.array]:
    """샘플 [low, high)가 걸친 청크마다의 (원본 위치, 바이트 수, 샘플 수) — 배열 셋."""
    offsets, lengths, counts = array.array("Q"), array.array("Q"), array.array("I")
    if high <= low:
        return offsets, lengths, counts
    starts = track.chunk_starts
    first = bisect_right(starts, low) - 1
    last = bisect_left(starts, high)
    if last - first == high - low:
        # 청크마다 샘플 하나 — 샘플의 위치와 크기가 곧 청크의 것이다. 샘플마다 돌지 않는다
        offsets.extend(array.array("Q", track.offsets[low:high]))
        lengths.extend(array.array("Q", track.sizes[low:high]))
        counts.extend(array.array("I", [1]) * (high - low))
        return offsets, lengths, counts
    # 청크마다 그 안에서 옮기는 샘플의 [처음, 끝) — 양 끝의 청크는 구간에 든 부분만이다.
    # 청크마다 파이썬 코드를 돌지 않는다: 크기의 누적합에서 두 자리의 차로 바이트 수를 낸다
    begins = array.array("q", starts[first:last])
    begins[0] = low
    ends = begins[1:]
    ends.append(high)
    sums = array.array("Q", accumulate(track.sizes[low:high], initial=0))
    piece_offsets = track.offsets[low:high]
    offsets.extend(map(piece_offsets.__getitem__, map(sub, begins, repeat(low))))
    lengths.extend(
        map(
            sub,
            map(sums.__getitem__, map(sub, ends, repeat(low))),
            map(sums.__getitem__, map(sub, begins, repeat(low))),
        )
    )
    counts.extend(map(sub, ends, begins))
    return offsets, lengths, counts


def _place(*tracks: tuple[Mp4Track | None, int, int, _TrackPart | None]):
    """옮기는 청크를 원본 위치 순서로 이어 붙여 놓는다 — (대응표의 줄들, 옮기는 바이트 수).

    대응표의 줄은 (원본 위치, 바이트 수, mdat 본문 안의 위치)다. 원본에서 이어져 있던 청크는
    한 줄로 합친다. 트랙의 ``chunk_positions`` · ``chunk_counts``를 채운다.

    청크는 수백만 개일 수 있고 대응표의 줄은 수십 개다. 청크마다 돌지 않는다 — 원본의 한 범위에
    든 청크의 바이트 수가 그 범위의 길이와 같으면 빈틈이 없는 것이므로, 범위를 반씩 나눠 가며
    빈틈이 있는 곳만 따라 내려간다.
    """
    feeds = []  # (청크의 원본 위치, 바이트 수의 누적합, 트랙의 부분)
    for track, low, high, part in tracks:
        if track is None or part is None:
            continue
        offsets, lengths, counts = _chunk_pieces(track, low, high)
        if not offsets:
            continue
        ends = map(add, offsets, lengths)
        if not all(map(le, ends, offsets[1:])):
            raise Mp4Error(MP4_INVALID, "청크가 원본 위치 순서가 아니거나 겹친다")
        part.chunk_counts.extend(counts)
        feeds.append((offsets, array.array("Q", accumulate(lengths, initial=0)), part))
    if not feeds:
        return [], 0

    def held(begin: int, end: int) -> int:
        """원본의 [begin, end)에서 시작하는 청크들의 바이트 수."""
        return sum(
            sums[bisect_left(offsets, end)] - sums[bisect_left(offsets, begin)]
            for offsets, sums, _part in feeds
        )

    def next_start(after: int) -> int | None:
        """after보다 뒤에서 시작하는 첫 청크의 위치."""
        found = [
            offsets[at]
            for offsets, _sums, _part in feeds
            if (at := bisect_right(offsets, after)) < len(offsets)
        ]
        return min(found, default=None)

    spans: list[tuple[int, int]] = []  # 빈틈없이 이어진 (원본 위치, 바이트 수) — 원본 위치 순서
    begin = min(offsets[0] for offsets, _sums, _part in feeds)
    end = max(offsets[-1] + sums[-1] - sums[-2] for offsets, sums, _part in feeds)
    pending = [(begin, end)]  # 아직 보지 않은 범위 — 시작은 늘 청크의 시작이다
    while pending:
        begin, end = pending.pop()
        size = held(begin, end)
        if size > end - begin:
            raise Mp4Error(MP4_INVALID, "청크가 원본 위치 순서가 아니거나 겹친다")
        if size == end - begin:
            spans.append((begin, size))
            continue
        middle = next_start(begin + (end - begin) // 2)
        if middle is None or middle >= end:
            middle = next_start(begin)
            if middle is None or middle >= end:
                spans.append((begin, size))  # 청크 하나와 그 뒤의 빈틈
                continue
        # 뒤쪽을 먼저 쌓아 앞쪽부터 꺼낸다 — spans가 원본 위치 순서로 찬다
        pending.append((middle, end))
        pending.append((begin, middle))

    runs: list[list[int]] = []
    position = 0
    for begin, size in spans:
        if runs and runs[-1][0] + runs[-1][1] == begin:
            runs[-1][1] += size
        else:
            runs.append([begin, size, position])
        position += size
    for offsets, _sums, part in feeds:
        for begin, size, target in runs:
            first, last = bisect_left(offsets, begin), bisect_left(offsets, begin + size)
            if last > first:
                part.chunk_positions.extend(
                    array.array("Q", map(add, offsets[first:last], repeat(target - begin)))
                )
    return [tuple(run) for run in runs], position


# ================================================================ 다시 인코딩한 조각 읽기


def read_encoded_piece(data: bytes) -> EncodedPiece:
    """인코더가 쓴 작은 mp4(조각 하나)에서 영상 샘플과 그 표를 읽는다.

    Raises:
        Mp4Error: mp4가 아니거나 영상 트랙 · 샘플 표가 없는 경우(``MP4_INVALID``)
    """
    try:
        moov_span = next(
            ((body, end) for kind, body, end in _boxes(data, 0, len(data)) if kind == b"moov"), None
        )
        if moov_span is None:
            raise Mp4Error(MP4_INVALID, "조각에 moov가 없다")
        for box_type, body, body_end in _boxes(data, *moov_span):
            if box_type != b"trak":
                continue
            leaf = _leaf_boxes(data, body, body_end)
            if b"hdlr" in leaf and data[leaf[b"hdlr"][0] + 8 : leaf[b"hdlr"][0] + 12] == b"vide":
                return _encoded_piece(data, leaf)
    except (struct.error, IndexError, KeyError) as e:
        raise Mp4Error(MP4_INVALID, f"조각의 표를 읽지 못했다: {e}") from e
    raise Mp4Error(MP4_INVALID, "조각에 영상 트랙이 없다")


def _encoded_piece(data: bytes, leaf: dict[bytes, tuple[int, int]]) -> EncodedPiece:
    count = struct.unpack_from(">I", data, leaf[b"stsz"][0] + 8)[0]
    sizes = _sample_sizes(data, leaf[b"stsz"], count)
    stts = _run_table(data, leaf[b"stts"], signed=False)
    if not count or (stts[2][-1] if stts[2] else 0) != count:
        raise Mp4Error(MP4_INVALID, f"조각의 샘플 {count}개와 stts가 맞지 않는다")
    deltas = [value for run, value in zip(stts[0], stts[1]) for _ in range(run)]
    composition = [0] * count
    if b"ctts" in leaf:
        ctts = _run_table(data, leaf[b"ctts"], signed=True)
        composition = [value for run, value in zip(ctts[0], ctts[1]) for _ in range(run)]
        if len(composition) != count:
            raise Mp4Error(MP4_INVALID, "조각의 ctts가 샘플 수와 맞지 않는다")
    if b"stss" in leaf:
        numbers = _read_table(data, leaf[b"stss"][0] + 8, _entry_count(data, leaf[b"stss"], 4), "I")
        sync = tuple(number - 1 for number in numbers)
    else:
        sync = tuple(range(count))
    offsets, _chunk_starts = _sample_offsets(data, leaf, sizes)
    if any(offset + size > len(data) for offset, size in zip(offsets, sizes)):
        raise Mp4Error(MP4_INVALID, "조각의 샘플이 파일을 넘는다")
    return EncodedPiece(
        timescale=_timescale(data, leaf[b"mdhd"][0]),
        stsd=data[slice(*leaf[b"stsd"])],
        decode_ticks=tuple(accumulate(deltas[:-1], initial=0)),
        composition=tuple(composition),
        sizes=tuple(sizes),
        sync_samples=sync,
        last_delta=deltas[-1],
        payload=b"".join(data[offset : offset + size] for offset, size in zip(offsets, sizes)),
    )


# ================================================================ 머리 쓰기


def build_section_head(plan: SectionFilePlan, encoded: Sequence[EncodedPiece]) -> bytes:
    """구간 파일의 머리를 만든다 — ftyp · moov · free · mdat 머리. 길이는 ``plan.body_offset``이다.

    받기와 인코딩이 끝난 뒤에 부른다. 다시 인코딩한 조각의 샘플(``EncodedPiece.payload``)은
    ``plan.encoded_offset``부터 encoded의 순서대로 이어 써야 한다.

    Args:
        encoded: ``plan.encoded_pieces``와 같은 순서 · 같은 프레임 수의 조각

    Raises:
        Mp4Error: 조각이 계획과 맞지 않거나(수 · 프레임 수 · timescale), 이음매에서 DTS가 뒤로
            가거나, SPS · PPS 번호가 원본과 겹치거나, moov가 잡아 둔 자리를 넘는
            경우(``MP4_INVALID``). 영상이 H.264가 아니면 ``MP4_UNSUPPORTED``
    """
    wanted = plan.encoded_pieces
    if len(encoded) != len(wanted):
        raise Mp4Error(MP4_INVALID, f"조각 {len(encoded)}개 · 계획 {len(wanted)}개")
    for piece, made in zip(wanted, encoded):
        if len(made.sizes) != piece.end - piece.first:
            raise Mp4Error(
                MP4_INVALID,
                f"{piece.kind} 조각의 프레임 {len(made.sizes)}개 · 계획 {piece.end - piece.first}개",
            )
        if len(made.payload) != sum(made.sizes):
            raise Mp4Error(MP4_INVALID, f"{piece.kind} 조각의 샘플 바이트가 크기 표와 맞지 않는다")
        if made.timescale != plan._video.timescale:
            raise Mp4Error(MP4_INVALID, f"{piece.kind} 조각의 timescale이 원본과 다르다")
    try:
        moov = _moov(plan, tuple(encoded))
    except (struct.error, IndexError) as e:
        raise Mp4Error(MP4_INVALID, str(e)) from e
    spare = plan.moov_reserved - len(moov)
    if spare < _FREE_HEADER_BYTES:
        raise Mp4Error(
            MP4_INVALID, f"moov {len(moov)}바이트가 잡아 둔 {plan.moov_reserved}바이트를 넘는다"
        )
    body = plan.moved_bytes + sum(len(made.payload) for made in encoded)
    return (
        SECTION_FILE_FTYP
        + moov
        + _box(b"free", bytes(spare - _FREE_HEADER_BYTES))
        + struct.pack(">I4sQ", 1, b"mdat", MDAT_HEADER_BYTES + body)
    )


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


def _packed(typecode: str, values) -> bytes:
    """정수들을 빅엔디언으로 이어 쓴다."""
    table = array.array(typecode, values)
    if sys.byteorder == "little":
        table.byteswap()
    return table.tobytes()


class _Runs:
    """(개수, 값) 구간을 이어 붙인다 — 이어지는 같은 값은 한 구간으로 합친다."""

    def __init__(self) -> None:
        self.counts = array.array("Q")
        self.values = array.array("q")

    def extend(self, counts, values) -> None:
        if not len(counts):
            return
        if self.counts and self.values[-1] == values[0]:
            self.counts[-1] += counts[0]
            counts, values = counts[1:], values[1:]
        self.counts.extend(array.array("Q", counts))
        self.values.extend(array.array("q", values))

    def extend_values(self, values: Sequence[int]) -> None:
        for value, group in groupby(values):
            self.extend([sum(1 for _ in group)], [value])

    def table(self, kind: bytes) -> bytes:
        """stts · ctts 상자. 음수가 있으면 버전 1(부호 있는 값)로 쓴다."""
        negative = bool(self.values) and min(self.values) < 0
        cells = array.array("I", bytes(8 * len(self.counts)))
        cells[0::2] = array.array("I", self.counts)
        cells[1::2] = array.array("I", (value & 0xFFFFFFFF for value in self.values) if negative else self.values)  # fmt: skip
        head = struct.pack(">BxxxI", 1 if negative else 0, len(self.counts))
        return _box(kind, head + _packed("I", cells))


def _moov(plan: SectionFilePlan, encoded: tuple[EncodedPiece, ...]) -> bytes:
    movie_duration = round((plan.end_time - plan.start_time) * plan._movie_timescale)
    parts = [_video_trak(plan, encoded, movie_duration)]
    if plan._audio is not None:
        parts.append(_audio_trak(plan, movie_duration))
    mvhd = _box(b"mvhd", _with_duration(plan._mvhd, movie_duration, 12))
    return _box(b"moov", mvhd + b"".join(parts))


def _with_duration(body: bytes, duration: int, at: int) -> bytes:
    """mvhd · mdhd(at=12) · tkhd(at=16) 본문의 길이 칸을 고친 본문을 돌려준다.

    at은 버전 0에서 길이 칸 앞의 칸(timescale 또는 예약)이 시작하는 자리다. 버전 0의 32비트
    칸에 담기지 않는 길이는 버전 1(64비트 시각 칸)로 바꿔 쓴다.
    """
    if body[0] == 1:
        patched = bytearray(body)
        struct.pack_into(">Q", patched, at + 12, duration)  # 시각 칸 둘이 8바이트씩이다
    elif duration < _U32_LIMIT:
        patched = bytearray(body)
        struct.pack_into(">I", patched, at + 4, duration)
    else:
        created, modified = struct.unpack_from(">II", body, 4)
        patched = bytearray(
            bytes([1]) + body[1:4] + struct.pack(">QQ", created, modified)
            + body[12 : at + 4] + struct.pack(">Q", duration) + body[at + 8 :]
        )  # fmt: skip
    return bytes(patched)


def _edit_list(movie_duration: int, media_time: int, empty: int = 0) -> bytes:
    """edts 상자 — 앞의 빈 편집(empty, 무비 틱)과 구간 하나."""
    rows = [(empty, -1)] if empty else []
    rows.append((movie_duration, media_time))
    if any(length >= _U32_LIMIT or time >= 1 << 31 for length, time in rows):
        payload = struct.pack(">BxxxI", 1, len(rows)) + b"".join(
            struct.pack(">QqI", length, time, 0x00010000) for length, time in rows
        )
    else:
        payload = struct.pack(">BxxxI", 0, len(rows)) + b"".join(
            struct.pack(">IiI", length, time, 0x00010000) for length, time in rows
        )
    return _box(b"edts", _box(b"elst", payload))


def _trak(
    part: _TrackPart, stsd: bytes, tables: bytes, media_duration: int, movie_duration: int, edits: bytes
) -> bytes:  # fmt: skip
    stbl = _box(b"stbl", _box(b"stsd", stsd) + tables)
    mdia = _box(
        b"mdia",
        _box(b"mdhd", _with_duration(part.mdhd, media_duration, 12))
        + _box(b"hdlr", part.hdlr)
        + _box(b"minf", part.minf_other + stbl),
    )
    tkhd = _box(b"tkhd", _with_duration(part.tkhd, movie_duration, 16))
    return _box(b"trak", tkhd + edits + mdia)


def _chunk_tables(positions, counts, body_offset: int) -> bytes:
    """stsc · co64 — 청크마다의 샘플 수와 위치."""
    # stsc는 샘플 수가 바뀌는 청크만 적는다(청크 번호는 1부터). 바뀌는 자리를 청크마다 파이썬
    # 코드를 돌지 않고 찾는다
    counts = array.array("I", counts)
    changes = array.array("I", [0] if counts else [])
    changes.extend(compress(range(1, len(counts)), map(ne, counts[1:], counts)))
    cells = array.array("I", bytes(12 * len(changes)))
    cells[0::3] = array.array("I", map(add, changes, repeat(1)))
    cells[1::3] = array.array("I", map(counts.__getitem__, changes))
    cells[2::3] = array.array("I", [1]) * len(changes)  # 샘플 설명은 늘 첫 번째 것이다
    stsc = struct.pack(">II", 0, len(changes)) + _packed("I", cells)
    offsets = array.array("Q", map(add, positions, repeat(body_offset)))
    return _box(b"stsc", stsc) + _box(
        b"co64", struct.pack(">II", 0, len(offsets)) + _packed("Q", offsets)
    )


def _video_trak(plan: SectionFilePlan, encoded: tuple[EncodedPiece, ...], movie_duration: int) -> bytes:  # fmt: skip
    part = plan._video
    moved = len(part.sizes)
    kinds = [piece.kind for piece in plan.encoded_pieces]
    # 조각을 원본의 시각 축에 올린다 — 조각에서 가장 먼저 표시되는 프레임이 원본의 그 프레임 시각에 온다
    shifted = []
    for made, source_pts in zip(encoded, plan._piece_pts):
        first_shown = min(map(add, made.decode_ticks, made.composition))
        shifted.append(source_pts - first_shown)
    before = [(made, shift) for made, shift, kind in zip(encoded, shifted, kinds) if kind != "tail"]
    after = [(made, shift) for made, shift, kind in zip(encoded, shifted, kinds) if kind == "tail"]

    deltas, composition = _Runs(), _Runs()
    sizes = array.array("I")
    sync = array.array("Q")
    positions, counts = array.array("Q"), array.array("I")
    encoded_position = plan.moved_bytes
    clock: int | None = None  # 지금까지 놓은 마지막 샘플의 DTS
    base: int | None = None  # 첫 샘플의 DTS
    last_delta = 0

    def put_piece(made: EncodedPiece, shift: int) -> None:
        nonlocal clock, base, last_delta, encoded_position
        first = made.decode_ticks[0] + shift
        if clock is not None:
            if first <= clock:
                raise Mp4Error(MP4_INVALID, "조각을 잇는 곳에서 DTS가 뒤로 간다")
            deltas.extend([1], [first - clock])
        base = first if base is None else base
        ticks = made.decode_ticks
        deltas.extend_values([b - a for a, b in zip(ticks, ticks[1:])])
        composition.extend_values(made.composition)
        sync.extend(len(sizes) + number for number in made.sync_samples)
        sizes.extend(made.sizes)
        positions.append(encoded_position)
        counts.append(len(made.sizes))
        encoded_position += len(made.payload)
        clock, last_delta = ticks[-1] + shift, made.last_delta

    for made, shift in before:
        put_piece(made, shift)
    if moved:
        if clock is not None:
            if part.first_dts <= clock:
                raise Mp4Error(MP4_INVALID, "머리 조각과 옮긴 영상 사이에서 DTS가 뒤로 간다")
            deltas.extend([1], [part.first_dts - clock])
        base = part.first_dts if base is None else base
        # 옮긴 샘플의 길이 — 마지막 샘플의 것은 다음에 오는 것(꼬리 조각)에 따라 다시 정한다
        moved_counts, moved_values = array.array("Q", part.delta_counts), part.delta_values
        last_delta = moved_values[-1]
        span = sum(map(mul, moved_counts, moved_values)) - last_delta
        moved_counts[-1] -= 1
        if moved_counts[-1] == 0:
            moved_counts, moved_values = moved_counts[:-1], moved_values[:-1]
        deltas.extend(moved_counts, moved_values)
        if part.composition_counts:
            composition.extend(part.composition_counts, part.composition_values)
        else:
            composition.extend([moved], [0])
        if part.sync_samples is not None:
            sync.extend(map(add, part.sync_samples, repeat(len(sizes))))
        sizes.extend(part.sizes)
        positions.extend(part.chunk_positions)
        counts.extend(part.chunk_counts)
        clock = part.first_dts + span
    for made, shift in after:
        put_piece(made, shift)
    if clock is None or base is None:
        # 영상 샘플이 없다 — 자리를 재려고 조각 없이 부른 경우뿐이다
        tables = (
            _Runs().table(b"stts")
            + _box(b"stss", struct.pack(">II", 0, 0))
            + _chunk_tables((), (), plan.body_offset)
            + _box(b"stsz", struct.pack(">III", 0, 0, 0))
        )
        return _trak(part, part.stsd, tables, 0, movie_duration, _edit_list(movie_duration, 0))
    deltas.extend([1], [last_delta])

    stsd = merged_sample_description(part.stsd, [made.stsd for made in encoded]) if encoded else part.stsd  # fmt: skip
    tables = deltas.table(b"stts")
    if any(composition.values):
        tables += composition.table(b"ctts")
    tables += _box(
        b"stss", struct.pack(">II", 0, len(sync)) + _packed("I", map(add, sync, repeat(1)))
    )
    tables += _chunk_tables(positions, counts, plan.body_offset)
    tables += _box(b"stsz", struct.pack(">III", 0, 0, len(sizes)) + _packed("I", sizes))
    media_duration = clock + last_delta - base
    edits = _edit_list(movie_duration, plan._first_pts - base)
    return _trak(part, stsd, tables, media_duration, movie_duration, edits)


def _audio_trak(plan: SectionFilePlan, movie_duration: int) -> bytes:
    part = plan._audio
    deltas, composition = _Runs(), _Runs()
    deltas.extend(part.delta_counts, part.delta_values)
    tables = deltas.table(b"stts")
    if part.composition_counts and any(part.composition_values):
        composition.extend(part.composition_counts, part.composition_values)
        tables += composition.table(b"ctts")
    tables += _chunk_tables(part.chunk_positions, part.chunk_counts, plan.body_offset)
    tables += _box(b"stsz", struct.pack(">III", 0, 0, len(part.sizes)) + _packed("I", part.sizes))
    media_duration = sum(map(mul, part.delta_counts, part.delta_values))
    lead = plan._audio_lead
    if lead >= 0:
        edits = _edit_list(movie_duration, lead)
    else:
        # 오디오가 구간 시작보다 늦게 시작한다 — 그만큼을 빈 편집으로 둔다
        empty = round(-lead * plan._movie_timescale / part.timescale)
        edits = _edit_list(max(movie_duration - empty, 0), 0, empty)
    return _trak(part, part.stsd, tables, media_duration, movie_duration, edits)


# ================================================================ 영상 설정 합치기

_VISUAL_ENTRY_FIXED_BYTES = 78  # 영상 샘플 엔트리에서 상자 머리 뒤, 하위 상자 앞의 고정 부분
_MAX_PARAMETER_SETS = 31  # avcC의 SPS 개수 칸은 5비트다


def merged_sample_description(original: bytes, extras: Sequence[bytes]) -> bytes:
    """원본 영상 샘플 설명(stsd 본문)의 avcC에 다시 인코딩한 조각들의 SPS · PPS를 더한다.

    Raises:
        Mp4Error: 샘플 설명이 H.264 하나가 아니면 ``MP4_UNSUPPORTED``. 조각의 SPS · PPS가
            원본 것과 내용이 다른데 번호가 같으면 ``MP4_INVALID`` — 그대로 쓰면 원본 샘플이
            조각의 설정으로 디코드된다
    """
    try:
        span, head, sps, pps, rest = _parameter_sets(original)
        for stsd in extras:
            _span, other_head, other_sps, other_pps, _rest = _parameter_sets(stsd)
            if other_head[4] != head[4]:
                raise Mp4Error(MP4_INVALID, "조각의 NAL 길이 칸이 원본과 다르다")
            sps += [item for item in other_sps if item not in sps]
            pps += [item for item in other_pps if item not in pps]
    except (struct.error, IndexError) as e:
        raise Mp4Error(MP4_INVALID, f"avcC를 읽지 못했다: {e}") from e
    for name, items, skip in (("SPS", sps, 4), ("PPS", pps, 1)):
        numbers = [_first_ue(item, skip) for item in items]
        if len(set(numbers)) != len(numbers):
            raise Mp4Error(MP4_INVALID, f"다른 {name}가 같은 번호 {sorted(numbers)}를 쓴다")
    if len(sps) > _MAX_PARAMETER_SETS or len(pps) > 255:
        raise Mp4Error(MP4_INVALID, "SPS · PPS가 avcC에 담을 수 있는 수를 넘는다")
    payload = (
        head
        + bytes([0xE0 | len(sps)])
        + b"".join(struct.pack(">H", len(item)) + item for item in sps)
        + bytes([len(pps)])
        + b"".join(struct.pack(">H", len(item)) + item for item in pps)
        + rest
    )
    avcc = _box(b"avcC", payload)
    box_start, box_end = span
    merged = bytearray(original[:box_start] + avcc + original[box_end:])
    # 샘플 엔트리 상자의 길이 칸 — 늘어난 만큼 더한다
    grown = len(avcc) - (box_end - box_start)
    struct.pack_into(">I", merged, 8, struct.unpack_from(">I", merged, 8)[0] + grown)
    return bytes(merged)


def _parameter_sets(stsd: bytes):
    """stsd 본문에서 avcC 상자의 (시작, 끝)과 (머리 5바이트, SPS 목록, PPS 목록, 나머지)를 읽는다."""
    if len(stsd) < 16 or struct.unpack_from(">I", stsd, 4)[0] != 1:
        raise Mp4Error(MP4_UNSUPPORTED, "영상 샘플 설명이 하나가 아니다")
    entry_size, entry_type = struct.unpack_from(">I4s", stsd, 8)
    if entry_type not in (b"avc1", b"avc3"):
        raise Mp4Error(MP4_UNSUPPORTED, f"영상이 H.264가 아니다 ({entry_type!r})")
    found = next(
        (
            (body - 8, body, end)
            for kind, body, end in _boxes(stsd, 16 + _VISUAL_ENTRY_FIXED_BYTES, 8 + entry_size)
            if kind == b"avcC"
        ),
        None,
    )
    if found is None:
        raise Mp4Error(MP4_INVALID, "영상 샘플 설명에 avcC가 없다")
    box_start, body, end = found
    payload = stsd[body:end]
    position, sps = 6, []
    for _ in range(payload[5] & 0x1F):
        length = struct.unpack_from(">H", payload, position)[0]
        sps.append(payload[position + 2 : position + 2 + length])
        position += 2 + length
    count, position, pps = payload[position], position + 1, []
    for _ in range(count):
        length = struct.unpack_from(">H", payload, position)[0]
        pps.append(payload[position + 2 : position + 2 + length])
        position += 2 + length
    return (box_start, end), payload[:5], sps, pps, payload[position:]


def _first_ue(nal: bytes, skip: int) -> int:
    """NAL에서 skip바이트 뒤의 첫 ue(v) 값 — SPS(4바이트 뒤) · PPS(1바이트 뒤)의 번호다."""
    bits = "".join(f"{byte:08b}" for byte in nal[skip : skip + 8])
    zeros = len(bits) - len(bits.lstrip("0"))
    if zeros * 2 + 1 > len(bits):
        raise Mp4Error(MP4_INVALID, "SPS · PPS의 번호를 읽지 못했다")
    return int(bits[zeros : zeros * 2 + 1], 2) - 1
