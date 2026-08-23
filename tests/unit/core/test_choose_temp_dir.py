"""임시 폴더를 다른 매체로 분리하는 판단(choose_temp_dir) 검증 (#192).

이 파일의 테스트는 conftest.py의 전역 격리 픽스처(_isolate_choose_temp_dir)를
필요한 만큼만 되돌려 실제 판단 로직·실 디스크 I/O 프로브를 검증한다.
다른 테스트 파일에는 이 격리가 그대로 적용되므로 기존 스위트는 무영향이다.

"애매하면 분리하지 않는다"는 #192의 원칙을 각 게이트(여유 공간·같은
볼륨·프로브 실패·속도 배수 미달)별로 개별 검증한다.
"""

import os

import core.downloaders.hls_aes_downloader as hls_aes_module
import core.downloaders.m3u8_downloader as m3u8_module
import core.utils.disk_speed as disk_speed_module
import core.utils.paths as paths_module
from core.models.download_data import DownloadData


def _restore_real_choose_temp_dir(monkeypatch):
    """conftest의 전역 격리를 이 테스트에 한해 되돌린다."""
    monkeypatch.setattr(m3u8_module, "choose_temp_dir", paths_module.choose_temp_dir)
    monkeypatch.setattr(hls_aes_module, "choose_temp_dir", paths_module.choose_temp_dir)


# ================================================================ measure_write_speed


def test_measure_write_speed_returns_positive_number_for_real_directory(tmp_path):
    """실제 쓰기 가능한 디렉토리는 양수 바이트/초를 돌려준다."""
    disk_speed_module.clear_speed_cache()
    speed = disk_speed_module.measure_write_speed(str(tmp_path), sample_bytes=64 * 1024)
    assert speed is not None
    assert speed > 0


def test_measure_write_speed_caches_per_volume(tmp_path, monkeypatch):
    """같은 볼륨은 두 번째 호출부터 실제 쓰기 없이 캐시된 값을 돌려준다."""
    disk_speed_module.clear_speed_cache()
    calls = []
    real_mkstemp = disk_speed_module.tempfile.mkstemp

    def counting_mkstemp(*args, **kwargs):
        calls.append(1)
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(disk_speed_module.tempfile, "mkstemp", counting_mkstemp)

    first = disk_speed_module.measure_write_speed(str(tmp_path), sample_bytes=1024)
    second = disk_speed_module.measure_write_speed(str(tmp_path), sample_bytes=1024)

    assert first == second
    assert len(calls) == 1, "캐시가 없으면 매번 실제로 파일을 썼을 것이다"


def test_measure_write_speed_missing_directory_returns_none(tmp_path):
    """없는 디렉토리는 None — 실패를 조용히 삼키되 값은 신뢰할 수 없다고 알린다."""
    disk_speed_module.clear_speed_cache()
    speed = disk_speed_module.measure_write_speed(str(tmp_path / "없는폴더"))
    assert speed is None


def test_measure_write_speed_leaves_no_residue(tmp_path):
    """프로브 파일은 측정 후 남지 않는다."""
    disk_speed_module.clear_speed_cache()
    disk_speed_module.measure_write_speed(str(tmp_path), sample_bytes=1024)
    assert list(tmp_path.iterdir()) == []


def test_measure_write_speed_calls_fsync(tmp_path, monkeypatch):
    """fsync를 실제로 호출한다 — 없으면 페이지 캐시만 재는 무의미한 측정이 된다(#191).

    f.write()는 OS 페이지 캐시에 즉시 반환되어(#191 실기 확인) 매체 속도와
    무관하게 항상 빠르게 보인다 — fsync 없이는 이 프로브 전체가 무효화된다.
    """
    disk_speed_module.clear_speed_cache()
    calls = []
    real_fsync = disk_speed_module.os.fsync

    def counting_fsync(fd):
        calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(disk_speed_module.os, "fsync", counting_fsync)

    disk_speed_module.measure_write_speed(str(tmp_path), sample_bytes=1024)

    assert len(calls) == 1, "fsync가 호출되지 않았다 — 페이지 캐시만 재는 측정으로 되돌아갔다"


# ================================================================ choose_temp_dir 게이트


def test_falls_back_when_scratch_has_insufficient_free_space(tmp_path, monkeypatch):
    """스크래치 볼륨 여유 공간이 부족하면 분리하지 않는다.

    다른 게이트(같은 볼륨·속도)가 우연히 같은 결과를 내 이 게이트를 안
    거치고도 테스트가 통과하는 걸 막기 위해, 그 게이트들은 전부 "통과"
    쪽으로 고정해 여유 공간 체크 단독으로 분기가 갈리는지 확인한다.
    """

    class _TinyUsage:
        free = 1024  # 임계(10GiB)보다 한참 작다

    scratch_str = str(tmp_path / "scratch")
    monkeypatch.setattr(paths_module.shutil, "disk_usage", lambda path: _TinyUsage())
    monkeypatch.setattr(paths_module, "_scratch_base_dir", lambda: scratch_str)
    monkeypatch.setattr(paths_module, "_same_volume", lambda a, b: False)
    # 속도 게이트는 확실히 통과시킨다(스크래치가 압도적으로 빠름) — 그래야
    # 여유 공간 게이트 단독으로 분기가 갈리는지 알 수 있다
    monkeypatch.setattr(
        paths_module,
        "measure_write_speed",
        lambda d: 100_000_000.0 if d == scratch_str else 1_000_000.0,
    )

    output_path = str(tmp_path / "out" / "video 1080p.mp4")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    result = paths_module.choose_temp_dir(output_path)

    assert result == paths_module.temp_dir_for(output_path)


def test_falls_back_when_scratch_and_output_are_same_volume(tmp_path, monkeypatch):
    """스크래치와 산출물 폴더가 이미 같은 볼륨이면 비교 없이 분리하지 않는다."""

    class _PlentyUsage:
        free = 100 * 1024**3

    monkeypatch.setattr(paths_module.shutil, "disk_usage", lambda path: _PlentyUsage())
    scratch = tmp_path / "scratch"
    monkeypatch.setattr(paths_module, "_scratch_base_dir", lambda: str(scratch))

    speed_calls = []
    monkeypatch.setattr(
        paths_module,
        "measure_write_speed",
        lambda d: speed_calls.append(d) or 999_999_999,
    )

    output_path = str(tmp_path / "out" / "video 1080p.mp4")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    result = paths_module.choose_temp_dir(output_path)

    assert result == paths_module.temp_dir_for(output_path)
    assert speed_calls == [], "같은 볼륨이면 속도를 잴 필요도 없다"


def test_falls_back_when_scratch_probe_fails(tmp_path, monkeypatch):
    """스크래치 폴더 속도 측정에 실패하면(None) 분리하지 않는다."""

    class _PlentyUsage:
        free = 100 * 1024**3

    monkeypatch.setattr(paths_module.shutil, "disk_usage", lambda path: _PlentyUsage())
    monkeypatch.setattr(paths_module, "_scratch_base_dir", lambda: str(tmp_path / "scratch"))
    monkeypatch.setattr(paths_module, "_same_volume", lambda a, b: False)
    monkeypatch.setattr(paths_module, "measure_write_speed", lambda d: None)

    output_path = str(tmp_path / "out" / "video 1080p.mp4")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    result = paths_module.choose_temp_dir(output_path)

    assert result == paths_module.temp_dir_for(output_path)


def test_falls_back_when_output_probe_fails(tmp_path, monkeypatch):
    """스크래치는 재도 산출물 쪽 속도를 못 재면(비교 기준 없음) 분리하지 않는다."""

    class _PlentyUsage:
        free = 100 * 1024**3

    scratch_str = str(tmp_path / "scratch")
    monkeypatch.setattr(paths_module.shutil, "disk_usage", lambda path: _PlentyUsage())
    monkeypatch.setattr(paths_module, "_scratch_base_dir", lambda: scratch_str)
    monkeypatch.setattr(paths_module, "_same_volume", lambda a, b: False)

    def fake_speed(directory):
        return 100_000_000.0 if directory == scratch_str else None

    monkeypatch.setattr(paths_module, "measure_write_speed", fake_speed)

    output_path = str(tmp_path / "out" / "video 1080p.mp4")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    result = paths_module.choose_temp_dir(output_path)

    assert result == paths_module.temp_dir_for(output_path)


def test_falls_back_when_scratch_is_not_clearly_faster(tmp_path, monkeypatch):
    """스크래치가 산출물보다 빠르긴 해도 배수 기준(_SEPARATION_SPEED_MARGIN) 미달이면 그대로 둔다."""

    class _PlentyUsage:
        free = 100 * 1024**3

    scratch_str = str(tmp_path / "scratch")
    monkeypatch.setattr(paths_module.shutil, "disk_usage", lambda path: _PlentyUsage())
    monkeypatch.setattr(paths_module, "_scratch_base_dir", lambda: scratch_str)
    monkeypatch.setattr(paths_module, "_same_volume", lambda a, b: False)

    def fake_speed(directory):
        # 스크래치가 1.2배만 빠르다 — margin(2.0배) 미달
        return 120.0 if directory == scratch_str else 100.0

    monkeypatch.setattr(paths_module, "measure_write_speed", fake_speed)

    output_path = str(tmp_path / "out" / "video 1080p.mp4")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    result = paths_module.choose_temp_dir(output_path)

    assert result == paths_module.temp_dir_for(output_path)


def test_separates_when_scratch_is_clearly_faster_and_has_room(tmp_path, monkeypatch):
    """모든 게이트를 통과하면(여유 공간+다른 볼륨+뚜렷이 빠름) 스크래치 폴더를 쓴다."""

    class _PlentyUsage:
        free = 100 * 1024**3

    scratch_base = tmp_path / "scratch"
    scratch_str = str(scratch_base)
    monkeypatch.setattr(paths_module.shutil, "disk_usage", lambda path: _PlentyUsage())
    monkeypatch.setattr(paths_module, "_scratch_base_dir", lambda: scratch_str)
    monkeypatch.setattr(paths_module, "_same_volume", lambda a, b: False)

    def fake_speed(directory):
        return 100_000_000.0 if directory == scratch_str else 1_000_000.0  # 100배 차이

    monkeypatch.setattr(paths_module, "measure_write_speed", fake_speed)

    output_path = str(tmp_path / "out" / "video 1080p.mp4")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    result = paths_module.choose_temp_dir(output_path)

    assert result == paths_module.temp_dir_for(output_path, base_dir=scratch_str)
    assert result != paths_module.temp_dir_for(output_path)


# ================================================================ 다운로더 연동


def test_m3u8_downloader_uses_scratch_temp_dir_when_conditions_are_met(tmp_path, monkeypatch):
    """조건이 맞으면 M3U8Downloader.temp_dir이 실제로 스크래치 폴더를 가리킨다."""
    _restore_real_choose_temp_dir(monkeypatch)

    class _PlentyUsage:
        free = 100 * 1024**3

    scratch_base = tmp_path / "scratch"
    scratch_str = str(scratch_base)
    monkeypatch.setattr(paths_module.shutil, "disk_usage", lambda path: _PlentyUsage())
    monkeypatch.setattr(paths_module, "_scratch_base_dir", lambda: scratch_str)
    monkeypatch.setattr(paths_module, "_same_volume", lambda a, b: False)
    monkeypatch.setattr(
        paths_module,
        "measure_write_speed",
        lambda d: 100_000_000.0 if d == scratch_str else 1_000_000.0,
    )

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    output_path = str(output_dir / "video 1080p.mp4")

    data = DownloadData(
        base_url="https://example.invalid/hls/video.m3u8",
        vod_url="https://chzzk.naver.com/video/1",
        output_path=output_path,
        resolution=1080,
        content_type="m3u8",
    )
    engine = m3u8_module.M3U8Downloader(data, logger=None)

    assert engine.temp_dir == paths_module.temp_dir_for(output_path, base_dir=scratch_str)
    assert engine.temp_dir != paths_module.temp_dir_for(output_path)


def test_m3u8_downloader_keeps_output_adjacent_temp_dir_by_default(tmp_path):
    """conftest의 전역 격리를 그대로 두면(기본) 기존 동작과 동일하다."""
    output_path = str(tmp_path / "video 1080p.mp4")
    data = DownloadData(
        base_url="https://example.invalid/hls/video.m3u8",
        vod_url="https://chzzk.naver.com/video/1",
        output_path=output_path,
        resolution=1080,
        content_type="m3u8",
    )
    engine = m3u8_module.M3U8Downloader(data, logger=None)

    assert engine.temp_dir == paths_module.temp_dir_for(output_path)
