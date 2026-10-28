"""시도마다의 다운로드 로그 (#410) — 앞 시도가 끝나기 전에 새 시도가 시작돼도 로그가 섞이지 않는다.

앞 시도(중지한 다운로드)의 엔진은 새 시도가 시작된 뒤에도 잠깐 더 돌며 줄을 남긴다. 두 시도의
DownloadLogger를 함께 열어 두고 각자 쓴 뒤, 실제 로그 파일의 글을 읽어 잰다.
"""

import logging

import pytest

import app.download.logger as download_logger_module
import config.config as config_module
from app.download.logger import DownloadLogger


@pytest.fixture(autouse=True)
def _logs_in_tmp(tmp_path, monkeypatch):
    """로그 폴더를 tmp_path 아래로 돌린다."""
    monkeypatch.setattr(config_module, "CONFIG_DIR", str(tmp_path))


def _text(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_lines_of_each_attempt_go_only_to_its_own_file():
    """두 시도의 로거를 함께 열고 각자 쓰면 줄은 자기 시도의 파일에만 있어야 한다.

    앞 시도 로거 → 새 시도 로거 순으로 만듦, 앞 시도가 "앞 시도의 줄", 새 시도가 "새 시도의 줄"을 씀
    -> 두 파일이 다르다, 앞 파일에 "앞 시도의 줄"만, 새 파일에 "새 시도의 줄"만 있다
    """
    earlier = DownloadLogger()
    later = DownloadLogger()

    earlier.info("앞 시도의 줄")
    later.info("새 시도의 줄")
    earlier.save_and_close()
    later.save_and_close()

    assert earlier.log_file != later.log_file
    assert "앞 시도의 줄" in _text(earlier.log_file)
    assert "새 시도의 줄" not in _text(earlier.log_file)
    assert "새 시도의 줄" in _text(later.log_file)
    assert "앞 시도의 줄" not in _text(later.log_file)


def test_closing_the_earlier_attempt_keeps_the_later_attempt_writing():
    """앞 시도가 로거를 닫아도 새 시도의 줄은 계속 새 파일에 쓰여야 한다.

    앞 · 새 시도 로거를 만듦 → 앞 시도 save_and_close → 새 시도가 "닫은 뒤의 줄"을 씀
    -> 새 파일에 "닫은 뒤의 줄"이 있다, 앞 파일에는 없다
    """
    earlier = DownloadLogger()
    later = DownloadLogger()

    earlier.save_and_close()
    later.info("닫은 뒤의 줄")
    later.save_and_close()

    assert "닫은 뒤의 줄" in _text(later.log_file)
    assert "닫은 뒤의 줄" not in _text(earlier.log_file)


def test_attempts_started_in_the_same_second_get_different_files(tmp_path, monkeypatch):
    """같은 초에 시작한 두 시도는 다른 파일을 받아야 한다 — 뒤의 것에 _2가 붙는다.

    로그 파일명의 시각을 2026-10-22 18:00:00으로 고정하고 로거 둘을 만듦
    -> 앞: download_20261022_180000.log, 뒤: download_20261022_180000_2.log
    """

    class _FixedClock:
        @staticmethod
        def now():
            from datetime import datetime

            return datetime(2026, 10, 22, 18, 0, 0)

    monkeypatch.setattr(download_logger_module, "datetime", _FixedClock)

    earlier = DownloadLogger()
    later = DownloadLogger()
    earlier.save_and_close()
    later.save_and_close()

    assert earlier.log_file == str(tmp_path / "logs" / "download_20261022_180000.log")
    assert later.log_file == str(tmp_path / "logs" / "download_20261022_180000_2.log")


def test_closing_leaves_no_handler_on_the_attempts_logger():
    """시도의 로거를 닫으면 logging 모듈에 남는 그 시도의 로거에 핸들러가 없어야 한다.

    로거를 만들고 한 줄 쓴 뒤 save_and_close, 그 로거의 이름으로 logging.getLogger를 조회
    -> 닫기 전 핸들러 1개, 닫은 뒤 0개
    """
    attempt = DownloadLogger()
    name = attempt.logger.name
    attempt.info("한 줄")
    assert len(logging.getLogger(name).handlers) == 1  # 전제: 이 시도의 파일 핸들러가 붙어 있었다

    attempt.save_and_close()

    assert logging.getLogger(name).handlers == []


def test_closing_twice_does_nothing_the_second_time():
    """save_and_close를 두 번 불러도 두 번째는 아무 일도 하지 않아야 한다.

    로거를 만들고 "한 줄"을 쓴 뒤 save_and_close 두 번
    -> 예외 없음, 파일에 "한 줄"이 한 번 있다
    """
    log = DownloadLogger()
    log.info("한 줄")

    log.save_and_close()
    log.save_and_close()

    assert _text(log.log_file).count("한 줄") == 1


def test_lines_still_reach_the_app_log(caplog):
    """시도마다의 로거도 앱 로그(루트 로거)로 줄을 넘겨야 한다.

    INFO 캡처, 로거를 만들고 "앱 로그로 가는 줄"을 씀
    -> 캡처에 그 줄이 있다, 레코드 이름이 "DownloadLogger."로 시작한다
    """
    caplog.set_level(logging.INFO)
    log = DownloadLogger()

    log.info("앱 로그로 가는 줄")
    log.save_and_close()

    records = [r for r in caplog.records if r.getMessage() == "앱 로그로 가는 줄"]
    assert len(records) == 1
    assert records[0].name.startswith("DownloadLogger.")
