"""mp4의 moov 찾기·해석·받기 (#178).

인코딩 완료 VOD의 매니페스트는 mp4 주소 하나만 준다. 시각을 파일 위치로 바꿀
정보는 파일 안의 moov 상자에만 있으므로, 구간만 받으려면 moov를 먼저 읽어야 한다.

세 층으로 나뉜다.

- ``scan_top_level`` · ``parse_moov`` — bytes만 받는 순수 함수. 네트워크를 모른다
- ``read_mp4_index`` — "파일의 이 범위를 달라"는 읽기 함수를 주입받아 moov를 찾아
  해석한다. 상자 머리의 크기를 따라 건너뛰므로 moov가 mdat 뒤에 있어도 mdat
  본문은 읽지 않는다
- ``fetch_mp4_index`` — 읽기 함수를 HTTP 범위 요청으로 채운 것

조각난(fragmented) mp4는 지원하지 않는다. 샘플 표가 moov가 아니라 파일 곳곳의
moof에 흩어져 있어 이 방식으로 읽을 수 없다 — 조용히 틀린 색인을 만들지 않고
``Mp4Error``로 거부한다.

실패 키는 번역하지 않은 i18n 키 원문이며 번역은 앱 계층이 한다
(``MetadataError``와 같은 방식).
"""

import re
import struct
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from fractions import Fraction
from itertools import accumulate

from core.api.session import get_thread_session
from core.models.mp4_index import Mp4Index, Mp4Track

# 실패 키 — 번역하지 않은 i18n 키 원문
MP4_FRAGMENTED = "Fragmented MP4 is not supported"  # moof·mvex가 있다
MP4_MOOV_NOT_FOUND = "Video index not found"  # 파일 끝까지 moov가 없다
MP4_INVALID = "Video index is damaged"  # 상자가 잘렸거나 표끼리 샘플 수가 맞지 않는다
MP4_UNSUPPORTED = "Video index layout is not supported"  # 영상 트랙 없음·여러 구간 편집 목록 등
MP4_RANGE_NOT_SUPPORTED = "Server does not support partial download"  # 범위 요청에 206이 아니다
MP4_RANGE_MISMATCH = (
    "Server returned a different range than requested"  # 206인데 범위·길이가 다르다
)
MP4_TOO_LONG = "Video is too long to read its index"  # 트랙의 샘플 수가 상한을 넘는다

# 첫 범위 요청의 크기(바이트). moov가 파일 앞에 있고 이보다 작으면 요청 한 번으로 끝난다
# — 10분짜리 표본의 moov가 약 340KB였다
_FIRST_READ_BYTES = 1024 * 1024

# 상자 머리만 읽을 때의 요청 크기(바이트) — 머리는 최대 16바이트지만 moov가 바로
# 뒤따르는 작은 상자(free 등)까지 한 번에 지나가도록 넉넉히 읽는다
_HEADER_READ_BYTES = 64 * 1024

# moov로 받아들이는 최대 크기(바이트). 손상된 크기 칸을 믿고 파일을 통째로 받는 것을 막는다
_MAX_MOOV_BYTES = 256 * 1024 * 1024

# 최상위 상자를 따라가는 최대 걸음 수 — 손상된 파일에서 요청이 끝없이 이어지지 않게 한다
_MAX_SCAN_STEPS = 16

# 범위 요청의 타임아웃(초) — file 다운로더의 범위 요청과 같은 값
_REQUEST_TIMEOUT = 30

# 범위 응답 본문을 나눠 읽는 단위(바이트) — 요청한 크기를 넘는 본문은 이만큼만 더 읽고 버린다
_READ_CHUNK_BYTES = 64 * 1024

# 트랙 하나에서 받아들이는 최대 샘플 수 — 24시간 분량이다.
#   영상: 60fps × 86,400초 = 5,184,000프레임
#   오디오: 48,000Hz ÷ 1,024샘플(AAC 한 프레임) × 86,400초 = 4,050,000프레임
# 12시간짜리 영상(영상 약 259만 · 오디오 약 203만 프레임)의 두 배다. 샘플 표는 샘플마다
# 값을 펼쳐 들고 있어야 하므로, moov의 개수 칸을 그대로 믿으면 작은 파일이 수십억 개를
# 선언해 메모리를 채울 수 있다 — 펼치기 전에 이 값으로 막는다
_MAX_SAMPLES = {b"vide": 60 * 86_400, b"soun": 48_000 * 86_400 // 1_024}

# Content-Range 머리의 모양 — "bytes 시작-끝/전체". 전체는 모르면 "*"다
_CONTENT_RANGE = re.compile(r"\s*bytes\s+(\d+)-(\d+)/(\d+|\*)\s*")

_CONTAINERS = (b"trak", b"edts", b"mdia", b"minf", b"stbl")


class Mp4Error(Exception):
    """mp4 색인을 만들지 못했다.

    message_key는 번역하지 않은 i18n 키 원문이다(이 모듈의 ``MP4_*`` 상수).
    """

    def __init__(self, message_key: str, detail: str = ""):
        super().__init__(f"{message_key}: {detail}" if detail else message_key)
        self.message_key = message_key


@dataclass(frozen=True)
class TopLevelScan:
    """최상위 상자를 한 번 훑은 결과를 담는다."""

    moov: tuple[int, int] | None  # moov의 (파일 안 시작 위치, 크기). 못 찾았으면 None
    next_offset: int  # 아직 머리를 읽지 못한 다음 상자의 파일 안 위치
    reached_end: bool  # 크기 0(파일 끝까지) 상자를 만나 더 따라갈 상자가 없다


def scan_top_level(data: bytes, base_offset: int = 0) -> TopLevelScan:
    """bytes에 든 최상위 상자 머리를 차례로 읽어 moov의 위치를 찾는다.

    ``data``는 파일의 ``base_offset``부터 읽은 조각이고, 그 위치가 상자의 시작이어야
    한다. 상자 본문이 조각 밖으로 나가도 머리의 크기만으로 다음 상자 위치를 계산한다.
    moov를 찾으면 거기서 멈춘다.

    Raises:
        Mp4Error: moof를 만난 경우(``MP4_FRAGMENTED``), 상자 크기가 머리보다 작은
            경우(``MP4_INVALID``)
    """
    position = 0
    while position + 8 <= len(data):
        size, box_type = struct.unpack_from(">I4s", data, position)
        header = 8
        if size == 1:
            if position + 16 > len(data):
                break
            size = struct.unpack_from(">Q", data, position + 8)[0]
            header = 16
        if box_type == b"moof":
            raise Mp4Error(MP4_FRAGMENTED)
        if size == 0:
            # 크기 0은 "파일 끝까지"다. moov가 이 상자라면 크기를 알 수 없고,
            # 다른 상자라면 그 뒤에는 아무것도 없다
            return TopLevelScan(None, base_offset + position, reached_end=True)
        if size < header:
            raise Mp4Error(MP4_INVALID, f"상자 {box_type!r}의 크기 {size}")
        if box_type == b"moov":
            return TopLevelScan((base_offset + position, size), base_offset + position, False)
        position += size
    return TopLevelScan(None, base_offset + position, reached_end=False)


def read_mp4_index(read: Callable[[int, int], bytes]) -> Mp4Index:
    """읽기 함수로 파일에서 moov를 찾아 색인을 만든다.

    파일 앞부분부터 읽고, moov가 없으면 상자 크기를 따라 뒤로 건너뛴다(mdat 뒤의
    moov). 건너뛴 상자의 본문은 읽지 않는다. 결과의 ``moov_range``에 moov를 찾은
    위치를 싣는다.

    Args:
        read: ``read(offset, size)`` — 파일의 offset부터 최대 size바이트를 돌려준다.
            파일 끝을 넘으면 있는 만큼만(없으면 빈 bytes) 돌려준다

    Raises:
        Mp4Error: moov가 없거나, 조각난 mp4이거나, 색인이 손상된 경우
    """
    offset = 0
    request = _FIRST_READ_BYTES
    for _ in range(_MAX_SCAN_STEPS):
        data = read(offset, request)
        if len(data) < 8:
            break
        scan = scan_top_level(data, offset)
        if scan.moov is not None:
            moov_offset, moov_size = scan.moov
            if moov_size > _MAX_MOOV_BYTES:
                raise Mp4Error(MP4_INVALID, f"moov 크기 {moov_size}")
            start = moov_offset - offset
            moov = data[start : start + moov_size]
            if len(moov) < moov_size:
                moov += read(moov_offset + len(moov), moov_size - len(moov))
            if len(moov) < moov_size:
                raise Mp4Error(MP4_INVALID, "moov가 파일 끝에서 잘렸다")
            return replace(parse_moov(moov), moov_range=(moov_offset, moov_offset + moov_size - 1))
        if scan.reached_end or scan.next_offset <= offset:
            break
        offset, request = scan.next_offset, _HEADER_READ_BYTES
    raise Mp4Error(MP4_MOOV_NOT_FOUND)


def fetch_mp4_index(url: str) -> Mp4Index:
    """mp4 주소에서 범위 요청으로 moov만 받아 색인을 만든다.

    요청에는 쿠키를 싣지 않는다 — file 다운로더가 같은 주소를 받는 방식과 같다.

    서버의 응답을 그대로 믿지 않는다. 받는 양이 요청한 만큼으로 묶여 있어야
    서버가 무엇을 보내든 메모리에 올라가는 양이 정해진다.

    - 206이 아니면 본문을 읽지 않고 거부한다. 그대로 읽으면 파일 전체를 받게 된다
    - ``Content-Range``가 요청한 시작·끝과 같아야 한다. 끝은 파일이 요청보다 먼저
      끝나는 경우에만 파일의 마지막 바이트여도 된다
    - 본문은 나눠 읽고, ``Content-Range``가 말한 길이를 넘으면 그 자리에서 거부한다

    Raises:
        Mp4Error: 범위 요청 미지원, 요청과 다른 범위·길이의 응답, moov 없음, 조각난 mp4,
            손상된 색인
        requests.RequestException: 연결 실패·타임아웃·HTTP 오류 상태
    """

    def read(offset: int, size: int) -> bytes:
        last = offset + size - 1
        headers = {"Range": f"bytes={offset}-{last}"}
        with get_thread_session().get(
            url, headers=headers, stream=True, timeout=_REQUEST_TIMEOUT
        ) as response:
            if response.status_code == 416:
                return b""  # 파일 끝을 넘은 요청 — 읽기 함수 계약대로 빈 bytes
            response.raise_for_status()
            if response.status_code != 206:
                raise Mp4Error(MP4_RANGE_NOT_SUPPORTED, f"status {response.status_code}")
            expected = _granted_length(response.headers.get("Content-Range"), offset, last)
            body = bytearray()
            for chunk in response.iter_content(chunk_size=_READ_CHUNK_BYTES):
                body += chunk
                if len(body) > expected:
                    raise Mp4Error(MP4_RANGE_MISMATCH, f"본문이 {expected}바이트를 넘는다")
            if len(body) != expected:
                raise Mp4Error(MP4_RANGE_MISMATCH, f"본문 {len(body)}바이트 · 기대 {expected}")
            return bytes(body)

    return read_mp4_index(read)


def _granted_length(content_range: str | None, first: int, last: int) -> int:
    """Content-Range가 요청(first~last)과 맞는지 확인하고 본문의 길이를 돌려준다.

    시작은 요청과 같아야 한다. 끝은 요청과 같거나, 파일이 요청보다 먼저 끝나는
    경우(전체 크기 − 1이 요청한 끝보다 앞)에 한해 파일의 마지막 바이트여야 한다.

    Raises:
        Mp4Error: 머리가 없거나 모양이 다르거나 범위가 요청과 다른 경우
            (``MP4_RANGE_MISMATCH``)
    """
    match = _CONTENT_RANGE.fullmatch(content_range or "")
    if match is None:
        raise Mp4Error(MP4_RANGE_MISMATCH, f"Content-Range {content_range!r}")
    got_first, got_last = int(match.group(1)), int(match.group(2))
    file_last = None if match.group(3) == "*" else int(match.group(3)) - 1
    ends_at_file_end = file_last is not None and got_last == file_last and file_last < last
    if got_first != first or not (got_last == last or ends_at_file_end):
        raise Mp4Error(MP4_RANGE_MISMATCH, f"요청 {first}-{last} · Content-Range {content_range!r}")
    return got_last - first + 1


def parse_moov(moov: bytes) -> Mp4Index:
    """moov 상자 전체(머리 포함)를 해석해 색인을 만든다.

    영상 트랙과 오디오 트랙을 각각 첫 번째 것만 읽는다. 시각은 편집 목록을 적용한
    표시 시각이고, 두 트랙을 통틀어 가장 먼저 표시되는 샘플이 0이다.

    편집 목록은 "앞의 빈 편집 + 구간 하나"만 받는다. 구간의 길이(끝 자르기)는
    적용하지 않는다.

    Raises:
        Mp4Error: mvex가 있는 경우(``MP4_FRAGMENTED``), 상자가 잘렸거나 표의 샘플 수가
            서로 다른 경우(``MP4_INVALID``), 영상 트랙이 없거나 편집 목록에 구간이
            여럿인 경우(``MP4_UNSUPPORTED``), 트랙의 샘플 수가 상한을 넘는
            경우(``MP4_TOO_LONG``)
    """
    try:
        return _parse_moov(moov)
    except (struct.error, IndexError) as e:
        raise Mp4Error(MP4_INVALID, str(e)) from e


# ================================================================ 내부 — 상자 읽기


def _boxes(data: bytes, start: int, end: int) -> Iterator[tuple[bytes, int, int]]:
    """[start, end) 안의 상자를 (종류, 본문 시작, 본문 끝)으로 낸다."""
    position = start
    while position + 8 <= end:
        size, box_type = struct.unpack_from(">I4s", data, position)
        header = 8
        if size == 1:
            size = struct.unpack_from(">Q", data, position + 8)[0]
            header = 16
        elif size == 0:
            size = end - position
        if size < header or position + size > end:
            raise Mp4Error(MP4_INVALID, f"상자 {box_type!r}의 크기 {size}")
        yield box_type, position + header, position + size
        position += size


def _leaf_boxes(data: bytes, start: int, end: int) -> dict[bytes, tuple[int, int]]:
    """트랙 안의 상자를 펼쳐 {종류: (본문 시작, 본문 끝)}로 모은다. 같은 종류는 첫 것만."""
    found: dict[bytes, tuple[int, int]] = {}
    for box_type, body, body_end in _boxes(data, start, end):
        if box_type in _CONTAINERS:
            for key, span in _leaf_boxes(data, body, body_end).items():
                found.setdefault(key, span)
        else:
            found.setdefault(box_type, (body, body_end))
    return found


def _timescale(data: bytes, body: int) -> int:
    """mvhd·mdhd의 timescale을 읽는다(버전 0은 32비트, 1은 64비트 시각 칸)."""
    position = body + (20 if data[body] == 1 else 12)
    return struct.unpack_from(">I", data, position)[0]


def _entry_count(data: bytes, span: tuple[int, int], entry_bytes: int, at: int = 4) -> int:
    """표의 항목 수 칸(본문의 at 위치)을 읽고, 그만큼의 항목이 상자 안에 들어가는지 확인한다.

    개수 칸을 그대로 믿고 읽으면 상자 밖의 바이트를 표로 읽거나, 개수만큼의 형식
    문자열·리스트를 먼저 만들다 메모리가 찬다.
    """
    body, body_end = span
    count = struct.unpack_from(">I", data, body + at)[0]
    if count * entry_bytes > body_end - (body + at + 4):
        raise Mp4Error(MP4_INVALID, f"항목 {count}개가 상자 크기를 넘는다")
    return count


def _u32_table(data: bytes, span: tuple[int, int], columns: int) -> list[tuple[int, ...]]:
    """버전·플래그(4) + 항목 수(4) 뒤에 32비트 열이 columns개씩 놓인 표를 읽는다."""
    count = _entry_count(data, span, columns * 4)
    values = struct.unpack_from(f">{count * columns}I", data, span[0] + 8)
    return [values[i : i + columns] for i in range(0, len(values), columns)]


@dataclass
class _RawTrack:
    """표시 시각으로 바꾸기 전의 트랙 — 틱 단위."""

    handler: bytes
    timescale: int
    empty_edit: Fraction  # 앞의 빈 편집 길이(초)
    presented: list[int]  # 샘플별 (PTS − 편집 목록의 media_time), 틱
    decoded: list[int]  # 샘플별 (DTS − 편집 목록의 media_time), 틱
    deltas: list[int]  # 샘플별 길이, 틱
    offsets: list[int]
    sizes: list[int]
    chunk_starts: list[int]  # 청크마다의 첫 샘플 인덱스
    sync_samples: tuple[int, ...]

    def start(self) -> Fraction | None:
        """이 트랙에서 가장 먼저 표시되는 샘플의 시각(초). 표시되는 샘플이 없으면 None."""
        shown = [ticks for ticks in self.presented if ticks >= 0]
        if not shown:
            return None
        return self.empty_edit + Fraction(min(shown), self.timescale)


def _parse_moov(moov: bytes) -> Mp4Index:
    top = list(_boxes(moov, 0, len(moov)))
    if len(top) != 1 or top[0][0] != b"moov":
        raise Mp4Error(MP4_INVALID, "moov 상자가 아니다")
    _, moov_body, moov_end = top[0]

    movie_timescale = 0
    tracks: dict[bytes, _RawTrack] = {}
    for box_type, body, body_end in _boxes(moov, moov_body, moov_end):
        if box_type == b"mvex":
            raise Mp4Error(MP4_FRAGMENTED)
        if box_type == b"mvhd":
            movie_timescale = _timescale(moov, body)
        elif box_type == b"trak":
            if movie_timescale <= 0:
                raise Mp4Error(MP4_INVALID, "mvhd가 trak보다 앞에 없다")
            track = _parse_track(moov, body, body_end, movie_timescale)
            if track is not None:
                tracks.setdefault(track.handler, track)

    video = tracks.get(b"vide")
    if video is None:
        raise Mp4Error(MP4_UNSUPPORTED, "영상 트랙이 없다")
    audio = tracks.get(b"soun")

    video_start = video.start()
    if video_start is None:
        raise Mp4Error(MP4_UNSUPPORTED, "표시되는 영상 프레임이 없다")
    audio_start = audio.start() if audio else None
    origin = video_start if audio_start is None else min(video_start, audio_start)

    video_track = _to_track(video, origin)
    shown = sorted(
        (index for index, ticks in enumerate(video.presented) if ticks >= 0),
        key=lambda index: video.presented[index],
    )
    sync = set(video.sync_samples)
    modal_delta = Counter(video.deltas).most_common(1)[0][0]
    if modal_delta <= 0:
        raise Mp4Error(MP4_INVALID, "영상 샘플 길이가 0이다")
    return Mp4Index(
        frame_pts=tuple(video_track.times[index] for index in shown),
        frame_samples=tuple(shown),
        keyframes=tuple(number for number, index in enumerate(shown) if index in sync),
        duration=max(video_track.times[index] + video_track.durations[index] for index in shown),
        fps=Fraction(video.timescale, modal_delta),
        video=video_track,
        audio=_to_track(audio, origin) if audio else None,
    )


def _to_track(raw: _RawTrack, origin: Fraction) -> Mp4Track:
    """틱 단위 트랙을 초 단위 표시 시각으로 바꾼다."""
    shift = raw.empty_edit - origin
    return Mp4Track(
        timescale=raw.timescale,
        times=tuple(float(shift + Fraction(ticks, raw.timescale)) for ticks in raw.presented),
        decode_times=tuple(float(shift + Fraction(ticks, raw.timescale)) for ticks in raw.decoded),
        durations=tuple(delta / raw.timescale for delta in raw.deltas),
        offsets=tuple(raw.offsets),
        sizes=tuple(raw.sizes),
        chunk_starts=tuple(raw.chunk_starts),
        sync_samples=raw.sync_samples,
    )


def _parse_track(data: bytes, start: int, end: int, movie_timescale: int) -> _RawTrack | None:
    """trak 하나를 읽는다. 영상·오디오가 아니거나 샘플 표가 없으면 None."""
    boxes = _leaf_boxes(data, start, end)
    if b"hdlr" not in boxes or b"mdhd" not in boxes:
        return None
    hdlr_body = boxes[b"hdlr"][0]
    handler = data[hdlr_body + 8 : hdlr_body + 12]
    if handler not in (b"vide", b"soun"):
        return None
    required = (b"stts", b"stsc", b"stsz")
    if any(name not in boxes for name in required) or not (b"stco" in boxes or b"co64" in boxes):
        raise Mp4Error(MP4_INVALID, f"{handler!r} 트랙에 샘플 표가 없다")
    timescale = _timescale(data, boxes[b"mdhd"][0])
    if timescale <= 0:
        raise Mp4Error(MP4_INVALID, "timescale이 0이다")

    # 샘플 수는 stsz가 정한다. 다른 표는 펼치기 전에 개수 합이 이 값과 같은지부터 본다
    count = struct.unpack_from(">I", data, boxes[b"stsz"][0] + 8)[0]
    if count > _MAX_SAMPLES[handler]:
        raise Mp4Error(MP4_TOO_LONG, f"{handler!r} 샘플 {count}개 · 상한 {_MAX_SAMPLES[handler]}")
    sizes = _sample_sizes(data, boxes[b"stsz"], count)
    time_runs = _u32_table(data, boxes[b"stts"], 2)
    declared = sum(run for run, _delta in time_runs)
    if declared != count:
        raise Mp4Error(MP4_INVALID, f"stts {declared}개 · stsz {count}개")
    deltas = [delta for run, delta in time_runs for _ in range(run)]
    decode_times = [0, *accumulate(deltas)][:count]
    composition = _composition_offsets(data, boxes.get(b"ctts"), count)
    empty_edit, media_time = _edit_list(data, boxes.get(b"elst"), movie_timescale)
    if b"stss" in boxes:
        # stss의 샘플 번호는 1부터다
        sync_samples = tuple(row[0] - 1 for row in _u32_table(data, boxes[b"stss"], 1))
    else:
        sync_samples = tuple(range(count))  # stss가 없으면 모든 샘플이 단독 디코드 가능하다

    offsets, chunk_starts = _sample_offsets(data, boxes, sizes)
    return _RawTrack(
        handler=handler,
        timescale=timescale,
        empty_edit=empty_edit,
        presented=[dts + cts - media_time for dts, cts in zip(decode_times, composition)],
        decoded=[dts - media_time for dts in decode_times],
        deltas=deltas,
        offsets=offsets,
        sizes=sizes,
        chunk_starts=chunk_starts,
        sync_samples=sync_samples,
    )


def _sample_sizes(data: bytes, span: tuple[int, int], count: int) -> list[int]:
    """stsz — 샘플별 크기. 고정 크기 칸이 0이 아니면 모든 샘플이 그 크기다.

    count는 호출자가 상한을 확인한 샘플 수다. 고정 크기일 때는 표가 없어 상자 크기로
    개수를 가늠할 수 없으므로, 상한 확인이 유일한 방어다.
    """
    uniform = struct.unpack_from(">I", data, span[0] + 4)[0]
    if uniform:
        return [uniform] * count
    _entry_count(data, span, 4, at=8)
    return list(struct.unpack_from(f">{count}I", data, span[0] + 12))


def _composition_offsets(data: bytes, span: tuple[int, int] | None, count: int) -> list[int]:
    """ctts — 샘플별 (PTS − DTS). 상자가 없으면 전부 0이다."""
    if span is None:
        return [0] * count
    runs = _entry_count(data, span, 8)
    # 버전 0은 부호 없는 값이지만 음수를 넣는 파일이 있어 버전과 무관하게 부호 있는 값으로 읽는다
    values = struct.unpack_from(">" + "Ii" * runs, data, span[0] + 8)
    declared = sum(values[0::2])
    if declared != count:
        raise Mp4Error(MP4_INVALID, f"ctts {declared}개 · stsz {count}개")
    return [values[i + 1] for i in range(0, len(values), 2) for _ in range(values[i])]


def _edit_list(
    data: bytes, span: tuple[int, int] | None, movie_timescale: int
) -> tuple[Fraction, int]:
    """elst — (앞의 빈 편집 길이(초), 구간의 media_time(틱)). 상자가 없으면 (0, 0)이다."""
    if span is None:
        return Fraction(0), 0
    body = span[0]
    version = data[body]
    count = struct.unpack_from(">I", data, body + 4)[0]
    entry_format, entry_size = (">Qq", 20) if version == 1 else (">Ii", 12)
    empty = 0
    media_times = []
    for index in range(count):
        duration, media_time = struct.unpack_from(entry_format, data, body + 8 + index * entry_size)
        if media_time == -1:
            if media_times:
                raise Mp4Error(MP4_UNSUPPORTED, "편집 목록의 구간 뒤에 빈 편집이 있다")
            empty += duration
        else:
            media_times.append(media_time)
    if len(media_times) > 1:
        raise Mp4Error(MP4_UNSUPPORTED, f"편집 목록의 구간이 {len(media_times)}개다")
    return Fraction(empty, movie_timescale), media_times[0] if media_times else 0


def _sample_offsets(
    data: bytes, boxes: dict[bytes, tuple[int, int]], sizes: list[int]
) -> tuple[list[int], list[int]]:
    """stsc + stco/co64 + stsz — (샘플별 파일 안 시작 위치, 청크마다의 첫 샘플 인덱스).

    청크 하나에 샘플이 이어 붙어 있다. stsc는 "이 청크부터는 청크당 샘플이 몇 개"를
    구간으로 적고, stco/co64는 청크의 시작 위치를 적는다.
    """
    if b"co64" in boxes:
        chunk_count = _entry_count(data, boxes[b"co64"], 8)
        chunk_offsets = struct.unpack_from(f">{chunk_count}Q", data, boxes[b"co64"][0] + 8)
    else:
        chunk_offsets = tuple(row[0] for row in _u32_table(data, boxes[b"stco"], 1))
    runs = _u32_table(data, boxes[b"stsc"], 3)
    if not runs and sizes:
        raise Mp4Error(MP4_INVALID, "stsc가 비어 있다")

    offsets: list[int] = []
    chunk_starts: list[int] = []
    sample = 0
    for run_index, (first_chunk, samples_per_chunk, _description) in enumerate(runs):
        # first_chunk는 1부터다. 구간은 다음 구간의 first_chunk 직전 청크까지다
        last_chunk = runs[run_index + 1][0] - 1 if run_index + 1 < len(runs) else len(chunk_offsets)
        for chunk in range(first_chunk - 1, last_chunk):
            position = chunk_offsets[chunk]
            chunk_starts.append(sample)
            for _ in range(samples_per_chunk):
                if sample >= len(sizes):
                    raise Mp4Error(MP4_INVALID, "stsc의 샘플 수가 stsz보다 많다")
                offsets.append(position)
                position += sizes[sample]
                sample += 1
    if sample != len(sizes):
        raise Mp4Error(MP4_INVALID, f"stsc {sample}개 · stsz {len(sizes)}개")
    return offsets, chunk_starts
