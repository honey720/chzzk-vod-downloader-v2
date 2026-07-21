"""main.set_language의 en_US 폴백 동작 단위 테스트 (PR #44 리뷰 반영).

번역 로드 실패 시 en_US는 이번 실행에만 적용되고,
유저가 저장한 language 설정(config)은 보존되어야 한다.
"""

import main


class _StubTranslator:
    """QTranslator 대역: en_US 번역 파일만 로드에 성공한다."""

    def __init__(self):
        self.loaded_paths = []

    def load(self, path: str) -> bool:
        self.loaded_paths.append(path)
        return "en_US" in path


class _StubApp:
    """QApplication 대역: installTranslator 호출만 기록한다."""

    def __init__(self):
        self.installed = []

    def installTranslator(self, translator) -> None:
        self.installed.append(translator)


def _patch_environment(monkeypatch):
    """set_language가 참조하는 전역 app과 config 저장 함수를 대역으로 바꾼다."""
    stub_app = _StubApp()
    saves = []
    monkeypatch.setattr(main, "app", stub_app, raising=False)
    monkeypatch.setattr(main.config, "save_config", lambda cfg: saves.append(dict(cfg)))
    return stub_app, saves


def test_fallback_preserves_saved_language(monkeypatch):
    """저장된 언어의 번역 로드에 실패해도 config의 language 값은 보존된다."""
    stub_app, saves = _patch_environment(monkeypatch)
    app_config = {"language": "xx_XX"}  # 번역 파일이 존재하지 않는 로케일

    main.set_language(app_config, _StubTranslator())

    assert app_config["language"] == "xx_XX"  # 저장된 설정 보존
    assert saves == []  # config를 저장(덮어쓰기)하지 않는다
    assert len(stub_app.installed) == 1  # 이번 실행에는 en_US 폴백이 적용된다


def test_fallback_does_not_persist_on_first_run(monkeypatch):
    """설정에 언어가 없고 시스템 언어 로드도 실패하면, en_US를 저장하지 않고 적용만 한다."""
    stub_app, saves = _patch_environment(monkeypatch)
    monkeypatch.setattr(
        main.QLocale, "system", lambda: main.QLocale("xx_XX"), raising=False
    )
    app_config = {}

    main.set_language(app_config, _StubTranslator())

    assert "language" not in app_config
    assert saves == []
    assert len(stub_app.installed) == 1


def test_successful_load_still_saves_detected_language(monkeypatch, tmp_path):
    """(기존 동작 보존) 설정에 언어가 없고 로드에 성공하면 감지된 언어를 저장한다."""
    stub_app, saves = _patch_environment(monkeypatch)
    monkeypatch.setattr(
        main.QLocale, "system", lambda: main.QLocale("en_US"), raising=False
    )
    monkeypatch.setattr(main.os.path, "exists", lambda path: True)
    app_config = {}

    main.set_language(app_config, _StubTranslator())

    assert app_config["language"] == "en_US"
    assert saves == [{"language": "en_US"}]
    assert len(stub_app.installed) == 1