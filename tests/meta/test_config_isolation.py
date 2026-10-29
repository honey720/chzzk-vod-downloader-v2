"""전역 설정 격리 픽스처(tests/conftest.py::_isolate_real_config) 검증 (#199 · #475).

테스트 스위트가 실유저 설정 DB · 옛 config.json · 앱 데이터 폴더의 실 로그 파일을 건드리지
않는다는 불변식을 지킨다. 이 픽스처가 나중에 실수로 지워지거나 무력화되면, 이 테스트가 즉시
실패로 잡아야 한다 — 그렇지 않으면 다른 테스트들이 조용히 실유저 데이터를 오염시켜도 아무도
못 잡는다(#150이 만든 경로 찾기 로깅 테스트가 #166의 보존 로직과 만나 실제로 이렇게 됐던
사고의 재발 방지). #475부터는 설정을 열기만 해도 옛 config.json을 가져와 지우므로, 격리가
빠지면 실유저의 config.json이 사라진다.
"""

import os
import platform

import app.system.settings as settings_module


def _real_default_data_dir() -> str:
    """이 OS의 실제 앱 데이터 폴더를 제품과 따로 같은 규칙으로 다시 센다."""
    app_name = "chzzk-vod-downloader-v2"
    if platform.system() == "Windows":
        return os.path.join(os.getenv("APPDATA"), app_name)
    if platform.system() == "Darwin":
        return os.path.join(os.path.expanduser("~/Library/Application Support"), app_name)
    return os.path.join(os.getenv("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), app_name)


def test_the_real_rule_matches_the_product_default():
    """테스트가 센 실제 위치가 제품의 기본 위치 규칙과 같아야 한다 — 아니면 아래 비교가 빈 비교다.

    제품의 `_default_data_dir()` == 테스트가 다시 센 경로
    """
    assert settings_module._default_data_dir() == _real_default_data_dir()


def test_data_dir_is_redirected_away_from_real_default_location():
    """모든 테스트에서 앱 데이터 폴더가 실제 OS 기본 위치가 아닌 임시 경로여야 한다.

    data_dir() != 실제 위치 · 경로에 _isolated_config
    """
    assert settings_module.data_dir() != _real_default_data_dir()
    assert "_isolated_config" in settings_module.data_dir()


def test_database_and_logs_are_under_the_isolated_folder():
    """DB 파일과 logs/도 같은 임시 위치 아래여야 한다 — 폴더만 도는 우회 방지.

    database_path() · logs_dir() -> 실제 위치 밖 · data_dir() 아래
    """
    real = _real_default_data_dir()
    for path in (settings_module.database_path(), settings_module.logs_dir()):
        assert not path.startswith(real)
        assert path.startswith(settings_module.data_dir())


def test_writing_through_isolated_settings_does_not_touch_real_files():
    """격리된 경로로 설정을 열고 써도 실제 DB와 실제 config.json은 그대로여야 한다.

    open_settings · save_settings -> 격리 위치에 DB 생김 · 실제 DB · config.json의 존재와 수정 시각 그대로
    """
    real_files = [
        os.path.join(_real_default_data_dir(), name)
        for name in (settings_module.DATABASE_NAME, "config.json")
    ]
    before = {path: os.path.getmtime(path) if os.path.exists(path) else None for path in real_files}

    settings_module.open_settings()
    settings_module.save_settings({"language": "ko_KR"})

    assert os.path.exists(settings_module.database_path())  # 격리된 위치엔 실제로 쓰였다
    after = {path: os.path.getmtime(path) if os.path.exists(path) else None for path in real_files}
    assert after == before
