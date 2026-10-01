"""테스트용 합성 mp4 조립기 — 상자를 직접 써서 작은 mp4 바이트를 만든다 (#178).

제품 코드(core.api.mp4)를 쓰지 않는다. 조립기가 샘플을 어디에 놓았는지 스스로
기록해 두고, 테스트는 해석 결과를 그 기록과 대조한다.
"""

import struct
from dataclasses import dataclass, field


def box(kind: bytes, payload: bytes) -> bytes:
    """상자 하나 — 크기(4) + 종류(4) + 본문."""
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def full_box(kind: bytes, payload: bytes, version: int = 0) -> bytes:
    """버전·플래그 4바이트가 앞에 붙는 상자."""
    return box(kind, bytes([version, 0, 0, 0]) + payload)


@dataclass
class TrackSpec:
    """트랙 하나의 재료."""

    handler: bytes  # b"vide" 또는 b"soun"
    timescale: int
    deltas: list[int]  # 샘플별 길이(틱), 디코드 순서
    sizes: list[int]  # 샘플별 크기(바이트)
    chunks: list[int]  # 청크별 샘플 수
    composition: list[int] | None = None  # 샘플별 ctts(PTS − DTS). None이면 상자를 쓰지 않는다
    sync: list[int] | None = None  # stss 샘플 번호(1부터). None이면 상자를 쓰지 않는다
    edits: list[tuple[int, int]] | None = None  # elst (길이[무비 틱], media_time). None이면 없음
    uniform_size: bool = False  # stsz를 고정 크기 칸으로 쓴다(sizes가 모두 같아야 한다)


@dataclass
class BuiltMp4:
    """조립 결과와, 조립기가 기록한 샘플 배치."""

    data: bytes
    moov: bytes
    moov_offset: int
    mdat_offset: int  # mdat 상자 머리의 위치
    mdat_body: tuple[int, int]  # mdat 본문의 (시작, 끝)
    sample_offsets: dict[bytes, list[int]] = field(default_factory=dict)  # 트랙별 샘플 시작 위치

    def sample_bytes(self, handler: bytes, index: int, size: int) -> bytes:
        """조립기가 그 샘플 자리에 써 넣은 바이트."""
        return _payload(handler, index, size)


def _payload(handler: bytes, index: int, size: int) -> bytes:
    """샘플마다 다른 무늬 — 트랙 첫 글자와 샘플 번호를 반복한다."""
    return (bytes([handler[0], index % 251]) * size)[:size]


def _runs(values: list[int]) -> list[tuple[int, int]]:
    """같은 값이 이어지는 구간을 (개수, 값)으로 묶는다."""
    runs: list[tuple[int, int]] = []
    for value in values:
        if runs and runs[-1][1] == value:
            runs[-1] = (runs[-1][0] + 1, value)
        else:
            runs.append((1, value))
    return runs


def _trak(spec: TrackSpec, chunk_offsets: list[int], co64: bool) -> bytes:
    stts = full_box(b"stts", struct.pack(">I", len(_runs(spec.deltas)))
                    + b"".join(struct.pack(">II", n, v) for n, v in _runs(spec.deltas)))  # fmt: skip
    tables = [full_box(b"stsd", struct.pack(">I", 0)), stts]
    if spec.composition is not None:
        runs = _runs(spec.composition)
        tables.append(full_box(b"ctts", struct.pack(">I", len(runs))
                               + b"".join(struct.pack(">Ii", n, v) for n, v in runs)))  # fmt: skip
    if spec.sync is not None:
        tables.append(
            full_box(b"stss", struct.pack(f">I{len(spec.sync)}I", len(spec.sync), *spec.sync))
        )
    # stsc: 청크당 샘플 수가 바뀌는 청크만 적는다 (first_chunk는 1부터)
    stsc_rows = []
    for number, count in enumerate(spec.chunks, start=1):
        if not stsc_rows or stsc_rows[-1][1] != count:
            stsc_rows.append((number, count, 1))
    tables.append(full_box(b"stsc", struct.pack(">I", len(stsc_rows))
                           + b"".join(struct.pack(">III", *row) for row in stsc_rows)))  # fmt: skip
    if spec.uniform_size:
        tables.append(full_box(b"stsz", struct.pack(">II", spec.sizes[0], len(spec.sizes))))
    else:
        tables.append(
            full_box(
                b"stsz", struct.pack(f">II{len(spec.sizes)}I", 0, len(spec.sizes), *spec.sizes)
            )
        )
    if co64:
        tables.append(
            full_box(
                b"co64", struct.pack(f">I{len(chunk_offsets)}Q", len(chunk_offsets), *chunk_offsets)
            )
        )
    else:
        tables.append(
            full_box(
                b"stco", struct.pack(f">I{len(chunk_offsets)}I", len(chunk_offsets), *chunk_offsets)
            )
        )

    mdhd = full_box(
        b"mdhd", struct.pack(">IIII", 0, 0, spec.timescale, sum(spec.deltas)) + bytes(4)
    )
    hdlr = full_box(b"hdlr", bytes(4) + spec.handler + bytes(12) + b"\x00")
    mdia = box(b"mdia", mdhd + hdlr + box(b"minf", box(b"stbl", b"".join(tables))))
    parts = [full_box(b"tkhd", bytes(80))]
    if spec.edits is not None:
        rows = b"".join(
            struct.pack(">IiI", length, media_time, 0x00010000) for length, media_time in spec.edits
        )
        parts.append(box(b"edts", full_box(b"elst", struct.pack(">I", len(spec.edits)) + rows)))
    return box(b"trak", b"".join(parts) + mdia)


def build_mp4(
    tracks: list[TrackSpec],
    *,
    movie_timescale: int = 1000,
    moov_first: bool = True,
    co64: bool = False,
    mvex: bool = False,
    top_level_extra: bytes = b"",
    large_mdat_header: bool = False,
    include_moov: bool = True,
) -> BuiltMp4:
    """트랙 재료로 mp4 바이트를 만든다.

    mdat에는 트랙들의 청크를 번갈아(트랙 순서대로 한 청크씩) 놓는다.

    Args:
        moov_first: True면 ftyp · moov · mdat, False면 ftyp · mdat · moov 순서
        co64: 청크 위치를 64비트 co64로 쓴다
        mvex: moov 안에 mvex 상자를 넣는다(조각난 mp4 표시)
        top_level_extra: ftyp 바로 뒤에 넣을 최상위 상자 바이트(moof 등)
        large_mdat_header: mdat 머리를 64비트 크기(16바이트)로 쓴다
        include_moov: False면 moov를 넣지 않는다
    """
    ftyp = box(b"ftyp", b"isom" + bytes(4) + b"isomavc1") + top_level_extra
    mdat_header = 16 if large_mdat_header else 8

    # mdat 본문에 놓일 순서 — 트랙마다 청크를 하나씩 번갈아
    order: list[tuple[int, int]] = []
    for chunk in range(max(len(spec.chunks) for spec in tracks)):
        order += [(t, chunk) for t, spec in enumerate(tracks) if chunk < len(spec.chunks)]

    def layout(body_start: int):
        position = body_start
        chunk_offsets = [[0] * len(spec.chunks) for spec in tracks]
        sample_offsets = [[] for _ in tracks]
        body = b""
        first_sample = [[sum(spec.chunks[:c]) for c in range(len(spec.chunks))] for spec in tracks]
        for t, chunk in order:
            spec = tracks[t]
            chunk_offsets[t][chunk] = position
            start = first_sample[t][chunk]
            for index in range(start, start + spec.chunks[chunk]):
                sample_offsets[t].append(position)
                body += _payload(spec.handler, index, spec.sizes[index])
                position += spec.sizes[index]
        return chunk_offsets, sample_offsets, body

    def moov_for(chunk_offsets) -> bytes:
        mvhd = full_box(b"mvhd", struct.pack(">IIII", 0, 0, movie_timescale, 0) + bytes(80))
        traks = b"".join(_trak(spec, chunk_offsets[t], co64) for t, spec in enumerate(tracks))
        return box(b"moov", mvhd + traks + (box(b"mvex", bytes(8)) if mvex else b""))

    # moov의 크기는 위치 값과 무관하므로 자리만 잡아 크기를 먼저 구한다
    placeholder = moov_for([[0] * len(spec.chunks) for spec in tracks]) if include_moov else b""
    mdat_offset = len(ftyp) + (len(placeholder) if moov_first else 0)
    body_start = mdat_offset + mdat_header
    chunk_offsets, sample_offsets, body = layout(body_start)
    if large_mdat_header:
        mdat = struct.pack(">I4sQ", 1, b"mdat", 16 + len(body)) + body
    else:
        mdat = box(b"mdat", body)
    moov = moov_for(chunk_offsets) if include_moov else b""
    assert len(moov) == len(placeholder)

    data = ftyp + moov + mdat if moov_first else ftyp + mdat + moov
    built = BuiltMp4(
        data=data,
        moov=moov,
        moov_offset=len(ftyp) if moov_first else len(ftyp) + len(mdat),
        mdat_offset=mdat_offset,
        mdat_body=(body_start, body_start + len(body)),
    )
    for t, spec in enumerate(tracks):
        built.sample_offsets[spec.handler] = sample_offsets[t]
    return built


# ================================================================ 표준 재료


def video_spec(**overrides) -> TrackSpec:
    """10fps 영상 12샘플 — 표시 순서 I B B P 세 묶음, 디코드 순서 I P B B.

    timescale 1000, 샘플 길이 100. 재정렬 지연 1프레임(100틱)을 편집 목록의
    media_time이 지운다 → 표시 시각은 0.0, 0.1 … 1.1초.

    디코드 순서 → 표시 순서: 샘플 [0,1,2,3] = 프레임 [0,3,1,2] (묶음마다 반복)
    """
    spec = TrackSpec(
        handler=b"vide",
        timescale=1000,
        deltas=[100] * 12,
        sizes=[40 + 3 * n for n in range(12)],
        chunks=[3, 3, 2, 4],
        composition=[100, 300, 0, 0] * 3,
        sync=[1, 5, 9],
        edits=[(1200, 100)],
    )
    for name, value in overrides.items():
        setattr(spec, name, value)
    return spec


def audio_spec(**overrides) -> TrackSpec:
    """8000Hz 오디오 16샘플 — 샘플 길이 1024틱(0.128초), 첫 샘플 하나는 편집 목록이 가린다."""
    spec = TrackSpec(
        handler=b"soun",
        timescale=8000,
        deltas=[1024] * 16,
        sizes=[7] * 16,
        chunks=[4, 4, 4, 4],
        edits=[(1920, 1024)],
        uniform_size=True,
    )
    for name, value in overrides.items():
        setattr(spec, name, value)
    return spec
