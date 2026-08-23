"""세그먼트 화이트리스트 필터(#180)의 경고 로그가 대량 오염 시 요약되는지 검증 (#191 후속).

실기에서 AppleDouble 사이드카가 세그먼트 수만큼(1011개) 쌓여 파일명을
전부 한 줄에 찍는 경고가 로그를 수십 KB로 부풀린 사례를 재현한다.
"""

from unittest.mock import MagicMock

import core.downloaders.m3u8_downloader as m3u8_module
from core.models.download_data import DownloadData


def _make_engine(tmp_path):
    data = DownloadData(
        base_url="https://example.invalid/hls/video.m3u8",
        vod_url="https://chzzk.naver.com/video/1",
        output_path=str(tmp_path / "out.mp4"),
        resolution=1080,
        content_type="m3u8",
    )
    engine = m3u8_module.M3U8Downloader(data, MagicMock())
    engine.temp_dir = str(tmp_path)
    return engine, data


def test_mass_pollution_warning_is_summarized_not_dumped_in_full(tmp_path):
    """오염 파일 1011개가 있어도 경고 한 줄에 전부 찍지 않고 개수+샘플만 남긴다."""
    engine, _data = _make_engine(tmp_path)
    for i in range(3):
        (tmp_path / f"{i:07d}.m4v").write_bytes(b"seg")  # 진짜 세그먼트
    for i in range(1011):
        (tmp_path / f"._{i:07d}.m4v").write_bytes(b"appledouble")  # 오염

    logger = MagicMock()
    engine.logger = logger

    matched = engine._list_segment_files((".m4v",))

    assert len(matched) == 3  # 진짜 세그먼트만 병합 대상
    assert logger.warning.call_count == 1
    message = logger.warning.call_args[0][0]
    assert "1011개" in message
    assert "10개 더" not in message  # 정확히 1001개가 남았는지(10개 표본 + 1001)
    assert "1001개 더" in message
    assert len(message) < 2000, "여전히 전체를 찍고 있다 — 요약 실패"
