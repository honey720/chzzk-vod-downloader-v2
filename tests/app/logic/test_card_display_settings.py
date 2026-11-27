"""카드 화면 설정 키 — 색조 · 애니메이션 표 줄과 「행 없음 → OS 값, 행 있음 → 행 값」 (#645).

씨앗은 설정 DB에 직접 쓴다(`tests.support.settings_db` — 제품의 쓰기 함수를 거치지 않는다). OS 값은
`motion_preference.system_animations`를 갈아 끼워 정한다. 위젯을 만들지 않는다.
"""

import json

import pytest

import app.system.motion_preference as motion_preference
import app.system.settings as settings
import app.viewmodels.card_display as card_display
from tests.support.settings_db import read_rows, write_rows


def _os_animations(monkeypatch: pytest.MonkeyPatch, value: bool) -> list[int]:
    """OS 애니메이션 값을 `value`로 정한다 — 돌려준 목록에 부른 횟수가 쌓인다."""
    calls: list[int] = []

    def read() -> bool:
        calls.append(1)
        return value

    monkeypatch.setattr(motion_preference, "system_animations", read)
    return calls


# ================================================================ 표 줄


def test_state_tint_defaults_to_on() -> None:
    """cardStateTint의 표 기본값은 참이어야 한다.

    행 없음 -> default_settings()["cardStateTint"] 참 · load_setting 참
    """
    assert settings.default_settings()["cardStateTint"] is True
    assert settings.load_setting("cardStateTint") is True


def test_animations_defaults_to_null() -> None:
    """animations의 표 기본값은 null(사용자가 고르지 않음)이어야 한다.

    행 없음 -> default_settings()["animations"] None · load_setting None
    """
    assert settings.default_settings()["animations"] is None
    assert settings.load_setting("animations") is None


@pytest.mark.parametrize("value", [None, True, False])
def test_animations_row_keeps_null_true_and_false(value: bool | None) -> None:
    """animations 행이 null · true · false면 그 값 그대로 읽혀야 한다.

    행 JSON null · true · false -> load_setting == value
    """
    write_rows({"animations": json.dumps(value)})
    assert settings.load_setting("animations") is value


@pytest.mark.parametrize(
    ("key", "broken", "default"),
    [
        ("animations", 0, None),
        ("animations", "false", None),
        ("cardStateTint", None, True),
        ("cardStateTint", "false", True),
        ("cardStateTint", 0, True),
    ],
)
def test_a_broken_row_falls_back_to_the_default(
    key: str, broken: object, default: object, caplog: pytest.LogCaptureFixture
) -> None:
    """animations · cardStateTint 행이 참 · 거짓(animations는 null도) 밖의 값이면 기본값 + 경고여야 한다.

    행 = broken -> load_setting == default, 그 키 이름이 든 WARNING
    """
    write_rows({key: json.dumps(broken)})
    with caplog.at_level("WARNING", logger="app.system.settings"):
        assert settings.load_setting(key) == default
    assert any(
        record.name == "app.system.settings" and f"'{key}'" in record.getMessage()
        for record in caplog.records
    )


def test_saving_null_animations_writes_no_row() -> None:
    """행이 없을 때 animations에 null을 저장하면 행을 쓰지 않아야 한다.

    행 없음 → update_settings({"animations": None}) -> animations 행 없음
    """
    write_rows({"language": json.dumps("ko_KR")})  # 표가 있는 DB — 비교할 행이 하나는 있다
    settings.update_settings({"animations": None})
    assert read_rows() == {"language": json.dumps("ko_KR")}


# ================================================================ 행 없음 → OS 값, 행 있음 → 행 값


@pytest.mark.parametrize("os_value", [True, False])
def test_without_a_row_animations_follow_the_os(
    monkeypatch: pytest.MonkeyPatch, os_value: bool
) -> None:
    """animations 행이 없으면 카드 화면 설정의 애니메이션은 OS 값이어야 한다.

    행 없음, OS -> os_value -> refresh().animations == os_value, OS를 한 번 읽음
    """
    calls = _os_animations(monkeypatch, os_value)
    assert card_display.refresh().animations is os_value
    assert calls == [1]


@pytest.mark.parametrize("os_value", [True, False])
def test_a_null_row_follows_the_os(monkeypatch: pytest.MonkeyPatch, os_value: bool) -> None:
    """animations 행이 null이면 카드 화면 설정의 애니메이션은 OS 값이어야 한다.

    행 null, OS -> os_value -> refresh().animations == os_value
    """
    _os_animations(monkeypatch, os_value)
    write_rows({"animations": "null"})
    assert card_display.refresh().animations is os_value


@pytest.mark.parametrize(("row", "os_value"), [(True, False), (False, True)])
def test_a_true_or_false_row_wins_over_the_os(
    monkeypatch: pytest.MonkeyPatch, row: bool, os_value: bool
) -> None:
    """animations 행이 참 · 거짓이면 OS 값과 다르더라도 행 값이어야 하고 OS를 읽지 않아야 한다.

    행 row, OS -> os_value(반대) -> refresh().animations == row, OS 읽음 0번
    """
    calls = _os_animations(monkeypatch, os_value)
    write_rows({"animations": json.dumps(row)})
    assert card_display.refresh().animations is row
    assert calls == []


def test_a_broken_animations_row_follows_the_os(monkeypatch: pytest.MonkeyPatch) -> None:
    """animations 행이 깨졌으면(검증에 걸림) 카드 화면 설정의 애니메이션은 OS 값이어야 한다.

    행 "yes", OS -> 거짓 -> refresh().animations 거짓
    """
    _os_animations(monkeypatch, False)
    write_rows({"animations": json.dumps("yes")})
    assert card_display.refresh().animations is False


@pytest.mark.parametrize(("row", "expected"), [(None, True), (False, False), (True, True)])
def test_state_tint_follows_its_row(row: bool | None, expected: bool) -> None:
    """카드 화면 설정의 색조는 cardStateTint 행 값이고 행이 없으면 참이어야 한다.

    행 없음 · false · true -> refresh().state_tint == expected
    """
    if row is not None:
        write_rows({"cardStateTint": json.dumps(row)})
    assert card_display.refresh().state_tint is expected


def test_following_the_os_is_logged_once_per_value(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """OS 값을 따를 때 그 값이 바뀔 때만 로그 한 줄을 남겨야 한다.

    OS 끔 → refresh 두 번 → OS 켬 → refresh -> 「OS 설정을 따름(끔)」 1줄 · 「(켬)」 1줄
    """
    monkeypatch.setitem(card_display._LOGGED, "os", None)
    _os_animations(monkeypatch, False)
    with caplog.at_level("INFO", logger="app.viewmodels.card_display"):
        card_display.refresh()
        card_display.refresh()
        _os_animations(monkeypatch, True)
        card_display.refresh()
    lines = [
        record.getMessage()
        for record in caplog.records
        if record.name == "app.viewmodels.card_display"
    ]
    assert lines == ["애니메이션: OS 설정을 따름(끔)", "애니메이션: OS 설정을 따름(켬)"]


# ================================================================ 활성 때 비교 · 넘긴 OS 값


def test_a_given_os_value_is_used_without_reading_the_os(monkeypatch: pytest.MonkeyPatch) -> None:
    """refresh에 OS 값을 넘기면 animations 행이 없을 때 그 값이어야 하고 OS를 읽지 않아야 한다.

    행 없음, OS 켬, refresh(False) -> animations 거짓, OS 읽음 0번
    """
    calls = _os_animations(monkeypatch, True)
    assert card_display.refresh(False).animations is False
    assert calls == []


@pytest.mark.parametrize(("applied", "now", "expected"), [(True, False, True), (True, True, False)])
def test_os_value_changed_compares_with_the_value_on_the_cards(
    monkeypatch: pytest.MonkeyPatch, applied: bool, now: bool, expected: bool
) -> None:
    """os_value_changed는 카드에 마지막으로 건 OS 값과 now가 다를 때만 참이어야 한다.

    행 없음, OS applied로 refresh → os_value_changed(now) -> expected
    """
    _os_animations(monkeypatch, applied)
    card_display.refresh()
    assert card_display.os_value_changed(now) is expected


def test_os_value_changed_after_use_records_now_and_is_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """use로 값을 바꾼 뒤 처음 부르면 os_value_changed는 거짓이고 그 값을 기준으로 적어야 한다.

    OS 켬으로 refresh → use(CardDisplay(animations=False)) → os_value_changed(False) 거짓
    → os_value_changed(True) 참
    """
    _os_animations(monkeypatch, True)
    card_display.refresh()
    card_display.use(card_display.CardDisplay(animations=False))
    assert card_display.os_value_changed(False) is False
    assert card_display.os_value_changed(True) is True


def test_animations_setting_does_not_move_the_os_value_on_the_cards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """animations_setting을 불러도 카드에 건 OS 값은 그대로여야 한다.

    OS 켬으로 refresh → OS 끔 → animations_setting() 거짓 → os_value_changed(False) 참
    """
    _os_animations(monkeypatch, True)
    card_display.refresh()
    _os_animations(monkeypatch, False)
    assert card_display.animations_setting() is False
    assert card_display.os_value_changed(False) is True
