"""개발 빌드/정식 릴리즈 구분 — config.get_app_version()과 커밋 판정 헬퍼 검증 (#195).

층 1(IS_RELEASE_BUILD — 필수·100% 판별)과 층 2(BUILD_COMMIT/git describe —
최선 노력)를 분리해서 검증한다. 소스 실행/빌드 실행 두 분기 모두
캐시(get_app_version.cache_clear())를 매번 비우고, 실제 git·파일시스템에
의존하지 않도록 헬퍼를 몽키패치한다.
"""

import subprocess
import tomllib
from pathlib import Path

import config.config as config_module


def _pyproject_version() -> str:
    root = Path(config_module.__file__).resolve().parent.parent
    with open(root / "pyproject.toml", "rb") as f:
        return tomllib.load(f)["project"]["version"]


def test_source_run_always_appends_dev_commit_suffix(monkeypatch):
    """소스 실행(pyproject.toml 발견)은 IS_RELEASE_BUILD와 무관하게 항상 dev 취급된다."""
    config_module.get_app_version.cache_clear()
    monkeypatch.setattr(config_module, "IS_RELEASE_BUILD", True)  # 소스 실행에선 무의미해야 한다
    monkeypatch.setattr(config_module, "_source_commit", lambda repo_root: "abc1234")

    result = config_module.get_app_version()

    assert result == f"{_pyproject_version()}+dev.abc1234"


def test_release_build_returns_clean_version(monkeypatch):
    """빌드 실행(pyproject.toml 없음) + 릴리즈 마커 True면 접미사 없는 깨끗한 버전이다."""
    config_module.get_app_version.cache_clear()
    fake_config_file = Path("/no/such/dir/config.py")  # pyproject.toml을 못 찾게 한다
    monkeypatch.setattr(config_module, "__file__", str(fake_config_file))
    monkeypatch.setattr(config_module, "IS_RELEASE_BUILD", True)

    result = config_module.get_app_version()

    assert result == config_module.APP_VERSION
    assert "+" not in result


def test_non_release_build_appends_build_commit_constant(monkeypatch):
    """빌드 실행 + 릴리즈 마커 False(주입 없음)면 BUILD_COMMIT 상수로 dev 접미사가 붙는다."""
    config_module.get_app_version.cache_clear()
    fake_config_file = Path("/no/such/dir/config.py")
    monkeypatch.setattr(config_module, "__file__", str(fake_config_file))
    monkeypatch.setattr(config_module, "IS_RELEASE_BUILD", False)
    monkeypatch.setattr(config_module, "BUILD_COMMIT", "deadbee")

    result = config_module.get_app_version()

    assert result == f"{config_module.APP_VERSION}+dev.deadbee"


def test_marker_absence_is_the_default_dev_signal(monkeypatch):
    """주입이 전혀 없으면(기본값 그대로) 곧 비정식으로 표시된다 — 층 1의 핵심 불변식."""
    config_module.get_app_version.cache_clear()
    fake_config_file = Path("/no/such/dir/config.py")
    monkeypatch.setattr(config_module, "__file__", str(fake_config_file))
    # IS_RELEASE_BUILD·BUILD_COMMIT을 건드리지 않는다 — "주입 없음"을 그대로 흉내낸다
    monkeypatch.setattr(config_module, "IS_RELEASE_BUILD", False, raising=True)
    monkeypatch.setattr(config_module, "BUILD_COMMIT", "unknown", raising=True)

    result = config_module.get_app_version()

    assert "+" in result
    assert result == f"{config_module.APP_VERSION}+dev.unknown"


def test_git_describe_success_is_used(monkeypatch, tmp_path):
    """git describe가 성공하면 그 출력을 그대로 쓴다(더티 접미사 포함)."""

    class _Result:
        stdout = "v2.9.5-3-gabc1234-dirty\n"

    monkeypatch.setattr(
        config_module.subprocess, "run", lambda *a, **k: _Result()
    )

    assert config_module._git_describe(str(tmp_path)) == "v2.9.5-3-gabc1234-dirty"


def test_git_describe_failure_returns_none(monkeypatch, tmp_path):
    """git이 없거나(FileNotFoundError) 실패하면(CalledProcessError) None을 돌려준다."""

    def boom(*args, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(config_module.subprocess, "run", boom)
    assert config_module._git_describe(str(tmp_path)) is None

    def called_process_error(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "git")

    monkeypatch.setattr(config_module.subprocess, "run", called_process_error)
    assert config_module._git_describe(str(tmp_path)) is None


def test_exported_commit_ignores_unsubstituted_placeholder(monkeypatch, tmp_path):
    """export-subst가 치환되지 않은 채(일반 clone) 남아 있으면 None을 돌려준다."""
    placeholder_file = tmp_path / "commit.txt"
    placeholder_file.write_text("$Format:%H$\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "_EXPORT_SUBST_FILE", str(placeholder_file))

    assert config_module._exported_commit() is None


def test_exported_commit_uses_substituted_hash(monkeypatch, tmp_path):
    """git archive가 실제로 치환한 커밋 해시는 짧게 잘라 돌려준다."""
    substituted_file = tmp_path / "commit.txt"
    substituted_file.write_text("a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "_EXPORT_SUBST_FILE", str(substituted_file))

    assert config_module._exported_commit() == "a1b2c3d4e"


def test_exported_commit_missing_file_returns_none(monkeypatch, tmp_path):
    """파일 자체가 없어도(구버전 clone 등) 조용히 None으로 폴백한다."""
    monkeypatch.setattr(config_module, "_EXPORT_SUBST_FILE", str(tmp_path / "missing.txt"))

    assert config_module._exported_commit() is None


def test_source_commit_prefers_git_over_export_subst(monkeypatch, tmp_path):
    """git describe가 성공하면 export-subst는 아예 시도하지 않는다."""
    monkeypatch.setattr(config_module, "_git_describe", lambda repo_root: "from-git")
    monkeypatch.setattr(config_module, "_exported_commit", lambda: "from-export")

    assert config_module._source_commit(str(tmp_path)) == "from-git"


def test_source_commit_falls_back_through_all_layers(monkeypatch, tmp_path):
    """git도 export-subst도 실패하면 최종적으로 unknown이다."""
    monkeypatch.setattr(config_module, "_git_describe", lambda repo_root: None)
    monkeypatch.setattr(config_module, "_exported_commit", lambda: None)

    assert config_module._source_commit(str(tmp_path)) == "unknown"


# ================================================================ 중복 제거 (#195 후속)
#
# git describe는 기본적으로 "가장 가까운 태그-거리-g해시"를 낸다. 그 태그가
# 현재 버전과 같으면 "2.9.5+dev.v2.9.5-6-g55d5f5b"처럼 버전이 두 번
# 찍힌다 — 아래는 그 중복을 떼는 로직을 사용자가 준 네 가지 예시 그대로
# 박제한다.


def test_dev_version_strips_matching_tag_with_v_prefix():
    """태그로부터 N커밋: "v2.9.5-6-g55d5f5b" -> "2.9.5+dev.6-g55d5f5b"."""
    assert config_module._dev_version("2.9.5", "v2.9.5-6-g55d5f5b") == "2.9.5+dev.6-g55d5f5b"


def test_dev_version_strips_matching_tag_and_keeps_dirty_suffix():
    """로컬 수정: "-dirty"는 거리·해시 뒤에 그대로 남는다."""
    assert (
        config_module._dev_version("2.9.5", "v2.9.5-6-g55d5f5b-dirty")
        == "2.9.5+dev.6-g55d5f5b-dirty"
    )


def test_dev_version_keeps_bare_hash_when_tag_not_found():
    """태그를 못 찾음(--always가 맨해시만 반환) — 겹치는 접두사가 없어 그대로 둔다."""
    assert config_module._dev_version("2.9.5", "55d5f5b") == "2.9.5+dev.55d5f5b"


def test_dev_version_keeps_unknown_as_is():
    """git 자체가 없음 — "unknown"도 접두사가 없으니 그대로 둔다."""
    assert config_module._dev_version("2.9.5", "unknown") == "2.9.5+dev.unknown"


def test_dev_version_exact_tag_commit_has_no_trailing_dot():
    """거리 0(정확히 태그 커밋)이면 describe가 태그명만 반환한다 — 빈 상세를 남기지 않는다."""
    assert config_module._dev_version("2.9.5", "v2.9.5") == "2.9.5+dev"
    assert "+dev." not in config_module._dev_version("2.9.5", "v2.9.5")


def test_dev_version_base_prefix_is_never_dropped():
    """앞의 버전 부분은 항상 기준선으로 남는다 — 해시만/unknown이어도 버전대는 알 수 있어야 한다."""
    for commit in ("55d5f5b", "unknown", "v2.9.5-6-g55d5f5b"):
        result = config_module._dev_version("2.9.5", commit)
        assert result.startswith("2.9.5+dev")


def test_strip_redundant_tag_prefix_without_v_prefix_also_matches():
    """v 접두사가 없는 태그(그냥 "2.9.5")도 중복으로 인식해 떼어낸다."""
    assert config_module._strip_redundant_tag_prefix("2.9.5", "2.9.5-6-g55d5f5b") == "6-g55d5f5b"
