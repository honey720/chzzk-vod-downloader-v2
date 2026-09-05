"""설정 스키마 표 게이트 (#257) — 표 단일 출처 · 깨진 값의 기본값 폴백 · 마이그레이션과의 순서.

핵심 단언은 **"새 키를 넣을 때 손댈 곳이 2(표 한 줄 + 사용처)인가"**다. `TestTheTableIsTheSingleSource`가
표에 임시 줄 하나를 붙이고 **다른 코드를 전혀 건드리지 않은 채** 등재 · 기본값 · 검증 ·
`update_config` 통과 후 보존이 전부 되는지 잰다. 기본값 dict가 표와 따로 하드코딩돼 있으면
(두 출처) 임시 키가 그 dict에 없어 여기서 실패한다 — 이 게이트가 없으면 "빠뜨려도 조용하다"
(#159·#253 전례)를 아무것도 막지 못한다.

그 밖: 기존 키 다섯의 깨진 값이 전부 기본값 폴백 + 경고(시작 크래시 0, `update_config` = 실제
시작 경로) · 검증이 마이그레이션 **뒤**에 도는가(구 스키마 config) · `version`이 정수가 아닐 때
마이그레이션 **앞**에서 잡히는가 · 기존 config(키 부재·구버전)가 그대로 읽히는가 · `window`
화이트리스트(`parse_saved_window`, #253에서 config 계층으로 옮겨 옴 — 순수 파이썬 값).

config 경로는 conftest의 autouse 픽스처가 테스트마다 임시 폴더로 격리한다. 위젯을 만들지 않는다
(순수 파일 게이트). #255가 넣은 원자적 저장·`.broken`·깊은 복사는 tests/unit/test_config_durability.py가
그대로 잰다(무변화).
"""

import json
import os

import pytest

import config.config as config_module
from config.config import InvalidSetting, Setting, parse_saved_window

#: 테스트가 쓰는 자리표시 인증 값 — 실제 쿠키가 아니다.
COOKIES = {"NID_AUT": "aut-placeholder", "NID_SES": "ses-placeholder"}

#: 현재 스키마의 온전한 config — 정규화가 아무것도 바꾸지 않아야 하는 기준선.
VALID = {
    "version": 2,
    "cookies": dict(COOKIES),
    "afterDownload": "sleep",
    "language": "ko_KR",
    "downloadPath": "D:/vod",
    "window": {"x": 10, "y": 20, "width": 700, "height": 600, "maximized": False},
}


def _write(cfg: object) -> None:
    """config.json에 JSON 값을 그대로 쓴다(제품의 save_config 미사용)."""
    os.makedirs(config_module.CONFIG_DIR, exist_ok=True)
    with open(config_module.CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f)


def _read() -> dict:
    """config.json을 파일에서 직접 읽는다(제품의 load_config 미사용)."""
    with open(config_module.CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def _warnings(caplog) -> list[str]:
    """config.config 로거의 WARNING 이상 메시지."""
    return [r.getMessage() for r in caplog.records if r.levelname in ("WARNING", "ERROR")]


class TestTheTableIsTheSingleSource:
    """★ 표에 임시 줄 하나를 붙이고 다른 코드를 안 건드린 채 등재·기본값·검증·보존이 전부 되는가."""

    PROBE_DEFAULT = 7

    @pytest.fixture
    def probe(self, monkeypatch):
        """표에 `_probe` 줄을 추가한다 — 정수만 받고 기본값 7. 다른 코드는 손대지 않는다."""

        def integer(value: object) -> int:
            """bool 아닌 정수만."""
            if isinstance(value, bool) or not isinstance(value, int):
                raise InvalidSetting(type(value).__name__)
            return value

        row = Setting("_probe", self.PROBE_DEFAULT, integer)
        monkeypatch.setattr(config_module, "SCHEMA", (*config_module.SCHEMA, row))
        return row

    def test_a_new_row_is_registered_in_the_defaults_and_the_fresh_file(self, probe):
        """등재: 기본값 표와 새로 만들어지는 config.json 둘 다에 임시 키가 있다."""
        assert config_module.default_config()["_probe"] == self.PROBE_DEFAULT, (
            "기본값 표가 표에서 도출되지 않는다(하드코딩된 dict가 따로 있다)"
        )
        cfg = config_module.load_config()  # 파일 생성
        assert cfg["_probe"] == self.PROBE_DEFAULT
        assert _read()["_probe"] == self.PROBE_DEFAULT, "새 파일에 임시 키가 없다"

    def test_a_missing_key_is_filled_with_the_row_default_on_startup(self, probe):
        """기본값: 키가 없는 기존 파일은 시작 경로(`update_config`)에서 기본값으로 채워진다."""
        _write(VALID)  # `_probe` 없음
        assert config_module.load_config()["_probe"] == self.PROBE_DEFAULT
        config_module.update_config()
        assert _read()["_probe"] == self.PROBE_DEFAULT, "누락 키를 기본값으로 채워 저장하지 않았다"

    def test_a_broken_value_falls_back_to_the_row_default_with_a_warning(self, probe, caplog):
        """검증: 표의 검증기에 걸린 값은 기본값으로 대체되고 경고가 남는다."""
        _write({**VALID, "_probe": "seven"})
        with caplog.at_level("WARNING", logger="config.config"):
            cfg = config_module.update_config()
        assert cfg["_probe"] == self.PROBE_DEFAULT
        assert _read()["_probe"] == self.PROBE_DEFAULT
        assert any("'_probe'" in m for m in _warnings(caplog)), _warnings(caplog)

    def test_a_valid_value_survives_update_config(self, probe):
        """보존: 유효한 값은 시작 경로를 거쳐도 그대로다 — 미등재 키를 지우는 함정이 여기서 잡힌다."""
        _write({**VALID, "_probe": 42})
        assert config_module.update_config()["_probe"] == 42
        assert _read()["_probe"] == 42, "정규화가 표의 키를 지웠다"

    def test_the_row_order_is_the_file_order(self, probe):
        """정렬도 표에서 나온다 — 임시 줄이 마지막이므로 파일에서도 마지막이다."""
        _write({"_probe": 1, **VALID})
        config_module.update_config()
        assert list(_read()) == [s.key for s in config_module.SCHEMA]

    def test_unknown_keys_are_still_dropped(self):
        """표에 없는 키는 버린다(구 reorder_config의 등재 규칙 그대로) — 등재가 보존의 전제."""
        _write({**VALID, "stray": 1})
        assert "stray" not in config_module.update_config()
        assert "stray" not in _read()


class TestBrokenValuesFallBackInsteadOfCrashing:
    """기존 키 다섯의 깨진 값 — 시작 경로(`update_config`)가 크래시하지 않고 기본값 + 경고."""

    @pytest.mark.parametrize(
        "key, broken",
        (
            ("version", "2"),  # TypeError: '<' not supported (실측)
            ("version", 1.5),  # Exception: No migration function for version 1.5 (실측)
            ("version", True),
            ("version", 0),
            ("version", None),
            ("cookies", "NID_AUT=a; NID_SES=b"),  # AttributeError: 'str' has no 'get' (실측)
            ("cookies", ["a", "b"]),
            ("cookies", None),
            ("downloadPath", ["D:/x"]),  # TypeError: _path_isdir … not list (실측)
            ("downloadPath", {"path": "D:/x"}),  # TypeError: _path_isdir … not dict (실측)
            ("downloadPath", 3),
            ("downloadPath", None),
            ("afterDownload", "hibernate"),
            ("afterDownload", 1),
            ("afterDownload", None),
            ("language", "xx_XX"),
            ("language", ["ko_KR"]),
            ("language", None),
            ("window", "not a dict"),
            ("window", {"x": "a", "y": 0, "width": 700, "height": 600}),
            ("window", None),
        ),
    )
    def test_the_key_falls_back_and_the_rest_is_preserved(self, key, broken, caplog):
        """깨진 키만 기본값이 되고 **나머지(인증 값 포함)는 그대로**이며 경고가 남는다."""
        _write({**VALID, key: broken})
        with caplog.at_level("WARNING", logger="config.config"):
            cfg = config_module.update_config()  # 크래시가 여기서 났었다
        default = config_module.default_config()[key]
        assert cfg[key] == default, (
            f"{key}={broken!r}가 기본값 {default!r}으로 대체되지 않았다: {cfg[key]!r}"
        )
        for other in VALID:
            if other != key:
                assert cfg[other] == VALID[other], f"{key}가 깨졌다고 {other}까지 바뀌었다"
        assert _read()[key] == default, "저장된 파일에 기본값이 반영되지 않았다"
        assert any(f"'{key}'" in m for m in _warnings(caplog)), (
            f"{key}의 폴백이 경고로 남지 않았다: {_warnings(caplog)}"
        )

    def test_a_broken_value_does_not_crash_the_readers(self):
        """읽기 쪽(`load_cookies`·`load_config().get`)도 검증된 형태만 본다 — 실측 크래시 두 곳."""
        _write({**VALID, "cookies": "str", "downloadPath": ["x"]})
        assert config_module.load_cookies() == {"NID_AUT": "", "NID_SES": ""}
        assert config_module.load_config()["downloadPath"] == ""

    def test_a_config_whose_top_level_is_not_an_object_starts_from_defaults(self, caplog):
        """최상위가 dict가 아닌 JSON(`[]`·문자열)은 기본값으로 뜬다 — 크래시 아님."""
        for raw in ([], "text", 3):
            _write(raw)
            with caplog.at_level("WARNING", logger="config.config"):
                assert config_module.update_config() == config_module.default_config()
            assert _read() == config_module.default_config()

    def test_a_valid_config_is_read_unchanged(self, caplog):
        """온전한 config는 값 하나 안 바뀌고 경고도 없다 — 기존 config.json 호환."""
        _write(VALID)
        with caplog.at_level("WARNING", logger="config.config"):
            assert config_module.update_config() == VALID
        assert _read() == VALID
        assert _warnings(caplog) == []


class TestCookiePolicy:
    """쿠키는 **문자열이 아닐 때만** 빈 문자열로 대체하고 내용은 검사하지 않는다(오너 결정)."""

    @pytest.mark.parametrize("weird", ("", " ", "not=a;cookie", "한글", "a" * 5000, "\n"))
    def test_string_contents_are_never_inspected(self, weird):
        """어떤 문자열이든 그대로 남는다 — 형식은 이 앱이 정하지 않는다."""
        _write({**VALID, "cookies": {"NID_AUT": weird, "NID_SES": "ses-placeholder"}})
        cfg = config_module.update_config()
        assert cfg["cookies"] == {"NID_AUT": weird, "NID_SES": "ses-placeholder"}

    def test_only_the_non_string_value_is_cleared(self, caplog):
        """값 하나가 숫자면 그 값만 비우고 다른 쿠키는 남긴다 — 통째로 지우지 않는다."""
        _write({**VALID, "cookies": {"NID_AUT": 12345, "NID_SES": "ses-placeholder"}})
        with caplog.at_level("WARNING", logger="config.config"):
            cfg = config_module.update_config()
        assert cfg["cookies"] == {"NID_AUT": "", "NID_SES": "ses-placeholder"}
        assert any("cookies.NID_AUT" in m for m in _warnings(caplog))

    def test_extra_cookie_keys_are_left_alone(self):
        """두 이름 밖의 키는 건드리지 않는다."""
        _write({**VALID, "cookies": {**COOKIES, "OTHER": "kept"}})
        assert config_module.update_config()["cookies"] == {**COOKIES, "OTHER": "kept"}

    def test_a_missing_cookie_name_is_tolerated(self):
        """이름이 하나 빠져도 나머지는 그대로이고 `load_cookies`가 빈 값으로 채운다."""
        _write({**VALID, "cookies": {"NID_AUT": "aut-placeholder"}})
        assert config_module.update_config()["cookies"] == {"NID_AUT": "aut-placeholder"}
        assert config_module.load_cookies() == {"NID_AUT": "aut-placeholder", "NID_SES": ""}

    def test_warnings_never_contain_the_value(self, caplog):
        """경고에 값을 찍지 않는다 — 인증 값이 로그에 남으면 안 된다."""
        _write({**VALID, "cookies": {"NID_AUT": ["secret-list-item"], "NID_SES": "s"}})
        with caplog.at_level("WARNING", logger="config.config"):
            config_module.update_config()
        _write({**VALID, "cookies": ["secret-list-item"]})
        with caplog.at_level("WARNING", logger="config.config"):
            config_module.update_config()
        assert _warnings(caplog), "전제: 경고가 났어야 한다"
        assert not any("secret" in m for m in _warnings(caplog)), _warnings(caplog)


class TestValidationRunsAfterMigration:
    """검증·정규화는 마이그레이션 **뒤** — 구 스키마 키가 새 검증에 걸려 버려지면 안 된다."""

    OLD_V1 = {
        "version": 1,
        "cookies": dict(COOKIES),
        "afterDownloadComplete": "shutdown",  # v1 이름 — v2에서 afterDownload로 옮긴다
        "threads": 4,  # v1 전용 키 — v2에서 지운다
    }

    def test_an_old_schema_value_is_migrated_before_it_could_be_dropped(self):
        """v1 config의 `afterDownloadComplete`가 `afterDownload`로 옮겨진 채 남는다."""
        _write(self.OLD_V1)
        cfg = config_module.update_config()
        assert cfg["afterDownload"] == "shutdown", (
            "구 스키마 키가 마이그레이션 전에 버려졌다(검증이 마이그레이션 앞에서 돈다)"
        )
        assert cfg["version"] == 2
        assert "afterDownloadComplete" not in cfg and "threads" not in cfg
        assert cfg["cookies"] == COOKIES, "마이그레이션이 인증 값을 건드렸다"
        assert _read() == cfg

    def test_a_file_without_a_version_key_is_treated_as_v1(self):
        """버전 키가 생기기 전 파일(키 부재)은 1로 보고 마이그레이션이 전부 돈다(기존 동작)."""
        _write({k: v for k, v in self.OLD_V1.items() if k != "version"})
        cfg = config_module.update_config()
        assert cfg["afterDownload"] == "shutdown"
        assert cfg["version"] == 2

    def test_missing_keys_of_an_old_file_are_filled_with_defaults(self):
        """구 파일에 없던 키(downloadPath·window 등)는 기본값으로 채워져 저장된다."""
        _write(self.OLD_V1)
        config_module.update_config()
        saved = _read()
        assert list(saved) == [s.key for s in config_module.SCHEMA]
        assert (
            saved["downloadPath"] == "" and saved["window"] == {} and saved["language"] == "en_US"
        )


class TestVersionIsCoercedBeforeMigration:
    """`version`만은 마이그레이션 **앞**에서 정수로 강제한다 — 아니면 판정 자체가 크래시한다."""

    def test_an_integral_float_version_still_migrates(self):
        """JSON을 거치며 `1.0`이 된 v1은 1로 읽혀 마이그레이션이 돈다."""
        _write({**TestValidationRunsAfterMigration.OLD_V1, "version": 1.0})
        cfg = config_module.update_config()
        assert cfg["afterDownload"] == "shutdown" and cfg["version"] == 2

    @pytest.mark.parametrize("broken", ("1", "two", 1.5, True, None, [], 0, -1))
    def test_a_non_integer_version_is_replaced_before_the_comparison(self, broken, caplog):
        """정수가 아닌 버전은 비교(`<`)에 닿기 전에 기본값(현재 버전)이 되고 경고가 남는다."""
        _write({**VALID, "version": broken})
        with caplog.at_level("WARNING", logger="config.config"):
            cfg = config_module.update_config()
        assert cfg["version"] == config_module.CONFIG_VERSION
        assert any("'version'" in m for m in _warnings(caplog))
        assert cfg["cookies"] == COOKIES

    def test_a_newer_version_passes_through(self):
        """현재보다 큰 버전(미래 앱의 파일)은 그대로 둔다 — 기존 동작(up to date 취급)."""
        _write({**VALID, "version": 99})
        assert config_module.update_config()["version"] == 99


class TestLoadConfigReturnsAValidatedCopy:
    """`load_config()`는 검증된 **복사본**을 돌려준다 — 파일은 손대지 않는다."""

    def test_load_does_not_rewrite_the_file(self):
        """읽기만으로는 파일이 바뀌지 않는다(정규화된 결과는 반환값에만)."""
        _write({**VALID, "downloadPath": 3, "stray": 1})
        before = os.stat(config_module.CONFIG_FILE).st_mtime_ns
        cfg = config_module.load_config()
        assert cfg["downloadPath"] == "" and "stray" not in cfg
        assert os.stat(config_module.CONFIG_FILE).st_mtime_ns == before
        assert _read()["downloadPath"] == 3

    def test_each_load_is_an_independent_object(self):
        """두 번 읽으면 중첩 dict까지 서로 다른 객체다."""
        _write(VALID)
        first, second = config_module.load_config(), config_module.load_config()
        first["cookies"]["NID_AUT"] = "changed"
        first["window"]["x"] = 999
        assert second["cookies"]["NID_AUT"] == "aut-placeholder"
        assert second["window"]["x"] == 10

    def test_defaults_come_out_as_fresh_copies(self):
        """표의 기본값 객체가 새어 나오지 않는다 — 반환값을 고쳐도 다음 기본값은 깨끗하다."""
        _write({"version": 2})
        cfg = config_module.load_config()
        cfg["cookies"]["NID_AUT"] = "polluted"
        cfg["window"]["x"] = 1
        assert config_module.load_config()["cookies"]["NID_AUT"] == ""
        assert config_module.default_config()["window"] == {}


class TestWindowWhitelist:
    """`parse_saved_window` — 원하는 형태만 통과한다(#253, #257에서 config 계층으로). 순수 파이썬 값."""

    def test_a_proper_record_parses(self):
        """형태가 맞는 기록은 ((x, y, width, height), 최대화)로 나온다 — Qt 타입이 아니다."""
        assert parse_saved_window(
            {"x": 1, "y": 2, "width": 700, "height": 600, "maximized": True}
        ) == ((1, 2, 700, 600), True)

    def test_integral_floats_are_accepted_and_maximized_defaults_to_false(self):
        """JSON을 거치며 700.0처럼 실수가 된 정수는 받는다 — 정수값이면 형태가 같다."""
        parsed = parse_saved_window({"x": 1.0, "y": 2.0, "width": 700.0, "height": 600.0})
        assert parsed == ((1, 2, 700, 600), False)
        assert all(type(n) is int for n in parsed[0])

    @pytest.mark.parametrize(
        "bad",
        (
            {"x": 1e1000, "y": 0, "width": 700, "height": 600},
            {"x": 0, "y": 0, "width": 700, "height": -1e1000},
            {"x": 10**30, "y": 0, "width": 700, "height": 600},
            {"x": -(2**31) - 1, "y": 0, "width": 700, "height": 600},
            {"x": 0, "y": 0, "width": 2**31, "height": 600},
            {"x": float("nan"), "y": 0, "width": 700, "height": 600},
            {"x": True, "y": 0, "width": 700, "height": 600},
            {"x": 0.5, "y": 0, "width": 700, "height": 600},
            {"x": "1", "y": 0, "width": 700, "height": 600},
            {"x": 0, "y": 0, "width": 0, "height": 600},
            {"x": 0, "y": 0, "width": 700},
            {"x": 0, "y": 0, "width": 700, "height": 600, "maximized": 1},
            [],
            "x",
            None,
            {},
        ),
    )
    def test_anything_else_is_rejected(self, bad):
        """화이트리스트 밖의 값은 전부 None — 예외가 아니라 정상 반환이다."""
        assert parse_saved_window(bad) is None, f"화이트리스트가 {bad!r}를 통과시켰다"

    def test_the_boundary_of_the_int_range_is_inclusive(self):
        """32비트 int의 양 끝값은 범위 안이다."""
        assert (
            parse_saved_window({"x": 2**31 - 1, "y": -(2**31), "width": 1, "height": 1}) is not None
        )

    def test_the_table_uses_the_same_whitelist(self, caplog):
        """표의 `window` 줄이 같은 화이트리스트를 쓴다 — 빈 dict는 기록 없음으로 통과, 깨진 기록은 {}."""
        _write({**VALID, "window": {}})
        assert config_module.update_config()["window"] == {}
        _write({**VALID, "window": {"x": 0, "y": 0, "width": 2**31, "height": 600}})
        with caplog.at_level("WARNING", logger="config.config"):
            assert config_module.update_config()["window"] == {}
        assert any("'window'" in m for m in _warnings(caplog))
