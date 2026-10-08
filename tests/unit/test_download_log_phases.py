"""DownloadLogger 단계 경계 줄(#110)과 요약 스크립트의 하위 호환 검증.

실제 로그 파일을 만들어 scripts/summarize_download_log.py가
(a) 기존 지표를 계속 뽑고 (b) 새 단계 지표를 추가로 뽑는지 종단 검증한다 —
새 줄의 메시지 형식과 요약 스크립트 패턴이 어긋나면 여기서 잡힌다.
"""

import tomllib
from pathlib import Path
from types import SimpleNamespace

import config.config as config_module
from app.download_logger import DownloadLogger
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


def test_build_marker_constants_stay_at_source_defaults():
    """저장소 소스의 BUILD_COMMIT·IS_RELEASE_BUILD는 항상 기본값이어야 한다 (#195).

    이 두 상수는 scripts/inject_build_info.py가 빌드 직전에만 실제 값으로
    고쳐 쓴다 — 그 결과가 커밋에 섞여 들어오면 안 된다. 특히
    ``IS_RELEASE_BUILD = True``가 실수로 커밋되면, 그 순간부터 소스 실행
    포함 모든 실행이 정식 릴리즈로 위장하고 로그에 깨끗한 버전이 찍혀
    아무도 눈치채지 못한다 — 이 기능(#195)의 목적 자체가 조용히
    무효화되는 자리다. #116의 APP_VERSION 일치 가드와 같은 성격·같은
    자리이며, 매 CI 실행마다 잡는다.
    """
    assert config_module.IS_RELEASE_BUILD is False
    assert config_module.BUILD_COMMIT == "unknown"


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


def test_download_info_logs_section_paths_instead_of_output_path(tmp_path, monkeypatch):
    """구간 경로를 넘기면 시작 정보 블록은 output_path 줄 대신 구간 파일마다 한 줄씩 적어야 한다 (#309).

    output_path "out.mp4"인 아이템, 구간 경로 ("a 1080p_1.mp4", "a 1080p_2.mp4")
    -> "output_path_1: 'a 1080p_1.mp4'" · "output_path_2: 'a 1080p_2.mp4'" 줄이 있고 "output_path: " 줄은 없다
    """
    logger = _make_logger(tmp_path, monkeypatch)
    logger.log_download_info(_stub_item(), ("a 1080p_1.mp4", "a 1080p_2.mp4"))
    log_file = Path(logger.log_file)
    logger.save_and_close()

    text = log_file.read_text(encoding="utf-8")
    assert "output_path_1: 'a 1080p_1.mp4'" in text
    assert "output_path_2: 'a 1080p_2.mp4'" in text
    assert "output_path: " not in text
    assert "download_path: 'downloads'" in text  # 같은 블록의 다른 줄은 그대로 찍힌다


def test_download_info_logs_output_path_without_sections(tmp_path, monkeypatch):
    """구간 경로를 넘기지 않으면 시작 정보 블록은 지금처럼 output_path 줄을 적어야 한다.

    output_path "out.mp4"인 아이템, 구간 경로 없음
    -> "output_path: 'out.mp4'" 줄이 있고 "output_path_1" 줄은 없다
    """
    logger = _make_logger(tmp_path, monkeypatch)
    logger.log_download_info(_stub_item())
    log_file = Path(logger.log_file)
    logger.save_and_close()

    text = log_file.read_text(encoding="utf-8")
    assert "output_path: 'out.mp4'" in text
    assert "output_path_1" not in text


def test_prepare_and_net_transfer_lines_are_added_without_changing_the_transfer_line(
    tmp_path, monkeypatch
):
    """준비 줄과 준비를 뺀 전송 줄을 더해도 Transfer 줄과 요약 지표는 그대로여야 한다 (#309).

    준비 6.90초(moov reused) · Transfer 12.34초 · 준비를 뺀 전송 5.44초를 차례로 로깅
    -> 로그에 세 줄이 그 글자 그대로, 요약의 transfer_seconds == 12.34
    """
    logger = _make_logger(tmp_path, monkeypatch)
    logger.log_prepare_complete(6.9, "moov reused")
    logger.log_transfer_complete(12.34, 987654321, 3, 40)
    logger.log_transfer_net(5.44)
    log_file = Path(logger.log_file)
    logger.save_and_close()

    text = log_file.read_text(encoding="utf-8")
    assert "Prepare completed in 6.90 seconds - moov reused" in text
    assert (
        "Transfer completed in 12.34 seconds - Bytes: 987654321 - Retries: 3 - Peak threads: 40"
        in text
    )
    assert "Transfer without prepare: 5.44 seconds" in text
    assert summarize(log_file)["transfer_seconds"] == 12.34


def test_prepare_line_without_a_note_ends_at_the_seconds(tmp_path, monkeypatch):
    """덧붙일 말이 없는 준비 줄은 시간에서 끝나야 한다 (#309).

    준비 0.02초, 덧붙인 말 없음 -> 줄이 "Prepare completed in 0.02 seconds"로 끝남
    """
    logger = _make_logger(tmp_path, monkeypatch)
    logger.log_prepare_complete(0.02)
    log_file = Path(logger.log_file)
    logger.save_and_close()

    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert [line for line in lines if "Prepare completed" in line][0].endswith(
        "Prepare completed in 0.02 seconds"
    )


def test_cut_stage_line_lists_each_stage_and_their_sum(tmp_path, monkeypatch):
    """컷 단계 줄은 구간 번호 · 단계마다의 시간 · 그 합을 한 줄에 적어야 한다 (#309).

    구간 2/3, 단계 probe 0.41초 · 0_head 1.20초 · audio 2.00초 · mux 0.30초
    -> "Cut 2/3 stages - probe: 0.41s, 0_head: 1.20s, audio: 2.00s, mux: 0.30s = 3.91s"
    """
    logger = _make_logger(tmp_path, monkeypatch)
    logger.log_cut_setup(0.52)
    logger.log_cut_stages(2, 3, (("probe", 0.41), ("0_head", 1.2), ("audio", 2.0), ("mux", 0.3)))
    log_file = Path(logger.log_file)
    logger.save_and_close()

    text = log_file.read_text(encoding="utf-8")
    assert "Cut frames prepared in 0.52 seconds" in text
    assert "Cut 2/3 stages - probe: 0.41s, 0_head: 1.20s, audio: 2.00s, mux: 0.30s = 3.91s" in text
