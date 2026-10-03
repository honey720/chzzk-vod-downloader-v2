"""다운로드 경로에서 준비 단계의 요청 다섯 개가 타임아웃을 갖는지 검증한다 (#320).

대상은 범위 · 세그먼트 요청보다 먼저 나가는 요청이다.

- file: 총 크기 조회 HEAD, 그다음 GET(HEAD의 content-length가 0일 때)
- m3u8: 플레이리스트 GET, 초기화 세그먼트 GET
- hls_aes: 플레이리스트 GET

가짜 세션은 요청을 실제로 보내지 않는다. "응답하지 않는 서버"는 타임아웃을 받은 요청에는
``requests.ReadTimeout``을 던지고, 타임아웃 없이 온 요청에는 ``_WouldNeverReturn``을 던진다 —
실제라면 그 요청은 반환되지 않는다.
"""

import os

import pytest
import requests

import core.downloaders.file_downloader as file_module
import core.downloaders.hls_aes_downloader as aes_module
import core.downloaders.m3u8_downloader as m3u8_module
from core.downloaders.file_downloader import FileDownloader
from core.downloaders.hls_aes_downloader import HlsAesDownloader
from core.downloaders.m3u8_downloader import M3U8Downloader
from core.models.download_data import DownloadData

TIMEOUT = 30  # 초 — 범위 · 세그먼트 요청이 쓰는 값. 제품 상수를 읽지 않고 직접 적는다
FILE_URL = "https://example.invalid/video.mp4"
M3U8_URL = "https://example.invalid/hls/video.m3u8"
AES_URL = "https://example.invalid/sea/media.m3u8"
KEY = bytes(range(16))

M3U8_PLAYLIST = "\n".join(
    ["#EXTM3U", '#EXT-X-MAP:URI="init.m4s"', "#EXTINF:2.000,", "seg_0.m4v", "#EXT-X-ENDLIST"]
)
# 세그먼트가 없는 플레이리스트 — 준비 단계가 플레이리스트 요청 하나로 끝난다
AES_PLAYLIST = "\n".join(
    ["#EXTM3U", '#EXT-X-KEY:METHOD=AES-128,URI="https://example.invalid/key"', "#EXT-X-ENDLIST"]
)


class _WouldNeverReturn(Exception):
    """타임아웃 없이 응답하지 않는 서버에 간 요청 — 실제로는 반환되지 않는다."""


class _Response:
    def __init__(self, text: str = "", content: bytes = b"", headers: dict | None = None):
        self.text = text
        self.content = content
        self.headers = headers or {}

    def raise_for_status(self):
        pass

    def close(self):
        pass


def _kind(method: str, url: str) -> str:
    """요청이 다섯 가운데 어느 것인지."""
    if url == FILE_URL:
        return "file-head" if method == "HEAD" else "file-get"
    if url == M3U8_URL:
        return "m3u8-playlist"
    if url.endswith("init.m4s"):
        return "m3u8-init"
    if url == AES_URL:
        return "aes-playlist"
    raise AssertionError(f"예상하지 않은 요청: {method} {url}")


class _Session:
    """요청의 종류와 받은 timeout을 기록한다. silent에 든 종류에는 응답하지 않는다."""

    def __init__(self, silent: str | None = None):
        self.silent = silent
        self.calls: list[tuple[str, object]] = []

    def _request(self, method: str, url: str, **kwargs) -> _Response:
        kind = _kind(method, url)
        self.calls.append((kind, kwargs.get("timeout")))
        if kind == self.silent:
            if kwargs.get("timeout") is None:
                raise _WouldNeverReturn(kind)
            raise requests.ReadTimeout(kind)
        if kind == "file-head":
            return _Response(headers={"content-length": "0"})  # 0이면 GET으로 다시 묻는다
        if kind == "file-get":
            return _Response(headers={"content-length": "1000"})
        if kind == "m3u8-playlist":
            return _Response(text=M3U8_PLAYLIST)
        if kind == "m3u8-init":
            return _Response(content=b"init")
        return _Response(text=AES_PLAYLIST)

    def head(self, url, **kwargs):
        return self._request("HEAD", url, **kwargs)

    def get(self, url, **kwargs):
        return self._request("GET", url, **kwargs)


class _Logger:
    """다운로더가 부르는 로거 메서드를 전부 받아 넘긴다."""

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _engine(kind: str, tmp_path, monkeypatch, session: _Session):
    """요청 종류에 맞는 다운로더와, 실패 · 완료 콜백이 받은 것을 준비한다."""
    family = kind.split("-")[0]
    module, cls, url, content_type = {
        "file": (file_module, FileDownloader, FILE_URL, "video"),
        "m3u8": (m3u8_module, M3U8Downloader, M3U8_URL, "m3u8"),
        "aes": (aes_module, HlsAesDownloader, AES_URL, "hls_aes"),
    }[family]
    monkeypatch.setattr(module, "get_thread_session", lambda: session)
    data = DownloadData(
        base_url=url,
        vod_url="https://chzzk.naver.com/video/1",
        output_path=str(tmp_path / "out.mp4"),
        resolution=1080,
        content_type=content_type,
    )
    failures: list[BaseException] = []
    finished: list[bool] = []
    engine = cls(data, _Logger(), on_failed=failures.append, on_finished=lambda: finished.append(1))
    if family == "aes":
        engine.set_key_resolver(lambda content, key_uri: KEY)
    return engine, data, failures, finished


def _prepare(kind: str, engine, data) -> None:
    """그 요청이 나가는 준비 단계까지만 실행한다."""
    if kind.startswith("file"):
        engine._get_total_size()
        return
    engine.prepare(data.content)
    if kind == "m3u8-init":
        engine._prepare_output()


FIVE = ["file-head", "file-get", "m3u8-playlist", "m3u8-init", "aes-playlist"]


@pytest.mark.parametrize("kind", FIVE)
def test_preparation_request_carries_the_timeout(kind, tmp_path, monkeypatch):
    """준비 단계의 요청은 범위 · 세그먼트 요청과 같은 timeout을 받아야 한다.

    요청 종류마다, 응답하는 가짜 세션
    -> 그 요청이 받은 timeout == 30
    """
    session = _Session()
    engine, data, _failures, _finished = _engine(kind, tmp_path, monkeypatch, session)

    _prepare(kind, engine, data)

    assert dict(session.calls)[kind] == TIMEOUT


@pytest.mark.parametrize("kind", FIVE)
def test_unanswered_preparation_request_fails_the_download(kind, tmp_path, monkeypatch):
    """준비 단계의 요청에 서버가 응답하지 않으면 다운로드가 타임아웃 예외로 실패 처리돼야 한다.

    요청 종류마다, 그 요청에만 응답하지 않는 가짜 세션으로 run()
    -> 실패 콜백이 requests.Timeout 하나를 받고, 완료 콜백은 불리지 않고, 산출물이 남지 않는다
    """
    session = _Session(silent=kind)
    engine, data, failures, finished = _engine(kind, tmp_path, monkeypatch, session)
    data.model.start()

    engine.run()

    assert session.calls[-1][0] == kind  # 응답하지 않는 그 요청까지 갔다
    assert len(failures) == 1 and isinstance(failures[0], requests.Timeout)
    assert finished == []
    assert not os.path.exists(data.output_path)
