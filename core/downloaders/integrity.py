"""받은 세그먼트가 온전한지 내용으로 확인한다 (#321).

CDN이 잘린 본문을 세그먼트 전체로 캐시하는 경우가 있다. 그때는 응답이 200이고
``Content-Length``도 잘린 길이로 와서, 상태 코드나 길이 머리만으로는 잘린 것을 알 수 없다.
그대로 이어 붙이면 깨진 영상 파일이 오류 없이 완료된다. 그래서 컨테이너의 구조로 확인한다.

- fMP4 미디어 세그먼트: 최상위 상자의 크기를 더한 값이 본문 길이와 같고, ``moof``마다 그
  뒤에 ``mdat``가 있다
- fMP4 초기화 세그먼트: 최상위 상자의 크기를 더한 값이 본문 길이와 같고, ``ftyp``와
  ``moov``가 있다
- MPEG-TS: 길이가 188바이트의 배수이고 패킷마다 첫 바이트가 0x47이다
- AES-128-CBC 암호문(복호화 전): 길이가 16바이트의 배수다

이 모듈은 바이트만 본다. 요청과 재시도는 다운로더가 한다 — 여기서 나는
``TruncatedSegmentError``를 다운로더가 일시 오류로 다시 받는다.
"""

import io
from collections.abc import Iterator, Mapping
from typing import BinaryIO

from core.downloaders.decrypt import AES_BLOCK_SIZE, TS_PACKET_SIZE, TS_SYNC_BYTE

# 표시 계층이 번역하는 실패 키 원문 — 다시 받아도 계속 잘려 올 때 카드에 이 사유가 나온다
SEGMENT_TRUNCATED = "Segment was received truncated"

BOX_HEADER_SIZE = 8  # 바이트 — 크기(4) + 종류(4)
LARGE_BOX_HEADER_SIZE = 16  # 바이트 — 크기 칸이 1이면 뒤의 8바이트가 실제 크기다


class TruncatedSegmentError(Exception):
    """받은 세그먼트가 온전하지 않을 때 — 잘렸거나 컨테이너 구조가 맞지 않는다.

    ``message_key``는 번역하지 않은 i18n 키 원문이다. 표시 계층이 번역한다.
    """

    def __init__(self, detail: str):
        super().__init__(detail)
        self.message_key = SEGMENT_TRUNCATED


def check_content_length(headers: Mapping[str, str] | None, received: int) -> None:
    """응답이 ``Content-Length``를 선언했으면 받은 본문 길이와 견준다.

    선언이 없거나 숫자가 아니면 견주지 않는다. 본문이 압축돼 오면(``Content-Encoding``)
    선언된 길이는 압축된 길이라 받은 길이와 견줄 수 없다 — 그때도 견주지 않는다.

    Args:
        headers: 응답 머리. 없으면 None
        received: 받은 본문의 바이트 수

    Raises:
        TruncatedSegmentError: 선언된 길이와 받은 길이가 다를 때
    """
    declared = declared_length(headers)
    if declared is not None and declared != received:
        raise TruncatedSegmentError(f"Content-Length {declared}인데 받은 본문은 {received}바이트다")


def declared_length(headers: Mapping[str, str] | None) -> int | None:
    """응답이 선언한 본문 길이(바이트). 받을 본문의 길이로 믿을 수 없으면 None이다.

    선언이 없거나 숫자가 아닐 때, 그리고 본문이 압축돼 올 때(``Content-Encoding`` — 선언된
    길이는 압축된 길이다)가 그렇다.
    """
    if not headers or headers.get("Content-Encoding"):
        return None
    declared = headers.get("Content-Length")
    if declared is None or not str(declared).strip().isdigit():
        return None
    return int(declared)


def _top_level_boxes(stream: BinaryIO, length: int) -> Iterator[str]:
    """최상위 상자의 종류를 차례로 낸다. 상자들이 본문을 정확히 채우지 않으면 실패한다.

    상자의 내용은 읽지 않고 머리만 읽으며 건너뛴다.
    """
    offset = 0
    while offset < length:
        stream.seek(offset)
        header = stream.read(BOX_HEADER_SIZE)
        if len(header) < BOX_HEADER_SIZE:
            raise TruncatedSegmentError(f"{offset}바이트 자리의 상자 머리가 잘렸다")
        size = int.from_bytes(header[:4], "big")
        kind = header[4:8].decode("latin-1")
        header_size = BOX_HEADER_SIZE
        if size == 1:
            large = stream.read(LARGE_BOX_HEADER_SIZE - BOX_HEADER_SIZE)
            if len(large) < LARGE_BOX_HEADER_SIZE - BOX_HEADER_SIZE:
                raise TruncatedSegmentError(f"{offset}바이트 자리의 상자 머리가 잘렸다")
            size = int.from_bytes(large, "big")
            header_size = LARGE_BOX_HEADER_SIZE
        elif size == 0:
            size = length - offset  # 크기 0은 "본문 끝까지"다 (ISO/IEC 14496-12)
        if size < header_size:
            raise TruncatedSegmentError(f"{offset}바이트 자리의 상자({kind!r}) 크기가 맞지 않는다")
        if offset + size > length:
            raise TruncatedSegmentError(
                f"상자({kind!r})가 {offset + size}바이트까지인데 본문은 {length}바이트다"
            )
        yield kind
        offset += size


def check_fmp4_media_segment(stream: BinaryIO, length: int) -> None:
    """fMP4 미디어 세그먼트가 온전한지 확인한다.

    - 최상위 상자의 크기를 더한 값이 본문 길이와 같다
    - ``moof``가 하나 이상 있고, ``moof``마다 그 뒤에 ``mdat``가 있다. 사이에 다른
      상자(``emsg`` 등)가 있어도 된다. 마지막 ``moof`` 뒤에도 ``mdat``가 있어야 한다
    - 끝 상자의 종류는 묻지 않는다 — 실제 세그먼트는 ``mdat`` 뒤의 ``emsg``로 끝난다

    둘째 조건은 본문이 ``moof`` 뒤에서 정확히 잘린 경우를 잡는다 — 그때는 크기의 합이
    본문 길이와 같다. 조각(``moof`` + ``mdat``) 하나가 끝난 자리에서 정확히 잘린 본문은
    구조만으로는 온전한 세그먼트와 구별되지 않는다.

    Args:
        stream: 세그먼트 본문. 처음부터 읽을 수 있어야 한다(seek)
        length: 본문의 바이트 수

    Raises:
        TruncatedSegmentError: 온전하지 않을 때
    """
    fragments = 0
    awaiting_mdat = False
    for kind in _top_level_boxes(stream, length):
        if kind == "moof":
            if awaiting_mdat:
                raise TruncatedSegmentError("moof 뒤에 mdat 없이 다음 moof가 온다")
            fragments += 1
            awaiting_mdat = True
        elif kind == "mdat":
            awaiting_mdat = False
    if awaiting_mdat:
        raise TruncatedSegmentError("마지막 moof 뒤에 mdat가 없다")
    if not fragments:
        raise TruncatedSegmentError("세그먼트에 moof 상자가 없다")


def check_fmp4_init_segment(data: bytes) -> None:
    """fMP4 초기화 세그먼트가 온전한지 확인한다.

    최상위 상자의 크기를 더한 값이 본문 길이와 같고, ``ftyp``와 ``moov``가 있어야 한다.

    Raises:
        TruncatedSegmentError: 온전하지 않을 때
    """
    kinds = set(_top_level_boxes(io.BytesIO(data), len(data)))
    missing = [kind for kind in ("ftyp", "moov") if kind not in kinds]
    if missing:
        raise TruncatedSegmentError(f"초기화 세그먼트에 {' · '.join(missing)} 상자가 없다")


def check_cbc_ciphertext(data: bytes) -> None:
    """AES-128-CBC로 암호화된 세그먼트의 길이가 블록(16바이트)의 배수인지 확인한다.

    배수가 아니면 본문이 블록 중간에서 잘린 것이다. 복호화는 그런 입력을 키 · 정렬 문제로
    보고 다운로드 전체를 실패시키므로, 복호화하기 전에 걸러 다시 받게 한다.

    Raises:
        TruncatedSegmentError: 본문이 비었거나 길이가 블록의 배수가 아닐 때
    """
    if not data or len(data) % AES_BLOCK_SIZE:
        raise TruncatedSegmentError(
            f"암호화된 세그먼트 길이 {len(data)}바이트가 {AES_BLOCK_SIZE}의 배수가 아니다"
        )


def check_ts_segment(data: bytes) -> None:
    """MPEG-TS 세그먼트(암호화돼 있었다면 복호화한 뒤)가 온전한지 확인한다.

    길이가 188바이트의 배수이고, 패킷마다 첫 바이트가 0x47이어야 한다. 빈 본문은 온전하지
    않다.

    Raises:
        TruncatedSegmentError: 온전하지 않을 때
    """
    if not data:
        raise TruncatedSegmentError("세그먼트 본문이 비어 있다")
    if len(data) % TS_PACKET_SIZE:
        raise TruncatedSegmentError(
            f"세그먼트 길이 {len(data)}바이트가 {TS_PACKET_SIZE}의 배수가 아니다"
        )
    sync_bytes = data[::TS_PACKET_SIZE]
    if sync_bytes.count(TS_SYNC_BYTE) != len(sync_bytes):
        raise TruncatedSegmentError("0x47로 시작하지 않는 TS 패킷이 있다")
