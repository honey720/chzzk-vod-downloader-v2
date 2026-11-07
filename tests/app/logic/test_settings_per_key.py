"""설정을 키 단위로 읽고 쓰는 함수 (#519) — `load_setting` · `update_settings`.

씨앗과 판정은 DB를 직접 읽고 쓴다(tests/support/settings_db.py). 제품이 쓰지 않을 모양(공백이 든
JSON)으로 씨앗을 써 두면, 행을 다시 썼는지가 문자열 비교로 드러난다. 앱 데이터 폴더는 conftest의
autouse 픽스처가 테스트마다 임시 폴더로 돌린다.
"""

import logging

import pytest

import app.system.settings as settings_module
from tests.support.settings_db import read_rows, read_settings, write_rows, write_settings

#: 테스트가 쓰는 자리표시 인증 값 — 실제 쿠키가 아니다.
COOKIES = {"NID_AUT": "aut-placeholder", "NID_SES": "ses-placeholder"}

#: 제품이 쓰지 않을 모양의 행 — 공백이 든 JSON. 다시 쓰이면 문자열이 바뀐다.
ODD_ROWS = {
    "cookies": '{ "NID_AUT" : "aut-placeholder" , "NID_SES" : "ses-placeholder" }',
    "sortOrder": '  "channel:desc"  ',
}


class TestLoadSetting:
    """키 하나 읽기 — 그 행만 읽고 그 키만 검증한다."""

    def test_a_stored_row_is_read(self) -> None:
        """행이 있는 키는 그 행의 값을 읽어야 한다.

        cookies 행 -> load_setting("cookies") == COOKIES
        """
        write_settings({"cookies": COOKIES})
        assert settings_module.load_setting("cookies") == COOKIES

    def test_a_missing_row_reads_the_default(self) -> None:
        """행이 없는 키는 표의 기본값을 읽어야 한다.

        빈 설정 테이블 -> load_setting("sortOrder") == "added:asc"
        """
        write_rows({})
        assert settings_module.load_setting("sortOrder") == "added:asc"

    def test_the_value_is_a_fresh_copy(self) -> None:
        """돌려준 값을 고쳐도 다음 읽기와 기본값이 바뀌지 않아야 한다.

        빈 설정 테이블 -> 읽은 window dict · favoritePaths 목록을 고침 -> 다시 읽으면 {} · []
        """
        write_rows({})
        settings_module.load_setting("window")["x"] = 1
        settings_module.load_setting("favoritePaths").append({"name": "a", "path": "b"})
        assert settings_module.load_setting("window") == {}
        assert settings_module.load_setting("favoritePaths") == []

    def test_an_invalid_row_reads_the_default_with_a_warning(self, caplog) -> None:
        """검증에 걸리는 행은 기본값으로 읽고 그 키 이름으로 경고해야 한다.

        sortOrder 행 "sideways"(표에 없는 정렬) -> load_setting == "added:asc" · 경고에 'sortOrder'
        """
        write_settings({"sortOrder": "sideways"})
        with caplog.at_level(logging.WARNING, logger=settings_module.__name__):
            assert settings_module.load_setting("sortOrder") == "added:asc"
        assert any("'sortOrder'" in record.getMessage() for record in caplog.records)

    def test_a_row_that_is_not_json_reads_the_default(self) -> None:
        """JSON이 아닌 행은 기본값으로 읽어야 한다.

        language 행 "{" -> load_setting("language") == "en_US"
        """
        write_rows({"language": "{"})
        assert settings_module.load_setting("language") == "en_US"

    def test_other_broken_rows_are_not_read(self, caplog) -> None:
        """다른 키의 행이 깨져 있어도 읽는 키에는 경고가 나지 않아야 한다 — 그 행만 읽는다.

        language 행 "{" · sortOrder 행 "title:asc" -> load_setting("sortOrder") == "title:asc" · 경고 0건
        """
        write_rows({"language": "{", "sortOrder": '"title:asc"'})
        with caplog.at_level(logging.WARNING, logger=settings_module.__name__):
            logging.getLogger(settings_module.__name__).warning("marker")  # 캡처가 살아 있다는 표식
            assert settings_module.load_setting("sortOrder") == "title:asc"
        assert [record.getMessage() for record in caplog.records] == ["marker"]

    def test_an_unknown_key_is_refused(self) -> None:
        """표에 없는 키를 읽으면 KeyError여야 한다.

        load_setting("stray") -> KeyError
        """
        with pytest.raises(KeyError, match="stray"):
            settings_module.load_setting("stray")


class TestUpdateSettings:
    """바꿀 키만 쓰기 — 넘긴 키만 검증해 저장된 값과 다른 행만 쓴다."""

    def test_one_key_is_written(self) -> None:
        """키 하나를 넘기면 그 키의 행 하나만 생겨야 한다.

        빈 설정 테이블 -> update_settings(sortOrder "title:desc") -> 행 {sortOrder: "title:desc"}
        """
        write_rows({})
        settings_module.update_settings({"sortOrder": "title:desc"})
        assert read_settings() == {"sortOrder": "title:desc"}

    def test_several_keys_are_written_together(self) -> None:
        """여러 키를 넘기면 그 키들의 행이 모두 써져야 한다.

        빈 설정 테이블 -> update_settings(cookies · language ko_KR) -> 두 행
        """
        write_rows({})
        settings_module.update_settings({"cookies": COOKIES, "language": "ko_KR"})
        assert read_settings() == {"cookies": COOKIES, "language": "ko_KR"}

    def test_keys_that_were_not_passed_are_not_rewritten(self) -> None:
        """넘기지 않은 키의 행은 문자열 그대로여야 한다.

        cookies · sortOrder 행(공백이 든 JSON) -> update_settings(language ko_KR)
        -> cookies · sortOrder 행 문자열 그대로 · language 행만 생김
        """
        write_rows(ODD_ROWS)
        settings_module.update_settings({"language": "ko_KR"})
        assert read_rows() == {**ODD_ROWS, "language": '"ko_KR"'}

    def test_an_unchanged_value_is_not_rewritten(self) -> None:
        """저장된 값과 같은 키는 다시 쓰지 않아야 한다.

        cookies · sortOrder 행(공백이 든 JSON) -> 같은 값으로 update_settings -> 두 행 문자열 그대로
        """
        write_rows(ODD_ROWS)
        settings_module.update_settings({"cookies": COOKIES, "sortOrder": "channel:desc"})
        assert read_rows() == ODD_ROWS

    def test_a_default_value_leaves_no_row(self) -> None:
        """행이 없는 키에 기본값을 넘기면 행을 만들지 않아야 한다.

        빈 설정 테이블 -> update_settings(afterDownload "none") -> 행 0
        """
        write_rows({})
        settings_module.update_settings({"afterDownload": "none"})
        assert read_rows() == {}

    def test_an_unknown_key_is_refused_without_writing(self) -> None:
        """표에 없는 키가 섞이면 KeyError이고 아무 행도 쓰지 않아야 한다.

        update_settings(language ko_KR · stray) -> KeyError · 행 0
        """
        write_rows({})
        with pytest.raises(KeyError, match="stray"):
            settings_module.update_settings({"language": "ko_KR", "stray": 1})
        assert read_rows() == {}

    def test_an_invalid_value_is_refused_without_writing(self) -> None:
        """검증에 걸리는 값이 섞이면 InvalidSetting이고 다른 키도 쓰지 않아야 한다.

        update_settings(language ko_KR · sortOrder "sideways") -> InvalidSetting · 행 0
        """
        write_rows({})
        with pytest.raises(settings_module.InvalidSetting):
            settings_module.update_settings({"language": "ko_KR", "sortOrder": "sideways"})
        assert read_rows() == {}

    def test_the_written_value_is_the_checked_one(self) -> None:
        """검증이 고치는 값(문자열이 아닌 쿠키)은 고친 값으로 써야 한다.

        update_settings(cookies NID_AUT 7 · NID_SES "s") -> 행 cookies {NID_AUT: "", NID_SES: "s"}
        """
        write_rows({})
        settings_module.update_settings({"cookies": {"NID_AUT": 7, "NID_SES": "s"}})
        assert read_settings() == {"cookies": {"NID_AUT": "", "NID_SES": "s"}}

    def test_load_cookies_reads_only_the_cookie_row(self, caplog) -> None:
        """쿠키 읽기는 cookies 행만 읽어야 한다 — 다른 행이 깨져도 경고가 나지 않는다.

        cookies 행 · language 행 "{" -> load_cookies() == COOKIES · 경고 0건
        """
        write_rows({"cookies": ODD_ROWS["cookies"], "language": "{"})
        with caplog.at_level(logging.WARNING, logger=settings_module.__name__):
            logging.getLogger(settings_module.__name__).warning("marker")  # 캡처가 살아 있다는 표식
            assert settings_module.load_cookies() == COOKIES
        assert [record.getMessage() for record in caplog.records] == ["marker"]


class TestTheSameApiInMemoryMode:
    """코드가 모르는 버전의 DB면 메모리 모드다 — 키 읽기 · 쓰기가 같은 결과를 내고 DB에 쓰지 않는다."""

    @pytest.fixture
    def newer(self) -> bytes:
        """user_version 2(이 코드의 목표 + 1)에 정렬 title:asc가 든 DB의 바이트."""
        write_settings(
            {"sortOrder": "title:asc"}, user_version=settings_module.DATABASE_VERSION + 1
        )
        with open(settings_module.database_path(), "rb") as file:
            return file.read()

    def test_keys_are_read_and_written_in_memory(self, newer: bytes) -> None:
        """메모리 모드에서는 기본값으로 읽고, 쓴 키는 이번 실행 동안 읽혀야 하며 DB는 그대로여야 한다.

        user_version 2 DB -> load_setting("sortOrder") == "added:asc"
        -> update_settings(sortOrder "channel:desc") 뒤 load_setting == "channel:desc" · DB 바이트 같음
        """
        settings_module.open_settings()
        assert settings_module.load_setting("sortOrder") == "added:asc"
        settings_module.update_settings({"sortOrder": "channel:desc"})
        assert settings_module.load_setting("sortOrder") == "channel:desc"
        with open(settings_module.database_path(), "rb") as file:
            assert file.read() == newer

    def test_memory_mode_refuses_the_same_bad_input(self, newer: bytes) -> None:
        """메모리 모드에서도 미등재 키는 KeyError, 검증에 걸리는 값은 InvalidSetting이어야 한다.

        user_version 2 DB -> update_settings(stray) KeyError · update_settings(sortOrder "sideways") InvalidSetting
        -> load_setting("sortOrder") == "added:asc"
        """
        settings_module.open_settings()
        with pytest.raises(KeyError):
            settings_module.update_settings({"stray": 1})
        with pytest.raises(settings_module.InvalidSetting):
            settings_module.update_settings({"sortOrder": "sideways"})
        assert settings_module.load_setting("sortOrder") == "added:asc"

    def test_a_value_read_in_memory_mode_is_a_fresh_copy(self, newer: bytes) -> None:
        """메모리 모드에서 읽은 값을 고쳐도 메모리 안 설정이 바뀌지 않아야 한다.

        user_version 2 DB -> 읽은 favoritePaths 목록에 항목을 더함 -> 다시 읽으면 []
        """
        settings_module.open_settings()
        settings_module.load_setting("favoritePaths").append({"name": "a", "path": "b"})
        assert settings_module.load_setting("favoritePaths") == []
