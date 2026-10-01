"""테스트용 합성 fMP4 조립기 — 초기화 세그먼트와 미디어 세그먼트를 상자 단위로 만든다 (#309).

제품 코드(core.api.fmp4)를 쓰지 않는다. 상자 조립은 mp4_builder의 것을 쓴다.
"""

import struct
from dataclasses import dataclass, field

from tests.unit.core.mp4_builder import box, full_box

KEY = 0x02000000  # 샘플 플래그: 다른 샘플에 기대지 않는다, 단독 디코드 가능
NON_KEY = 0x01010000  # 샘플 플래그: 다른 샘플에 기댄다, 단독 디코드 불가(non-sync 비트)


@dataclass
class InitTrack:
    """초기화 세그먼트의 트랙 하나."""

    track_id: int
    handler: bytes  # b"vide" 또는 b"soun"
    timescale: int
    codec: bytes = b"avc1"
    edits: list[tuple[int, int]] | None = None  # elst (길이[무비 틱], media_time). None이면 없음
    trex: tuple[int, int, int] | None = (
        0,
        0,
        0,
    )  # (기본 길이, 기본 크기, 기본 플래그). None이면 trex 없음


@dataclass
class Sample:
    """trun의 샘플 하나. None인 칸은 trun에 쓰지 않는다(한 trun 안에서는 모든 샘플이 같아야 한다)."""

    duration: int | None = None
    size: int | None = None
    flags: int | None = None
    composition: int | None = None


@dataclass
class Run:
    """trun 하나."""

    samples: list[Sample]
    first_sample_flags: int | None = None
    version: int = 0  # 1이면 composition offset이 부호 있는 값이다


@dataclass
class Traf:
    """traf 하나."""

    track_id: int
    runs: list[Run]
    decode_time: int | None = 0  # tfdt의 시작 DTS. None이면 tfdt를 쓰지 않는다
    tfdt_version: int = 1
    default_duration: int | None = None  # tfhd의 기본값. None이면 그 칸을 쓰지 않는다
    default_size: int | None = None
    default_flags: int | None = None


@dataclass
class Fragment:
    """moof 하나와 그 뒤의 mdat."""

    trafs: list[Traf]
    mdat_size: int = 64  # mdat 본문 바이트 수
    extra: bytes = field(default=b"")  # moof 안 맨 뒤에 넣을 상자 bytes


def init_segment(
    tracks: list[InitTrack], *, movie_timescale: int = 1000, mvex: bool = True
) -> bytes:
    """ftyp · moov(mvhd · trak… · mvex)로 된 초기화 세그먼트."""
    mvhd = full_box(b"mvhd", struct.pack(">IIII", 0, 0, movie_timescale, 0) + bytes(80))
    traks = b""
    trexes = b""
    for track in tracks:
        tkhd = full_box(b"tkhd", struct.pack(">III", 0, 0, track.track_id) + bytes(68))
        parts = [tkhd]
        if track.edits is not None:
            rows = b"".join(
                struct.pack(">IiI", length, media_time, 0x00010000)
                for length, media_time in track.edits
            )
            parts.append(
                box(b"edts", full_box(b"elst", struct.pack(">I", len(track.edits)) + rows))
            )
        mdhd = full_box(b"mdhd", struct.pack(">IIII", 0, 0, track.timescale, 0) + bytes(4))
        hdlr = full_box(b"hdlr", bytes(4) + track.handler + bytes(12) + b"\x00")
        # stsd: 항목 수 1 + 샘플 엔트리(크기 · 4글자 코드 · 나머지)
        entry = struct.pack(">I4s", 16, track.codec) + bytes(8)
        stsd = full_box(b"stsd", struct.pack(">I", 1) + entry)
        empty = b"".join(
            full_box(kind, struct.pack(">I", 0)) for kind in (b"stts", b"stsc", b"stco")
        )
        stbl = box(b"stbl", stsd + empty + full_box(b"stsz", struct.pack(">II", 0, 0)))
        parts.append(box(b"mdia", mdhd + hdlr + box(b"minf", stbl)))
        traks += box(b"trak", b"".join(parts))
        if track.trex is not None:
            trexes += full_box(b"trex", struct.pack(">5I", track.track_id, 1, *track.trex))
    moov = box(b"moov", mvhd + traks + (box(b"mvex", trexes) if mvex else b""))
    return box(b"ftyp", b"iso5" + bytes(4) + b"iso5iso6mp41") + moov


def _trun(run: Run) -> bytes:
    first = run.samples[0] if run.samples else Sample()
    flags = 0
    flags |= 0x000004 if run.first_sample_flags is not None else 0
    flags |= 0x000100 if first.duration is not None else 0
    flags |= 0x000200 if first.size is not None else 0
    flags |= 0x000400 if first.flags is not None else 0
    flags |= 0x000800 if first.composition is not None else 0
    body = struct.pack(">I", len(run.samples))
    if run.first_sample_flags is not None:
        body += struct.pack(">I", run.first_sample_flags)
    for sample in run.samples:
        if sample.duration is not None:
            body += struct.pack(">I", sample.duration)
        if sample.size is not None:
            body += struct.pack(">I", sample.size)
        if sample.flags is not None:
            body += struct.pack(">I", sample.flags)
        if sample.composition is not None:
            body += struct.pack(">i" if run.version else ">I", sample.composition)
    return box(b"trun", bytes([run.version]) + flags.to_bytes(3, "big") + body)


def _traf(traf: Traf) -> bytes:
    flags = 0x020000  # default-base-is-moof
    body = struct.pack(">I", traf.track_id)
    for bit, value in (
        (0x000008, traf.default_duration),
        (0x000010, traf.default_size),
        (0x000020, traf.default_flags),
    ):
        if value is not None:
            flags |= bit
            body += struct.pack(">I", value)
    tfhd = box(b"tfhd", bytes([0]) + flags.to_bytes(3, "big") + body)
    tfdt = b""
    if traf.decode_time is not None:
        tfdt = full_box(
            b"tfdt",
            struct.pack(">Q" if traf.tfdt_version else ">I", traf.decode_time),
            traf.tfdt_version,
        )
    return box(b"traf", tfhd + tfdt + b"".join(_trun(run) for run in traf.runs))


def moof(fragment: Fragment, sequence: int = 1) -> bytes:
    """moof 상자 하나."""
    mfhd = full_box(b"mfhd", struct.pack(">I", sequence))
    return box(b"moof", mfhd + b"".join(_traf(traf) for traf in fragment.trafs) + fragment.extra)


def media_segment(fragments: list[Fragment], *, styp: bool = True) -> bytes:
    """(styp) · moof · mdat · moof · mdat … 로 된 미디어 세그먼트."""
    data = box(b"styp", b"msdh" + bytes(4) + b"msdhmsix") if styp else b""
    for number, fragment in enumerate(fragments, start=1):
        data += moof(fragment, number) + box(b"mdat", b"\xdd" * fragment.mdat_size)
    return data
