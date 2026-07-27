"""HLS 미디어 플레이리스트 파싱·세그먼트 복호화 규칙 검증 (#57).

core/api/hls.py(순수 파서)와 core/downloaders/decrypt.py(AES-128-CBC)의 계약을
고정한다. 실제 치지직 응답에서 확인된 형태를 기준으로 삼는다:
``#EXT-X-KEY:METHOD=AES-128,URI="..."`` — **IV 속성 없음**, ``EXT-X-MAP`` 없음,
``EXT-X-MEDIA-SEQUENCE`` 0, 세그먼트 확장자 .ts.
"""

import pytest
from Crypto.Cipher import AES

from core.api.hls import HlsKey, parse_media_playlist
from core.downloaders.decrypt import (
    AES_BLOCK_SIZE,
    TS_PACKET_SIZE,
    decrypt_segment,
    looks_like_ts,
    sequence_iv,
)

KEY_URI = "https://api.chzzk.naver.com/service/v1/encryption/videos/VID/aes_key"

# 치지직 실응답과 같은 형태의 최소 플레이리스트 (IV 속성 없음)
CHZZK_STYLE = "\n".join(
    [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-TARGETDURATION:4",
        "#EXT-X-MEDIA-SEQUENCE:0",
        f'#EXT-X-KEY:METHOD=AES-128,URI="{KEY_URI}"',
        "#EXTINF:3.840,",
        "segment-000000.ts",
        "#EXTINF:3.840,",
        "segment-000001.ts",
        "#EXT-X-ENDLIST",
    ]
)


# ================================================================ 플레이리스트 파싱


def test_parses_chzzk_style_playlist():
    """치지직 실응답 형태: AES-128 키(IV 없음), TS 세그먼트, 초기화 세그먼트 없음."""
    pl = parse_media_playlist(CHZZK_STYLE)

    assert pl.segments == ("segment-000000.ts", "segment-000001.ts")
    assert pl.media_sequence == 0
    assert pl.init_uri is None
    assert pl.key == HlsKey(method="AES-128", uri=KEY_URI, iv=None)
    assert pl.key.is_aes_128 is True


def test_sequence_of_offsets_by_media_sequence():
    """세그먼트의 시퀀스 번호는 EXT-X-MEDIA-SEQUENCE부터 센다 (IV 유도의 근거)."""
    pl = parse_media_playlist(CHZZK_STYLE.replace("MEDIA-SEQUENCE:0", "MEDIA-SEQUENCE:100"))

    assert pl.media_sequence == 100
    assert (pl.sequence_of(0), pl.sequence_of(1)) == (100, 101)


def test_explicit_iv_is_parsed_as_bytes():
    """IV 속성이 있으면 16진 문자열을 바이트로 파싱한다."""
    iv_hex = "0x000102030405060708090a0b0c0d0e0f"
    pl = parse_media_playlist(
        CHZZK_STYLE.replace(f'URI="{KEY_URI}"', f'URI="{KEY_URI}",IV={iv_hex}')
    )

    assert pl.key.iv == bytes(range(16))


def test_method_none_means_unencrypted():
    """METHOD=NONE은 암호화 해제 지시이므로 키 없음으로 다룬다."""
    pl = parse_media_playlist(CHZZK_STYLE.replace(f'METHOD=AES-128,URI="{KEY_URI}"', "METHOD=NONE"))

    assert pl.key is None
    assert pl.segments  # 세그먼트 목록 자체는 그대로 파싱된다


def test_ext_x_map_is_parsed_when_present():
    """fMP4 플레이리스트의 초기화 세그먼트(EXT-X-MAP)도 파싱한다."""
    pl = parse_media_playlist(CHZZK_STYLE.replace("#EXTM3U", '#EXTM3U\n#EXT-X-MAP:URI="init.m4s"'))

    assert pl.init_uri == "init.m4s"


def test_plain_playlist_has_no_key():
    """평문 플레이리스트는 키 없이 세그먼트만 나온다."""
    pl = parse_media_playlist("#EXTM3U\n#EXTINF:2.0,\na.ts\n#EXT-X-ENDLIST")

    assert pl.key is None
    assert pl.segments == ("a.ts",)


# ================================================================ 복호화


def _encrypt(plain: bytes, key: bytes, iv: bytes) -> bytes:
    """테스트용 암호화 — PKCS#7 패딩 후 AES-128-CBC."""
    pad = AES_BLOCK_SIZE - (len(plain) % AES_BLOCK_SIZE)
    padded = plain + bytes([pad]) * pad
    return AES.new(key, AES.MODE_CBC, iv).encrypt(padded)


def _fake_ts(packets: int = 3) -> bytes:
    """동기 바이트가 붙은 가짜 MPEG-TS 바이트열."""
    return b"".join(bytes([0x47]) + bytes(TS_PACKET_SIZE - 1) for _ in range(packets))


def test_decrypt_round_trip_strips_padding():
    """암호화 → 복호화가 원본과 정확히 일치해야 한다 (PKCS#7 패딩 제거 포함)."""
    key, iv = bytes(range(16)), sequence_iv(7)
    plain = _fake_ts()

    assert decrypt_segment(_encrypt(plain, key, iv), key, iv) == plain


def test_sequence_iv_is_big_endian_128bit():
    """IV는 시퀀스 번호의 128비트 빅엔디언 표현이다 (RFC 8216 §5.2)."""
    assert sequence_iv(0) == bytes(16)
    assert sequence_iv(1) == bytes(15) + b"\x01"
    assert sequence_iv(258) == bytes(14) + b"\x01\x02"


@pytest.mark.parametrize(
    ("key", "iv", "data"),
    [
        (bytes(8), bytes(16), bytes(16)),  # 키 길이 오류
        (bytes(16), bytes(8), bytes(16)),  # IV 길이 오류
        (bytes(16), bytes(16), bytes(17)),  # 블록 배수 아님
    ],
)
def test_decrypt_rejects_malformed_input(key, iv, data):
    """키·IV 길이와 블록 정렬이 어긋나면 ValueError로 실패해야 한다."""
    with pytest.raises(ValueError):
        decrypt_segment(data, key, iv)


def test_looks_like_ts_detects_sync_bytes():
    """TS 판정은 첫 바이트와 다음 패킷 경계의 동기 바이트(0x47)로 한다."""
    assert looks_like_ts(_fake_ts()) is True
    # 키가 틀리면 난수가 되어 통과할 수 없다
    assert looks_like_ts(bytes(TS_PACKET_SIZE * 2)) is False
    assert looks_like_ts(b"\x47" * 10) is False  # 너무 짧다
