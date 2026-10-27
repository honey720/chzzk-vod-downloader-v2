"""scripts/inject_build_info.py 검증 (#195).

빌드 직전 config/config.py의 BUILD_COMMIT·IS_RELEASE_BUILD 상수를 실제
값으로 고쳐 쓰는 스크립트다. 실제 저장소 파일은 건드리지 않고, 임시
파일에 최소 재현 내용을 만들어 그 파일을 대상으로 검증한다.
"""

import subprocess

import pytest

import scripts.inject_build_info as inject_module


FAKE_CONFIG = '''\
APP_VERSION = "9.9.9"

BUILD_COMMIT = "unknown"

IS_RELEASE_BUILD = False


def get_app_version():
    return APP_VERSION
'''


@pytest.fixture
def fake_config_path(tmp_path, monkeypatch):
    """CONFIG_PATH를 임시 파일로 바꿔치기한다 — 실제 저장소 파일은 무변경."""
    path = tmp_path / "config.py"
    path.write_text(FAKE_CONFIG, encoding="utf-8")
    monkeypatch.setattr(inject_module, "CONFIG_PATH", path)
    return path


def test_inject_writes_commit_and_release_marker(fake_config_path, monkeypatch):
    """git describe가 성공하면 그 값이 BUILD_COMMIT에, --release가 IS_RELEASE_BUILD에 실린다."""
    monkeypatch.setattr(inject_module, "_git_describe", lambda repo_root: "v1.2.3-4-gabc1234")

    commit = inject_module.inject(release=True)

    assert commit == "v1.2.3-4-gabc1234"
    text = fake_config_path.read_text(encoding="utf-8")
    assert 'BUILD_COMMIT = "v1.2.3-4-gabc1234"' in text
    assert "IS_RELEASE_BUILD = True" in text
    assert 'APP_VERSION = "9.9.9"' in text  # 무관한 상수는 그대로


def test_inject_without_release_flag_keeps_marker_false(fake_config_path, monkeypatch):
    """--release 없이 부르면(로컬 빌드) 커밋만 채우고 마커는 False로 남는다."""
    monkeypatch.setattr(inject_module, "_git_describe", lambda repo_root: "abc1234")

    inject_module.inject(release=False)

    text = fake_config_path.read_text(encoding="utf-8")
    assert 'BUILD_COMMIT = "abc1234"' in text
    assert "IS_RELEASE_BUILD = False" in text


def test_inject_falls_back_to_unknown_when_git_fails(fake_config_path, monkeypatch):
    """git이 없거나 실패해도 빌드를 막지 않고 unknown으로 폴백한다."""

    def boom(*args, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(inject_module.subprocess, "run", boom)

    commit = inject_module.inject(release=True)

    assert commit == "unknown"
    text = fake_config_path.read_text(encoding="utf-8")
    assert 'BUILD_COMMIT = "unknown"' in text


def test_git_describe_timeout_falls_back_to_unknown(monkeypatch, tmp_path):
    """subprocess 타임아웃도 SubprocessError 계열이라 unknown으로 폴백한다."""

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=10)

    monkeypatch.setattr(inject_module.subprocess, "run", timeout)

    assert inject_module._git_describe(tmp_path) == "unknown"


def test_inject_raises_if_constants_not_found(tmp_path, monkeypatch):
    """상수 형식이 바뀌어 정규식이 못 찾으면 조용히 넘어가지 않고 명확히 실패한다."""
    path = tmp_path / "config.py"
    path.write_text("APP_VERSION = '9.9.9'\n", encoding="utf-8")
    monkeypatch.setattr(inject_module, "CONFIG_PATH", path)
    monkeypatch.setattr(inject_module, "_git_describe", lambda repo_root: "abc1234")

    with pytest.raises(RuntimeError, match="찾지 못했다"):
        inject_module.inject(release=True)
