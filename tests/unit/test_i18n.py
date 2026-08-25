"""app/i18n.py 검증 (#212) — JSON 카탈로그 조회, QTranslator/self.tr() 대체.

실제 translations/ 디렉토리는 건드리지 않는다 — tmp_path에 임시 카탈로그를
만들어 그걸 대상으로 검증한다(test_inject_build_info.py와 동일 패턴).
"""

import json

import pytest

import app.i18n as i18n


@pytest.fixture
def fake_translations_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(i18n, "TRANSLATIONS_DIR", tmp_path)
    return tmp_path


def _write_catalog(directory, language: str, catalog: dict[str, str]):
    (directory / f"{language}.json").write_text(
        json.dumps(catalog, ensure_ascii=False), encoding="utf-8"
    )


class TestHasCatalog:
    def test_true_when_file_exists(self, fake_translations_dir):
        _write_catalog(fake_translations_dir, "ko_KR", {"Hello": "안녕"})
        assert i18n.has_catalog("ko_KR") is True

    def test_false_when_missing(self, fake_translations_dir):
        assert i18n.has_catalog("fr_FR") is False


class TestResolveLanguage:
    def test_config_language_wins_when_catalog_exists(self, fake_translations_dir):
        _write_catalog(fake_translations_dir, "ko_KR", {})
        _write_catalog(fake_translations_dir, "en_US", {})

        assert i18n.resolve_language("ko_KR", system_locale="en_US") == "ko_KR"

    def test_falls_back_to_system_locale_when_config_language_has_no_catalog(
        self, fake_translations_dir
    ):
        _write_catalog(fake_translations_dir, "en_US", {})

        assert i18n.resolve_language("fr_FR", system_locale="en_US") == "en_US"

    def test_falls_back_to_default_when_nothing_matches(self, fake_translations_dir):
        # DEFAULT_LANGUAGE(en_US) 카탈로그조차 없는 극단 상황 -- 그래도 예외 없이 값을 돌려준다
        assert i18n.resolve_language("fr_FR", system_locale="de_DE") == i18n.DEFAULT_LANGUAGE

    def test_no_config_language_uses_system_locale(self, fake_translations_dir):
        _write_catalog(fake_translations_dir, "ko_KR", {})

        assert i18n.resolve_language(None, system_locale="ko_KR") == "ko_KR"

    def test_does_not_persist_anything(self, fake_translations_dir):
        """main.py의 구 set_language와 달리 config 쓰기 부작용이 없다 — 순수 조회."""
        _write_catalog(fake_translations_dir, "ko_KR", {})

        i18n.resolve_language(None, system_locale="ko_KR")

        # 카탈로그 파일 두 개(ko_KR.json)만 있어야 한다 -- 다른 파일이 안 생겼는지 확인
        assert [p.name for p in fake_translations_dir.iterdir()] == ["ko_KR.json"]


class TestTranslate:
    def test_returns_translation_when_key_found(self, fake_translations_dir):
        _write_catalog(fake_translations_dir, "ko_KR", {"Warning": "경고"})

        assert i18n.translate("Warning", "ko_KR") == "경고"

    def test_falls_back_to_key_when_key_not_in_catalog(self, fake_translations_dir):
        _write_catalog(fake_translations_dir, "ko_KR", {"Warning": "경고"})

        assert i18n.translate("Unmapped string", "ko_KR") == "Unmapped string"

    def test_falls_back_to_default_language_catalog_when_language_missing(
        self, fake_translations_dir
    ):
        _write_catalog(fake_translations_dir, i18n.DEFAULT_LANGUAGE, {"Warning": "Warning"})

        assert i18n.translate("Warning", "fr_FR") == "Warning"

    def test_returns_key_when_no_catalog_at_all(self, fake_translations_dir):
        # 카탈로그 파일이 하나도 없는 극단 상황 -- 예외 없이 원문을 돌려준다
        assert i18n.translate("Anything", "ko_KR") == "Anything"

    def test_never_raises_on_missing_catalog_or_key(self, fake_translations_dir):
        """조회 실패는 항상 원문 반환이지 예외가 아니다 — qt tr()의 미번역 폴백과 동일."""
        try:
            result = i18n.translate("Whatever", "xx_XX")
        except Exception as e:  # noqa: BLE001 -- 이 테스트의 목적이 "예외가 안 난다"이다
            pytest.fail(f"translate()가 예외를 던짐: {e}")
        assert result == "Whatever"
