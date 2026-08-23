"""AES(SEA) 경로 검증 — 매니페스트 판정·파싱과 HlsAesDownloader 실행 (#57).

- SEA 매니페스트: 지원 판정(AES-128-CBC + HTTP 키 시스템)과 Representation 파싱.
  실제 멤버십 VOD 매니페스트 박제본(dash_manifest_sea_*.xml)을 쓴다
- 다운로더: 키 취득 주입, 미지원 방식 거부, 전체 파이프라인(다운로드 →
  복호화 → 순서 병합)을 가짜 세션으로 검증

키 값은 테스트에서도 의미 없는 더미를 쓴다 — 실제 키는 어디에도 두지 않는다.
"""

import os
import threading

import pytest
from Crypto.Cipher import AES

import core.downloaders.base as base_module
import core.downloaders.hls_aes_downloader as aes_module
from core.api.dash import is_supported_sea, parse_sea_manifest
from core.downloaders.decrypt import AES_BLOCK_SIZE, TS_PACKET_SIZE, sequence_iv
from core.downloaders.hls_aes_downloader import DecryptionError, HlsAesDownloader
from core.models.content import Content, ContentType
from core.models.download_data import DownloadData

KEY = bytes(range(16))  # 테스트용 더미 키
KEY_URI = "https://api.chzzk.naver.com/service/v1/encryption/videos/VID/aes_key"
BASE_URL = "https://example.invalid/hls-aes/rep/media.m3u8"
SEGMENT_COUNT = 5


# ================================================================ SEA 매니페스트


@pytest.mark.parametrize(
    "fixture_name", ["dash_manifest_sea_13714380.xml", "dash_manifest_sea_14283698.xml"]
)
def test_real_sea_manifest_is_supported(load_mock_response, fixture_name):
    """실제 매니페스트는 지원 대상(AES-128-CBC + keysys:http)으로 판정돼야 한다."""
    assert is_supported_sea(load_mock_response(fixture_name)) is True


@pytest.mark.parametrize(
    "fixture_name", ["dash_manifest_sea_13714380.xml", "dash_manifest_sea_14283698.xml"]
)
def test_parse_sea_manifest_uses_nvod_m3u_as_base_url(load_mock_response, fixture_name):
    """암호화 비디오 Representation은 BaseURL이 없어 nvod:m3u를 base_url로 쓴다."""
    reps, auto_resolution, auto_base_url = parse_sea_manifest(load_mock_response(fixture_name))

    assert [rep[0] for rep in reps] == [144, 720, 1080]  # 해상도 오름차순
    assert auto_resolution == 1080
    for _, url in reps:
        assert "/hls-aes/" in url and url.endswith("media.m3u8")
    assert auto_base_url == reps[-1][1]
    # 오디오 Representation(비암호화)은 이 경로의 대상이 아니다
    assert all("cmaf" not in url for _, url in reps)


def test_drm_manifest_is_not_supported():
    """라이선스 서버형 DRM(Widevine 등)은 지원 대상이 아니다 — 우회하지 않는다."""
    drm_xml = (
        '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period><AdaptationSet><Representation '
        'width="1920" height="1080"><ContentProtection '
        'schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"/>'
        "</Representation></AdaptationSet></Period></MPD>"
    )

    assert is_supported_sea(drm_xml) is False


def test_plain_manifest_is_not_sea(load_mock_response):
    """ContentProtection이 없는 평문 매니페스트는 SEA가 아니다."""
    assert is_supported_sea(load_mock_response("dash_manifest.xml")) is False


# ================================================================ 다운로더 계약


def _make_content(content_type=ContentType.CHZZK_VIDEO_HLS_AES) -> Content:
    return Content(
        content_type=content_type,
        url="https://chzzk.naver.com/video/13714380",
        output_path="out.mp4",
        resolution=144,
        base_url=BASE_URL,
    )


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        (ContentType.CHZZK_VIDEO_HLS_AES, True),
        (ContentType.CHZZK_VIDEO_M3U8, False),
        (ContentType.CHZZK_VIDEO, False),
        (ContentType.CHZZK_CLIP, False),
    ],
)
def test_supports_matrix(content_type, expected):
    """암호화 VOD 타입만 이 다운로더가 처리한다."""
    assert HlsAesDownloader.supports(_make_content(content_type)) is expected


def test_requires_key_resolution_is_declared():
    """서비스가 키 리졸버를 주입하도록 계약을 선언해야 한다."""
    assert HlsAesDownloader.requires_key_resolution is True
    # base_url은 매니페스트 조회 시점에 확정되므로 재해석하지 않는다
    assert HlsAesDownloader.requires_base_url_resolution is False


# ================================================================ 가짜 서버


def _segment_body(index: int, packets: int = 2) -> bytes:
    """세그먼트별로 구별되는 가짜 TS 본문 — 각 188바이트 패킷이 0x47로 시작한다."""
    return b"".join(
        bytes([0x47]) + bytes([index + 1]) * (TS_PACKET_SIZE - 1) for _ in range(packets)
    )


def _encrypt(plain: bytes, iv: bytes) -> bytes:
    pad = AES_BLOCK_SIZE - (len(plain) % AES_BLOCK_SIZE)
    return AES.new(KEY, AES.MODE_CBC, iv).encrypt(plain + bytes([pad]) * pad)


def _playlist_text(method: str = "AES-128") -> str:
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-MEDIA-SEQUENCE:0"]
    if method == "NONE":
        lines.append("#EXT-X-KEY:METHOD=NONE")
    else:
        lines.append(f'#EXT-X-KEY:METHOD={method},URI="{KEY_URI}"')
    for i in range(SEGMENT_COUNT):
        lines += ["#EXTINF:3.840,", f"segment-{i:06d}.ts"]
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines)


class _Response:
    def __init__(self, text: str = "", body: bytes = b""):
        self.text = text
        self._body = body

    @property
    def content(self) -> bytes:
        """비스트리밍 요청용 본문 (prepare의 키 검증이 쓴다)."""
        return self._body

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=8192):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]


class FakeSession:
    """플레이리스트와 암호화 세그먼트를 URL로 분기해 돌려주는 가짜 세션."""

    def __init__(self, playlist: str):
        self._playlist = playlist

    def get(self, url, **kwargs):
        if url.endswith("media.m3u8"):
            return _Response(text=self._playlist)
        index = int(url.rsplit("segment-", 1)[1].removesuffix(".ts"))
        return _Response(body=_encrypt(_segment_body(index), sequence_iv(index)))


class RunLogger:
    """run() 경로가 쓰는 로거 인터페이스를 기록만 하며 흉내 낸다."""

    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.completed_times: list[float] = []
        self.closed = 0

    def __getattr__(self, name):
        return lambda *args, **kwargs: None

    def log_download_complete(self, total_time):
        self.completed_times.append(total_time)

    def log_error(self, message, exception=None):
        self.errors.append(message)

    def log_exception(self, message, exception=None):
        self.errors.append(message)

    def warning(self, message):
        self.warnings.append(message)

    def save_and_close(self):
        self.closed += 1


def _make_engine(tmp_path, monkeypatch, playlist=None, key_resolver=None):
    """가짜 세션이 연결된 RUNNING 이전 상태의 엔진을 준비한다."""
    data = DownloadData(
        base_url=BASE_URL,
        vod_url="https://chzzk.naver.com/video/13714380",
        output_path=str(tmp_path / "out.mp4"),
        resolution=144,
        content_type="hls_aes",
    )
    logger = RunLogger()
    monkeypatch.setattr(
        aes_module, "get_thread_session", lambda: FakeSession(playlist or _playlist_text())
    )
    # remux(#88·#92)는 성공 시 스트림 복사 재포장이다 — 이 파일의 검증 대상은
    # 복호화·공급 순서·바이트이므로 받은 스트림을 그대로 쓰는 스텁으로 대체한다.
    # 실제 ffmpeg 실행은 test_ffmpeg_utils.py가 검증한다
    def _passthrough_remux_stream(chunks, dst_path):
        with open(dst_path, "wb") as f:
            for chunk in chunks:
                f.write(chunk)

    monkeypatch.setattr(base_module, "remux_stream", _passthrough_remux_stream)

    finished = threading.Event()
    failures: list[BaseException] = []
    merge_starts: list[bool] = []

    def on_finished():
        data.model.finish()
        finished.set()

    engine = HlsAesDownloader(
        data,
        logger,
        on_finished=on_finished,
        on_failed=failures.append,
        on_merge_start=lambda: merge_starts.append(True),
    )
    if key_resolver is not None:
        engine.set_key_resolver(key_resolver)
    return engine, data, logger, finished, failures, merge_starts


# ================================================================ prepare·키 취득


def test_prepare_fetches_key_through_injected_resolver(tmp_path, monkeypatch):
    """키는 주입된 리졸버로 취득하고, 계획은 세그먼트 목록·후처리 필요를 담는다."""
    calls: list[tuple] = []

    def resolver(content, key_uri):
        calls.append((content, key_uri))
        return KEY

    engine, data, *_ = _make_engine(tmp_path, monkeypatch, key_resolver=resolver)
    plan = engine.prepare(data.content)

    assert [uri for _, uri in calls] == [KEY_URI]  # 플레이리스트의 키 URI를 그대로 쓴다
    assert plan.part_count == SEGMENT_COUNT
    assert plan.total_size is None
    assert plan.requires_postprocess is True
    assert plan.items[0] == (0, "segment-000000.ts")


def test_prepare_without_resolver_fails(tmp_path, monkeypatch):
    """키 리졸버가 없으면 명확히 실패해야 한다 (임의 우회 없음)."""
    engine, data, *_ = _make_engine(tmp_path, monkeypatch)

    with pytest.raises(DecryptionError, match="key_resolver"):
        engine.prepare(data.content)


def test_prepare_rejects_unsupported_method(tmp_path, monkeypatch):
    """AES-128이 아닌 방식은 우회를 시도하지 않고 거부한다."""
    engine, data, *_ = _make_engine(
        tmp_path,
        monkeypatch,
        playlist=_playlist_text(method="SAMPLE-AES"),
        key_resolver=lambda c, u: KEY,
    )

    with pytest.raises(DecryptionError, match="지원하지 않는 암호화 방식"):
        engine.prepare(data.content)


def test_prepare_rejects_playlist_without_key(tmp_path, monkeypatch):
    """암호화 정보가 없는 플레이리스트는 이 경로의 대상이 아니다."""
    engine, data, *_ = _make_engine(
        tmp_path, monkeypatch, playlist=_playlist_text(method="NONE"), key_resolver=lambda c, u: KEY
    )

    with pytest.raises(DecryptionError, match="#EXT-X-KEY"):
        engine.prepare(data.content)


def test_wrong_key_fails_fast_instead_of_writing_garbage(tmp_path, monkeypatch):
    """키가 틀리면 첫 세그먼트에서 즉시 실패하고 결과 파일을 남기지 않는다."""
    wrong_key = bytes([0xFF]) * 16
    engine, data, logger, finished, failures, _ = _make_engine(
        tmp_path, monkeypatch, key_resolver=lambda c, u: wrong_key
    )

    data.model.start()
    engine.run()

    assert not finished.is_set()
    assert any(isinstance(e, DecryptionError) for e in failures)
    assert not (tmp_path / "out.mp4").exists()


def test_wrong_key_failure_does_not_leak_key_value(tmp_path, monkeypatch):
    """키가 틀려 실패해도 실제 키 바이트가 실패 예외·로그 어디에도 남지 않는다 (SPEC §8.1).

    decrypt.py의 예외 메시지는 길이만 담아 이미 안전하지만(코드 확인), 그
    사실을 지금까지 겨냥해 검증한 테스트는 없었다 — 향후 누군가 디버깅
    편의로 키 값을 메시지에 실으면(예: f"key={key}") 이 테스트가 잡아야 한다.
    올바른 키·틀린 키 둘 다(하나라도 새면 실패)를 검사한다.
    """
    wrong_key = bytes([0xFF]) * 16
    engine, data, logger, finished, failures, _ = _make_engine(
        tmp_path, monkeypatch, key_resolver=lambda c, u: wrong_key
    )

    data.model.start()
    engine.run()

    assert not finished.is_set()
    failure_text = "\n".join(str(e) for e in failures)
    log_text = "\n".join(logger.errors) + "\n".join(logger.warnings)
    for secret in (wrong_key, KEY):
        assert secret.hex() not in failure_text
        assert secret.hex() not in log_text
        assert repr(secret) not in failure_text
        assert repr(secret) not in log_text


def test_short_key_failure_does_not_leak_key_value(tmp_path, monkeypatch):
    """리졸버가 길이가 틀린 키를 주면 값이 아니라 길이만 실패 메시지에 남는다 (SPEC §8.1)."""
    short_key = b"\xab\xcd\xef"  # 16바이트가 아닌 더미
    engine, data, *_ = _make_engine(tmp_path, monkeypatch, key_resolver=lambda c, u: short_key)

    with pytest.raises(DecryptionError) as exc_info:
        engine.prepare(data.content)

    message = str(exc_info.value)
    assert short_key.hex() not in message
    assert str(short_key) not in message
    assert "길이" in message or "3" in message  # 값 대신 길이 정보만 담겼는지 정황 확인


# ================================================================ 전체 파이프라인


def test_run_downloads_decrypts_and_merges_in_order(tmp_path, monkeypatch):
    """전체 파이프라인이 복호화된 세그먼트를 순서대로 이어붙인 파일을 만든다."""
    engine, data, logger, finished, failures, merge_starts = _make_engine(
        tmp_path, monkeypatch, key_resolver=lambda c, u: KEY
    )

    data.model.start()
    thread = threading.Thread(target=engine.run, daemon=True)
    thread.start()
    assert finished.wait(timeout=30), "완료 콜백이 호출되지 않았다"
    thread.join(timeout=10)

    expected = b"".join(_segment_body(i) for i in range(SEGMENT_COUNT))
    assert (tmp_path / "out.mp4").read_bytes() == expected
    assert merge_starts == [True]
    assert failures == [] and logger.errors == [] and logger.warnings == []
    assert data.completed_threads == SEGMENT_COUNT
    # TS 경로에는 초기화 세그먼트가 없어 병합 수 = 세그먼트 수다
    assert data.merged_segments == SEGMENT_COUNT
    assert not (tmp_path / "CVDv2_temp_out").exists()  # 임시 폴더 삭제 (#105 산출물 파생 이름)


def test_stray_dotfile_in_temp_dir_is_excluded_from_merge(tmp_path, monkeypatch):
    """세그먼트가 아닌 파일이 임시 폴더에 있어도 병합에서 제외되고 경고가 남는다 (#180).

    m3u8(fMP4) 경로와 같은 오염 경로 — TS 경로는 확장자가 달라(``.ts``)
    화이트리스트를 경로별로 따로 확인해야 한다는 지시에 따른 검증이다.
    """
    engine, data, logger, finished, failures, merge_starts = _make_engine(
        tmp_path, monkeypatch, key_resolver=lambda c, u: KEY
    )

    def _pollute_then_start_merge():
        for name in ("._0.ts", ".DS_Store", "notes.txt"):
            with open(os.path.join(engine.temp_dir, name), "wb") as f:
                f.write(b"not-a-segment")
        merge_starts.append(True)

    engine._on_merge_start = _pollute_then_start_merge

    data.model.start()
    thread = threading.Thread(target=engine.run, daemon=True)
    thread.start()
    assert finished.wait(timeout=30), "완료 콜백이 호출되지 않았다"
    thread.join(timeout=10)

    expected = b"".join(_segment_body(i) for i in range(SEGMENT_COUNT))
    assert (tmp_path / "out.mp4").read_bytes() == expected  # 오염 없이 정상
    assert failures == [] and logger.errors == []
    assert len(logger.warnings) == 1
    assert "._0.ts" in logger.warnings[0]
    assert ".DS_Store" in logger.warnings[0]
    assert "notes.txt" in logger.warnings[0]
