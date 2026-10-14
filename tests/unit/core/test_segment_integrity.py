"""받은 세그먼트를 내용으로 확인하는 규칙 — fMP4 상자 · MPEG-TS 패킷 · 길이 (#321).

입력은 상자 구조만 갖춘 합성 본문이다(tests/unit/fmp4_samples.py). 상자 순서
``styp moof mdat emsg moof mdat emsg``는 실제 세그먼트에서 본 것이다.
"""

import io

import pytest

from core.downloaders.integrity import (
    SEGMENT_TRUNCATED,
    TruncatedSegmentError,
    check_cbc_ciphertext,
    check_content_length,
    check_fmp4_init_segment,
    check_fmp4_media_segment,
    check_ts_segment,
    declared_content_length,
)
from tests.unit.fmp4_samples import box, real_shaped_media_segment, ts_segment

FRAGMENT = box(b"moof", b"m" * 16) + box(b"mdat", b"d" * 40)
INIT = box(b"ftyp", b"iso6") + box(b"moov", b"v" * 100)


def _media(body: bytes) -> None:
    check_fmp4_media_segment(io.BytesIO(body), len(body))


@pytest.mark.parametrize(
    "body",
    [
        real_shaped_media_segment(),  # styp moof mdat emsg moof mdat emsg
        FRAGMENT,  # moof mdat
        box(b"styp") + FRAGMENT + FRAGMENT,
        box(b"moof") + box(b"emsg", b"e") + box(b"mdat", b"d"),  # moof와 mdat 사이의 다른 상자
        # 크기 칸이 1이면 뒤의 8바이트가 크기다 (머리 16바이트 + 내용 4바이트 = 20)
        box(b"moof") + (1).to_bytes(4, "big") + b"mdat" + (20).to_bytes(8, "big") + b"dddd",
        box(b"moof") + (0).to_bytes(4, "big") + b"mdat" + b"dddd",  # 크기 0은 본문 끝까지
    ],
    ids=["real-shaped", "one-fragment", "two-fragments", "emsg-between", "large-size", "to-end"],
)
def test_intact_media_segment_passes(body):
    """상자가 본문을 정확히 채우고 moof마다 뒤에 mdat가 있는 미디어 세그먼트는 통과해야 한다.

    주석의 상자 순서마다
    -> 예외 없음
    """
    _media(body)


@pytest.mark.parametrize(
    "body",
    [
        real_shaped_media_segment()[:-1],  # 마지막 상자의 끝 1바이트가 없다
        real_shaped_media_segment()[:60],  # 첫 mdat의 중간에서 끝난다
        (box(b"styp") + FRAGMENT)[:13],  # 둘째 상자의 머리(8바이트) 중간에서 끝난다
        box(b"styp") + box(b"moof", b"m" * 16),  # moof 뒤에서 정확히 끝난다
        box(b"styp") + FRAGMENT + box(b"moof", b"m" * 16),  # 마지막 moof 뒤에 mdat가 없다
        box(b"moof") + box(b"moof") + box(b"mdat", b"d"),  # moof 뒤에 mdat 없이 다음 moof
        box(b"styp", b"msdh"),  # moof가 없다
        b"",
        (4).to_bytes(4, "big") + b"moof",  # 상자 크기가 머리보다 작다
    ],
    ids=[
        "last-byte-missing",
        "cut-inside-mdat",
        "cut-inside-header",
        "ends-after-moof",
        "last-moof-without-mdat",
        "moof-moof",
        "no-moof",
        "empty",
        "size-below-header",
    ],
)
def test_incomplete_media_segment_is_rejected(body):
    """상자가 본문을 정확히 채우지 않거나 mdat 없는 moof가 있는 미디어 세그먼트는 거부돼야 한다.

    주석의 본문마다
    -> TruncatedSegmentError, message_key == SEGMENT_TRUNCATED
    """
    with pytest.raises(TruncatedSegmentError) as raised:
        _media(body)

    assert raised.value.message_key == SEGMENT_TRUNCATED


def test_media_segment_cut_after_a_whole_fragment_is_not_detected():
    """조각 하나가 끝난 자리에서 정확히 잘린 미디어 세그먼트는 구조 검사를 통과한다 (알려진 한계).

    styp moof mdat emsg moof mdat emsg 에서 첫 emsg까지만 남긴 본문
    -> 예외 없음
    """
    body = real_shaped_media_segment()
    first_fragment_end = len(body) - (len(body) - len(box(b"styp", b"msdh"))) // 2

    _media(body[:first_fragment_end])


def test_intact_init_segment_passes():
    """ftyp와 moov가 본문을 정확히 채우는 초기화 세그먼트는 통과해야 한다.

    ftyp(4바이트 내용) + moov(100바이트 내용)
    -> 예외 없음
    """
    check_fmp4_init_segment(INIT)


@pytest.mark.parametrize(
    "body",
    [
        INIT[:-1],  # moov의 끝 1바이트가 없다
        INIT[:10],  # ftyp의 중간에서 끝난다
        box(b"ftyp", b"iso6"),  # ftyp 뒤에서 정확히 끝난다 — moov가 없다
        box(b"moov", b"v" * 100),  # ftyp가 없다
        b"",
    ],
    ids=["last-byte-missing", "cut-inside-ftyp", "no-moov", "no-ftyp", "empty"],
)
def test_incomplete_init_segment_is_rejected(body):
    """상자가 본문을 정확히 채우지 않거나 ftyp · moov가 없는 초기화 세그먼트는 거부돼야 한다.

    주석의 본문마다
    -> TruncatedSegmentError
    """
    with pytest.raises(TruncatedSegmentError):
        check_fmp4_init_segment(body)


def test_intact_ts_segment_passes():
    """길이가 188의 배수이고 패킷마다 0x47로 시작하는 TS 세그먼트는 통과해야 한다.

    패킷 3개(564바이트)
    -> 예외 없음
    """
    check_ts_segment(ts_segment(3))


@pytest.mark.parametrize(
    "body",
    [
        ts_segment(3)[:-1],  # 563바이트 — 188의 배수가 아니다
        ts_segment(3)[:200],
        # 길이는 564(188의 배수)이고 둘째 패킷의 첫 바이트가 0x47이 아니다
        ts_segment(1) + b"\x00" + ts_segment(2)[1:],
        b"",
    ],
    ids=["one-byte-short", "cut-inside-packet", "bad-sync-byte", "empty"],
)
def test_incomplete_ts_segment_is_rejected(body):
    """길이가 188의 배수가 아니거나 0x47로 시작하지 않는 패킷이 있는 TS 세그먼트는 거부돼야 한다.

    주석의 본문마다
    -> TruncatedSegmentError
    """
    with pytest.raises(TruncatedSegmentError):
        check_ts_segment(body)


@pytest.mark.parametrize(("length", "intact"), [(32, True), (16, True), (31, False), (0, False)])
def test_ciphertext_length_must_be_a_multiple_of_the_block(length, intact):
    """AES-128-CBC 암호문은 길이가 16의 배수일 때만 통과해야 한다.

    길이 32 · 16 -> 예외 없음 / 길이 31 · 0 -> TruncatedSegmentError
    """
    if intact:
        check_cbc_ciphertext(b"c" * length)
    else:
        with pytest.raises(TruncatedSegmentError):
            check_cbc_ciphertext(b"c" * length)


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Length": "600"},
        {},  # 선언이 없다
        None,  # 머리가 없는 응답
        {"Content-Length": "abc"},  # 숫자가 아니다
        {"Content-Length": "300", "Content-Encoding": "gzip"},  # 압축된 길이라 견줄 수 없다
    ],
    ids=["equal", "absent", "no-headers", "not-a-number", "encoded"],
)
def test_content_length_that_matches_or_cannot_be_compared_passes(headers):
    """Content-Length가 받은 길이와 같거나 견줄 수 없으면 통과해야 한다.

    주석의 머리마다, 받은 본문 600바이트
    -> 예외 없음
    """
    check_content_length(headers, 600)


def test_content_length_that_differs_is_rejected():
    """Content-Length가 받은 본문 길이와 다르면 거부돼야 한다.

    Content-Length 1000, 받은 본문 600바이트
    -> TruncatedSegmentError
    """
    with pytest.raises(TruncatedSegmentError):
        check_content_length({"Content-Length": "1000"}, 600)


def test_content_length_of_an_identity_encoded_body_is_compared():
    """Content-Encoding이 identity인 응답의 Content-Length는 받은 본문 길이와 견줘야 한다.

    Content-Length 1000 · Content-Encoding identity, 받은 본문 600바이트 · 1000바이트
    -> 600: TruncatedSegmentError / 1000: 예외 없음
    """
    headers = {"Content-Length": "1000", "Content-Encoding": "identity"}

    check_content_length(headers, 1000)
    with pytest.raises(TruncatedSegmentError):
        check_content_length(headers, 600)


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Content-Length": "65536"}, 65536),
        ({}, None),
        (None, None),
        ({"Content-Length": "100", "Content-Encoding": "gzip"}, None),  # 압축된 본문의 길이다
        ({"Content-Length": "100", "Content-Encoding": "identity"}, 100),
        ({"Content-Length": "abc"}, None),
    ],
    ids=["plain", "missing", "no-headers", "compressed", "identity", "not-a-number"],
)
def test_declared_content_length_is_given_only_when_it_can_be_compared(headers, expected):
    """declared_content_length는 받은 길이와 견줄 수 있는 Content-Length만 돌려주고, 아니면 None을 돌려줘야 한다.

    주석의 경우마다 응답 머리
    -> 기대값
    """
    assert declared_content_length(headers) == expected
