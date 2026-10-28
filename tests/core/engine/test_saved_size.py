"""구간 없는 다운로드가 완료하며 저장된 파일의 크기를 공유 데이터에 적는지 검증 (#309).

완료 카드는 받은 양이 아니라 저장된 파일의 크기를 적는다 — 세그먼트를 다시 포장한 파일은
받은 양과 크기가 다르다. 구간 다운로드는 형식마다의 구간 테스트 파일이 잰다.
"""

import os
import threading

import core.engine.base as base_module
from tests.core.sources import test_file_downloader_run as file_run
from tests.core.sources import test_hls_aes_downloader as aes_run
from tests.core.sources import test_m3u8_downloader_run as m3u8_run

PADDING = b"\x00" * 777  # 다시 포장한 파일이 받은 양보다 커지는 만큼(바이트)


def _run_to_the_end(engine, data, finished) -> None:
    data.model.start()
    thread = threading.Thread(target=engine.run, daemon=True)
    thread.start()
    assert finished.wait(timeout=30), "완료 콜백이 호출되지 않았다"
    thread.join(timeout=10)


def _padded_remux_stream(chunks, dst_path, **_wait):
    """받은 스트림 뒤에 PADDING을 덧붙여 쓴다 — 파일 크기가 받은 양과 달라진다."""
    with open(dst_path, "wb") as f:
        for chunk in chunks:
            f.write(chunk)
        f.write(PADDING)


def test_whole_mp4_run_reports_the_size_of_the_saved_file(tmp_path, monkeypatch):
    """구간 없는 mp4 다운로드는 완료하며 저장된 파일의 크기를 공유 데이터에 적어야 한다.

    4 MiB 파일 -> 완료 전 output_size is None, 완료 뒤 output_size == 파일 크기 == 4 MiB
    """
    engine, data, _logger, output, finished, failures = file_run._make_engine(tmp_path, monkeypatch)
    assert data.output_size is None

    _run_to_the_end(engine, data, finished)

    assert failures == []
    assert data.output_size == os.path.getsize(output) == file_run.TOTAL_SIZE


def test_whole_m3u8_run_reports_the_size_of_the_saved_file_not_what_it_received(
    tmp_path, monkeypatch
):
    """구간 없는 m3u8 다운로드는 받은 양이 아니라 저장된 파일의 크기를 공유 데이터에 적어야 한다.

    다시 포장이 받은 것에 777바이트를 덧붙임
    -> output_size == 파일 크기 == 로그의 Output size, 받은 양(total_downloaded_size)과 다르다
    """
    engine, data, logger, output, finished, failures, _starts = m3u8_run._make_engine(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(base_module, "remux_stream", _padded_remux_stream)
    assert data.output_size is None

    _run_to_the_end(engine, data, finished)

    assert failures == []
    assert data.output_size == os.path.getsize(output)
    assert data.output_size == logger.postprocess_completes[0][1]
    assert data.output_size != data.total_downloaded_size


def test_whole_encrypted_run_reports_the_size_of_the_saved_file_not_what_it_received(
    tmp_path, monkeypatch
):
    """구간 없는 암호화 VOD 다운로드는 받은 양이 아니라 저장된 파일의 크기를 공유 데이터에 적어야 한다.

    다시 포장이 받은 것에 777바이트를 덧붙임
    -> output_size == 파일 크기, 받은 양(total_downloaded_size)과 다르다
    """
    engine, data, _logger, finished, failures, _starts = aes_run._make_engine(
        tmp_path, monkeypatch, key_resolver=lambda c, u: aes_run.KEY
    )
    monkeypatch.setattr(base_module, "remux_stream", _padded_remux_stream)
    assert data.output_size is None

    _run_to_the_end(engine, data, finished)

    assert failures == []
    assert data.output_size == os.path.getsize(tmp_path / "out.mp4") > len(PADDING)
    assert data.output_size != data.total_downloaded_size
