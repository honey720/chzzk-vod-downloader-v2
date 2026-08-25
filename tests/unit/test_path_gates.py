"""path_gates.normalize_path 단위 테스트 (#146 ⓑ-4, #219).

절대화 자체의 계약(멱등·빈 문자열 예외·상대경로 해석)만 여기서 고정한다.
실제 배선(mainWindow가 이 함수를 판정 전에 호출하는지)은
tests/unit/test_default_download_path.py가 실물 윈도우로 검증한다.
"""

import os

from app.viewmodels.path_gates import normalize_path


class TestNormalizePath:
    def test_absolute_path_is_unchanged(self, tmp_path):
        absolute = str(tmp_path)
        assert normalize_path(absolute) == absolute

    def test_idempotent(self, tmp_path):
        absolute = str(tmp_path)
        assert normalize_path(normalize_path(absolute)) == normalize_path(absolute)

    def test_empty_string_passes_through_unchanged(self):
        """빈 문자열은 '미설정'이라는 별개 의미다 — cwd로 바뀌면 안 된다."""
        assert normalize_path("") == ""

    def test_relative_path_resolved_against_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert normalize_path("relative dir") == os.path.abspath("relative dir")
        assert os.path.isabs(normalize_path("relative dir"))

    def test_user_home_expanded(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        result = normalize_path(os.path.join("~", "downloads"))
        assert "~" not in result
        assert os.path.isabs(result)
