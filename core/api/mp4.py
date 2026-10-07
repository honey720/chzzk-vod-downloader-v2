"""mp4의 moov 찾기·해석·받기 (#178).

인코딩 완료 VOD의 매니페스트는 mp4 주소 하나만 준다. 시각을 파일 위치로 바꿀
정보는 파일 안의 moov 상자에만 있으므로, 구간만 받으려면 moov를 먼저 읽어야 한다.

세 층으로 나뉜다.

- ``scan_top_level`` · ``parse_moov`` — bytes만 받는 순수 함수. 네트워크를 모른다
- ``read_mp4_head`` · ``read_mp4_index`` — "파일의 이 범위를 달라"는 읽기 함수를
  주입받아 moov를 찾아 해석한다. 상자 머리의 크기를 따라 건너뛰므로 moov가 mdat
  뒤에 있어도 mdat 본문은 읽지 않는다. ``read_mp4_head``는 그때 받은 바이트도 돌려준다
- ``fetch_mp4_head`` · ``fetch_mp4_index`` — 읽기 함수를 HTTP 범위 요청으로 채운 것

조각난(fragmented) mp4는 지원하지 않는다. 샘플 표가 moov가 아니라 파일 곳곳의
moof에 흩어져 있어 이 방식으로 읽을 수 없다 — 조용히 틀린 색인을 만들지 않고
``Mp4Error``로 거부한다.

실패 키는 번역하지 않은 i18n 키 원문이며 번역은 앱 계층이 한다
(``MetadataError``와 같은 방식).
"""

import array
import re
from bisect import bisect_right
import struct
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from fractions import Fraction
from itertools import accumulate, chain, compress, islice, repeat
from operator import add, le, mul, sub, truediv

from core.api.session import get_thread_session
from core.models.mp4_index import (
    Mp4Head,
    Mp4Index,
    Mp4Raw,
    Mp4Summary,
    Mp4Track,
    PendingMp4Head,
)
from core.models.sample_column import (
    CHUNK_ITEMS,
    array_in_chunks,
    count_column,
    fill_in_chunks,
    float_column,
    in_chunks,
    offset_column,
)

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

# 오디오 샘플 엔트리(mp4a 등)에서 머리(8바이트) 뒤 자식 상자가 시작하기까지의 고정 칸
# 길이(바이트), 엔트리 버전별. 버전 0이 28이고 QuickTime의 버전 1은 16, 버전 2는 36이 더 있다
_AUDIO_ENTRY_FIXED_BYTES = {0: 28, 1: 44, 2: 64}

# esds 안 서술자의 꼬리표 (ISO/IEC 14496-1)
_ES_DESCRIPTOR = 0x03
_DECODER_CONFIG_DESCRIPTOR = 0x04


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
    return read_mp4_head(read).index


def read_mp4_head(read: Callable[[int, int], bytes]) -> Mp4Head:
    """읽기 함수로 파일에서 moov를 찾아 색인을 만들고, 그때 받은 앞부분의 바이트도 돌려준다 (#309).

    ``read_mp4_index``와 같은 순서로 읽는다. moov가 첫 읽기 안에서 시작하면 파일의
    0부터 moov 끝까지를 ``data``에 싣는다 — 부분 mp4를 만드는 쪽이 moov를 다시 받지
    않아도 된다. 그렇지 않으면(mdat 뒤의 moov 등) ``data``는 None이다.

    Raises:
        Mp4Error: moov가 없거나, 조각난 mp4이거나, 색인이 손상된 경우
    """
    return index_mp4(read_mp4_raw(read))


def read_mp4_raw(read: Callable[[int, int], bytes]) -> Mp4Raw:
    """읽기 함수로 파일에서 moov를 찾아 그 바이트를 돌려준다 — 해석하지 않는다 (#309).

    읽는 순서는 ``read_mp4_index``와 같다. 받은 것을 ``summarize_mp4``(프레임률 · 길이만) ·
    ``index_mp4``(색인)에 넘겨 쓴다.

    Raises:
        Mp4Error: moov가 없거나 잘린 경우
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
            return Mp4Raw(
                moov=moov,
                moov_range=(moov_offset, moov_offset + moov_size - 1),
                prefix=data[:start] if offset == 0 else None,
            )
        if scan.reached_end or scan.next_offset <= offset:
            break
        offset, request = scan.next_offset, _HEADER_READ_BYTES
    raise Mp4Error(MP4_MOOV_NOT_FOUND)


def index_mp4(raw: Mp4Raw) -> Mp4Head:
    """받아 둔 moov를 해석해 색인과 파일 앞부분의 바이트를 돌려준다 (#309).

    Raises:
        Mp4Error: 조각난 mp4이거나 색인이 손상된 경우
    """
    parse_started = time.perf_counter()
    parsed = parse_moov(raw.moov)
    parse_seconds = time.perf_counter() - parse_started
    return Mp4Head(
        index=replace(parsed, moov_range=raw.moov_range),
        data=raw.prefix + raw.moov if raw.prefix is not None else None,
        parse_seconds=parse_seconds,
    )


def pending_mp4_head(raw: Mp4Raw) -> PendingMp4Head:
    """받아 둔 moov를 필요할 때 한 번만 해석하는 묶음으로 싼다 (#309) — ``PendingMp4Head``."""
    # 모듈 전역을 호출 시점에 조회한다 — 테스트의 monkeypatch 지점
    # 기억하는 실패는 Mp4Error뿐이다 — moov가 틀린 것이라 다시 해석해도 같다
    return PendingMp4Head(raw, lambda held: index_mp4(held), permanent=(Mp4Error,))


def summarize_mp4(raw: Mp4Raw) -> Mp4Summary:
    """받아 둔 moov에서 프레임률 · 길이만 읽는다 — ``summarize_moov``."""
    return summarize_moov(raw.moov)


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
    return fetch_mp4_head(url).index


def _range_reader(url: str) -> Callable[[int, int], bytes]:
    """그 주소를 HTTP 범위 요청으로 읽는 읽기 함수 — 응답을 검사한다(``fetch_mp4_index`` 참고)."""

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

    return read


def fetch_mp4_head(url: str) -> Mp4Head:
    """mp4 주소에서 범위 요청으로 moov를 받아 색인과 받은 바이트를 돌려준다 (#309).

    요청·검사는 ``fetch_mp4_index``와 같다. 구간 다운로드처럼 moov의 바이트가 다시
    필요한 쪽이 쓴다 — 결과를 넘겨 쓰면 moov를 한 번만 받는다.

    Raises:
        Mp4Error: ``fetch_mp4_index``와 같다
        requests.RequestException: 연결 실패·타임아웃·HTTP 오류 상태
    """
    return read_mp4_head(_range_reader(url))


def fetch_mp4_raw(url: str) -> Mp4Raw:
    """mp4 주소에서 범위 요청으로 moov의 바이트만 받는다 — 해석하지 않는다 (#309).

    요청·검사는 ``fetch_mp4_index``와 같다.

    Raises:
        Mp4Error: 범위 요청 미지원, 요청과 다른 범위·길이의 응답, moov 없음
        requests.RequestException: 연결 실패·타임아웃·HTTP 오류 상태
    """
    return read_mp4_raw(_range_reader(url))


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
    except (struct.error, IndexError, ValueError, OverflowError) as e:
        raise Mp4Error(MP4_INVALID, str(e)) from e


# ================================================================ 프레임률 · 길이만 읽기 (#309)

# 가볍게 읽기를 포기하고 전체 해석으로 넘어가는 한계. 값은 어느 쪽이든 같다 — 이 한계는 속도만 가른다
_SUMMARY_MAX_RUNS = 50_000  # stts의 구간 수 — 샘플 길이가 거의 매번 바뀌는 영상
_SUMMARY_MAX_SCAN = 4_096  # 샘플을 하나씩 들여다보는 횟수


class _NeedsFullParse(Exception):
    """가볍게 읽을 수 없는 모양이다 — 전체 해석으로 같은 값을 구한다."""


@dataclass
class _TimeTrack:
    """트랙의 시각 표만 — 구간(개수, 값) 그대로. 샘플마다 펴지 않는다."""

    handler: bytes
    timescale: int
    empty_edit: Fraction  # 앞의 빈 편집 길이(초)
    media_time: int  # 편집 목록의 media_time(틱)
    count: int  # 샘플 수(stsz)
    runs: array.array  # stts의 구간마다의 샘플 수
    deltas: array.array  # stts의 구간마다의 샘플 길이(틱)
    composition_runs: array.array | None  # ctts의 구간마다의 샘플 수. 상자가 없으면 None
    composition: array.array | None  # ctts의 구간마다의 (PTS − DTS)
    _ends: array.array | None = None  # ctts 구간이 끝나는 샘플 번호의 누적 — 필요할 때 만든다

    def composition_at(self, sample: int) -> int:
        """그 샘플의 (PTS − DTS)."""
        if self.composition is None:
            return 0
        if len(self.composition) == self.count:
            return self.composition[sample]  # 구간마다 샘플 하나 — 번호가 곧 자리다
        if self._ends is None:
            self._ends = array_in_chunks("q", accumulate(self.composition_runs))
        return self.composition[bisect_right(self._ends, sample)]

    def spread(self) -> tuple[int, int]:
        """(PTS − DTS)의 (가장 작은 값, 가장 큰 값)."""
        if not self.composition:
            return 0, 0
        return (
            min(map(min, in_chunks(self.composition))),
            max(map(max, in_chunks(self.composition))),
        )


def summarize_moov(moov: bytes) -> Mp4Summary:
    """moov에서 프레임률 · 영상 길이 · 영상 샘플 수만 읽는다 — 색인을 만들지 않는다 (#309).

    ``parse_moov``가 만든 색인의 ``fps`` · ``duration``과 **비트까지 같은 값**을 돌려준다.
    샘플마다 값을 펴지 않고 stts · ctts의 구간(개수, 값)에서 구한다.

    - 프레임률: timescale ÷ 가장 많은 샘플 길이. 구간의 개수를 길이마다 더해 센다
    - 길이: 표시되는 샘플 가운데 (표시 시각 + 그 샘플의 길이)가 가장 큰 값. 표시 시각과 길이는
      각각 float로 반올림한 뒤 더하므로, 수학적으로 가장 늦게 끝나는 샘플이 float로도 가장
      크다고 할 수 없다. 대신 float 덧셈은 두 값에 대해 줄지 않으므로, **샘플 길이마다 표시
      시각이 가장 늦은 샘플**만 견주면 전체의 최댓값이 나온다. 그 샘플은 그 길이의 마지막
      샘플에서 (PTS − DTS)의 폭만큼 앞까지 안에 있다 — 그 안만 들여다본다
    - 0이 되는 시각(가장 먼저 표시되는 샘플)은 트랙의 앞에서부터 찾는다

    가볍게 읽을 수 없는 모양(샘플 길이가 거의 매번 바뀌는 영상 등)이면 ``parse_moov``로 같은 값을
    구한다. 샘플 위치 표(stsc · stco)는 읽지 않는다 — 그 표의 손상은 여기서 드러나지 않고 색인을
    만들 때 드러난다.

    Raises:
        Mp4Error: ``parse_moov``와 같다(샘플 위치 표의 손상은 빼고)
    """
    try:
        return _summarize(moov)
    except _NeedsFullParse:
        index = parse_moov(moov)
        return Mp4Summary(fps=index.fps, duration=index.duration, frames=len(index.video.sizes))
    except (struct.error, IndexError, ValueError, OverflowError) as e:
        raise Mp4Error(MP4_INVALID, str(e)) from e


def _summarize(moov: bytes) -> Mp4Summary:
    top = list(_boxes(moov, 0, len(moov)))
    if len(top) != 1 or top[0][0] != b"moov":
        raise Mp4Error(MP4_INVALID, "moov 상자가 아니다")
    _, moov_body, moov_end = top[0]
    movie_timescale = 0
    tracks: dict[bytes, _TimeTrack] = {}
    for box_type, body, body_end in _boxes(moov, moov_body, moov_end):
        if box_type == b"mvex":
            raise Mp4Error(MP4_FRAGMENTED)
        if box_type == b"mvhd":
            movie_timescale = _timescale(moov, body)
        elif box_type == b"trak":
            if movie_timescale <= 0:
                raise Mp4Error(MP4_INVALID, "mvhd가 trak보다 앞에 없다")
            track = _time_track(moov, body, body_end, movie_timescale)
            if track is not None:
                tracks.setdefault(track.handler, track)
    video = tracks.get(b"vide")
    if video is None:
        raise Mp4Error(MP4_UNSUPPORTED, "영상 트랙이 없다")
    audio = tracks.get(b"soun")

    video_first = _first_shown(video)
    if video_first is None:
        raise Mp4Error(MP4_UNSUPPORTED, "표시되는 영상 프레임이 없다")
    origin = video.empty_edit + Fraction(video_first, video.timescale)
    audio_first = _first_shown(audio) if audio else None
    if audio_first is not None:
        origin = min(origin, audio.empty_edit + Fraction(audio_first, audio.timescale))

    # 가장 많은 샘플 길이 — 같은 수면 먼저 나온 길이(Counter.most_common과 같은 순서)
    seen: dict[int, int] = {}
    for run, delta in zip(video.runs, video.deltas):
        if run:
            seen[delta] = seen.get(delta, 0) + run
    modal_delta = max(seen.items(), key=lambda entry: entry[1])[0]
    if modal_delta <= 0:
        raise Mp4Error(MP4_INVALID, "영상 샘플 길이가 0이다")

    # 표시 시각(초) = (offset + ticks × step) ÷ scale — 색인의 식(_to_track) 그대로다
    shift = video.empty_edit - origin
    offset = shift.numerator * video.timescale
    step = shift.denominator
    scale = shift.denominator * video.timescale
    ends = [
        (offset + ticks * step) / scale + delta / video.timescale
        for delta, ticks in _latest_shown_by_delta(video).items()
    ]
    return Mp4Summary(
        fps=Fraction(video.timescale, modal_delta), duration=max(ends), frames=video.count
    )


def _time_track(data: bytes, start: int, end: int, movie_timescale: int) -> _TimeTrack | None:
    """trak 하나의 시각 표를 읽는다. 영상·오디오가 아니거나 샘플 표가 없으면 None."""
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
    count = struct.unpack_from(">I", data, boxes[b"stsz"][0] + 8)[0]
    if count > _MAX_SAMPLES[handler]:
        raise Mp4Error(MP4_TOO_LONG, f"{handler!r} 샘플 {count}개 · 상한 {_MAX_SAMPLES[handler]}")
    table = _read_table(data, boxes[b"stts"][0] + 8, 2 * _entry_count(data, boxes[b"stts"], 8), "I")
    runs, deltas = table[0::2], table[1::2]
    if sum(runs) != count:
        raise Mp4Error(MP4_INVALID, f"stts {sum(runs)}개 · stsz {count}개")
    composition_runs = composition = None
    if b"ctts" in boxes:
        span = boxes[b"ctts"]
        entries = _entry_count(data, span, 8)
        composition_runs = _read_table(data, span[0] + 8, 2 * entries, "I")[0::2]
        composition = _read_table(data, span[0] + 8, 2 * entries, "i")[1::2]
        if sum(composition_runs) != count:
            raise Mp4Error(MP4_INVALID, f"ctts {sum(composition_runs)}개 · stsz {count}개")
    empty_edit, media_time = _edit_list(data, boxes.get(b"elst"), movie_timescale)
    return _TimeTrack(
        handler=handler,
        timescale=timescale,
        empty_edit=empty_edit,
        media_time=media_time,
        count=count,
        runs=runs,
        deltas=deltas,
        composition_runs=composition_runs,
        composition=composition,
    )


def _first_shown(track: _TimeTrack) -> int | None:
    """표시되는 샘플 가운데 가장 이른 표시 시각(틱 — media_time을 뺀 값). 없으면 None.

    DTS는 줄지 않으므로, 지금까지 찾은 값보다 (DTS + 가장 작은 PTS − DTS)가 큰 샘플부터는 더
    이를 수 없다 — 거기서 멈춘다.
    """
    lowest, _highest = track.spread()
    best: int | None = None
    sample = dts = scanned = 0
    for run, delta in zip(track.runs, track.deltas):
        for _ in range(run):
            if best is not None and dts + lowest - track.media_time > best:
                return best
            shown = dts + track.composition_at(sample) - track.media_time
            if shown >= 0 and (best is None or shown < best):
                best = shown
            scanned += 1
            if scanned > _SUMMARY_MAX_SCAN:
                raise _NeedsFullParse
            sample += 1
            dts += delta
    return best


def _latest_shown_by_delta(track: _TimeTrack) -> dict[int, int]:
    """샘플 길이(틱) → 그 길이의 표시되는 샘플 가운데 가장 늦은 표시 시각(틱 — media_time을 뺀 값).

    구간을 뒤에서부터 본다. 길이마다 마지막 샘플의 DTS에서 (PTS − DTS)의 폭만큼 앞까지만
    들여다보면 된다 — 그보다 앞의 샘플은 표시 시각이 마지막 샘플을 넘지 못한다.
    """
    if len(track.runs) > _SUMMARY_MAX_RUNS:
        raise _NeedsFullParse
    lowest, highest = track.spread()
    width = highest - lowest
    starts = [0, *accumulate(track.runs)]  # 구간의 첫 샘플 번호
    bases = [0, *accumulate(map(mul, track.runs, track.deltas))]  # 구간의 첫 샘플의 DTS
    last_dts: dict[int, int] = {}  # 길이 → 그 길이의 마지막 샘플의 DTS
    latest: dict[int, int] = {}
    scanned = 0
    for number in range(len(track.runs) - 1, -1, -1):
        run, delta = track.runs[number], track.deltas[number]
        if not run:
            continue
        base = bases[number]
        end_dts = base + (run - 1) * delta
        threshold = last_dts.setdefault(delta, end_dts) - width
        if end_dts < threshold:
            continue  # 이 길이의 뒤쪽 구간에서 이미 가장 늦은 것을 찾았다
        first = 0 if delta == 0 else max(0, -((base - threshold) // delta))
        for position in range(first, run):
            shown = (
                base
                + position * delta
                + track.composition_at(starts[number] + position)
                - track.media_time
            )
            if shown >= 0 and shown > latest.get(delta, -1):
                latest[delta] = shown
            scanned += 1
            if scanned > _SUMMARY_MAX_SCAN:
                raise _NeedsFullParse
    return latest


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
    """버전·플래그(4) + 항목 수(4) 뒤에 32비트 열이 columns개씩 놓인 표를 읽는다.

    항목이 적은 표(stsc)에 쓴다. 샘플 수만큼 긴 표는 ``_read_table``로 배열에 읽는다.
    """
    count_ = _entry_count(data, span, columns * 4)
    values = struct.unpack_from(f">{count_ * columns}I", data, span[0] + 8)
    return [values[i : i + columns] for i in range(0, len(values), columns)]


def _read_table(data: bytes, start: int, values: int, typecode: str) -> array.array:
    """data의 start부터 빅엔디언 정수 values개를 배열로 통째로 읽는다 (#309).

    긴 영상의 표는 항목이 수백만 개다. 값을 하나씩 파이썬 객체로 풀어 리스트에 담으면
    해석하는 동안 색인의 몇 배를 쓴다 — 바이트를 C 배열에 그대로 옮기고 바이트 순서만 맞춘다.

    Args:
        typecode: ``"I"``(32비트 부호 없음) · ``"i"``(32비트 부호 있음) · ``"Q"``(64비트 부호 없음)

    Raises:
        Mp4Error: 표가 data의 끝을 넘는 경우(``MP4_INVALID``)
    """
    table = array.array(typecode)
    end = start + values * table.itemsize
    if values < 0 or end > len(data):
        raise Mp4Error(MP4_INVALID, f"표의 항목 {values}개가 상자를 넘는다")
    table.frombytes(data[start:end])
    if sys.byteorder == "little":
        table.byteswap()
    return table


def _expand_runs(runs: array.array, values: array.array, typecode: str) -> array.array:
    """(개수, 값) 구간들을 샘플마다 값 하나씩으로 펼친다 — 반복은 C에서 돈다(잘라서)."""
    return array_in_chunks(typecode, chain.from_iterable(map(repeat, values, runs)))


def _shown(ticks: array.array) -> compress:
    """표시되는(0 이상인) 값만 낸다 — 편집 목록이 가린 샘플은 음수다."""
    return compress(ticks, map(le, repeat(0), ticks))


@dataclass
class _RawTrack:
    """표시 시각으로 바꾸기 전의 트랙 — 틱 단위. 샘플별 표는 C 배열이다."""

    handler: bytes
    timescale: int
    empty_edit: Fraction  # 앞의 빈 편집 길이(초)
    presented: array.array  # 샘플별 (PTS − 편집 목록의 media_time), 틱
    decoded: array.array  # 샘플별 (DTS − 편집 목록의 media_time), 틱
    deltas: array.array  # 샘플별 길이, 틱
    lowest_lead: int  # (PTS − DTS)의 최솟값, 틱. ctts가 없으면 0 — 표시 순서를 잘라 정렬할 때 쓴다
    # stts의 (개수, 길이) 구간 — 샘플별 길이(초)를 구간마다 한 번만 나눠서 만드는 데 쓴다
    delta_runs: tuple[array.array, array.array]
    offsets: array.array
    sizes: array.array
    chunk_starts: array.array  # 청크마다의 첫 샘플 인덱스
    sync_samples: array.array
    declared_bitrate: int | None = None  # 오디오 샘플 엔트리가 선언한 비트레이트(bit/s)

    def start(self) -> Fraction | None:
        """이 트랙에서 가장 먼저 표시되는 샘플의 시각(초). 표시되는 샘플이 없으면 None."""
        firsts = [min(_shown(chunk), default=None) for chunk in in_chunks(self.presented)]
        first = min((value for value in firsts if value is not None), default=None)
        if first is None:
            return None
        return self.empty_edit + Fraction(first, self.timescale)


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
    # 표시 순서 — 표시 시각(틱)으로 샘플 번호를 정렬하고, 가려진(음수) 샘플을 뺀다. 같은 시각은
    # 샘플 번호 순서다(정렬이 안정적이다). 샘플마다 파이썬 함수를 부르지 않는다
    presented = video.presented
    order = _presentation_order(presented, video.decoded, video.lowest_lead)
    shown = array_in_chunks(
        "Q", compress(order, map(le, repeat(0), map(presented.__getitem__, order)))
    )
    del order
    sync = frozenset(video.sync_samples)
    lengths: Counter = Counter()
    for chunk in in_chunks(video.deltas):
        lengths.update(chunk)  # 처음 나온 순서가 그대로다 — 수가 같으면 먼저 나온 길이가 뽑힌다
    modal_delta = lengths.most_common(1)[0][0]
    if modal_delta <= 0:
        raise Mp4Error(MP4_INVALID, "영상 샘플 길이가 0이다")
    times, durations = video_track.times, video_track.durations
    return Mp4Index(
        frame_pts=float_column(map(times.__getitem__, shown)),
        frame_samples=count_column(shown),
        keyframes=count_column(_positions_in(shown, sync)),
        duration=_largest(
            map(add, map(times.__getitem__, shown), map(durations.__getitem__, shown))
        ),
        fps=Fraction(video.timescale, modal_delta),
        video=video_track,
        audio=_to_track(audio, origin) if audio else None,
    )


# 표시 순서의 조각을 끊을 자리를 찾을 때 한 번에 더 들여다보는 샘플 수 — B프레임의 재배열은
# 몇 프레임 안에서 끝나므로 한두 번이면 찾는다
_ORDER_STEP = 64


def _presentation_order(
    presented: array.array, decoded: array.array, lowest_lead: int
) -> array.array:
    """표시 순서 — 표시 시각(틱)으로 정렬한 샘플 번호. 같은 시각은 샘플 번호 순서다.

    ``sorted(range(n), key=presented.__getitem__)``과 같은 결과를 조각마다 따로 정렬해 낸다 —
    정렬 한 번이 통째로 C에서 돌면 그동안 GIL을 놓지 않는다(``CHUNK_ITEMS``).

    조각은 **앞쪽의 가장 늦은 표시 시각이 뒤쪽의 어느 표시 시각보다도 늦지 않은 자리**에서만
    끊는다. 그런 자리에서 끊으면 조각을 따로 정렬해 이은 것이 전체를 안정 정렬한 것과 같다
    (값이 같은 샘플은 앞 조각의 것이 번호도 앞이다). 뒤쪽의 표시 시각은 모두
    ``decoded[끝] + lowest_lead`` 이상이다 — DTS는 줄지 않는다.

    Args:
        lowest_lead: (PTS − DTS)의 최솟값(틱). ctts가 없으면 0이다
    """
    total = len(presented)
    order = array.array("Q")
    if total <= CHUNK_ITEMS:
        order.extend(sorted(range(total), key=presented.__getitem__))
        return order
    start = 0
    while start < total:
        end = min(start + CHUNK_ITEMS, total)
        # 이 조각의 가장 늦은 표시 시각 — 앞 조각들의 것은 볼 필요가 없다. 앞 조각은 그 뒤의 어느
        # 표시 시각보다도 늦지 않은 자리에서 끊겼으므로 이 조각의 어느 값도 그보다 이르지 않다
        latest = max(presented[start:end])
        while end < total and latest > decoded[end] + lowest_lead:
            further = min(end + _ORDER_STEP, total)
            latest = max(latest, max(presented[end:further]))
            end = further
        order.extend(sorted(range(start, end), key=presented.__getitem__))
        start = end
    return order


def _largest(values: Iterator[float]) -> float:
    """값들의 최댓값 — ``max(values)``와 같다. 잘라서 훑는다(``CHUNK_ITEMS``). 값이 하나는 있어야 한다."""
    largest = max(islice(values, CHUNK_ITEMS))
    while True:
        further = max(islice(values, CHUNK_ITEMS), default=None)
        if further is None:
            return largest
        largest = max(largest, further)


def _positions_in(samples: array.array, wanted: frozenset) -> array.array:
    """samples 가운데 wanted에 든 것의 자리(0부터) — ``compress(count(), …)``와 같다.

    걸러 내는 반복은 **들어가는 쪽**을 잘라야 한다 — 나오는 값이 드물면(키프레임은 수십 프레임에
    하나다) 나오는 쪽을 ``CHUNK_ITEMS``개씩 잘라도 조각 하나가 표 전체를 훑는다.
    """
    positions = array.array("Q")
    for start in range(0, len(samples), CHUNK_ITEMS):
        chunk = samples[start : start + CHUNK_ITEMS]
        positions.extend(
            compress(range(start, start + len(chunk)), map(wanted.__contains__, chunk))
        )
    return positions


def _to_track(raw: _RawTrack, origin: Fraction) -> Mp4Track:
    """틱 단위 트랙을 초 단위 표시 시각으로 바꾼다."""
    shift = raw.empty_edit - origin
    # 시각 = shift + ticks ÷ timescale. 샘플마다 Fraction을 만들지 않고 같은 유리수를 정수 둘의
    # 나눗셈으로 낸다: (shift의 분자 × timescale + ticks × shift의 분모) ÷ (shift의 분모 × timescale).
    # 정수 ÷ 정수는 가장 가까운 float로 반올림되고 float(Fraction)도 같은 나눗셈이라 값이 같다.
    # 계산은 샘플마다 파이썬 코드를 돌지 않고 map으로 C에서 돈다 (#309)
    offset = shift.numerator * raw.timescale
    step = shift.denominator
    scale = shift.denominator * raw.timescale

    def seconds(ticks: array.array) -> map:
        scaled = ticks if step == 1 else map(mul, ticks, repeat(step))
        moved = scaled if offset == 0 else map(add, scaled, repeat(offset))
        return map(truediv, moved, repeat(scale))

    times = float_column(seconds(raw.presented))
    runs, run_deltas = raw.delta_runs
    return Mp4Track(
        timescale=raw.timescale,
        times=times,
        # PTS와 DTS가 같은 트랙은 같은 표를 함께 쓴다 — 고칠 수 없는 표라 나눠 쓸 수 있다
        decode_times=times if raw.decoded is raw.presented else float_column(seconds(raw.decoded)),
        # 길이(초) = 길이(틱) ÷ timescale. 구간마다 한 번 나눈 값을 개수만큼 편다 — 값은 같다
        durations=float_column(
            chain.from_iterable(map(repeat, map(truediv, run_deltas, repeat(raw.timescale)), runs))
        ),
        offsets=offset_column(raw.offsets),
        sizes=count_column(raw.sizes),
        chunk_starts=count_column(raw.chunk_starts),
        sync_samples=count_column(raw.sync_samples),
        declared_bitrate=raw.declared_bitrate,
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
    time_table = _read_table(
        data, boxes[b"stts"][0] + 8, 2 * _entry_count(data, boxes[b"stts"], 8), "I"
    )
    time_runs, time_deltas = time_table[0::2], time_table[1::2]
    declared = sum(time_runs)
    if declared != count:
        raise Mp4Error(MP4_INVALID, f"stts {declared}개 · stsz {count}개")
    deltas = _expand_runs(time_runs, time_deltas, "I")
    # 샘플의 DTS는 앞 샘플들의 길이를 더한 값이다 — 누적은 C에서 돈다
    decode_times = array_in_chunks("q", accumulate(deltas, initial=0))
    decode_times.pop()  # 마지막 값은 트랙의 끝이다 — 샘플의 DTS가 아니다
    has_composition = b"ctts" in boxes
    composition = _composition_offsets(data, boxes[b"ctts"], count) if has_composition else None
    empty_edit, media_time = _edit_list(data, boxes.get(b"elst"), movie_timescale)
    if b"stss" in boxes:
        # stss의 샘플 번호는 1부터다
        numbers = _read_table(
            data, boxes[b"stss"][0] + 8, _entry_count(data, boxes[b"stss"], 4), "I"
        )
        sync_samples = array_in_chunks("q", map(sub, numbers, repeat(1)))
    else:
        # stss가 없으면 모든 샘플이 단독 디코드 가능하다
        sync_samples = array_in_chunks("q", range(count))

    offsets, chunk_starts = _sample_offsets(data, boxes, sizes)
    decoded = (
        decode_times
        if media_time == 0
        else array_in_chunks("q", map(sub, decode_times, repeat(media_time)))
    )
    return _RawTrack(
        handler=handler,
        timescale=timescale,
        empty_edit=empty_edit,
        # ctts가 없으면 PTS가 DTS다 — 같은 배열을 함께 쓴다(오디오 트랙이 그렇다)
        presented=(
            array_in_chunks("q", map(add, decoded, composition)) if has_composition else decoded
        ),
        decoded=decoded,
        deltas=deltas,
        lowest_lead=min(map(min, in_chunks(composition))) if composition else 0,
        delta_runs=(time_runs, time_deltas),
        offsets=offsets,
        sizes=sizes,
        chunk_starts=chunk_starts,
        sync_samples=sync_samples,
        declared_bitrate=(
            _declared_bitrate(data, boxes[b"stsd"])
            if handler == b"soun" and b"stsd" in boxes
            else None
        ),
    )


def _declared_bitrate(data: bytes, stsd: tuple[int, int]) -> int | None:
    """오디오 트랙의 첫 샘플 엔트리가 선언한 비트레이트(bit/s)를 읽는다. 없으면 None (#309).

    스트림 전체에 대해 하나로 적힌 값이다 — 파일의 어느 부분을 받았는지와 무관하다.
    esds(DecoderConfigDescriptor)의 avgBitrate, btrt의 avgBitrate, esds의 maxBitrate,
    btrt의 maxBitrate 순서로 0이 아닌 첫 값을 쓴다.

    없어도 되는 정보다. 엔트리의 모양을 모르거나 상자가 손상돼 읽지 못하면 색인 해석을
    실패시키지 않고 None을 돌려준다.
    """
    try:
        body, body_end = stsd
        entry = body + 8  # 버전·플래그(4) + 항목 수(4) 뒤가 첫 샘플 엔트리다
        if struct.unpack_from(">I", data, body + 4)[0] < 1:
            return None
        size = struct.unpack_from(">I", data, entry)[0]
        if entry + size > body_end:
            return None
        version = struct.unpack_from(">H", data, entry + 16)[0]
        children = entry + 8 + _AUDIO_ENTRY_FIXED_BYTES[version]
        esds = btrt = (0, 0)  # (max, avg)
        for kind, child, child_end in _boxes(data, children, entry + size):
            if kind == b"esds":
                esds = _esds_bitrates(data, child, child_end)
            elif kind == b"btrt":
                # 버퍼 크기(4) 뒤가 maxBitrate · avgBitrate다
                if child + 12 > child_end:
                    raise Mp4Error(MP4_INVALID, "btrt가 잘렸다")
                btrt = struct.unpack_from(">II", data, child + 4)
    except (Mp4Error, struct.error, IndexError, KeyError):
        return None
    return next((value for value in (esds[1], btrt[1], esds[0], btrt[0]) if value), None)


def _esds_bitrates(data: bytes, body: int, end: int) -> tuple[int, int]:
    """esds 본문에서 DecoderConfigDescriptor의 (maxBitrate, avgBitrate)를 읽는다."""
    position = _descriptor_body(data, body + 4, end, _ES_DESCRIPTOR)  # 버전·플래그(4) 뒤
    flags = data[position + 2]  # ES_ID(2) 뒤의 플래그 — 선택 칸이 있는지
    position += 3
    if flags & 0x80:
        position += 2  # dependsOn_ES_ID
    if flags & 0x40:
        position += 1 + data[position]  # URL — 길이(1) + 문자열
    if flags & 0x20:
        position += 2  # OCR_ES_ID
    position = _descriptor_body(data, position, end, _DECODER_CONFIG_DESCRIPTOR)
    # 객체 종류(1) · 스트림 종류(1) · 버퍼 크기(3) 뒤가 maxBitrate · avgBitrate다
    if position + 13 > end:
        raise Mp4Error(MP4_INVALID, "esds가 잘렸다")
    return struct.unpack_from(">II", data, position + 5)


def _descriptor_body(data: bytes, position: int, end: int, tag: int) -> int:
    """position의 서술자가 tag인지 확인하고 본문이 시작하는 위치를 돌려준다.

    길이 칸은 바이트마다 7비트씩 최대 4바이트다 — 맨 위 비트가 켜져 있으면 이어진다.
    """
    if position >= end or data[position] != tag:
        raise Mp4Error(MP4_INVALID, f"esds에 서술자 {tag:#x}가 없다")
    position += 1
    for _ in range(4):
        position += 1
        if not data[position - 1] & 0x80:
            break
    return position


def _sample_sizes(data: bytes, span: tuple[int, int], count: int) -> array.array:
    """stsz — 샘플별 크기. 고정 크기 칸이 0이 아니면 모든 샘플이 그 크기다.

    count는 호출자가 상한을 확인한 샘플 수다. 고정 크기일 때는 표가 없어 상자 크기로
    개수를 가늠할 수 없으므로, 상한 확인이 유일한 방어다.
    """
    uniform = struct.unpack_from(">I", data, span[0] + 4)[0]
    if uniform:
        return array.array("I", [uniform]) * count
    _entry_count(data, span, 4, at=8)
    return _read_table(data, span[0] + 12, count, "I")


def _composition_offsets(data: bytes, span: tuple[int, int] | None, count: int) -> array.array:
    """ctts — 샘플별 (PTS − DTS). 상자가 없으면 전부 0이다."""
    if span is None:
        return array.array("q", bytes(8 * count))
    runs = _entry_count(data, span, 8)
    # 버전 0은 부호 없는 값이지만 음수를 넣는 파일이 있어 버전과 무관하게 부호 있는 값으로 읽는다.
    # 개수(부호 없음)와 값(부호 있음)을 같은 바이트에서 따로 읽는다 — 칸마다 형식 글자를 늘어놓은
    # 형식 문자열("IiIi…")은 struct가 캐시에 붙들어 긴 영상에서 수십 MB가 남는다 (#309)
    counts = _read_table(data, span[0] + 8, 2 * runs, "I")[0::2]
    offsets = _read_table(data, span[0] + 8, 2 * runs, "i")[1::2]
    declared = sum(counts)
    if declared != count:
        raise Mp4Error(MP4_INVALID, f"ctts {declared}개 · stsz {count}개")
    return _expand_runs(counts, offsets, "q")


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
    data: bytes, boxes: dict[bytes, tuple[int, int]], sizes: array.array
) -> tuple[array.array, array.array]:
    """stsc + stco/co64 + stsz — (샘플별 파일 안 시작 위치, 청크마다의 첫 샘플 인덱스).

    청크 하나에 샘플이 이어 붙어 있다. stsc는 "이 청크부터는 청크당 샘플이 몇 개"를
    구간으로 적고, stco/co64는 청크의 시작 위치를 적는다.
    """
    if b"co64" in boxes:
        chunk_count = _entry_count(data, boxes[b"co64"], 8)
        chunk_offsets = _read_table(data, boxes[b"co64"][0] + 8, chunk_count, "Q")
    else:
        chunk_count = _entry_count(data, boxes[b"stco"], 4)
        chunk_offsets = _read_table(data, boxes[b"stco"][0] + 8, chunk_count, "I")
    runs = _u32_table(data, boxes[b"stsc"], 3)
    if not runs and sizes:
        raise Mp4Error(MP4_INVALID, "stsc가 비어 있다")

    offsets = array.array("q")
    chunk_starts = array.array("q")
    sample = 0
    for run_index, (first_chunk, samples_per_chunk, _description) in enumerate(runs):
        # first_chunk는 1부터다. 구간은 다음 구간의 first_chunk 직전 청크까지다
        last_chunk = runs[run_index + 1][0] - 1 if run_index + 1 < len(runs) else len(chunk_offsets)
        if samples_per_chunk == 1:
            # 청크마다 샘플 하나 — 샘플의 위치가 곧 청크의 위치다. 청크마다 돌지 않는다
            chunks = max(last_chunk - (first_chunk - 1), 0)
            if sample + chunks > len(sizes):
                raise Mp4Error(MP4_INVALID, "stsc의 샘플 수가 stsz보다 많다")
            fill_in_chunks(chunk_starts, range(sample, sample + chunks))
            # 종류가 다른 배열끼리는 바로 잇지 못한다 — 값으로 넘긴다
            fill_in_chunks(offsets, iter(chunk_offsets[first_chunk - 1 : last_chunk]))
            sample += chunks
            continue
        for chunk in range(first_chunk - 1, last_chunk):
            chunk_starts.append(sample)
            if not samples_per_chunk:
                continue
            last = sample + samples_per_chunk
            if last > len(sizes):
                raise Mp4Error(MP4_INVALID, "stsc의 샘플 수가 stsz보다 많다")
            # 청크 안의 샘플은 이어 붙어 있다 — 청크 위치에서 앞 샘플들의 크기를 누적한 자리다.
            # 샘플마다 파이썬 루프를 돌지 않고 청크 단위로 누적한다(긴 영상의 해석 시간 #309)
            offsets.extend(accumulate(sizes[sample : last - 1], initial=chunk_offsets[chunk]))
            sample = last
    if sample != len(sizes):
        raise Mp4Error(MP4_INVALID, f"stsc {sample}개 · stsz {len(sizes)}개")
    return offsets, chunk_starts
