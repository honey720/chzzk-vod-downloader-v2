"""DownloadLogger 단계 경계 줄(#110)과 요약 스크립트의 하위 호환 검증.

실제 로그 파일을 만들어 scripts/summarize_download_log.py가
(a) 기존 지표를 계속 뽑고 (b) 새 단계 지표를 추가로 뽑는지 종단 검증한다 —
새 줄의 메시지 형식과 요약 스크립트 패턴이 어긋나면 여기서 잡힌다.
"""

import tomllib
from pathlib import Path
from types import SimpleNamespace

import config.config as config_module
from download.logger import DownloadLogger
from scripts.summarize_download_log import summarize


def _make_logger(tmp_path, monkeypatch) -> DownloadLogger:
    """로그를 tmp_path 아래에 쓰는 DownloadLogger를 만든다."""
    monkeypatch.setattr(config_module, "CONFIG_DIR", str(tmp_path))
    return DownloadLogger()


def _stub_item() -> SimpleNamespace:
    """log_download_info가 참조하는 속성만 가진 아이템 스텁."""
    return SimpleNamespace(
        content_type="m3u8",
        title="제목",
        channel_name="채널",
        live_open_date="2026-07-28",
        duration=90,
        resolution=1080,
        total_size=0,
        output_path="out.mp4",
        download_path="downloads",
    )


def _pyproject_version() -> str:
    """정본(pyproject.toml)의 버전 문자열."""
    root = Path(config_module.__file__).resolve().parent.parent
    with open(root / "pyproject.toml", "rb") as f:
        return tomllib.load(f)["project"]["version"]


def test_get_app_version_matches_pyproject_exactly():
    """소스 실행의 앱 버전은 정본(pyproject) 문자열과 정확히 일치한다 (#116).

    구 구현의 importlib.metadata는 버전을 정규화해(2.9.0-rc1 → 2.9.0rc1)
    정본과 어긋났다 — 이제 pyproject 직접 읽기라 완전 일치를 요구한다.

    #195부터 소스 실행은 ``+dev.<커밋>`` 접미사가 붙는다(개발 빌드 구분) —
    이 가드가 보는 것은 버전 숫자 부분의 일치이지 커밋 접미사가 아니므로,
    ``+`` 앞부분만 잘라 비교한다.
    """
    config_module.get_app_version.cache_clear()
    base_version = config_module.get_app_version().split("+", 1)[0]
    assert base_version == _pyproject_version()


def test_version_mirror_constant_matches_pyproject():
    """미러 상수(APP_VERSION)는 정본(pyproject)과 정확히 일치해야 한다 (#116).

    Nuitka 빌드 실행 파일에는 pyproject.toml이 없어 이 상수가 쓰인다.
    이 테스트가 실패하면 버전 인상 시 config/config.py의 APP_VERSION을
    함께 갱신하지 않은 것이다 — 배포 빌드가 다시 틀린 버전을 기록하게 된다.
    """
    assert config_module.APP_VERSION == _pyproject_version()


def test_phase_lines_parse_and_old_lines_still_parse(tmp_path, monkeypatch):
    """완료 조건: 새 줄이 파싱되고, 기존 요약 스크립트 지표도 계속 동작한다."""
    logger = _make_logger(tmp_path, monkeypatch)
    logger.log_download_info(_stub_item())
    logger.log_download_start(1000, 100, 10, 4)
    logger.log_transfer_complete(12.34, 987654321, 3, 40)
    logger.log_postprocess_start("remux")
    logger.log_postprocess_complete(5.67, 987000000)
    logger.log_download_complete(18.01)
    logger.log_total_breakdown(12.34, 5.67)
    log_file = Path(logger.log_file)
    logger.save_and_close()

    summary = summarize(log_file)

    # 기존 지표 하위 호환 (#110 요건 5 — 기존 줄 형식 불변)
    assert summary["total_size"] == 1000
    assert summary["part_size"] == 100
    assert summary["segments"] == 10
    assert summary["initial_threads"] == 4
    assert summary["completed_in_seconds"] == 18.01
    assert summary["recovered"] is True

    # 새 단계 지표
    assert summary["app_version"] == config_module.get_app_version()
    assert summary["transfer_seconds"] == 12.34
    assert summary["transfer_bytes"] == 987654321
    assert summary["transfer_retries"] == 3
    assert summary["transfer_peak_threads"] == 40
    assert summary["postprocess_kind"] == "remux"
    assert summary["postprocess_seconds"] == 5.67
    assert summary["postprocess_output_bytes"] == 987000000


def test_breakdown_line_marks_missing_postprocess(tmp_path, monkeypatch):
    """후처리 없는 경로(file)의 구분 줄은 "(no postprocess)"로 남는다 (#110 요건 6)."""
    logger = _make_logger(tmp_path, monkeypatch)
    logger.log_transfer_complete(3.21, 42, 0, 8)
    logger.log_download_complete(3.25)
    logger.log_total_breakdown(3.21, None)
    log_file = Path(logger.log_file)
    logger.save_and_close()

    text = log_file.read_text(encoding="utf-8")
    assert "Total time breakdown - Transfer: 3.21s (no postprocess)" in text

    summary = summarize(log_file)
    assert summary["transfer_seconds"] == 3.21
    assert summary["postprocess_seconds"] is None
    assert summary["postprocess_kind"] is None


def test_breakdown_line_shows_transfer_plus_postprocess_sum(tmp_path, monkeypatch):
    """구분 줄은 전송+후처리=전체 형태로 남는다 (#110 요건 4)."""
    logger = _make_logger(tmp_path, monkeypatch)
    logger.log_total_breakdown(10.0, 2.5)
    log_file = Path(logger.log_file)
    logger.save_and_close()

    text = log_file.read_text(encoding="utf-8")
    assert "Total time breakdown - Transfer: 10.00s + Postprocess: 2.50s = 12.50s" in text
