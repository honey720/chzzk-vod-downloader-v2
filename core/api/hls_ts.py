"""암호화된 HLS MPEG-TS에서 구간을 정하는 데 필요한 것 받기 — 플레이리스트 · 세그먼트 (#309).

암호화 VOD는 AES-128로 암호화한 MPEG-TS 세그먼트로 내려온다. 구간을 받으려면 세그먼트를
받기 전에 프레임 시각을 알아야 하는데, 그 정보는 세그먼트 안의 PES 머리에 있고
**복호화한 뒤에만** 읽을 수 있다. 구간의 양 끝이 든 세그먼트를 받아 복호화해 읽는다.

**세그먼트는 언제나 전체 요청으로 받는다. 범위 요청을 보내지 않는다**(``core/api/hls_fmp4.py``와
같은 이유다). 세그먼트 전체가 CBC로 암호화돼 있어 앞부분만 받아서는 온전한지 확인할 수도 없다.

받은 세그먼트는 엔진과 같은 순서로 확인한다(``open_ts_segment``): 서버가 말한 길이와 받은
길이 → 암호문 길이가 16바이트의 배수 → 복호화 → 복호화한 TS가 188바이트의 배수이고 패킷마다
0x47로 시작. 엔진(``core/downloaders/hls_aes_downloader.py``)도 같은 함수를 쓴다. 온전하지
않으면 ``TruncatedSegmentError``다. 여기서는 다시 받지 않는다.

받은 것은 ``TsHead``에 모은다. 세그먼트의 **복호화한** 본문은 ``TsHead.segment_dir``(엔진의
세그먼트 임시 폴더)에 엔진과 같은 이름의 파일로 두고, 프레임 정보만 메모리에 둔다. 구간을
해석한 쪽이 이것을 ``Content.ts_head``로 넘기면 엔진은 같은 것을 다시 받지 않는다.
받아 둔 세그먼트는 그것을 받은 플레이리스트에 묶인다(``TsHead.playlist_ref`` — 주소에서 쿼리를
뺀 값). 엔진이 받을 플레이리스트가 그것과 다르면 엔진은 받아 둔 것을 쓰지 않는다.

**복호화 키는 받는 쪽이 인자로 준다. 이 모듈은 키를 받아 오지도 보관하지도 않는다.**
``TsHead``에 키를 넣지 않는다 — 엔진은 키를 다시 받는다. 키 값은 로그 · 예외 메시지에 싣지
않는다.

요청에는 쿠키를 싣지 않는다 — hls_aes 다운로더가 같은 주소를 받는 방식과 같다.
"""

import os
from collections.abc import Mapping
from urllib.parse import urljoin, urlsplit, urlunsplit

from core.api.hls import HlsPlaylist, parse_media_playlist
from core.api.mpegts import TS_UNSUPPORTED, TsError, parse_ts
from core.api.session import get_thread_session
from core.downloaders import integrity
from core.downloaders.base import REQUEST_TIMEOUT
from core.downloaders.decrypt import DecryptionError, decrypt_segment, looks_like_ts, sequence_iv
from core.models.ts_index import TsHead, TsStreams

# 세그먼트 하나로 받아들이는 최대 크기(바이트) — 256MB. 손상된 응답을 끝없이 받지 않게 한다
_MAX_SEGMENT_BYTES = 256 * 1024 * 1024

_READ_CHUNK_BYTES = 64 * 1024  # 응답 본문을 나눠 읽는 단위(바이트)


def segment_iv(playlist: HlsPlaylist, index: int) -> bytes:
    """index번째 세그먼트의 IV — ``#EXT-X-KEY``에 IV가 있으면 그 값, 없으면 미디어 시퀀스 번호다.

    Raises:
        DecryptionError: 플레이리스트에 암호화 정보가 없는 경우
    """
    if playlist.key is None:
        raise DecryptionError("암호화 정보(#EXT-X-KEY)가 없는 플레이리스트다")
    explicit = playlist.key.iv
    return explicit if explicit is not None else sequence_iv(playlist.sequence_of(index))


def open_ts_segment(
    body: bytes,
    headers: Mapping[str, str] | None,
    key: bytes,
    iv: bytes,
    *,
    key_check: bool = False,
) -> bytes:
    """받은 암호화 세그먼트를 확인하고 복호화해, 온전한 MPEG-TS bytes를 돌려준다.

    확인하는 순서: 서버가 말한 길이(Content-Length)와 받은 길이 → 암호문 길이가 16바이트의
    배수 → 복호화 → 복호화한 TS가 188바이트의 배수이고 패킷마다 0x47로 시작. 구간 해석과
    엔진이 같은 순서로 확인하도록 한 곳에 둔다.

    Args:
        body: 받은 본문(암호문)
        headers: 응답 머리. 없으면 None — 길이를 견주지 않는다
        key: 16바이트 AES-128 키
        iv: 16바이트 초기화 벡터(``segment_iv``)
        key_check: True면 복호화 결과가 TS로 보이지 않을 때 ``DecryptionError``를 낸다 — 키가
            맞는지 처음 확인하는 자리에서 쓴다. False면 그런 결과도 온전하지 않은 세그먼트로
            보아 ``TruncatedSegmentError``를 낸다(다시 받게 하는 쪽)

    Raises:
        TruncatedSegmentError: 받은 길이가 서버가 말한 길이와 다르거나, 암호문 · 복호화한
            TS가 온전하지 않은 경우
        DecryptionError: ``key_check``인데 복호화 결과가 MPEG-TS가 아닌 경우 — 키 또는 IV
            규칙이 맞지 않는다
        ValueError: 키 · IV의 길이가 16바이트가 아닌 경우
    """
    integrity.check_content_length(headers, len(body))
    integrity.check_cbc_ciphertext(body)
    plain = decrypt_segment(body, key, iv)
    if key_check and not looks_like_ts(plain):
        raise DecryptionError("복호화 결과가 MPEG-TS가 아니다 — 키 또는 IV 규칙이 맞지 않는다")
    integrity.check_ts_segment(plain)
    return plain


def fetch_ts_head(playlist_url: str, segment_dir: str | None = None) -> TsHead:
    """플레이리스트를 받아 ``TsHead``를 만든다. 세그먼트와 키는 아직 받지 않는다.

    Args:
        playlist_url: 미디어 플레이리스트의 주소
        segment_dir: 프레임 정보를 읽으려고 받는 세그먼트(복호화한 것)를 둘 폴더 — 엔진의
            세그먼트 임시 폴더. 없으면 만든다. None이면 받은 본문을 버린다 — 엔진이 다시 받는다

    Raises:
        DecryptionError: 플레이리스트에 ``#EXT-X-KEY``가 없거나 AES-128이 아닌 경우
        requests.RequestException: 연결 실패·타임아웃·HTTP 오류 상태
    """
    response = get_thread_session().get(playlist_url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    playlist = parse_media_playlist(response.text)
    if playlist.key is None:
        raise DecryptionError("암호화 정보(#EXT-X-KEY)가 없는 플레이리스트다")
    if not playlist.key.is_aes_128:
        raise DecryptionError(f"지원하지 않는 암호화 방식: {playlist.key.method}")
    return TsHead(
        playlist=playlist, segment_dir=segment_dir, playlist_ref=playlist_ref(playlist_url)
    )


def playlist_ref(playlist_url: str) -> str:
    """플레이리스트를 가리키는 값 — 주소에서 쿼리와 프래그먼트를 뺀 것.

    받아 둔 세그먼트가 어느 플레이리스트(해상도)의 것인지 견주는 데 쓴다(``TsHead.playlist_ref``).
    해상도는 경로로 갈린다.

    경로만 견주고 쿼리는 견주지 않는다. 암호화 VOD의 플레이리스트 · 세그먼트 · 키 주소에는
    쿼리가 없었다(실측). 쿼리가 붙는다면 요청마다 바뀌는 서명 값이 실리는 자리라, 쿼리까지
    견주면 같은 플레이리스트인데도 받아 둔 세그먼트를 버리게 된다.
    """
    parts = urlsplit(playlist_url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def ts_key_uri(playlist_url: str, head: TsHead) -> str:
    """복호화 키의 주소 — 플레이리스트 주소에 ``#EXT-X-KEY``의 URI를 붙인 것.

    키를 받는 일은 호출하는 쪽이 한다(쿠키가 필요해 앱 계층의 리졸버가 받는다).
    """
    return urljoin(playlist_url, head.playlist.key.uri)


def ts_segment_file_name(segment_count: int, index: int) -> str:
    """index번째 세그먼트를 임시 폴더에 둘 때의 파일 이름 — hls_aes 다운로더가 쓰는 이름과 같다.

    번호는 0부터이고 세그먼트 수의 자릿수만큼 0으로 채운다.
    """
    return f"{index:0{len(str(segment_count))}d}.ts"


def segment_streams(head: TsHead, playlist_url: str, index: int, key: bytes) -> TsStreams:
    """index번째 세그먼트의 프레임 정보를 돌려준다 — 이미 읽었으면 그것을, 아니면 받아서 읽는다.

    세그먼트를 전체 요청으로 받아 확인하고 복호화해(``open_ts_segment``) 읽는다.
    ``head.segment_dir``이 있으면 복호화한 본문을 그 폴더에 두고 ``head.stored``에 적는다 —
    엔진이 다시 받지 않는다. 결과는 ``head.segments``에 넣어 둔다. 같은 세그먼트를 두 번
    요청하지 않는다.

    복호화 결과가 TS로 보이지 않으면 키가 틀린 것으로 본다(``DecryptionError``).

    Args:
        head: ``fetch_ts_head``의 결과
        playlist_url: 미디어 플레이리스트의 주소
        index: 세그먼트 인덱스
        key: 16바이트 AES-128 키. 보관하지 않는다

    Raises:
        TruncatedSegmentError: 받은 세그먼트가 온전하지 않은 경우
        DecryptionError: 복호화 결과가 MPEG-TS가 아닌 경우
        TsError: 세그먼트를 해석하지 못했거나 너무 큰 경우
        requests.RequestException: 연결 실패·타임아웃·HTTP 오류 상태
        OSError: 세그먼트 파일을 쓰지 못한 경우
    """
    found = head.segments.get(index)
    if found is None:
        url = urljoin(playlist_url, head.playlist.segments[index])
        plain = fetch_ts_segment(url, key, segment_iv(head.playlist, index))
        found = parse_ts(plain)  # 해석하지 못하는 세그먼트는 파일로 두지 않는다
        if head.segment_dir is not None:
            os.makedirs(head.segment_dir, exist_ok=True)
            name = ts_segment_file_name(len(head.playlist.segments), index)
            _write_whole(os.path.join(head.segment_dir, name), plain)
            head.stored.add(index)
        head.segments[index] = found
    return found


def fetch_ts_segment(url: str, key: bytes, iv: bytes) -> bytes:
    """세그먼트를 전체 요청으로 받아 확인하고 복호화한 bytes를 돌려준다.

    요청은 엔진의 세그먼트 요청과 같다 — 스레드 세션, 범위 없는 GET, 같은 제한 시간.

    Raises:
        TruncatedSegmentError: 받은 세그먼트가 온전하지 않은 경우
        DecryptionError: 복호화 결과가 MPEG-TS가 아닌 경우 — 키 또는 IV 규칙이 맞지 않는다
        TsError: 세그먼트가 너무 큰 경우(``TS_UNSUPPORTED``)
        requests.RequestException: 연결 실패·타임아웃·HTTP 오류 상태
    """
    with get_thread_session().get(url, stream=True, timeout=REQUEST_TIMEOUT) as response:
        response.raise_for_status()
        body = bytearray()
        for chunk in response.iter_content(chunk_size=_READ_CHUNK_BYTES):
            body.extend(chunk)
            if len(body) > _MAX_SEGMENT_BYTES:
                raise TsError(TS_UNSUPPORTED, f"세그먼트가 {_MAX_SEGMENT_BYTES}바이트를 넘는다")
        headers = getattr(response, "headers", None)
    return open_ts_segment(bytes(body), headers, key, iv, key_check=True)


def _write_whole(path: str, data: bytes) -> None:
    """data를 path에 쓴다 — 쓰는 동안은 ``path + ".part"``에 두고 다 쓰면 이름을 바꾼다."""
    partial = path + ".part"
    try:
        with open(partial, "wb") as out:
            out.write(data)
        os.replace(partial, path)
    except BaseException:
        if os.path.exists(partial):
            os.remove(partial)
        raise
