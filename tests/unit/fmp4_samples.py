"""fMP4 · MPEG-TS 세그먼트의 합성 본문 — 상자 구조만 갖춘 최소 입력 (#321).

다운로더가 받은 세그먼트를 구조로 확인하므로(core/downloaders/integrity.py), 세그먼트를
끝까지 받는 테스트의 입력은 유효한 상자여야 한다. 내용은 임의의 바이트다 — 실제 영상의
바이트가 아니다.
"""

TS_PACKET_SIZE = 188  # 바이트
TS_SYNC_BYTE = 0x47  # TS 패킷의 첫 바이트
BOX_HEADER_SIZE = 8  # 바이트 — 크기(4) + 종류(4)


def box(kind: bytes, payload: bytes = b"") -> bytes:
    """ISO BMFF 상자 하나 — 크기(4바이트 빅엔디언) + 종류(4바이트) + 내용."""
    return (BOX_HEADER_SIZE + len(payload)).to_bytes(4, "big") + kind + payload


def media_segment(length: int, fill: bytes = b"x") -> bytes:
    """길이가 정확히 length인 미디어 세그먼트 — 빈 ``moof`` 뒤에 fill로 채운 ``mdat``."""
    return box(b"moof") + box(b"mdat", fill * (length - 2 * BOX_HEADER_SIZE))


def real_shaped_media_segment(payload: bytes = b"x" * 64) -> bytes:
    """실제 세그먼트와 같은 상자 순서 — ``styp moof mdat emsg moof mdat emsg``."""
    fragment = box(b"moof", b"m" * 16) + box(b"mdat", payload) + box(b"emsg", b"e" * 8)
    return box(b"styp", b"msdh") + fragment + fragment


def init_segment(length: int, fill: bytes = b"\xf0") -> bytes:
    """길이가 정확히 length인 초기화 세그먼트 — 빈 ``ftyp`` 뒤에 fill로 채운 ``moov``."""
    return box(b"ftyp") + box(b"moov", fill * (length - 2 * BOX_HEADER_SIZE))


def ts_segment(packets: int, fill: int = 1) -> bytes:
    """TS 패킷 packets개 — 패킷마다 0x47로 시작하고 나머지는 fill 바이트다."""
    return (bytes([TS_SYNC_BYTE]) + bytes([fill]) * (TS_PACKET_SIZE - 1)) * packets


def split(body: bytes, size: int) -> list[bytes]:
    """본문을 size 바이트씩 나눈 청크 목록."""
    return [body[offset : offset + size] for offset in range(0, len(body), size)]
