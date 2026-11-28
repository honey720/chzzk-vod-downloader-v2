"""설정 `defaultResolution` (#646) — 허용 값은 그대로 읽히고 행이 없으면 "best"다."""

import pytest

import app.system.settings as settings_module
from tests.support.settings_db import write_settings


def test_missing_row_reads_best() -> None:
    """행이 없으면 defaultResolution은 "best"여야 한다.

    빈 설정 DB -> load_setting("defaultResolution") == "best"
    """
    write_settings({})
    assert settings_module.load_setting("defaultResolution") == "best"


@pytest.mark.parametrize("value", ["best", 1080, 720, 480, 360, 144, 720.0])
def test_allowed_values_are_read_back(value: str | int | float) -> None:
    """허용 값 행은 그 값으로 읽혀야 한다 — 정수값 float는 정수로.

    행 "best" · 1080 · 720 · 480 · 360 · 144 · 720.0 -> 같은 값(720.0은 720, int)
    """
    write_settings({"defaultResolution": value})
    read = settings_module.load_setting("defaultResolution")
    assert read == value
    assert type(read) is (str if value == "best" else int)
