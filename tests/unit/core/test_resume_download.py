"""실패·종료 뒤 같은 디스크 데이터로 이어받는 실제 엔진 계약."""

import json
from pathlib import Path

import pytest
import requests

import core.downloaders.base as base
import core.downloaders.m3u8_downloader as module
from core.downloaders.resume import ResumeMismatchError, ResumeStore
from core.utils.persistence import StorageBusyError
from tests.unit.core.test_m3u8_downloader_run import (
    _make_engine,
    _expected_output,
    M3U8Session,
)


def failed_merge(tmp_path, monkeypatch):
    engine, data, _, output, finished, errors, _ = _make_engine(tmp_path, monkeypatch)

    def fail(chunks, path):
        raise base.FFmpegError("simulated failure")

    monkeypatch.setattr(base, "remux_stream", fail)
    data.model.start()
    engine.run()
    assert errors and not finished.is_set() and not output.exists()
    assert len(list(Path(engine.temp_dir).glob("*.m4v"))) == 6
    return engine, data


@pytest.mark.parametrize(
    "missing,expected", [({2}, [1, 2]), ({0}, [0]), ({2, 3, 5}, [1, 4, 2, 3, 5]), (set(), [])]
)
def test_resume_requests_only_gaps_and_preceding_segments(tmp_path, monkeypatch, missing, expected):
    first, _ = failed_merge(tmp_path, monkeypatch)
    for index in missing:
        first._store.segment_path(index).unlink()
    engine, data, _, output, finished, errors, merges = _make_engine(tmp_path, monkeypatch)
    requested = []

    class Session(M3U8Session):
        def get(self, url, **kwargs):
            if "seg_" in url:
                requested.append(int(url.rsplit("seg_", 1)[1].split(".")[0]))
            return super().get(url, **kwargs)

    monkeypatch.setattr(module, "get_thread_session", lambda: Session(3))
    data.model.start()
    engine.run()
    assert not errors and finished.is_set() and merges == [True]
    assert sorted(requested) == sorted(expected)
    anchors = [i for i in expected if i not in missing]
    assert requested[: len(anchors)] == anchors
    assert output.read_bytes() == _expected_output(3)
    assert data.completed_threads == 6
    assert not list(Path(engine.temp_dir).glob("*.m4v"))
    assert engine._store.record["complete"] is True


def test_corrupted_segment_is_not_reused(tmp_path, monkeypatch):
    first, _ = failed_merge(tmp_path, monkeypatch)
    first._store.segment_path(3).write_bytes(b"damaged")
    engine, data, _, output, finished, errors, _ = _make_engine(tmp_path, monkeypatch)
    data.model.start()
    engine.run()
    assert not errors and finished.is_set()
    assert output.read_bytes() == _expected_output(3)


def test_network_failure_retains_completed_and_partial_without_mp4(tmp_path, monkeypatch):
    engine, data, _, output, finished, errors, _ = _make_engine(tmp_path, monkeypatch)
    session = M3U8Session(3)
    original = session.get

    def get(url, **kwargs):
        if "seg_3" in url:
            raise requests.ConnectionError("offline")
        return original(url, **kwargs)

    session.get = get
    monkeypatch.setattr(module, "get_thread_session", lambda: session)
    monkeypatch.setattr(base, "_TRANSIENT_ERROR_REQUEUE_LIMIT", 0)
    data.adjust_threads = 1
    data.model.start()
    engine.run()
    assert errors and not finished.is_set() and not output.exists()
    assert len(list(Path(engine.temp_dir).glob("*.m4v"))) == 3
    # 실패 콜백이 도착했을 때는 잠금도 풀려 즉시 이어받을 수 있다.
    store = ResumeStore(data.content)
    store.open()
    store.close()


def test_changed_playlist_preserves_old_files_and_refuses_merge(tmp_path, monkeypatch):
    first, _ = failed_merge(tmp_path, monkeypatch)
    before = first._store.path.read_bytes()
    engine, data, _, output, finished, errors, _ = _make_engine(tmp_path, monkeypatch)
    session = M3U8Session(3)
    original = session.get

    def get(url, **kwargs):
        response = original(url, **kwargs)
        if url.endswith(".m3u8"):
            response.text = response.text.replace("2.000", "3.000")
        return response

    session.get = get
    monkeypatch.setattr(module, "get_thread_session", lambda: session)
    data.model.start()
    engine.run()
    assert isinstance(errors[0], ResumeMismatchError)
    assert not output.exists() and not finished.is_set()
    assert first._store.path.read_bytes() == before
    assert len(list(Path(engine.temp_dir).glob("*.m4v"))) == 6


def test_exclusive_writer_and_corrupt_record_preserve_data(tmp_path, monkeypatch):
    first, data = failed_merge(tmp_path, monkeypatch)
    store = ResumeStore(data.content)
    store.open()
    rival = ResumeStore(data.content)
    with pytest.raises(StorageBusyError):
        rival.open()
    rival.close()
    store.close()
    first._store.path.write_text("broken", encoding="utf-8")
    store = ResumeStore(data.content)
    with pytest.raises(ResumeMismatchError):
        store.open()
    store.close()
    assert first._store.path.read_text() == "broken"
    assert len(list(Path(first.temp_dir).glob("*.m4v"))) == 6


def test_crash_after_output_publish_is_reconciled_without_requests(tmp_path, monkeypatch):
    first, _ = failed_merge(tmp_path, monkeypatch)
    output = tmp_path / "out.mp4"
    output.write_bytes(_expected_output(3))
    from core.downloaders.resume import digest_file

    record = first._store.record
    record.update(finalizing=True, output_sha256=digest_file(output))
    first._store.flush()
    engine, data, _, _, finished, errors, _ = _make_engine(tmp_path, monkeypatch)

    def unexpected():
        pytest.fail("이미 확정된 MP4를 다시 받으면 안 됩니다")

    monkeypatch.setattr(module, "get_thread_session", unexpected)
    data.model.start()
    engine.run()
    assert finished.is_set() and not errors
    assert json.loads(first._store.path.read_text())["complete"] is True


def test_record_write_failure_preserves_segments(tmp_path, monkeypatch):
    first, _ = failed_merge(tmp_path, monkeypatch)
    engine, data, _, output, finished, errors, _ = _make_engine(tmp_path, monkeypatch)

    def disk_full(*args):
        raise OSError("disk full")

    monkeypatch.setattr("core.downloaders.resume.atomic_json", disk_full)
    data.model.start()
    engine.run()
    assert errors and not finished.is_set() and not output.exists()
    assert len(list(Path(first.temp_dir).glob("*.m4v"))) == 6


def test_process_death_recovers_segment_committed_before_checkpoint(tmp_path, monkeypatch):
    import subprocess
    import sys
    from tests.unit.fmp4_samples import media_segment

    engine, data, _, _, _, _, _ = _make_engine(tmp_path, monkeypatch)
    script = """
import os, sys
from pathlib import Path
from core.models.download_data import DownloadData
from core.downloaders.resume import ResumeStore
from tests.unit.core.test_m3u8_downloader_run import _playlist_text, INIT_CONTENT
from tests.unit.fmp4_samples import media_segment
s=ResumeStore(DownloadData("", "https://chzzk.naver.com/video/1", sys.argv[1], 1080, "m3u8").content)
s.open()
lines=_playlist_text().splitlines()
segments=[x for x in lines if x and not x.startswith("#")]
s.adopt(lines, segments, INIT_CONTENT, sys.argv[1])
s.segment_path(0).write_bytes(media_segment(128))
os._exit(23)
"""
    result = subprocess.run([sys.executable, "-c", script, data.output_path], check=False)
    assert result.returncode == 23
    store = ResumeStore(data.content)
    store.open()  # 죽은 프로세스의 잠금은 OS가 해제한다.
    store.count, store.width = 6, 1
    assert store.verified() == {0: len(media_segment(128))}
    store.close()


def test_final_output_never_overwrites_an_existing_file(tmp_path, monkeypatch):
    engine, data, _, output, finished, errors, _ = _make_engine(tmp_path, monkeypatch)
    output.write_bytes(b"important existing video")
    data.model.start()
    engine.run()
    assert errors and not finished.is_set()
    assert output.read_bytes() == b"important existing video"
    assert len(list(Path(engine.temp_dir).glob("*.m4v"))) == 6


def test_atomic_publish_refuses_existing_destination(tmp_path):
    from core.utils.persistence import publish_file

    source, destination = tmp_path / "building", tmp_path / "done.mp4"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")
    with pytest.raises(OSError):
        publish_file(source, destination)
    assert source.read_bytes() == b"new" and destination.read_bytes() == b"old"


def test_real_ffmpeg_merges_received_fragments_into_decodable_mp4(tmp_path, monkeypatch):
    import subprocess
    from tests.unit.core.test_ffmpeg_utils import _make_tiny_fmp4
    from tests.unit.core.test_m3u8_downloader_run import TextResponse, StreamResponse
    from core.utils.ffmpeg import get_ffmpeg_exe, remux_stream

    source = tmp_path / "source.mp4"
    _make_tiny_fmp4(source)
    payload = source.read_bytes()
    offset = 0
    while payload[offset + 4 : offset + 8] != b"moof":
        offset += int.from_bytes(payload[offset : offset + 4], "big")
    init, media = payload[:offset], payload[offset:]
    engine, data, _, output, finished, errors, _ = _make_engine(tmp_path, monkeypatch)

    class Session:
        def get(self, url, **kwargs):
            if url.endswith(".m3u8"):
                return TextResponse(
                    text='#EXTM3U\n#EXT-X-MAP:URI="init.m4s"\n#EXTINF:1.0,\nseg_0.m4v\n#EXT-X-ENDLIST'
                )
            if url.endswith("init.m4s"):
                return TextResponse(content=init)
            return StreamResponse(media, 0)

    monkeypatch.setattr(module, "get_thread_session", lambda: Session())
    monkeypatch.setattr(base, "remux_stream", remux_stream)
    data.model.start()
    engine.run()
    assert finished.is_set() and not errors and output.stat().st_size > 0
    decoded = subprocess.run(
        [get_ffmpeg_exe(), "-v", "error", "-i", str(output), "-f", "framemd5", "-"],
        capture_output=True,
        check=True,
    )
    frames = [line for line in decoded.stdout.splitlines() if line and not line.startswith(b"#")]
    assert len(frames) == 10


def test_signed_addresses_are_redacted_from_exception_logs():
    import logging
    from core.utils.logging_setup import RedactingFormatter

    try:
        raise requests.HTTPError("403 https://cdn.invalid/a.m4v?token=SECRET&expires=123")
    except requests.HTTPError:
        import sys

        record = logging.LogRecord(
            "download", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
        )
    text = RedactingFormatter().format(record)
    assert "SECRET" not in text and "expires=123" not in text
    assert "HTTPError" in text and "a.m4v" in text


def test_orphan_fragments_without_identity_record_are_not_mixed(tmp_path, monkeypatch):
    engine, _ = failed_merge(tmp_path, monkeypatch)
    engine._store.path.unlink()
    other, data, _, output, finished, errors, _ = _make_engine(tmp_path, monkeypatch)
    data.model.start()
    other.run()
    assert isinstance(errors[0], ResumeMismatchError)
    assert not output.exists() and not finished.is_set()
    assert len(list(Path(engine.temp_dir).glob("*.m4v"))) == 6
