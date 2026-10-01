"""HLS fMP4에서 구간을 정하는 데 필요한 것만 받기 — 플레이리스트 · 초기화 세그먼트 · moof (#309).

인코딩 전 다시보기는 HLS fMP4로 내려온다. 구간을 받으려면 세그먼트를 받기 전에 프레임
시각을 알아야 하는데, 그 정보는 세그먼트 앞부분의 moof에 있다. 세그먼트 전체가 아니라
moof까지만 범위 요청으로 받는다.

받은 것은 ``Fmp4Head``에 모은다. 구간을 해석한 쪽(헤드리스 스크립트 등)이 이것을
``Content.fmp4_head``로 넘기면 엔진은 같은 것을 다시 받지 않는다.

요청에는 쿠키를 싣지 않는다 — m3u8 다운로더가 같은 주소를 받는 방식과 같다.

서버가 범위 요청을 받지 않고 200으로 전체를 보내면 그 본문을 그대로 쓴다 — 세그먼트
하나는 수 MB라 받아도 되고, 다시 요청해도 같은 답이 온다.
"""

from urllib.parse import urljoin

from core.api.fmp4 import parse_init_segment, parse_media_segment, scan_moof
from core.api.hls import parse_media_playlist
from core.api.mp4 import (
    MP4_INVALID,
    MP4_RANGE_MISMATCH,
    MP4_UNSUPPORTED,
    Mp4Error,
    _granted_length,
)
from core.api.session import get_thread_session
from core.models.fmp4_index import Fmp4Head, Fmp4Init, Fmp4Segment

# 실패 키 — 번역하지 않은 i18n 키 원문
HLS_NOT_FMP4 = "This recording cannot be downloaded in sections"  # EXT-X-MAP 없음 · MAP이 여럿

# moof를 읽는 첫 요청의 크기(바이트). 4초짜리 60fps 세그먼트의 moof가 수 KB였다 —
# 한 번에 들어오도록 넉넉히 잡는다
_MOOF_READ_BYTES = 64 * 1024

# moof 하나를 읽으려고 요청을 키우는 최대 크기(바이트). 손상된 크기 칸을 믿고 끝없이
# 받지 않게 한다
_MAX_MOOF_BYTES = 16 * 1024 * 1024

# 세그먼트 하나에서 따라가는 moof의 최대 수 — 손상된 세그먼트에서 요청이 끝없이 이어지지
# 않게 한다
_MAX_FRAGMENTS = 256

# 범위 요청에 200으로 답한 본문(세그먼트 전체)을 받아들이는 최대 크기(바이트)
_MAX_SEGMENT_BYTES = 256 * 1024 * 1024

_REQUEST_TIMEOUT = 30  # 요청 타임아웃(초) — m3u8 다운로더의 세그먼트 요청과 같은 값
_READ_CHUNK_BYTES = 64 * 1024  # 응답 본문을 나눠 읽는 단위(바이트)


def fetch_fmp4_head(playlist_url: str) -> Fmp4Head:
    """플레이리스트와 초기화 세그먼트를 받아 ``Fmp4Head``를 만든다. 세그먼트의 moof는 아직 받지 않는다.

    Raises:
        Mp4Error: 플레이리스트에 EXT-X-MAP이 없거나 둘 이상인 경우(``HLS_NOT_FMP4``),
            초기화 세그먼트를 해석하지 못한 경우
        requests.RequestException: 연결 실패·타임아웃·HTTP 오류 상태
    """
    session = get_thread_session()
    response = session.get(playlist_url, timeout=_REQUEST_TIMEOUT)
    response.raise_for_status()
    playlist = parse_media_playlist(response.text)
    if playlist.init_uri is None:
        raise Mp4Error(HLS_NOT_FMP4, "플레이리스트에 초기화 세그먼트(EXT-X-MAP)가 없다")
    if len(playlist.init_uris) > 1:
        raise Mp4Error(HLS_NOT_FMP4, "초기화 세그먼트가 중간에 바뀌는 플레이리스트다")
    init_response = session.get(urljoin(playlist_url, playlist.init_uri), timeout=_REQUEST_TIMEOUT)
    init_response.raise_for_status()
    init_data = init_response.content
    return Fmp4Head(playlist=playlist, init_data=init_data, init=parse_init_segment(init_data))


def segment_frames(head: Fmp4Head, playlist_url: str, index: int) -> Fmp4Segment:
    """index번째 세그먼트의 프레임 정보를 돌려준다 — 이미 받았으면 그것을, 아니면 moof만 받아 해석한다.

    받은 결과는 ``head.segments``에 넣어 둔다. 같은 세그먼트를 두 번 요청하지 않는다.

    Raises:
        Mp4Error: moof를 읽지 못했거나 해석하지 못한 경우
        requests.RequestException: 연결 실패·타임아웃·HTTP 오류 상태
    """
    found = head.segments.get(index)
    if found is None:
        url = urljoin(playlist_url, head.playlist.segments[index])
        found = fetch_segment_moofs(url, head.init)
        head.segments[index] = found
    return found


def fetch_segment_moofs(url: str, init: Fmp4Init) -> Fmp4Segment:
    """세그먼트에서 moof만 범위 요청으로 받아 프레임 정보를 읽는다.

    앞부분을 받아 첫 mdat 앞의 moof를 읽고, mdat 뒤에 상자가 더 있으면 그 위치부터 같은
    방식으로 이어 읽는다. mdat 본문은 받지 않는다.

    서버가 206이 아니라 200으로 답하면 본문이 세그먼트 전체다 — 그대로 해석한다.

    Raises:
        Mp4Error: 요청과 다른 범위의 응답(``MP4_RANGE_MISMATCH``), moof가 없거나 너무
            큰 경우(``MP4_INVALID``), 해석 실패
        requests.RequestException: 연결 실패·타임아웃·HTTP 오류 상태
    """
    heads = []  # moof가 든 앞부분들 — 이어 붙이면 mdat 없는 세그먼트가 된다
    offset = 0
    for _ in range(_MAX_FRAGMENTS):
        size = _MOOF_READ_BYTES
        while True:
            data, total, whole = _read_range(url, offset, size)
            if whole:
                return parse_media_segment(data, init)
            scan = scan_moof(data)
            reached_end = len(data) < size or (total is not None and offset + len(data) >= total)
            if scan.complete or scan.next_offset is not None or reached_end:
                break
            # mdat의 머리가 아직 안 보인다 — moof가 받은 것보다 길다. 필요한 만큼 키워 다시 받는다
            size = max(size * 4, scan.moof_end + _MOOF_READ_BYTES)
            if size > _MAX_MOOF_BYTES:
                raise Mp4Error(MP4_INVALID, f"moof가 {_MAX_MOOF_BYTES}바이트를 넘는다")
        if scan.complete:
            heads.append(data[: scan.moof_end])
        if scan.next_offset is None:
            break
        offset += scan.next_offset
        if total is not None and offset >= total:
            break
    if not heads:
        raise Mp4Error(MP4_INVALID, "세그먼트에 moof가 없다")
    return parse_media_segment(b"".join(heads), init)


def _read_range(url: str, offset: int, size: int) -> tuple[bytes, int | None, bool]:
    """url의 offset부터 size바이트를 범위 요청으로 받는다 — (본문, 전체 크기, 전체를 받았는지).

    - 206: 본문은 요청한 범위다. 전체 크기는 Content-Range가 말한 값(모르면 None)
    - 200: 서버가 범위를 무시했다. 본문은 파일 전체이고 셋째 값이 True다
    - 416: 파일 끝을 넘은 요청 — 빈 본문

    Raises:
        Mp4Error: Content-Range가 요청과 다르거나 본문 길이가 맞지 않는 경우
            (``MP4_RANGE_MISMATCH``), 200 본문이 너무 큰 경우(``MP4_UNSUPPORTED``)
    """
    last = offset + size - 1
    headers = {"Range": f"bytes={offset}-{last}"}
    with get_thread_session().get(
        url, headers=headers, stream=True, timeout=_REQUEST_TIMEOUT
    ) as response:
        if response.status_code == 416:
            return b"", None, False
        response.raise_for_status()
        whole = response.status_code != 206
        if whole:
            limit = _MAX_SEGMENT_BYTES
            total = None
        else:
            content_range = response.headers.get("Content-Range")
            limit = _granted_length(content_range, offset, last)
            total_text = (content_range or "").rsplit("/", 1)[-1].strip()
            total = int(total_text) if total_text.isdigit() else None
        body = bytearray()
        for chunk in response.iter_content(chunk_size=_READ_CHUNK_BYTES):
            body += chunk
            if len(body) > limit:
                if whole:
                    raise Mp4Error(MP4_UNSUPPORTED, f"세그먼트가 {limit}바이트를 넘는다")
                raise Mp4Error(MP4_RANGE_MISMATCH, f"본문이 {limit}바이트를 넘는다")
        if not whole and len(body) != limit:
            raise Mp4Error(MP4_RANGE_MISMATCH, f"본문 {len(body)}바이트 · 기대 {limit}")
        return bytes(body), (len(body) if whole else total), whole
