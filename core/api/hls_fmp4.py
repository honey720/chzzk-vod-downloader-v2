"""HLS fMP4에서 구간을 정하는 데 필요한 것 받기 — 플레이리스트 · 초기화 세그먼트 · 세그먼트 (#309).

인코딩 전 다시보기는 HLS fMP4로 내려온다. 구간을 받으려면 세그먼트를 받기 전에 프레임
시각을 알아야 하는데, 그 정보는 세그먼트 안의 moof에 있다. 구간의 양 끝이 든 세그먼트를
받아 그 moof를 읽는다.

**세그먼트는 언제나 전체 요청으로 받는다. 범위 요청을 보내지 않는다.** 다시보기를 내주는
CDN은 캐시에 없는 세그먼트에 범위 요청이 오면 200과 잘린 본문으로 답하고, 그 잘린 본문을
세그먼트 전체로 캐시한다 — 그 뒤의 전체 요청은 누구의 것이든 잘린 본문을 받는다. moof만
골라 받는 길이 없으므로 세그먼트를 통째로 받고, 받은 것을 버리지 않는다.

받은 것은 ``Fmp4Head``에 모은다. 세그먼트의 본문은 ``Fmp4Head.segment_dir``(엔진의 세그먼트
임시 폴더)에 엔진과 같은 이름의 파일로 두고, 프레임 정보만 메모리에 둔다. 구간을 해석한
쪽(헤드리스 스크립트 등)이 이것을 ``Content.fmp4_head``로 넘기면 엔진은 같은 것을 다시
받지 않는다.

요청에는 쿠키를 싣지 않는다 — m3u8 다운로더가 같은 주소를 받는 방식과 같다.
"""

import os
from collections.abc import Callable
from typing import BinaryIO
from urllib.parse import urljoin

from core.api.fmp4 import parse_init_segment, read_media_segment
from core.api.hls import parse_media_playlist
from core.api.mp4 import MP4_TRUNCATED, MP4_UNSUPPORTED, Mp4Error
from core.api.session import get_thread_session
from core.models.fmp4_index import Fmp4Head, Fmp4Init, Fmp4Segment

# 실패 키 — 번역하지 않은 i18n 키 원문
HLS_NOT_FMP4 = "This recording cannot be downloaded in sections"  # EXT-X-MAP 없음 · MAP이 여럿

# 세그먼트 하나로 받아들이는 최대 크기(바이트) — 256MB. 손상된 응답을 끝없이 받지 않게 한다
_MAX_SEGMENT_BYTES = 256 * 1024 * 1024

_REQUEST_TIMEOUT = 30  # 요청 타임아웃(초) — m3u8 다운로더의 세그먼트 요청과 같은 값
_READ_CHUNK_BYTES = 64 * 1024  # 응답 본문을 나눠 읽는 단위(바이트)


def fetch_fmp4_head(playlist_url: str, segment_dir: str | None = None) -> Fmp4Head:
    """플레이리스트와 초기화 세그먼트를 받아 ``Fmp4Head``를 만든다. 세그먼트는 아직 받지 않는다.

    Args:
        playlist_url: 미디어 플레이리스트의 주소
        segment_dir: 프레임 정보를 읽으려고 받는 세그먼트를 둘 폴더(엔진의 세그먼트 임시
            폴더). 없으면 만든다. None이면 받은 본문을 버린다 — 엔진이 다시 받는다

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
    return Fmp4Head(
        playlist=playlist,
        init_data=init_data,
        init=parse_init_segment(init_data),
        segment_dir=segment_dir,
    )


def segment_file_name(segment_count: int, index: int) -> str:
    """index번째 세그먼트를 임시 폴더에 둘 때의 파일 이름 — m3u8 다운로더가 쓰는 이름과 같다.

    번호는 1부터이고(0은 초기화 세그먼트) 세그먼트 수의 자릿수만큼 0으로 채운다.
    """
    return f"{index + 1:0{len(str(segment_count))}d}.m4v"


def segment_frames(head: Fmp4Head, playlist_url: str, index: int) -> Fmp4Segment:
    """index번째 세그먼트의 프레임 정보를 돌려준다 — 이미 읽었으면 그것을, 아니면 세그먼트를 받아 읽는다.

    세그먼트를 전체 요청으로 받아 그 안의 moof를 모두 읽는다. ``head.segment_dir``이 있으면
    받은 본문을 그 폴더에 두고 ``head.stored``에 적는다 — 엔진이 다시 받지 않는다. 결과는
    ``head.segments``에 넣어 둔다. 같은 세그먼트를 두 번 요청하지 않는다.

    Raises:
        Mp4Error: 받은 본문이 잘렸거나(``MP4_TRUNCATED``) moof를 해석하지 못한 경우
        requests.RequestException: 연결 실패·타임아웃·HTTP 오류 상태
        OSError: 세그먼트 파일을 쓰지 못한 경우
    """
    found = head.segments.get(index)
    if found is None:
        url = urljoin(playlist_url, head.playlist.segments[index])
        if head.segment_dir is None:
            found = _fetch_frames(url, head.init)
        else:
            os.makedirs(head.segment_dir, exist_ok=True)
            name = segment_file_name(len(head.playlist.segments), index)
            path = os.path.join(head.segment_dir, name)
            download_segment(url, path)
            try:
                found = read_segment_file(path, head.init)
            except Mp4Error:
                os.remove(path)  # 잘렸거나 해석할 수 없는 세그먼트를 엔진이 쓰게 두지 않는다
                raise
            head.stored.add(index)
        head.segments[index] = found
    return found


def download_segment(url: str, path: str) -> int:
    """세그먼트를 전체 요청으로 받아 path에 쓴다. 받은 바이트 수를 돌려준다.

    받는 동안은 ``path + ".part"``에 쓰고 다 받으면 이름을 바꾼다 — path에는 다 받은
    파일만 놓인다. 서버가 말한 길이(Content-Length)와 받은 길이가 다르면 실패다.

    Raises:
        Mp4Error: 받은 길이가 서버가 말한 길이와 다른 경우(``MP4_TRUNCATED``), 세그먼트가
            너무 큰 경우(``MP4_UNSUPPORTED``)
        requests.RequestException: 연결 실패·타임아웃·HTTP 오류 상태
    """
    partial = path + ".part"
    try:
        with open(partial, "wb") as out:
            received = _stream(url, out.write)
        os.replace(partial, path)
    except BaseException:
        if os.path.exists(partial):
            os.remove(partial)
        raise
    return received


def read_segment_file(path: str, init: Fmp4Init) -> Fmp4Segment:
    """받아 둔 세그먼트 파일에서 moof를 모두 읽어 프레임 정보를 돌려준다. 파일이 온전한지도 본다.

    mdat의 본문은 읽지 않는다. 상자들이 말하는 끝이 파일 크기와 다르면 잘린 파일이다.

    Raises:
        Mp4Error: 파일이 잘린 경우(``MP4_TRUNCATED``), moof를 해석하지 못한 경우
        OSError: 파일을 읽지 못한 경우
    """
    with open(path, "rb") as f:
        return read_media_segment(_reader(f), init, os.path.getsize(path))


def _fetch_frames(url: str, init: Fmp4Init) -> Fmp4Segment:
    """세그먼트를 받아 프레임 정보만 읽고 본문은 버린다 — 둘 곳(segment_dir)이 없을 때."""
    body = bytearray()
    _stream(url, body.extend)
    data = bytes(body)
    return read_media_segment(lambda offset, size: data[offset : offset + size], init, len(data))


def _stream(url: str, write: Callable[[bytes], object]) -> int:
    """url을 전체 요청으로 받아 조각마다 write에 넘긴다. 받은 바이트 수를 돌려준다."""
    with get_thread_session().get(url, stream=True, timeout=_REQUEST_TIMEOUT) as response:
        response.raise_for_status()
        received = 0
        for chunk in response.iter_content(chunk_size=_READ_CHUNK_BYTES):
            received += len(chunk)
            if received > _MAX_SEGMENT_BYTES:
                raise Mp4Error(MP4_UNSUPPORTED, f"세그먼트가 {_MAX_SEGMENT_BYTES}바이트를 넘는다")
            write(chunk)
        declared = declared_length(response.headers)
        if declared is not None and declared != received:
            raise Mp4Error(MP4_TRUNCATED, f"받은 {received}바이트 · 서버가 말한 {declared}바이트")
        return received


def declared_length(headers) -> int | None:
    """응답 머리의 Content-Length — 본문 그대로의 길이일 때만. 압축 전송이거나 없으면 None이다."""
    if headers.get("Content-Encoding", "identity").lower() != "identity":
        return None  # 압축된 본문의 길이다 — 받은 바이트 수와 견줄 수 없다
    text = headers.get("Content-Length")
    return int(text) if text is not None and text.strip().isdigit() else None


def _reader(file: BinaryIO) -> Callable[[int, int], bytes]:
    """열린 파일을 ``read(offset, size)`` 읽기 함수로 감싼다."""

    def read(offset: int, size: int) -> bytes:
        file.seek(offset)
        return file.read(size)

    return read
