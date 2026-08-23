"""전역 config.json 격리 픽스처(tests/conftest.py::_isolate_real_config) 검증 (#199).

테스트 스위트가 실유저 config.json(과 CONFIG_DIR/logs의 실 로그 파일)을
건드리지 않는다는 불변식을 지킨다. 이 픽스처가 나중에 실수로 지워지거나
무력화되면, 이 테스트가 즉시 실패로 잡아야 한다 — 그렇지 않으면 다른
테스트들이 조용히 실유저 데이터를 오염시켜도 아무도 못 잡는다(#150이
만든 경로 찾기 로깅 테스트가 #166의 보존 로직과 만나 실제로 이렇게
됐던 사고의 재발 방지).
"""

import os
import platform

import config.config as config_module


def _real_default_config_dir() -> str:
    """이 OS의 실제 기본 CONFIG_DIR을 config.py와 동일한 규칙으로 재계산한다."""
    app_name = config_module.APP_NAME
    if platform.system() == "Windows":
        return os.path.join(os.getenv("APPDATA"), app_name)
    if platform.system() == "Darwin":
        return os.path.join(os.path.expanduser("~/Library/Application Support"), app_name)
    return os.path.join(os.getenv("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), app_name)


def test_config_dir_is_redirected_away_from_real_default_location():
    """모든 테스트에서 CONFIG_DIR이 실제 OS 기본 위치가 아닌 임시 경로여야 한다."""
    assert config_module.CONFIG_DIR != _real_default_config_dir()
    assert "_isolated_config" in config_module.CONFIG_DIR


def test_config_file_is_redirected_away_from_real_default_location():
    """CONFIG_FILE도 같은 임시 위치 아래에 있어야 한다 — CONFIG_DIR만 도는 우회 방지."""
    real_default_file = os.path.join(_real_default_config_dir(), "config.json")
    assert config_module.CONFIG_FILE != real_default_file
    assert config_module.CONFIG_FILE.startswith(config_module.CONFIG_DIR)


def test_writing_through_isolated_config_does_not_touch_real_file():
    """격리된 경로에 실제로 save_config를 호출해도 실 config.json은 그대로다."""
    real_file = os.path.join(_real_default_config_dir(), "config.json")
    before = os.path.exists(real_file)
    before_mtime = os.path.getmtime(real_file) if before else None

    config_module.load_config()  # CONFIG_DIR을 실제로 생성한다(save_config는 안 만든다)
    config_module.save_config({"marker": "test_writing_through_isolated_config"})

    assert os.path.exists(config_module.CONFIG_FILE)  # 격리된 위치엔 실제로 쓰였다
    after = os.path.exists(real_file)
    assert after == before  # 실 파일의 존재 여부 자체가 안 바뀐다
    if before:
        assert os.path.getmtime(real_file) == before_mtime  # 수정도 안 됐다
