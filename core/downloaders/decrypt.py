"""세그먼트 복호화 — HLS/SEA AES-128-CBC (#57, SPEC §8.1).

치지직의 암호화 VOD는 MPEG-DASH SEA(``aes128-cbc:2013``) = HLS
``METHOD=AES-128``과 동형인 **표준 세그먼트 암호화**다. 세그먼트 전체가
AES-128-CBC로 암호화되어 있고, 키는 플레이리스트의 ``#EXT-X-KEY URI``가
가리키는 치지직 API가 **인증된 세션에** 평문 HTTP로 내준다
(``keysys:http:2013``). CDM·라이선스 챌린지·PSSH는 없다.

이 모듈은 바이트 변환만 한다 — 키를 어떻게 얻는지는 모른다. 키 취득은
쿠키(=유저 본인 인증)가 필요해 앱 계층 리졸버가 주입한다
(core/services/download_service.py의 key_resolver).

**키 값은 로그·예외 메시지에 절대 싣지 않는다.**
"""

from Crypto.Cipher import AES

AES_BLOCK_SIZE = 16
# MPEG-TS 패킷은 188바이트이며 항상 0x47 동기 바이트로 시작한다 (ISO/IEC 13818-1)
TS_SYNC_BYTE = 0x47
TS_PACKET_SIZE = 188


def sequence_iv(sequence_number: int) -> bytes:
    """미디어 시퀀스 번호를 IV로 변환한다 (RFC 8216 §5.2).

    ``#EXT-X-KEY``에 IV 속성이 없을 때 쓰는 규칙 — 시퀀스 번호를 128비트
    빅엔디언으로 표현한 값이 그 세그먼트의 IV다.
    """
    return sequence_number.to_bytes(AES_BLOCK_SIZE, "big")


def _strip_pkcs7(data: bytes) -> bytes:
    """PKCS#7 패딩을 제거한다. 패딩이 유효하지 않으면 원본을 그대로 돌려준다.

    유효하지 않은 패딩은 대개 키가 틀렸다는 뜻이지만, 여기서 예외를 던지면
    패딩을 쓰지 않는 스트림까지 막게 되므로 판정은 호출부(looks_like_ts)에
    맡기고 이 함수는 손상 없이 통과시킨다.
    """
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= AES_BLOCK_SIZE and len(data) >= pad and data[-pad:] == bytes([pad]) * pad:
        return data[:-pad]
    return data


def decrypt_segment(data: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-128-CBC 세그먼트 하나를 복호화하고 PKCS#7 패딩을 제거한다.

    Args:
        data: 암호화된 세그먼트 바이트 (블록 크기의 배수여야 한다)
        key: 16바이트 AES-128 키
        iv: 16바이트 초기화 벡터

    Returns:
        bytes: 복호화된 세그먼트

    Raises:
        ValueError: 키·IV 길이가 16바이트가 아니거나 데이터가 블록 배수가 아닌 경우
    """
    if len(key) != AES_BLOCK_SIZE:
        raise ValueError(f"AES-128 키는 {AES_BLOCK_SIZE}바이트여야 한다 (받은 길이: {len(key)})")
    if len(iv) != AES_BLOCK_SIZE:
        raise ValueError(f"IV는 {AES_BLOCK_SIZE}바이트여야 한다 (받은 길이: {len(iv)})")
    if len(data) % AES_BLOCK_SIZE:
        raise ValueError(f"암호문 길이가 블록 배수가 아니다 (길이: {len(data)})")

    return _strip_pkcs7(AES.new(key, AES.MODE_CBC, iv).decrypt(data))


def looks_like_ts(data: bytes) -> bool:
    """복호화 결과가 MPEG-TS로 보이는지 검사한다 (키·IV 판정용).

    첫 바이트와 두 번째 패킷 경계의 동기 바이트를 확인한다. 키가 틀리면
    출력이 난수라 이 검사를 통과할 확률이 사실상 없으므로, 첫 세그먼트에서
    한 번 확인해 **수 GB짜리 쓰레기 파일을 만들기 전에** 실패시킬 수 있다.
    """
    if len(data) < TS_PACKET_SIZE + 1:
        return False
    return data[0] == TS_SYNC_BYTE and data[TS_PACKET_SIZE] == TS_SYNC_BYTE
