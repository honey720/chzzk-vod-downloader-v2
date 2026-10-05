"""암호화 TS 세그먼트 받기 · 복호화 · 검사(core/api/hls_ts.py) 단위 테스트 (#309).

입력은 tests/unit/core/ts_builder.py가 패킷을 직접 조립한 합성 TS를 AES-128-CBC로 암호화한
것이다. 서버는 띄우지 않는다 — tests/unit/core/range_host.py가 requests 안에서 답한다.

핵심 계약:
- 받은 세그먼트는 길이 → 16바이트 배수 → 복호화 → TS 검사 순서로 확인한다
- 세그먼트는 전체 요청으로 한 번만 받고, 복호화한 본문을 엔진과 같은 이름의 파일로 둔다
- 복호화 키는 모델 · Content · 로그 · 예외 메시지 어디에도 남지 않는다
"""

import logging
import os

import pytest
from Crypto.Cipher import AES

import core.api.hls_ts as hls_ts_module
from core.api.dash import parse_sea_manifest
from core.api.hls import parse_media_playlist
from core.api.hls_ts import (
    fetch_ts_head,
    fetch_ts_segment,
    open_ts_segment,
    playlist_ref,
    segment_iv,
    segment_streams,
    ts_key_uri,
    ts_segment_file_name,
)
from core.api.mpegts import parse_ts
from core.downloaders.decrypt import DecryptionError, sequence_iv
from core.downloaders.hls_aes_downloader import DecryptionError as AesDecryptionError
from core.downloaders.integrity import TruncatedSegmentError
from core.models.content import Content, ContentType
from core.models.ts_index import TsHead
from tests.unit.core.range_host import RangeHost
from tests.unit.core.ts_builder import Frame, build_ts

KEY = bytes.fromhex("3ac59f02d7e8416b90a1c2f4e5067788")  # 테스트용 키 — 실제 키가 아니다
WRONG_KEY = bytes.fromhex("00112233445566778899aabbccddeeff")
KEY_URI = "https://key.test/aes_key"


def _plain(number: int) -> bytes:
    """number번째 합성 세그먼트(복호화한 것) — 영상 3프레임과 오디오 PES 하나."""
    base = 90_000 * (number + 1)
    frames = [Frame(pts=base + n * 3000, idr=n == 0) for n in range(3)]
    return build_ts(frames, adts=[(base, 4)])


def _encrypt(plain: bytes, sequence: int, key: bytes = KEY) -> bytes:
    """PKCS#7로 채워 AES-128-CBC로 암호화한다 — IV는 미디어 시퀀스 번호."""
    pad = 16 - len(plain) % 16
    return AES.new(key, AES.MODE_CBC, sequence_iv(sequence)).encrypt(plain + bytes([pad]) * pad)


def _playlist(count: int = 3, key_line: str | None = None) -> bytes:
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-MEDIA-SEQUENCE:0"]
    lines.append(f'#EXT-X-KEY:METHOD=AES-128,URI="{KEY_URI}"' if key_line is None else key_line)
    for number in range(count):
        lines += ["#EXTINF:0.100000,", f"segment-{number:06d}.ts"]
    return "\n".join([*lines, "#EXT-X-ENDLIST"]).encode("utf-8")


def _files(count: int = 3) -> dict[str, bytes]:
    files = {"vod/media.m3u8": _playlist(count)}
    for number in range(count):
        files[f"vod/segment-{number:06d}.ts"] = _encrypt(_plain(number), number)
    return files


@pytest.fixture
def host(monkeypatch) -> RangeHost:
    """플레이리스트와 암호화한 세그먼트 3개를 내주는 호스트 — 모듈의 요청이 이 호스트로 간다."""
    served = RangeHost(_files())
    monkeypatch.setattr(hls_ts_module, "get_thread_session", served.session)
    return served


def _key_forms(key: bytes) -> list[str]:
    """키 값이 글에 섞여 나올 수 있는 모양들."""
    return [key.hex(), key.hex().upper(), repr(key), repr(key)[2:-1], str(list(key))]


def _has_key(text: str, key: bytes = KEY) -> bool:
    return any(form in text for form in _key_forms(key))


# ================================================================ open_ts_segment


def test_open_ts_segment_returns_the_decrypted_segment():
    """open_ts_segment는 온전한 암호화 세그먼트를 복호화한 TS로 돌려줘야 한다.

    합성 세그먼트 1을 암호화한 것, Content-Length가 받은 길이와 같다
    -> 복호화 전의 TS bytes
    """
    body = _encrypt(_plain(1), 1)

    plain = open_ts_segment(body, {"Content-Length": str(len(body))}, KEY, sequence_iv(1))

    assert plain == _plain(1)


@pytest.mark.parametrize(
    "damage",
    ["content-length", "not-block-multiple", "block-truncated", "bad-sync"],
)
def test_open_ts_segment_rejects_a_segment_that_is_not_whole(damage):
    """open_ts_segment는 길이 · 암호문 · 복호화한 TS 가운데 하나라도 온전하지 않으면 잘림 오류를 내야 한다.

    합성 세그먼트를 암호화한 것에 주석의 손상 —
    content-length: 머리의 길이가 본문보다 길다 / not-block-multiple: 끝 1바이트를 뺌 /
    block-truncated: 끝 16바이트(블록 하나)를 뺌 / bad-sync: 둘째 패킷의 동기 바이트를 바꿔 암호화
    -> TruncatedSegmentError
    """
    body = _encrypt(_plain(0), 0)
    headers = {"Content-Length": str(len(body))}
    if damage == "content-length":
        headers = {"Content-Length": str(len(body) + 16)}
    elif damage == "not-block-multiple":
        body = body[:-1]
        headers = None
    elif damage == "block-truncated":
        body = body[:-16]
        headers = None
    else:
        broken = bytearray(_plain(0))
        broken[188] = 0x00
        body = _encrypt(bytes(broken), 0)
        headers = None

    with pytest.raises(TruncatedSegmentError):
        open_ts_segment(body, headers, KEY, sequence_iv(0))


def test_open_ts_segment_tells_a_wrong_key_only_when_asked():
    """open_ts_segment는 key_check일 때만 TS가 아닌 복호화 결과를 키 오류로 알려야 한다.

    합성 세그먼트를 암호화한 것을 다른 키로 연다
    -> key_check=True: DecryptionError / key_check=False: TruncatedSegmentError
    """
    body = _encrypt(_plain(0), 0)

    with pytest.raises(DecryptionError):
        open_ts_segment(body, None, WRONG_KEY, sequence_iv(0), key_check=True)
    with pytest.raises(TruncatedSegmentError):
        open_ts_segment(body, None, WRONG_KEY, sequence_iv(0))


def test_decryption_error_is_the_same_class_from_both_modules():
    """DecryptionError는 decrypt 모듈과 hls_aes_downloader 모듈에서 같은 클래스로 import돼야 한다.

    두 모듈에서 import한 이름
    -> 같은 객체
    """
    assert AesDecryptionError is DecryptionError


@pytest.mark.parametrize(
    ("key_line", "index", "expected"),
    [
        ('#EXT-X-KEY:METHOD=AES-128,URI="k"', 5, sequence_iv(5)),
        ('#EXT-X-KEY:METHOD=AES-128,URI="k",IV=0x' + "ab" * 16, 5, bytes.fromhex("ab" * 16)),
    ],
    ids=["sequence-number", "explicit"],
)
def test_segment_iv_is_the_explicit_value_or_the_sequence_number(key_line, index, expected):
    """segment_iv는 #EXT-X-KEY에 IV가 있으면 그 값을, 없으면 미디어 시퀀스 번호를 돌려줘야 한다.

    주석의 #EXT-X-KEY 줄, MEDIA-SEQUENCE 0, 세그먼트 인덱스 5
    -> 기대값
    """
    playlist = parse_media_playlist(_playlist(6, key_line).decode("utf-8"))

    assert segment_iv(playlist, index) == expected


# ================================================================ 플레이리스트 · 세그먼트 받기


def test_fetch_ts_head_reads_the_playlist_and_fetches_no_segment(host):
    """fetch_ts_head는 플레이리스트만 받아 해석하고 세그먼트와 키는 받지 않아야 한다.

    세그먼트 셋 · AES-128 키가 적힌 플레이리스트
    -> 요청 == [플레이리스트], 세그먼트 셋, segments · stored가 비어 있다, 키 주소 == KEY_URI
    """
    url = host.url("vod/media.m3u8")

    head = fetch_ts_head(url, "unused")

    assert [name for _m, name, _h in host.requests] == ["vod/media.m3u8"]
    assert len(head.playlist.segments) == 3
    assert (head.segments, head.stored) == ({}, set())
    assert ts_key_uri(url, head) == KEY_URI


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.test/hls-aes/720/media.m3u8",
        "https://cdn.test/hls-aes/720/media.m3u8?token=abc&expires=1",
        "https://cdn.test/hls-aes/720/media.m3u8#part",
        "https://cdn.test/hls-aes/720/media.m3u8?token=zzz#part",
    ],
    ids=["bare", "query", "fragment", "query-and-fragment"],
)
def test_playlist_ref_has_no_query_and_no_fragment(url):
    """playlist_ref는 주소에서 쿼리와 프래그먼트를 뺀 값이어야 한다.

    같은 경로에 쿼리 · 프래그먼트만 다른 주소 넷
    -> 모두 "https://cdn.test/hls-aes/720/media.m3u8", "?" · "#" · "token" 없음
    """
    ref = playlist_ref(url)

    assert ref == "https://cdn.test/hls-aes/720/media.m3u8"
    assert "?" not in ref and "#" not in ref and "token" not in ref


def test_playlist_ref_differs_between_paths():
    """playlist_ref는 경로가 다른 플레이리스트끼리 달라야 한다.

    경로가 720 · 1080인 주소(쿼리는 같다)
    -> 두 값이 다르다
    """
    low = playlist_ref("https://cdn.test/hls-aes/720/media.m3u8?token=abc")
    high = playlist_ref("https://cdn.test/hls-aes/1080/media.m3u8?token=abc")

    assert low != high


@pytest.mark.parametrize(
    "fixture_name", ["dash_manifest_sea_13714380.xml", "dash_manifest_sea_14283698.xml"]
)
def test_playlist_ref_differs_between_the_resolutions_of_a_real_manifest(
    load_mock_response, fixture_name
):
    """실제 매니페스트의 해상도마다 플레이리스트 주소의 playlist_ref가 서로 달라야 한다.

    박제한 SEA 매니페스트의 해상도 셋(144 · 720 · 1080)의 플레이리스트 주소
    -> playlist_ref 셋이 모두 다르다
    """
    reps, _resolution, _url = parse_sea_manifest(load_mock_response(fixture_name))

    refs = {playlist_ref(url) for _height, url in reps}

    assert len(reps) == 3
    assert len(refs) == 3


def test_fetch_ts_head_records_the_playlist_ref_without_the_query(monkeypatch):
    """fetch_ts_head는 받은 주소에서 쿼리를 뺀 값을 playlist_ref에 적어야 한다.

    "vod/media.m3u8?token=abc"로 받는다
    -> head.playlist_ref == 호스트의 "vod/media.m3u8" 주소
    """
    files = _files()
    files["vod/media.m3u8?token=abc"] = files["vod/media.m3u8"]
    served = RangeHost(files)
    monkeypatch.setattr(hls_ts_module, "get_thread_session", served.session)

    head = fetch_ts_head(served.url("vod/media.m3u8?token=abc"))

    assert head.playlist_ref == served.url("vod/media.m3u8")


@pytest.mark.parametrize(
    "key_line",
    ["#EXT-X-VERSION:3", '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="k"'],
    ids=["no-key", "other-method"],
)
def test_fetch_ts_head_rejects_a_playlist_it_cannot_decrypt(monkeypatch, key_line):
    """fetch_ts_head는 #EXT-X-KEY가 없거나 AES-128이 아닌 플레이리스트를 거부해야 한다.

    주석의 플레이리스트
    -> DecryptionError
    """
    served = RangeHost({"vod/media.m3u8": _playlist(1, key_line)})
    monkeypatch.setattr(hls_ts_module, "get_thread_session", served.session)

    with pytest.raises(DecryptionError):
        fetch_ts_head(served.url("vod/media.m3u8"))


def test_segment_streams_fetches_once_and_stores_the_decrypted_segment(host, tmp_path):
    """segment_streams는 세그먼트를 전체 요청으로 한 번만 받고, 복호화한 본문을 엔진과 같은 이름으로 둬야 한다.

    세그먼트 1을 두 번 읽음. segment_dir 지정
    -> 세그먼트 요청 1건(Range 머리 없음), 파일 "1.ts" == 복호화한 bytes,
       stored == {1}, 결과 == parse_ts(복호화한 bytes), 둘째 호출은 같은 객체
    """
    url = host.url("vod/media.m3u8")
    folder = tmp_path / "segments"
    head = fetch_ts_head(url, str(folder))

    first = segment_streams(head, url, 1, KEY)
    again = segment_streams(head, url, 1, KEY)

    requests = [(name, header) for _m, name, header in host.requests if name.endswith(".ts")]
    assert requests == [("vod/segment-000001.ts", None)]
    assert (folder / ts_segment_file_name(3, 1)).read_bytes() == _plain(1)
    assert os.listdir(folder) == ["1.ts"]
    assert head.stored == {1}
    assert first == parse_ts(_plain(1))
    assert again is first


def test_segment_streams_keeps_nothing_without_a_folder(host):
    """segment_streams는 둘 폴더가 없으면 프레임 정보만 보관하고 stored에 적지 않아야 한다.

    segment_dir 없이 만든 TsHead로 세그먼트 0을 읽음
    -> 결과 == parse_ts(복호화한 bytes), stored가 비어 있다
    """
    url = host.url("vod/media.m3u8")
    head = fetch_ts_head(url)

    found = segment_streams(head, url, 0, KEY)

    assert found == parse_ts(_plain(0))
    assert head.stored == set()


@pytest.mark.parametrize(
    ("damage", "error"),
    [("truncated", TruncatedSegmentError), ("wrong-key", DecryptionError)],
)
def test_segment_streams_leaves_no_file_when_the_segment_is_rejected(
    monkeypatch, tmp_path, damage, error
):
    """segment_streams는 세그먼트가 온전하지 않거나 키가 맞지 않으면 실패하고 파일을 남기지 않아야 한다.

    truncated: 서버가 끝 16바이트를 뺀 세그먼트를 준다 / wrong-key: 다른 키로 읽는다
    -> 주석의 예외, segment_dir에 파일 없음, stored · segments가 비어 있다
    """
    files = _files()
    if damage == "truncated":
        files["vod/segment-000000.ts"] = files["vod/segment-000000.ts"][:-16]
    served = RangeHost(files)
    monkeypatch.setattr(hls_ts_module, "get_thread_session", served.session)
    url = served.url("vod/media.m3u8")
    folder = tmp_path / "segments"
    head = fetch_ts_head(url, str(folder))

    with pytest.raises(error):
        segment_streams(head, url, 0, WRONG_KEY if damage == "wrong-key" else KEY)

    assert not folder.exists() or os.listdir(folder) == []
    assert (head.stored, head.segments) == (set(), {})


@pytest.mark.parametrize(
    ("count", "index", "expected"),
    [(6, 0, "0.ts"), (6, 5, "5.ts"), (2351, 28, "0028.ts"), (10, 9, "09.ts"), (10, 0, "00.ts")],
)
def test_ts_segment_file_name_is_zero_based_and_padded(count, index, expected):
    """ts_segment_file_name은 세그먼트 번호(0부터)를 세그먼트 수의 자릿수만큼 0으로 채운 이름을 돌려줘야 한다.

    주석의 경우마다 (세그먼트 수, 인덱스)
    -> 기대한 파일 이름
    """
    assert ts_segment_file_name(count, index) == expected


# ================================================================ 키가 남지 않는다


def _logged(caplog) -> str:
    """캡처한 로그 레코드 전체를 한 글로 — 메시지 · 인자 · 예외 글."""
    parts = []
    for record in caplog.records:
        parts += [record.getMessage(), repr(record.args), record.exc_text or ""]
        if record.exc_info and record.exc_info[1] is not None:
            parts.append(repr(record.exc_info[1]))
    return "\n".join(parts)


def test_key_is_not_left_in_the_model_the_content_or_the_log_on_success(host, tmp_path, caplog):
    """세그먼트를 받아 복호화한 뒤 TsHead · Content의 repr과 로그 어디에도 키 값이 없어야 한다.

    세그먼트 셋을 KEY로 읽은 TsHead를 Content.ts_head에 실음. 로그는 모든 로거를 DEBUG로 캡처
    -> repr(head) · repr(content) · 로그 전체에 키의 16진 · bytes 표기 없음
       (캡처가 살아 있는지 표식 레코드로 먼저 확인)
    """
    url = host.url("vod/media.m3u8")
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("core.api.hls_ts").debug("표식")
        head = fetch_ts_head(url, str(tmp_path / "segments"))
        for index in range(3):
            segment_streams(head, url, index, KEY)
    content = Content(content_type=ContentType.CHZZK_VIDEO_HLS_AES, url="u", ts_head=head)

    assert "표식" in _logged(caplog)
    assert head.stored == {0, 1, 2}
    assert not _has_key(repr(head))
    assert not _has_key(repr(content))
    assert not _has_key(_logged(caplog))
    assert all(not isinstance(value, bytes | bytearray) for value in vars(head).values())


@pytest.mark.parametrize("damage", ["wrong-key", "truncated", "short-key"])
def test_key_is_not_left_in_the_error_the_model_or_the_log_on_failure(
    monkeypatch, tmp_path, caplog, damage
):
    """세그먼트 읽기가 실패해도 예외 글 · TsHead의 repr · 로그 어디에도 준 키 값이 없어야 한다.

    wrong-key: 다른 키로 읽는다 / truncated: 잘린 세그먼트 / short-key: 15바이트 키
    -> 예외가 나고, str · repr(예외) · repr(head) · 로그 전체에 준 키의 표기 없음
    """
    files = _files()
    key = KEY
    if damage == "wrong-key":
        key = WRONG_KEY
    elif damage == "truncated":
        files["vod/segment-000000.ts"] = files["vod/segment-000000.ts"][:-16]
    else:
        key = KEY[:15]
    served = RangeHost(files)
    monkeypatch.setattr(hls_ts_module, "get_thread_session", served.session)
    url = served.url("vod/media.m3u8")

    with caplog.at_level(logging.DEBUG):
        logging.getLogger("core.api.hls_ts").debug("표식")
        head = fetch_ts_head(url, str(tmp_path / "segments"))
        with pytest.raises((DecryptionError, TruncatedSegmentError, ValueError)) as info:
            segment_streams(head, url, 0, key)

    assert "표식" in _logged(caplog)
    for text in (str(info.value), repr(info.value), repr(head), _logged(caplog)):
        assert not _has_key(text, key)


def test_ts_head_has_no_field_that_can_hold_a_key():
    """TsHead에는 키를 담을 칸이 없어야 한다 — 필드 이름이 정해진 여섯 개다.

    TsHead의 필드 이름
    -> {"playlist", "segments", "frame_rate", "segment_dir", "stored", "playlist_ref"}
    """
    assert set(TsHead.__dataclass_fields__) == {
        "playlist",
        "segments",
        "frame_rate",
        "segment_dir",
        "stored",
        "playlist_ref",
    }


def test_fetch_ts_segment_sends_a_whole_request(host):
    """fetch_ts_segment는 범위 없는 요청 하나로 세그먼트를 받아 복호화해야 한다.

    세그먼트 2의 주소
    -> 요청 1건 · Range 머리 없음, 결과 == 복호화한 bytes
    """
    plain = fetch_ts_segment(host.url("vod/segment-000002.ts"), KEY, sequence_iv(2))

    assert host.requests == [("GET", "vod/segment-000002.ts", None)]
    assert plain == _plain(2)
