"""fMP4(조각난 mp4) 세그먼트에서 프레임 시각·키프레임 읽기 (#309).

라이브 다시보기(m3u8 경로)의 세그먼트 형식이 HLS fMP4다. 암호화 VOD(hls_aes
경로)는 MPEG-TS라 ``core.api.mpegts``가 다룬다.

조각난 mp4는 샘플 표가 moov가 아니라 세그먼트마다의 moof에 있다. 그래서
``core.api.mp4.parse_moov``는 이 형식을 거부하고, 이 모듈이 따로 읽는다. 상자를
따라가는 코드와 방어 상한은 ``core.api.mp4``의 것을 그대로 쓴다.

- ``parse_init_segment`` — 초기화 세그먼트(``EXT-X-MAP``)의 moov에서 트랙의
  timescale·코덱·편집 목록·기본값(trex)을 읽는다
- ``parse_media_segment`` — 미디어 세그먼트의 moof에서 샘플별 DTS·PTS·크기·키프레임을
  읽는다. mdat 본문은 보지 않으므로 세그먼트의 앞부분만 줘도 된다
- ``read_media_segment`` — 읽기 함수로 세그먼트의 moof만 골라 읽어 같은 해석을 한다
- ``scan_moof`` — 세그먼트 앞부분만으로 위 해석이 되는지, moof가 어디서 끝나는지
- ``fmp4_origin`` · ``build_fmp4_index`` — 여러 세그먼트를 VOD 시작 = 0 기준의
  초 단위 색인으로 잇는다

bytes(또는 주입받은 읽기 함수)만 받는 순수 함수다. 실패는 ``core.api.mp4.Mp4Error``로 던진다.
"""

import struct
from collections.abc import Callable, Sequence
from fractions import Fraction

from core.api.mp4 import (
    _MAX_SAMPLES,
    MP4_INVALID,
    MP4_TOO_LONG,
    MP4_UNSUPPORTED,
    Mp4Error,
    _boxes,
    _declared_bitrate,
    _edit_list,
    _leaf_boxes,
    _timescale,
)
from core.models.fmp4_index import (
    Fmp4Index,
    Fmp4Init,
    Fmp4Samples,
    Fmp4Segment,
    Fmp4Track,
    MoofScan,
)

# tfhd 플래그 — 켜져 있으면 그 칸이 상자에 있다
_TFHD_BASE_DATA_OFFSET = 0x000001
_TFHD_SAMPLE_DESCRIPTION = 0x000002
_TFHD_DEFAULT_DURATION = 0x000008
_TFHD_DEFAULT_SIZE = 0x000010
_TFHD_DEFAULT_FLAGS = 0x000020

# trun 플래그 — 켜져 있으면 그 칸이 상자에(샘플마다) 있다
_TRUN_DATA_OFFSET = 0x000001
_TRUN_FIRST_SAMPLE_FLAGS = 0x000004
_TRUN_SAMPLE_DURATION = 0x000100
_TRUN_SAMPLE_SIZE = 0x000200
_TRUN_SAMPLE_FLAGS = 0x000400
_TRUN_SAMPLE_COMPOSITION = 0x000800

# 샘플 플래그의 sample_is_non_sync_sample 비트 — 꺼져 있으면 단독 디코드 가능(키프레임)
_SAMPLE_IS_NON_SYNC = 0x00010000

# 세그먼트 파일에서 moof만 골라 읽을 때(read_media_segment) mdat가 아닌 상자 하나로
# 받아들이는 최대 크기(바이트) — 16MB. 손상된 크기 칸을 믿고 통째로 읽지 않게 한다.
# moof를 범위 요청으로 읽을 때의 상한(core.api.hls_fmp4)과 같은 값이다
_MAX_KEPT_BOX_BYTES = 16 * 1024 * 1024

# 세그먼트 하나에서 따라가는 최상위 상자의 최대 수 — 손상된 파일에서 끝없이 돌지 않게 한다
_MAX_TOP_LEVEL_BOXES = 4096

# 샘플마다의 칸이 없는 trun이 한 세그먼트에서 선언할 수 있는 분량의 상한(초). 그런 trun은
# 상자 크기로 샘플 수를 묶을 수 없어, 기본 샘플 길이로 센 분량을 이 값으로 막는다.
# HLS 세그먼트는 수 초다(표본은 4초) — 그 30배다
_MAX_IMPLIED_SECONDS = 120


def parse_init_segment(data: bytes) -> Fmp4Init:
    """초기화 세그먼트 bytes에서 트랙 정보를 읽는다.

    영상 트랙과 오디오 트랙을 각각 첫 번째 것만 읽는다. 편집 목록은 "앞의 빈 편집 +
    구간 하나"만 받는다(``core.api.mp4``와 같은 규칙).

    Raises:
        Mp4Error: moov가 없거나 상자가 잘렸거나 영상과 오디오의 트랙 번호가 같은
            경우(``MP4_INVALID``), mvex가 없거나(조각난
            mp4의 초기화 세그먼트가 아니다) 영상 트랙이 없는 경우(``MP4_UNSUPPORTED``)
    """
    try:
        return _parse_init_segment(data)
    except (struct.error, IndexError) as e:
        raise Mp4Error(MP4_INVALID, str(e)) from e


def parse_media_segment(data: bytes, init: Fmp4Init) -> Fmp4Segment:
    """미디어 세그먼트 bytes의 moof에서 샘플별 DTS·PTS·길이·크기·키프레임을 읽는다.

    세그먼트 하나에 moof가 여럿이면 모두 읽어 이어 붙인다. mdat 본문은 보지 않는다 —
    bytes가 mdat 도중에 끝나도 그 앞의 moof는 읽는다(``scan_moof`` 참조).

    샘플 값은 trun에 있으면 그 값, 없으면 tfhd의 기본값, 그것도 없으면 초기화
    세그먼트의 trex 기본값이다. 첫 샘플의 플래그만은 trun의 first_sample_flags가
    있으면 그것이 앞선다.

    Args:
        data: 미디어 세그먼트(또는 그 앞부분)
        init: 같은 스트림의 ``parse_init_segment`` 결과

    Raises:
        Mp4Error: moof가 잘렸거나 trun의 샘플 수가 상자 크기를 넘는 경우(``MP4_INVALID``),
            tfdt가 없는 경우(``MP4_UNSUPPORTED``), 트랙의 샘플 수가 상한을 넘거나
            샘플마다의 칸이 없는 trun이 세그먼트 하나의 분량을 넘는 샘플 수를 선언한
            경우(``MP4_TOO_LONG``)
    """
    try:
        return _parse_media_segment(data, init)
    except (struct.error, IndexError) as e:
        raise Mp4Error(MP4_INVALID, str(e)) from e


def read_media_segment(read: Callable[[int, int], bytes], init: Fmp4Init) -> Fmp4Segment:
    """읽기 함수로 미디어 세그먼트의 moof만 읽어 ``parse_media_segment``와 같은 결과를 낸다.

    최상위 상자의 머리를 차례로 읽고 mdat의 본문은 건너뛴다 — 세그먼트가 아무리 커도
    메모리에 올라가는 것은 mdat가 아닌 상자들뿐이다. 받아 둔 세그먼트 파일에서 프레임
    정보를 다시 읽을 때 쓴다.

    Args:
        read: ``read(offset, size)`` — 세그먼트의 offset부터 최대 size바이트를 돌려준다.
            끝을 넘으면 있는 만큼만(없으면 빈 bytes) 돌려준다
        init: 같은 스트림의 ``parse_init_segment`` 결과

    Raises:
        Mp4Error: 상자 크기가 머리보다 작거나, mdat가 아닌 상자가 너무 크거나 잘렸거나,
            상자가 너무 많은 경우(``MP4_INVALID``). 그 밖은 ``parse_media_segment``와 같다
    """
    kept = []
    offset = 0
    for _ in range(_MAX_TOP_LEVEL_BOXES):
        head = read(offset, 16)
        if len(head) < 8:
            return parse_media_segment(b"".join(kept), init)
        size, box_type = struct.unpack_from(">I4s", head, 0)
        header = 8
        if size == 1:
            if len(head) < 16:
                raise Mp4Error(MP4_INVALID, f"상자 {box_type!r}의 머리가 잘렸다")
            size = struct.unpack_from(">Q", head, 8)[0]
            header = 16
        if box_type == b"mdat":
            if size == 0:  # "끝까지" — 그 뒤에는 아무것도 없다
                return parse_media_segment(b"".join(kept), init)
        elif size > _MAX_KEPT_BOX_BYTES:
            raise Mp4Error(MP4_INVALID, f"상자 {box_type!r}의 크기 {size}")
        if size < header:
            raise Mp4Error(MP4_INVALID, f"상자 {box_type!r}의 크기 {size}")
        if box_type != b"mdat":
            body = read(offset, size)
            if len(body) != size:
                raise Mp4Error(MP4_INVALID, f"상자 {box_type!r}가 잘렸다")
            kept.append(body)
        offset += size
    raise Mp4Error(MP4_INVALID, f"최상위 상자가 {_MAX_TOP_LEVEL_BOXES}개를 넘는다")


def scan_moof(data: bytes) -> MoofScan:
    """세그먼트의 앞부분 bytes에 첫 mdat 앞의 moof가 다 들어 있는지 본다.

    세그먼트는 보통 (styp · sidx 등) · moof · mdat 순서다. 프레임 시각은 moof에만
    있으므로, mdat가 시작하는 위치까지만 받으면 ``parse_media_segment``를 할 수 있다.

    - ``complete``가 참이면 ``data[:moof_end]``로 해석할 수 있다
    - 거짓이면 ``moof_end``까지(적어도 그만큼) 더 받아야 한다
    - ``next_offset``은 첫 mdat가 끝나는 위치다. 세그먼트가 그보다 길면 moof가 더
      있을 수 있다 — 그 위치부터 다시 받아 같은 방식으로 읽는다

    Raises:
        Mp4Error: 상자 크기가 머리보다 작은 경우(``MP4_INVALID``)
    """
    position = 0
    seen_moof = False
    while position + 8 <= len(data):
        size, box_type = struct.unpack_from(">I4s", data, position)
        header = 8
        if size == 1:
            if position + 16 > len(data):
                break
            size = struct.unpack_from(">Q", data, position + 8)[0]
            header = 16
        # 크기 0은 "끝까지"다. mdat에만 허용한다 — 그 뒤에는 아무것도 없다.
        # 머리보다 작은 크기는 mdat여도 거부한다. 그대로 두면 next_offset이 mdat 머리 안쪽을 가리킨다
        if size < header and not (size == 0 and box_type == b"mdat"):
            raise Mp4Error(MP4_INVALID, f"상자 {box_type!r}의 크기 {size}")
        if box_type == b"mdat":
            return MoofScan(seen_moof, position, position + size if size else None)
        seen_moof = seen_moof or box_type == b"moof"
        position += size
    # mdat 머리를 아직 못 봤다 — position은 다음에 읽어야 할 상자의 위치다
    return MoofScan(False, position, None)


def fmp4_origin(init: Fmp4Init, first: Fmp4Segment) -> Fraction:
    """VOD의 **첫 세그먼트**에서 가장 이른 표시 시각(초)을 구한다 — 이 값이 VOD 시작이다.

    편집 목록을 적용한 시각으로, 영상과 오디오를 통틀어 본다. 편집 목록이 가린
    샘플(PTS가 media_time보다 앞)은 세지 않는다.

    Raises:
        Mp4Error: 표시되는 샘플이 하나도 없는 경우(``MP4_INVALID``)
    """
    starts = []
    for track, samples in ((init.video, first.video), (init.audio, first.audio)):
        if track is None:
            continue
        shown = [pts for pts in samples.presentation_times if pts >= track.media_time]
        if shown:
            starts.append(_seconds(track, min(shown)))
    if not starts:
        raise Mp4Error(MP4_INVALID, "표시되는 샘플이 없다")
    return min(starts)


def build_fmp4_index(
    init: Fmp4Init, segments: Sequence[Fmp4Segment], origin: Fraction
) -> Fmp4Index:
    """세그먼트들을 이어 VOD 시작 = 0 기준의 초 단위 색인을 만든다.

    시각 = 빈 편집 + (PTS − media_time) ÷ timescale − origin. 편집 목록이 가린 영상
    샘플은 ``frame_pts``에 넣지 않는다.

    Args:
        init: 초기화 세그먼트
        segments: 재생 순서대로의 미디어 세그먼트(연속하지 않아도 된다)
        origin: VOD 시작의 시각(초) — 첫 세그먼트의 ``fmp4_origin``

    Raises:
        Mp4Error: 표시되는 영상 프레임이 없는 경우(``MP4_INVALID``)
    """
    video = init.video
    present: list[Fraction] = []
    decode: list[float] = []
    shown: list[bool] = []
    key_samples: set[int] = set()
    audio: list[float] = []
    for segment in segments:
        base = len(present)
        key_samples.update(base + sample for sample in segment.video.sync_samples)
        for dts, pts in zip(segment.video.decode_times, segment.video.presentation_times):
            present.append(_seconds(video, pts) - origin)
            decode.append(float(_seconds(video, dts) - origin))
            shown.append(pts >= video.media_time)
        if init.audio is not None:
            audio += [
                float(_seconds(init.audio, pts) - origin)
                for pts in segment.audio.presentation_times
            ]
    order = sorted(
        (sample for sample in range(len(present)) if shown[sample]), key=present.__getitem__
    )
    if not order:
        raise Mp4Error(MP4_INVALID, "표시되는 영상 프레임이 없다")
    return Fmp4Index(
        frame_pts=tuple(float(present[sample]) for sample in order),
        frame_samples=tuple(order),
        keyframes=tuple(number for number, sample in enumerate(order) if sample in key_samples),
        decode_times=tuple(decode),
        audio_pts=tuple(audio),
    )


# ================================================================ 내부


def _seconds(track: Fmp4Track, ticks: int) -> Fraction:
    """틱 단위 시각에 편집 목록을 적용해 초로 바꾼다."""
    return track.empty_edit + Fraction(ticks - track.media_time, track.timescale)


def _parse_init_segment(data: bytes) -> Fmp4Init:
    moov = next((span for kind, *span in _top_level(data) if kind == b"moov"), None)
    if moov is None:
        raise Mp4Error(MP4_INVALID, "초기화 세그먼트에 moov가 없다")

    movie_timescale = 0
    defaults: dict[int, tuple[int, int, int]] = {}  # track_id → (길이, 크기, 플래그)
    raw_tracks = []
    has_mvex = False
    for box_type, body, body_end in _boxes(data, moov[0], moov[1]):
        if box_type == b"mvhd":
            movie_timescale = _timescale(data, body)
        elif box_type == b"trak":
            raw_tracks.append((body, body_end))
        elif box_type == b"mvex":
            has_mvex = True
            for kind, trex, _trex_end in _boxes(data, body, body_end):
                if kind == b"trex":
                    track_id, _description, duration, size, flags = struct.unpack_from(
                        ">5I", data, trex + 4
                    )
                    defaults[track_id] = (duration, size, flags)
    if not has_mvex:
        raise Mp4Error(MP4_UNSUPPORTED, "mvex가 없다 — 조각난 mp4의 초기화 세그먼트가 아니다")
    if movie_timescale <= 0:
        raise Mp4Error(MP4_INVALID, "mvhd의 timescale이 없다")

    tracks: dict[str, Fmp4Track] = {}
    for body, body_end in raw_tracks:
        boxes = _leaf_boxes(data, body, body_end)
        if any(name not in boxes for name in (b"tkhd", b"mdhd", b"hdlr", b"stsd")):
            continue
        handler = data[boxes[b"hdlr"][0] + 8 : boxes[b"hdlr"][0] + 12]
        if handler not in (b"vide", b"soun"):
            continue
        tkhd = boxes[b"tkhd"][0]
        track_id = struct.unpack_from(">I", data, tkhd + (20 if data[tkhd] == 1 else 12))[0]
        timescale = _timescale(data, boxes[b"mdhd"][0])
        if timescale <= 0:
            raise Mp4Error(MP4_INVALID, "timescale이 0이다")
        empty_edit, media_time = _edit_list(data, boxes.get(b"elst"), movie_timescale)
        duration, size, flags = defaults.get(track_id, (0, 0, 0))
        # stsd: 버전·플래그(4) + 항목 수(4) + 첫 항목의 크기(4) 뒤가 4글자 코드다
        stsd = boxes[b"stsd"][0]
        tracks.setdefault(
            handler.decode("ascii"),
            Fmp4Track(
                track_id=track_id,
                handler=handler.decode("ascii"),
                codec=data[stsd + 12 : stsd + 16].decode("latin-1"),
                timescale=timescale,
                empty_edit=empty_edit,
                media_time=media_time,
                default_duration=duration,
                default_size=size,
                default_flags=flags,
                declared_bitrate=(
                    _declared_bitrate(data, boxes[b"stsd"]) if handler == b"soun" else None
                ),
            ),
        )
    if "vide" not in tracks:
        raise Mp4Error(MP4_UNSUPPORTED, "영상 트랙이 없다")
    init = Fmp4Init(video=tracks["vide"], audio=tracks.get("soun"))
    _require_distinct_tracks(init)
    return init


def _require_distinct_tracks(init: Fmp4Init) -> None:
    """영상과 오디오의 트랙 번호가 같으면 거부한다.

    미디어 세그먼트의 traf는 트랙 번호로 샘플의 주인을 가린다. 번호가 같으면 두
    트랙의 샘플이 한 그릇에 섞이고, 영상과 오디오가 같은 샘플을 돌려주게 된다.
    """
    if init.audio is not None and init.audio.track_id == init.video.track_id:
        raise Mp4Error(MP4_INVALID, f"영상과 오디오의 트랙 번호가 같다: {init.video.track_id}")


def _top_level(data: bytes):
    """최상위 상자를 (종류, 본문 시작, 본문 끝)으로 낸다. 본문이 bytes 밖으로 나가는 상자에서 멈춘다.

    세그먼트의 앞부분만 받은 경우 마지막 상자(mdat)는 잘려 있다 — 그 상자는 내지 않는다.
    """
    position = 0
    while position + 8 <= len(data):
        size, box_type = struct.unpack_from(">I4s", data, position)
        header = 8
        if size == 1:
            if position + 16 > len(data):
                return
            size = struct.unpack_from(">Q", data, position + 8)[0]
            header = 16
        elif size == 0:
            size = len(data) - position
        if size < header:
            raise Mp4Error(MP4_INVALID, f"상자 {box_type!r}의 크기 {size}")
        if position + size > len(data):
            if box_type == b"moof":
                raise Mp4Error(MP4_INVALID, "moof가 잘렸다")
            return
        yield box_type, position + header, position + size
        position += size


class _Run:
    """한 트랙의 샘플을 세그먼트 안에서 모으는 그릇."""

    def __init__(self, track: Fmp4Track):
        self.track = track
        self.limit = _MAX_SAMPLES[track.handler.encode("ascii")]
        self.implied = 0  # 샘플마다의 칸이 없는 trun이 선언한 샘플 수의 합
        self.decode_times: list[int] = []
        self.presentation_times: list[int] = []
        self.durations: list[int] = []
        self.sizes: list[int] = []
        self.sync_samples: list[int] = []

    def freeze(self) -> Fmp4Samples:
        """모은 샘플을 불변 모델로 돌려준다."""
        return Fmp4Samples(
            decode_times=tuple(self.decode_times),
            presentation_times=tuple(self.presentation_times),
            durations=tuple(self.durations),
            sizes=tuple(self.sizes),
            sync_samples=tuple(self.sync_samples),
        )


def _parse_media_segment(data: bytes, init: Fmp4Init) -> Fmp4Segment:
    _require_distinct_tracks(init)  # 직접 만든 Fmp4Init도 여기서 걸린다
    runs = {init.video.track_id: _Run(init.video)}
    if init.audio is not None:
        runs[init.audio.track_id] = _Run(init.audio)

    fragments = 0
    for box_type, body, body_end in _top_level(data):
        if box_type != b"moof":
            continue
        fragments += 1
        for kind, traf, traf_end in _boxes(data, body, body_end):
            if kind == b"traf":
                _read_track_fragment(data, traf, traf_end, runs)

    video = runs[init.video.track_id]
    audio = runs[init.audio.track_id] if init.audio is not None else _Run(init.video)
    return Fmp4Segment(video=video.freeze(), audio=audio.freeze(), fragments=fragments)


def _read_track_fragment(data: bytes, start: int, end: int, runs: dict[int, _Run]) -> None:
    """traf 하나 — tfhd의 기본값과 tfdt의 시작 시각으로 trun들의 샘플을 run에 더한다."""
    header = decode_time = None
    truns = []
    for kind, body, body_end in _boxes(data, start, end):
        if kind == b"tfhd":
            header = body
        elif kind == b"tfdt":
            decode_time = struct.unpack_from(">Q" if data[body] == 1 else ">I", data, body + 4)[0]
        elif kind == b"trun":
            truns.append((body, body_end))
    if header is None:
        raise Mp4Error(MP4_INVALID, "traf에 tfhd가 없다")
    flags = struct.unpack_from(">I", data, header)[0] & 0xFFFFFF
    run = runs.get(struct.unpack_from(">I", data, header + 4)[0])
    if run is None:
        return  # 영상·오디오가 아닌 트랙
    if decode_time is None:
        raise Mp4Error(MP4_UNSUPPORTED, "traf에 tfdt가 없다 — 조각의 시작 시각을 알 수 없다")

    # tfhd의 선택 칸은 플래그 순서대로 놓인다. 있으면 trex 기본값을 덮는다
    position = header + 8
    position += 8 if flags & _TFHD_BASE_DATA_OFFSET else 0
    position += 4 if flags & _TFHD_SAMPLE_DESCRIPTION else 0
    track = run.track
    default_duration, default_size, default_flags = (
        track.default_duration,
        track.default_size,
        track.default_flags,
    )
    if flags & _TFHD_DEFAULT_DURATION:
        default_duration = struct.unpack_from(">I", data, position)[0]
        position += 4
    if flags & _TFHD_DEFAULT_SIZE:
        default_size = struct.unpack_from(">I", data, position)[0]
        position += 4
    if flags & _TFHD_DEFAULT_FLAGS:
        default_flags = struct.unpack_from(">I", data, position)[0]

    for body, body_end in truns:
        decode_time = _read_run(
            data, body, body_end, run, decode_time, (default_duration, default_size, default_flags)
        )


def _read_run(
    data: bytes,
    body: int,
    body_end: int,
    run: _Run,
    decode_time: int,
    defaults: tuple[int, int, int],
) -> int:
    """trun 하나의 샘플을 run에 더하고, 이어지는 trun의 시작 DTS를 돌려준다."""
    default_duration, default_size, default_flags = defaults
    version = data[body]
    flags = struct.unpack_from(">I", data, body)[0] & 0xFFFFFF
    count = struct.unpack_from(">I", data, body + 4)[0]
    position = body + 8
    position += 4 if flags & _TRUN_DATA_OFFSET else 0
    first_flags = None
    if flags & _TRUN_FIRST_SAMPLE_FLAGS:
        first_flags = struct.unpack_from(">I", data, position)[0]
        position += 4

    has_duration = bool(flags & _TRUN_SAMPLE_DURATION)
    has_size = bool(flags & _TRUN_SAMPLE_SIZE)
    has_flags = bool(flags & _TRUN_SAMPLE_FLAGS)
    has_composition = bool(flags & _TRUN_SAMPLE_COMPOSITION)
    fields = has_duration + has_size + has_flags + has_composition

    # 펼치기 전에 개수를 묶는다 — 샘플마다 칸이 있으면 상자 크기가, 없으면 기본 샘플 길이로
    # 센 분량(세그먼트 길이 × 프레임률)이 막는다
    if count * fields * 4 > body_end - position:
        raise Mp4Error(MP4_INVALID, f"trun의 샘플 {count}개가 상자 크기를 넘는다")
    if not fields and count:
        if default_duration <= 0:
            raise Mp4Error(MP4_INVALID, "샘플마다의 칸이 없는 trun에 기본 샘플 길이가 없다")
        allowed = _MAX_IMPLIED_SECONDS * run.track.timescale // default_duration
        if run.implied + count > allowed:
            raise Mp4Error(
                MP4_TOO_LONG,
                f"{run.track.handler} 샘플 {run.implied + count}개 · "
                f"세그먼트 하나의 상한 {allowed}개({_MAX_IMPLIED_SECONDS}초)",
            )
        run.implied += count
    if len(run.decode_times) + count > run.limit:
        raise Mp4Error(
            MP4_TOO_LONG,
            f"{run.track.handler} 샘플 {len(run.decode_times) + count}개 · 상한 {run.limit}",
        )

    # composition offset은 버전 1에서 부호 있는 값이다
    layout = (
        ">"
        + "I" * (has_duration + has_size + has_flags)
        + ("i" if version else "I") * has_composition
    )
    values = struct.unpack_from(layout[0] + layout[1:] * count, data, position) if fields else ()
    for sample in range(count):
        row = iter(values[sample * fields : (sample + 1) * fields])
        duration = next(row) if has_duration else default_duration
        size = next(row) if has_size else default_size
        if has_flags:
            sample_flags = next(row)
        elif sample == 0 and first_flags is not None:
            sample_flags = first_flags
        else:
            sample_flags = default_flags
        composition = next(row) if has_composition else 0
        if not sample_flags & _SAMPLE_IS_NON_SYNC:
            run.sync_samples.append(len(run.decode_times))
        run.decode_times.append(decode_time)
        run.presentation_times.append(decode_time + composition)
        run.durations.append(duration)
        run.sizes.append(size)
        decode_time += duration
    return decode_time
