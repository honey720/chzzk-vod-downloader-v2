"""잘려 받아진 세그먼트를 다시 받고, 계속 잘려 오면 실패시키는지 run()으로 검증한다 (#321).

가짜 세션은 요청을 보내지 않는다. 잘린 본문은 ``Content-Length``까지 잘린 길이로 준다 —
상태 코드와 길이 머리로는 잘린 것을 알 수 없는 응답이다.

- m3u8: fMP4 미디어 세그먼트, 초기화 세그먼트
- hls_aes: AES-128-CBC로 암호화된 MPEG-TS 세그먼트
"""

import pytest
import requests
from Crypto.Cipher import AES

import core.downloaders.base as base_module
import core.downloaders.hls_aes_downloader as aes_module
import core.downloaders.m3u8_downloader as m3u8_module
from core.downloaders.decrypt import sequence_iv
from core.downloaders.hls_aes_downloader import HlsAesDownloader
from core.downloaders.integrity import SEGMENT_TRUNCATED, TruncatedSegmentError
from core.downloaders.m3u8_downloader import M3U8Downloader
from core.models.download_data import DownloadData
from tests.unit.fmp4_samples import box, real_shaped_media_segment, ts_segment

ATTEMPTS = 11  # 첫 시도 1 + 일시 오류로 다시 받는 횟수 10 (#131의 상한)
SEGMENTS = 3
KEY = bytes(range(16))

M3U8_URL = "https://example.invalid/hls/video.m3u8"
M3U8_PLAYLIST = "\n".join(
    ["#EXTM3U", '#EXT-X-MAP:URI="init.m4s"']
    + [line for i in range(SEGMENTS) for line in ("#EXTINF:2.000,", f"seg_{i}.m4v")]
    + ["#EXT-X-ENDLIST"]
)
INIT = box(b"ftyp", b"iso6") + box(b"moov", b"v" * 64)

AES_URL = "https://example.invalid/sea/media.m3u8"
AES_PLAYLIST = "\n".join(
    ["#EXTM3U", "#EXT-X-MEDIA-SEQUENCE:0", '#EXT-X-KEY:METHOD=AES-128,URI="https://k.invalid/key"']
    + [line for i in range(SEGMENTS) for line in ("#EXTINF:3.840,", f"segment-{i}.ts")]
    + ["#EXT-X-ENDLIST"]
)


def _media(index: int) -> bytes:
    """index번째 미디어 세그먼트 — 실제와 같은 상자 순서, 세그먼트마다 다른 내용."""
    return real_shaped_media_segment(bytes([index + 1]) * 64)


def _plain_ts(index: int) -> bytes:
    return ts_segment(4, fill=index + 1)


def _encrypted(index: int) -> bytes:
    plain = _plain_ts(index)
    pad = 16 - len(plain) % 16
    cipher = AES.new(KEY, AES.MODE_CBC, sequence_iv(index))
    return cipher.encrypt(plain + bytes([pad]) * pad)


class _Declared:
    """본문과 다른 길이를 Content-Length로 선언해서 줄 본문."""

    def __init__(self, body: bytes, declared: int):
        self.body = body
        self.declared = declared


class _BrokenRead:
    """본문을 읽다가 예외가 나는 응답 — 응답은 왔지만 본문이 끝까지 오지 않았다."""

    def __init__(self, error: Exception):
        self.error = error

    def raise_for_status(self):
        pass

    @property
    def content(self) -> bytes:
        raise self.error


class _NoResponse:
    """요청 자체가 예외로 끝나는 경우 — 연결 실패 · 타임아웃."""

    def __init__(self, error: Exception):
        self.error = error


class _Response:
    """본문과 Content-Length를 가진 가짜 응답 — 따로 정하지 않으면 본문의 길이를 선언한다."""

    def __init__(self, body: bytes | _Declared):
        declared = body.declared if isinstance(body, _Declared) else len(body)
        self.content = body.body if isinstance(body, _Declared) else body
        self.text = self.content.decode("utf-8", "replace")
        self.headers = {"Content-Length": str(declared)}

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=8192):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset : offset + chunk_size]


class _Session:
    """주소마다 준비된 본문을 차례로 준다. 마지막 본문은 되풀이한다. 요청 횟수를 센다."""

    def __init__(self, bodies: dict[str, list[bytes]]):
        self._bodies = bodies
        self.requests: dict[str, int] = {}

    def get(self, url, **kwargs):
        name = url.rsplit("/", 1)[1]
        served = self.requests.get(name, 0)
        self.requests[name] = served + 1
        queue = self._bodies[name]
        body = queue[min(served, len(queue) - 1)]
        if isinstance(body, _NoResponse):
            raise body.error
        return body if isinstance(body, _BrokenRead) else _Response(body)


class _Logger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _passthrough_remux_stream(chunks, dst_path):
    """받은 스트림을 그대로 파일에 쓰는 remux_stream 스텁 — 공급된 바이트를 잰다."""
    with open(dst_path, "wb") as f:
        for chunk in chunks:
            f.write(chunk)


def _run(cls, module, url, content_type, session, tmp_path, monkeypatch):
    """가짜 세션으로 run()을 끝까지 돌리고 (데이터, 실패 콜백이 받은 것, 완료 횟수)를 돌려준다."""
    monkeypatch.setattr(module, "get_thread_session", lambda: session)
    monkeypatch.setattr(base_module, "remux_stream", _passthrough_remux_stream)
    data = DownloadData(
        base_url=url,
        vod_url="https://chzzk.naver.com/video/1",
        output_path=str(tmp_path / "out.mp4"),
        resolution=1080,
        content_type=content_type,
    )
    failures: list[BaseException] = []
    finished: list[bool] = []

    def on_finished():
        data.model.finish()
        finished.append(True)

    engine = cls(data, _Logger(), on_finished=on_finished, on_failed=failures.append)
    if cls is HlsAesDownloader:
        engine.set_key_resolver(lambda content, key_uri: KEY)
    data.model.start()
    engine.run()
    return data, failures, finished


def _run_m3u8(tmp_path, monkeypatch, **overrides: list[bytes]):
    bodies = {"video.m3u8": [M3U8_PLAYLIST.encode()], "init.m4s": [INIT]}
    bodies.update({f"seg_{i}.m4v": [_media(i)] for i in range(SEGMENTS)})
    bodies.update(overrides)
    session = _Session(bodies)
    result = _run(M3U8Downloader, m3u8_module, M3U8_URL, "m3u8", session, tmp_path, monkeypatch)
    return session, *result


def _run_aes(tmp_path, monkeypatch, **overrides: list[bytes]):
    bodies = {"media.m3u8": [AES_PLAYLIST.encode()]}
    bodies.update({f"segment-{i}.ts": [_encrypted(i)] for i in range(SEGMENTS)})
    bodies.update(overrides)
    session = _Session(bodies)
    result = _run(HlsAesDownloader, aes_module, AES_URL, "hls_aes", session, tmp_path, monkeypatch)
    return session, *result


M3U8_OUTPUT = INIT + b"".join(_media(i) for i in range(SEGMENTS))
AES_OUTPUT = b"".join(_plain_ts(i) for i in range(SEGMENTS))

# 잘린 미디어 세그먼트 — 둘째 세그먼트(seg_1)의 본문
MEDIA_CUTS = {
    "cut-inside-mdat": _media(1)[:60],
    "ends-after-moof": _media(1)[:36],  # styp(12) + moof(24) — 상자 경계에서 정확히 끝난다
}
# 잘린 암호문 — 둘째 세그먼트(segment-1)의 본문
CIPHER_CUTS = {
    "cut-inside-block": _encrypted(1)[:100],  # 16의 배수가 아니다
    "cut-at-block-boundary": _encrypted(1)[:192],  # 16의 배수, 복호화하면 192바이트
}


def _assert_failed_as_truncated(failures, finished, data, tmp_path):
    assert len(failures) == 1 and isinstance(failures[0], TruncatedSegmentError)
    assert failures[0].message_key == SEGMENT_TRUNCATED
    assert finished == []
    assert not (tmp_path / "out.mp4").exists()


# ================================================================ m3u8 — 미디어 세그먼트


def test_intact_m3u8_download_completes_with_one_request_per_segment(tmp_path, monkeypatch):
    """온전한 세그먼트만 오면 다시 받지 않고 완료돼야 한다.

    초기화 세그먼트와 미디어 세그먼트 3개가 전부 온전한 가짜 세션
    -> 완료 1회, 실패 없음, 주소마다 요청 1회, 산출물 == 초기화 + 세그먼트 3개
    """
    session, data, failures, finished = _run_m3u8(tmp_path, monkeypatch)

    assert (failures, finished) == ([], [True])
    assert set(session.requests.values()) == {1}
    assert data.failed_threads == 0
    assert (tmp_path / "out.mp4").read_bytes() == M3U8_OUTPUT


@pytest.mark.parametrize("cut", MEDIA_CUTS)
def test_truncated_media_segment_is_fetched_again_until_intact(cut, tmp_path, monkeypatch):
    """잘린 미디어 세그먼트는 다시 받고, 온전한 본문이 오면 다운로드가 완료돼야 한다.

    seg_1이 두 번 잘려 오고(Content-Length도 잘린 길이) 세 번째에 온전하게 온다
    -> 완료 1회, 실패 없음, seg_1 요청 3회, 산출물 == 초기화 + 온전한 세그먼트 3개
    """
    truncated = MEDIA_CUTS[cut]
    session, data, failures, finished = _run_m3u8(
        tmp_path, monkeypatch, **{"seg_1.m4v": [truncated, truncated, _media(1)]}
    )

    assert (failures, finished) == ([], [True])
    assert session.requests["seg_1.m4v"] == 3
    assert data.failed_threads == 2
    assert (tmp_path / "out.mp4").read_bytes() == M3U8_OUTPUT


@pytest.mark.parametrize("cut", MEDIA_CUTS)
def test_media_segment_that_stays_truncated_fails_the_download(cut, tmp_path, monkeypatch):
    """미디어 세그먼트가 계속 잘려 오면 상한까지 다시 받은 뒤 잘림 실패 키로 실패해야 한다.

    seg_1이 항상 잘려 온다
    -> seg_1 요청 11회, 실패 콜백이 TruncatedSegmentError 하나를 받음, 완료 없음, 산출물 없음
    """
    session, data, failures, finished = _run_m3u8(
        tmp_path, monkeypatch, **{"seg_1.m4v": [MEDIA_CUTS[cut]]}
    )

    assert session.requests["seg_1.m4v"] == ATTEMPTS
    _assert_failed_as_truncated(failures, finished, data, tmp_path)


def test_media_segment_shorter_than_its_content_length_is_fetched_again(tmp_path, monkeypatch):
    """받은 미디어 세그먼트가 선언된 Content-Length와 길이가 다르면 다시 받아야 한다.

    seg_1이 처음에는 상자 구조가 온전한 본문에 100바이트 더 긴 Content-Length로 오고,
    다음에는 맞는 Content-Length로 온다
    -> 완료 1회, 실패 없음, seg_1 요청 2회
    """
    mismatched = _Declared(_media(1), len(_media(1)) + 100)
    session, _data, failures, finished = _run_m3u8(
        tmp_path, monkeypatch, **{"seg_1.m4v": [mismatched, _media(1)]}
    )

    assert (failures, finished) == ([], [True])
    assert session.requests["seg_1.m4v"] == 2
    assert (tmp_path / "out.mp4").read_bytes() == M3U8_OUTPUT


# ================================================================ m3u8 — 초기화 세그먼트

INIT_CUTS = {
    "cut-inside-moov": INIT[:40],
    "ends-after-ftyp": INIT[:12],  # ftyp(12) — 상자 경계에서 정확히 끝난다
}


@pytest.mark.parametrize("cut", INIT_CUTS)
def test_truncated_init_segment_is_fetched_again_until_intact(cut, tmp_path, monkeypatch):
    """잘린 초기화 세그먼트는 다시 받고, 온전한 본문이 오면 다운로드가 완료돼야 한다.

    init.m4s가 두 번 잘려 오고 세 번째에 온전하게 온다
    -> 완료 1회, 실패 없음, init.m4s 요청 3회, 산출물 == 초기화 + 세그먼트 3개
    """
    truncated = INIT_CUTS[cut]
    session, _data, failures, finished = _run_m3u8(
        tmp_path, monkeypatch, **{"init.m4s": [truncated, truncated, INIT]}
    )

    assert (failures, finished) == ([], [True])
    assert session.requests["init.m4s"] == 3
    assert (tmp_path / "out.mp4").read_bytes() == M3U8_OUTPUT


@pytest.mark.parametrize("cut", INIT_CUTS)
def test_init_segment_that_stays_truncated_fails_the_download(cut, tmp_path, monkeypatch):
    """초기화 세그먼트가 계속 잘려 오면 상한까지 다시 받은 뒤 잘림 실패 키로 실패해야 한다.

    init.m4s가 항상 잘려 온다
    -> init.m4s 요청 11회, 미디어 세그먼트 요청 없음, 실패 콜백이 TruncatedSegmentError 하나를 받음
    """
    session, data, failures, finished = _run_m3u8(
        tmp_path, monkeypatch, **{"init.m4s": [INIT_CUTS[cut]]}
    )

    assert session.requests["init.m4s"] == ATTEMPTS
    assert not any(name.startswith("seg_") for name in session.requests)
    _assert_failed_as_truncated(failures, finished, data, tmp_path)


# 본문이 선언된 Content-Length보다 짧을 때 requests가 본문을 읽으며 내는 예외들
SHORT_READS = {
    "chunked": requests.exceptions.ChunkedEncodingError("IncompleteRead"),
    "decoding": requests.exceptions.ContentDecodingError("incomplete stream"),
}


@pytest.mark.parametrize("error", SHORT_READS)
def test_init_segment_whose_body_ends_early_is_fetched_again_until_intact(
    error, tmp_path, monkeypatch
):
    """초기화 세그먼트의 본문이 선언된 길이보다 먼저 끝나면 다시 받고, 온전하면 완료돼야 한다.

    init.m4s의 본문 읽기가 두 번 주석의 예외로 끝나고 세 번째에 온전하게 온다
    -> 완료 1회, 실패 없음, init.m4s 요청 3회, 산출물 == 초기화 + 세그먼트 3개
    """
    short = _BrokenRead(SHORT_READS[error])
    session, _data, failures, finished = _run_m3u8(
        tmp_path, monkeypatch, **{"init.m4s": [short, short, INIT]}
    )

    assert (failures, finished) == ([], [True])
    assert session.requests["init.m4s"] == 3
    assert (tmp_path / "out.mp4").read_bytes() == M3U8_OUTPUT


@pytest.mark.parametrize("error", SHORT_READS)
def test_init_segment_whose_body_keeps_ending_early_fails_as_truncated(
    error, tmp_path, monkeypatch
):
    """초기화 세그먼트의 본문이 계속 먼저 끝나면 상한까지 다시 받은 뒤 잘림 실패 키로 실패해야 한다.

    init.m4s의 본문 읽기가 항상 주석의 예외로 끝난다
    -> init.m4s 요청 11회, 실패 콜백이 TruncatedSegmentError 하나를 받고 원인은 그 예외
    """
    session, data, failures, finished = _run_m3u8(
        tmp_path, monkeypatch, **{"init.m4s": [_BrokenRead(SHORT_READS[error])]}
    )

    assert session.requests["init.m4s"] == ATTEMPTS
    _assert_failed_as_truncated(failures, finished, data, tmp_path)
    assert failures[0].__cause__ is SHORT_READS[error]


NETWORK_ERRORS = {
    "connection": requests.ConnectionError("연결 실패"),
    "timeout": requests.ReadTimeout("응답 없음"),
}


@pytest.mark.parametrize("error", NETWORK_ERRORS)
def test_network_error_on_the_init_segment_fails_without_fetching_again(
    error, tmp_path, monkeypatch
):
    """초기화 세그먼트 요청이 연결 실패 · 타임아웃으로 끝나면 다시 받지 않고 그 예외로 실패해야 한다.

    init.m4s 요청이 주석의 예외로 끝난다
    -> init.m4s 요청 1회, 실패 콜백이 그 예외 하나를 받음, 완료 없음
    """
    session, _data, failures, finished = _run_m3u8(
        tmp_path, monkeypatch, **{"init.m4s": [_NoResponse(NETWORK_ERRORS[error])]}
    )

    assert session.requests["init.m4s"] == 1
    assert failures == [NETWORK_ERRORS[error]]
    assert finished == []


# ================================================================ hls_aes — 암호화된 TS


def test_intact_aes_download_completes_without_refetching(tmp_path, monkeypatch):
    """온전한 암호화 세그먼트만 오면 다시 받지 않고 완료돼야 한다.

    암호화된 TS 세그먼트 3개가 전부 온전한 가짜 세션
    -> 완료 1회, 실패 없음, 다시 받은 횟수 0, 산출물 == 복호화한 세그먼트 3개
    """
    session, data, failures, finished = _run_aes(tmp_path, monkeypatch)

    assert (failures, finished) == ([], [True])
    assert data.failed_threads == 0
    # 첫 세그먼트는 준비 단계의 키 확인에서 한 번 더 받는다
    assert session.requests == {
        "media.m3u8": 1,
        "segment-0.ts": 2,
        "segment-1.ts": 1,
        "segment-2.ts": 1,
    }
    assert (tmp_path / "out.mp4").read_bytes() == AES_OUTPUT


@pytest.mark.parametrize("cut", CIPHER_CUTS)
def test_truncated_encrypted_segment_is_fetched_again_until_intact(cut, tmp_path, monkeypatch):
    """잘린 암호화 세그먼트는 다시 받고, 온전한 본문이 오면 다운로드가 완료돼야 한다.

    segment-1이 두 번 잘려 오고(Content-Length도 잘린 길이) 세 번째에 온전하게 온다
    -> 완료 1회, 실패 없음, segment-1 요청 3회, 산출물 == 복호화한 세그먼트 3개
    """
    truncated = CIPHER_CUTS[cut]
    session, data, failures, finished = _run_aes(
        tmp_path, monkeypatch, **{"segment-1.ts": [truncated, truncated, _encrypted(1)]}
    )

    assert (failures, finished) == ([], [True])
    assert session.requests["segment-1.ts"] == 3
    assert data.failed_threads == 2
    assert (tmp_path / "out.mp4").read_bytes() == AES_OUTPUT


@pytest.mark.parametrize("cut", CIPHER_CUTS)
def test_encrypted_segment_that_stays_truncated_fails_the_download(cut, tmp_path, monkeypatch):
    """암호화 세그먼트가 계속 잘려 오면 상한까지 다시 받은 뒤 잘림 실패 키로 실패해야 한다.

    segment-1이 항상 잘려 온다
    -> segment-1 요청 11회, 실패 콜백이 TruncatedSegmentError 하나를 받음, 완료 없음, 산출물 없음
    """
    session, data, failures, finished = _run_aes(
        tmp_path, monkeypatch, **{"segment-1.ts": [CIPHER_CUTS[cut]]}
    )

    assert session.requests["segment-1.ts"] == ATTEMPTS
    _assert_failed_as_truncated(failures, finished, data, tmp_path)


def test_decrypted_segment_with_a_bad_sync_byte_is_fetched_again(tmp_path, monkeypatch):
    """복호화한 세그먼트에 0x47로 시작하지 않는 패킷이 있으면 다시 받아야 한다.

    segment-1이 처음에는 둘째 패킷의 첫 바이트가 0x00인 TS(길이는 188의 배수)를 암호화한
    본문으로 오고, 다음에는 온전하게 온다
    -> 완료 1회, 실패 없음, segment-1 요청 2회
    """
    plain = _plain_ts(1)
    broken = plain[:188] + b"\x00" + plain[189:]
    pad = 16 - len(broken) % 16
    cipher = AES.new(KEY, AES.MODE_CBC, sequence_iv(1))
    broken_encrypted = cipher.encrypt(broken + bytes([pad]) * pad)

    session, _data, failures, finished = _run_aes(
        tmp_path, monkeypatch, **{"segment-1.ts": [broken_encrypted, _encrypted(1)]}
    )

    assert (failures, finished) == ([], [True])
    assert session.requests["segment-1.ts"] == 2
    assert (tmp_path / "out.mp4").read_bytes() == AES_OUTPUT


def test_encrypted_segment_shorter_than_its_content_length_is_fetched_again(tmp_path, monkeypatch):
    """받은 암호화 세그먼트가 선언된 Content-Length와 길이가 다르면 다시 받아야 한다.

    segment-1이 처음에는 온전한 암호문에 16바이트 더 긴 Content-Length로 오고,
    다음에는 맞는 Content-Length로 온다
    -> 완료 1회, 실패 없음, segment-1 요청 2회
    """
    mismatched = _Declared(_encrypted(1), len(_encrypted(1)) + 16)
    session, _data, failures, finished = _run_aes(
        tmp_path, monkeypatch, **{"segment-1.ts": [mismatched, _encrypted(1)]}
    )

    assert (failures, finished) == ([], [True])
    assert session.requests["segment-1.ts"] == 2
    assert (tmp_path / "out.mp4").read_bytes() == AES_OUTPUT
