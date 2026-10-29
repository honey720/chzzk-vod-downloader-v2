"""설정 DB 게이트 (#475) — 버전 올리기 · config.json 가져오기 · 코드가 모르는 버전 · 초기화.

씨앗과 판정은 DB · config.json을 직접 읽고 쓴다(tests/support/settings_db.py) — 제품의 저장 함수로
씨앗을 만들면 제품과 같은 모양이라 차이가 생길 수 없다. 앱 데이터 폴더는 conftest의 autouse
픽스처가 테스트마다 임시 폴더로 돌린다. 위젯을 만들지 않는다.
"""

import json
import os
import sqlite3

import pytest

import app.system.settings as settings_module
from tests.support.settings_db import (
    has_settings_table,
    read_rows,
    read_settings,
    user_version,
    write_config_json,
    write_rows,
    write_settings,
)

#: 테스트가 쓰는 자리표시 인증 값 — 실제 쿠키가 아니다.
COOKIES = {"NID_AUT": "aut-placeholder", "NID_SES": "ses-placeholder"}

#: 2.11.x가 저장하던 모양의 config.json — 키 여섯 전부(version 포함).
OLD_CONFIG = {
    "version": 2,
    "cookies": dict(COOKIES),
    "afterDownload": "shutdown",
    "language": "ko_KR",
    "downloadPath": "D:/vod",
    "window": {"x": 10, "y": 20, "width": 700, "height": 600, "maximized": True},
}


def _config_json_path() -> str:
    """앱 데이터 폴더의 옛 config.json 경로."""
    return os.path.join(settings_module.data_dir(), "config.json")


def _database_bytes() -> bytes:
    """DB 파일의 바이트 — 쓰기가 없었는지 견줄 때."""
    with open(settings_module.database_path(), "rb") as file:
        return file.read()


class TestImportConfigJson:
    """0 → 1 단계의 config.json 가져오기. ⚠️ 2.14.0에서 `import_config_json`과 함께 이 클래스를 지운다."""

    def test_the_three_keys_are_imported_and_the_file_is_removed(self):
        """config.json이 있으면 쿠키 · 저장 위치 · 언어만 넣고, 커밋한 뒤 파일을 지워야 한다.

        config.json 키 여섯 -> 설정 테이블에 cookies · downloadPath · language 셋 · user_version 1
        · config.json 없음
        """
        write_config_json(OLD_CONFIG)
        settings_module.load_settings()
        assert read_settings() == {
            "cookies": COOKIES,
            "downloadPath": "D:/vod",
            "language": "ko_KR",
        }
        assert user_version() == 1
        assert not os.path.exists(_config_json_path())

    def test_the_dropped_keys_start_from_defaults(self):
        """가져오지 않은 키(창 크기 · 다운로드 뒤 동작)는 기본값으로 읽혀야 한다.

        config.json afterDownload=shutdown · window 기록 -> afterDownload "none" · window {}
        """
        write_config_json(OLD_CONFIG)
        settings = settings_module.open_settings()
        assert settings["afterDownload"] == "none"
        assert settings["window"] == {}
        assert settings["cookies"] == COOKIES

    @pytest.mark.parametrize(
        "content",
        (
            b'{"cookies": {"NID_AUT": "a"',  # 잘린 JSON
            b"[1, 2]",  # 최상위가 dict가 아님
            b'"text"',
            b"\xff\xfe\x00{",  # utf-8로 못 읽는 바이트
        ),
    )
    def test_an_unreadable_file_starts_from_defaults_and_is_removed(self, content, caplog):
        """못 읽는 config.json이면 아무것도 넣지 않고 기본값으로 시작하며 파일을 지워야 한다.

        깨진 config.json -> 설정 테이블 비어 있음 · 읽은 설정 = 기본값 · user_version 1 · 파일 없음 · 경고
        """
        path = write_config_json("")
        with open(path, "wb") as file:
            file.write(content)
        with caplog.at_level("WARNING", logger="app.system.settings"):
            settings = settings_module.load_settings()
        assert settings == settings_module.default_settings()
        assert read_settings() == {}
        assert user_version() == 1
        assert not os.path.exists(path)
        assert any("config.json" in r.getMessage() for r in caplog.records), caplog.text

    def test_an_invalid_key_is_skipped_and_the_others_are_imported(self, caplog):
        """세 키 중 표의 검증에 걸린 키만 빼고 나머지는 넣어야 한다.

        config.json language="xx_XX" · 쿠키 · 저장 위치 정상 -> language 행 없음 · 나머지 둘 있음 · 경고
        """
        write_config_json({**OLD_CONFIG, "language": "xx_XX"})
        with caplog.at_level("WARNING", logger="app.system.settings"):
            settings_module.load_settings()
        assert read_settings() == {"cookies": COOKIES, "downloadPath": "D:/vod"}
        assert any("'language'" in r.getMessage() for r in caplog.records), caplog.text

    def test_without_a_file_the_database_starts_empty(self):
        """config.json이 없으면 빈 설정 테이블로 버전 1이 되어야 한다.

        config.json 없음 -> 설정 테이블 있음 · 행 0 · user_version 1
        """
        settings_module.load_settings()
        assert has_settings_table()
        assert read_settings() == {}
        assert user_version() == 1

    def test_a_database_at_version_one_never_looks_for_the_file(self):
        """DB 버전이 1이면 config.json이 있어도 가져오지도 지우지도 않아야 한다.

        user_version 1 · 언어 en_US · config.json(언어 ko_KR) -> 언어 en_US · config.json 남음
        """
        write_settings({"language": "en_US"})
        path = write_config_json(OLD_CONFIG)
        assert settings_module.load_settings()["language"] == "en_US"
        assert os.path.exists(path)

    def test_a_failure_inside_the_step_leaves_version_zero_and_the_file(self, monkeypatch):
        """단계 안에서 예외가 나면 user_version 0 · 설정 테이블 없음 · config.json 그대로여야 한다.

        가져오기 뒤 · 버전 쓰기 전에 예외 -> 예외가 올라옴 · user_version 0 · 테이블 없음 · config.json 남음
        다음 열기(예외 없음) -> 세 키 가져옴 · user_version 1 · config.json 없음
        """
        path = write_config_json(OLD_CONFIG)
        real_import = settings_module.import_config_json

        def import_then_fail(conn):
            """실제 가져오기를 한 뒤 예외를 낸다 — 버전 쓰기 · 커밋 앞."""
            real_import(conn)
            raise RuntimeError("injected")

        monkeypatch.setattr(settings_module, "import_config_json", import_then_fail)
        with pytest.raises(RuntimeError, match="injected"):
            settings_module.load_settings()
        assert user_version() == 0
        assert not has_settings_table()
        assert os.path.exists(path), "커밋 전에 config.json이 지워졌다"

        monkeypatch.setattr(settings_module, "import_config_json", real_import)
        assert settings_module.load_settings()["cookies"] == COOKIES
        assert user_version() == 1
        assert not os.path.exists(path)

    def test_an_already_raised_version_skips_the_step(self):
        """잠금을 잡은 뒤 버전이 이미 올라가 있으면(다른 실행이 먼저 올림) 단계를 돌리지 않아야 한다.

        DB user_version 1 · 단계 0을 돌리라는 요청 · config.json 있음 -> 1을 돌려줌 · config.json 남음
        """
        write_settings({})
        path = write_config_json(OLD_CONFIG)
        conn = sqlite3.connect(settings_module.database_path(), isolation_level=None)
        try:
            assert settings_module._run_step(conn, 0) == 1
        finally:
            conn.close()
        assert os.path.exists(path)
        assert read_settings() == {}


class TestANewerDatabaseIsNeverWritten:
    """코드가 모르는 버전(옛 판이 새 판의 DB를 엶)이면 DB에 쓰지 않고 메모리 안 기본값으로 돈다."""

    @pytest.fixture
    def newer(self):
        """user_version 2(이 코드의 목표 + 1)에 언어 ko_KR이 든 DB."""
        write_settings({"language": "ko_KR"}, user_version=settings_module.DATABASE_VERSION + 1)
        return _database_bytes()

    def test_open_save_and_reset_leave_the_file_byte_for_byte(self, newer):
        """시작 · 저장 · 초기화를 거쳐도 DB 파일의 바이트가 그대로여야 한다.

        user_version 2 DB -> open_settings · save_settings(언어 en_US) · reset_settings 뒤 바이트 같음
        """
        settings_module.open_settings()
        settings_module.save_settings({"language": "en_US"})
        settings_module.reset_settings()
        assert _database_bytes() == newer

    def test_the_run_uses_defaults_in_memory(self, newer):
        """읽은 설정은 기본값이고, 저장한 값은 이번 실행 동안 메모리에서 읽혀야 한다.

        user_version 2 DB(언어 ko_KR) -> 처음 읽기 = 기본값 · 언어 ko_KR 저장 뒤 읽기 = ko_KR
        """
        assert settings_module.open_settings() == settings_module.default_settings()
        settings_module.save_settings({"language": "ko_KR"})
        assert settings_module.load_settings()["language"] == "ko_KR"

    def test_the_warning_is_logged_once(self, newer, caplog):
        """경고는 처음 열 때 한 번만 남아야 한다.

        user_version 2 DB · 읽기 셋 · 저장 하나 -> 메모리 경고 1건
        """
        with caplog.at_level("WARNING", logger="app.system.settings"):
            for _ in range(3):
                settings_module.load_settings()
            settings_module.save_settings({"language": "ko_KR"})
        memory = [r for r in caplog.records if "in memory only" in r.getMessage()]
        assert len(memory) == 1, caplog.text

    def test_a_newer_database_is_not_set_aside(self, newer):
        """높은 버전 DB는 손상이 아니다 — 비켜 두지 않고 제자리에 그대로 있어야 한다.

        user_version 2 DB -> open_settings · save_settings 뒤 cvdv2.db.broken 없음 · DB 바이트 같음
        """
        settings_module.open_settings()
        settings_module.save_settings({"language": "en_US"})
        assert not os.path.exists(settings_module.broken_database_path())
        assert _database_bytes() == newer


def _write_damaged(content: bytes = b"not a database " * 100) -> bytes:
    """DB 자리에 DB가 아닌 바이트를 쓴다."""
    os.makedirs(settings_module.data_dir(), exist_ok=True)
    with open(settings_module.database_path(), "wb") as file:
        file.write(content)
    return content


def _broken_bytes() -> bytes:
    """비켜 둔 cvdv2.db.broken의 바이트."""
    with open(settings_module.broken_database_path(), "rb") as file:
        return file.read()


class TestADamagedDatabaseIsSetAside:
    """DB가 아니거나 손상된 파일은 cvdv2.db.broken으로 비켜 두고 새 DB로 시작한다."""

    def test_a_file_that_is_not_a_database_is_moved_and_a_new_one_starts(self, caplog):
        """DB가 아닌 파일이면 .broken으로 옮겨지고, 새 DB가 버전 1로 생겨 저장이 남아야 한다.

        DB 자리에 글자 파일 -> .broken = 그 바이트 · 새 DB user_version 1 · 읽기 = 기본값
        · 언어 ko_KR 저장 뒤 새 DB에 그 행 · 오류 로그에 .broken 경로
        """
        damaged = _write_damaged()
        with caplog.at_level("ERROR", logger="app.system.settings"):
            assert settings_module.load_settings() == settings_module.default_settings()
        assert _broken_bytes() == damaged
        assert user_version() == 1
        settings_module.save_settings({"language": "ko_KR"})
        assert read_settings() == {"language": "ko_KR"}
        assert any(
            settings_module.broken_database_path() in r.getMessage() for r in caplog.records
        ), caplog.text

    def test_damage_found_while_reading_rows_is_set_aside(self):
        """머리는 멀쩡하고 표의 쪽이 깨진 DB(읽을 때 malformed)도 비켜 두고 새 DB로 읽어야 한다.

        언어 행이 든 DB의 둘째 쪽(4096바이트부터 200바이트)을 0xFF로 -> 읽기 = 기본값 · .broken 있음
        """
        write_settings({"language": "ko_KR"})
        with open(settings_module.database_path(), "r+b") as file:
            file.seek(4096)  # 둘째 쪽 — 설정 테이블의 쪽(쪽 크기 4096)
            file.write(b"\xff" * 200)
        assert settings_module.load_settings() == settings_module.default_settings()
        assert os.path.exists(settings_module.broken_database_path())
        assert read_settings() == {}

    def test_damage_is_set_aside_even_when_the_rollback_fails(self, monkeypatch, caplog):
        """트랜잭션 안에서 손상이 드러나고 되돌리기마저 다른 예외를 내도, 손상 DB는 비켜 둬야 한다.

        저장 위치 행이 든 DB의 둘째 쪽을 0xFF로 · ROLLBACK이 OperationalError를 냄
        -> save_settings(언어 ko_KR) 예외 없음 · .broken 있음 · 새 DB에 언어 ko_KR 행만 · 되돌리기 실패 경고
        """
        write_settings({"downloadPath": "D:/vod"})
        with open(settings_module.database_path(), "r+b") as file:
            file.seek(4096)  # 둘째 쪽 — 설정 테이블의 쪽(쪽 크기 4096)
            file.write(b"\xff" * 200)

        class RollbackFails(sqlite3.Connection):
            """ROLLBACK만 다른 예외를 낸다 — 손상 DB에서 되돌리기도 실패하는 상황."""

            def execute(self, sql, *args):
                if sql == "ROLLBACK":
                    raise sqlite3.OperationalError("injected rollback failure")
                return super().execute(sql, *args)

        real_connect = sqlite3.connect
        # 이 패치만 되돌린다 — monkeypatch.undo()는 conftest의 앱 데이터 폴더 격리까지 되돌린다
        with monkeypatch.context() as patch:
            patch.setattr(
                settings_module.sqlite3,
                "connect",
                lambda *args, **kwargs: real_connect(*args, factory=RollbackFails, **kwargs),
            )
            with caplog.at_level("WARNING", logger="app.system.settings"):
                settings_module.save_settings({"language": "ko_KR"})
        assert os.path.exists(settings_module.broken_database_path())
        assert read_settings() == {"language": "ko_KR"}
        assert any("rollback failed" in r.getMessage() for r in caplog.records), caplog.text

    def test_only_one_broken_file_is_kept(self):
        """이미 .broken이 있으면 새로 깨진 파일이 그 자리를 덮어써야 한다 — 하나만 남는다.

        옛 .broken(바이트 A) · DB 자리에 바이트 B -> .broken = B · 폴더의 .broken 하나
        """
        os.makedirs(settings_module.data_dir(), exist_ok=True)
        with open(settings_module.broken_database_path(), "wb") as file:
            file.write(b"older damage " * 10)
        newer = _write_damaged(b"newer damage " * 100)
        settings_module.load_settings()
        assert _broken_bytes() == newer
        names = os.listdir(settings_module.data_dir())
        assert [n for n in names if n.endswith(settings_module.BROKEN_SUFFIX)] == [
            os.path.basename(settings_module.broken_database_path())
        ]

    def test_a_lock_timeout_is_not_damage(self, monkeypatch):
        """다른 연결이 쓰기 잠금을 쥐어 대기가 넘치면 예외가 올라오고 DB는 비켜지지 않아야 한다.

        다른 연결의 BEGIN EXCLUSIVE · BUSY_TIMEOUT 0.1초 -> save_settings가 OperationalError
        · .broken 없음 · 잠금을 푼 뒤 원래 행 그대로
        """
        write_settings({"language": "ko_KR"})
        monkeypatch.setattr(settings_module, "BUSY_TIMEOUT", 0.1)
        holder = sqlite3.connect(settings_module.database_path(), isolation_level=None)
        try:
            holder.execute("BEGIN EXCLUSIVE")
            with pytest.raises(sqlite3.OperationalError):
                settings_module.save_settings({"language": "en_US"})
            holder.execute("ROLLBACK")
        finally:
            holder.close()
        assert not os.path.exists(settings_module.broken_database_path())
        assert read_settings() == {"language": "ko_KR"}

    def test_a_file_that_cannot_be_moved_runs_in_memory(self, monkeypatch, caplog):
        """깨진 파일을 옮기지 못하면 그 파일을 그대로 두고 메모리 안 기본값으로 돌아야 한다.

        DB 자리에 글자 파일 · os.replace가 PermissionError -> 읽기 = 기본값 · 파일 바이트 같음
        · 저장 뒤에도 같음 · 메모리 경고
        """
        damaged = _write_damaged()

        def refuse(src, dst):
            """옮기기를 거절한다 — 다른 프로그램이 파일을 쥔 상황."""
            raise PermissionError("held by another program")

        monkeypatch.setattr(settings_module.os, "replace", refuse)
        with caplog.at_level("WARNING", logger="app.system.settings"):
            assert settings_module.load_settings() == settings_module.default_settings()
            settings_module.save_settings({"language": "ko_KR"})
        assert _database_bytes() == damaged
        assert settings_module.load_settings()["language"] == "ko_KR"
        assert any("in memory only" in r.getMessage() for r in caplog.records), caplog.text


class TestSaveAndReset:
    """저장은 키마다 한 행(JSON) · 표 밖의 키는 거부 · 초기화는 전부 기본값."""

    def test_a_saved_value_is_one_json_row(self):
        """저장한 키는 설정 테이블에 JSON 문자열 한 행으로 들어가야 한다.

        save_settings(cookies · window) -> 두 행, 값은 JSON 해석 결과가 같은 dict
        """
        window = {"x": 1, "y": 2, "width": 700, "height": 600, "maximized": False}
        settings_module.save_settings({"cookies": COOKIES, "window": window})
        assert read_settings() == {"cookies": COOKIES, "window": window}

    def test_an_unknown_key_is_refused_without_writing(self):
        """표에 없는 키가 섞이면 KeyError이고 아무 행도 쓰지 않아야 한다.

        save_settings(language · stray) -> KeyError · 설정 테이블 행 0
        """
        settings_module.load_settings()
        with pytest.raises(KeyError, match="stray"):
            settings_module.save_settings({"language": "ko_KR", "stray": 1})
        assert read_settings() == {}

    def test_saving_a_loaded_dict_writes_only_the_changed_row(self):
        """읽은 dict를 통째로 넘겨도 저장된 값과 다른 키의 행만 써야 한다.

        쿠키 · 저장 위치 행(제품이 쓰지 않을 공백 모양 JSON) · 언어 행 없음
        -> 읽은 dict의 언어를 ko_KR로 바꿔 저장 -> 쿠키 · 저장 위치 행 문자열 그대로 · 언어 행만 생김
        · 기본값 그대로인 afterDownload · window 행 없음
        """
        seeded = {
            "cookies": '{ "NID_AUT" : "aut-placeholder" , "NID_SES" : "ses-placeholder" }',
            "downloadPath": '  "D:/vod"  ',
        }
        write_rows(seeded)
        settings = settings_module.load_settings()
        settings["language"] = "ko_KR"
        settings_module.save_settings(settings)
        assert read_rows() == {**seeded, "language": '"ko_KR"'}

    def test_saving_unchanged_values_writes_nothing(self):
        """값이 하나도 바뀌지 않았으면 행을 하나도 쓰지 않아야 한다.

        빈 설정 테이블 -> 기본값 dict 저장 -> 행 0
        """
        settings_module.save_settings(settings_module.load_settings())
        assert read_rows() == {}

    def test_reset_of_one_key_removes_only_that_row(self):
        """키 하나를 초기화하면 그 행만 지워지고 그 키는 기본값으로 읽혀야 한다.

        쿠키 · 언어 행 -> reset_settings("language") -> 쿠키 행만 · 언어 en_US
        """
        write_settings({"cookies": COOKIES, "language": "ko_KR"})
        settings_module.reset_settings("language")
        assert read_settings() == {"cookies": COOKIES}
        assert settings_module.load_settings()["language"] == "en_US"

    def test_reset_of_an_unknown_key_is_refused(self):
        """표에 없는 키를 초기화하려 하면 KeyError여야 한다.

        reset_settings("stray") -> KeyError
        """
        with pytest.raises(KeyError, match="stray"):
            settings_module.reset_settings("stray")

    def test_reset_brings_every_key_back_to_its_default(self):
        """초기화하면 모든 설정이 기본값으로 읽혀야 한다.

        쿠키 · 언어 · 저장 위치가 든 DB -> reset_settings 뒤 읽기 = 기본값 · 행 0
        """
        write_settings({"cookies": COOKIES, "language": "ko_KR", "downloadPath": "D:/vod"})
        settings_module.reset_settings()
        assert settings_module.load_settings() == settings_module.default_settings()
        assert read_settings() == {}

    def test_a_value_survives_a_new_connection(self):
        """저장한 값은 다음 읽기(새 연결)에서 그대로 나와야 한다 — 다음 실행과 같은 경로.

        save_settings(언어 ko_KR · 쿠키) -> load_settings 언어 ko_KR · load_cookies 쿠키
        """
        settings_module.save_settings({"language": "ko_KR", "cookies": COOKIES})
        assert settings_module.load_settings()["language"] == "ko_KR"
        assert settings_module.load_cookies() == COOKIES


class TestPaths:
    """경로는 앱 데이터 폴더 한 곳에서 나온다."""

    def test_the_database_and_logs_live_in_the_data_folder(self):
        """DB 파일과 logs/는 앱 데이터 폴더 바로 아래여야 한다.

        data_dir() -> database_path() · logs_dir()의 부모가 data_dir()
        """
        assert os.path.dirname(settings_module.database_path()) == settings_module.data_dir()
        assert os.path.dirname(settings_module.logs_dir()) == settings_module.data_dir()
        assert os.path.basename(settings_module.logs_dir()) == "logs"

    def test_the_database_file_is_created_on_first_read(self):
        """첫 읽기에서 앱 데이터 폴더와 DB 파일이 만들어져야 한다.

        폴더 없음 -> load_settings 뒤 DB 파일 있음
        """
        assert not os.path.exists(settings_module.data_dir())
        settings_module.load_settings()
        assert os.path.isfile(settings_module.database_path())
        assert json.dumps(read_settings()) == "{}"
