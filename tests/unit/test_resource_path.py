"""main.resource_path의 경로 해석 단위 테스트 (#43).

Nuitka onefile 빌드에서는 CWD가 리소스 해제 경로와 무관하므로,
리소스 경로는 CWD가 아닌 main.py(__file__) 위치 기준으로 해석되어야 한다.
"""

import os
from pathlib import Path

import main

ROOT_DIR = Path(main.__file__).resolve().parent


def test_resource_path_is_anchored_to_main_file(monkeypatch, tmp_path):
    """CWD를 바꿔도 main.py 위치 기준으로 해석되어야 한다 (Nuitka onefile 대응)."""
    monkeypatch.chdir(tmp_path)

    result = Path(main.resource_path("translations/ko_KR.qm"))

    assert result == ROOT_DIR / "translations" / "ko_KR.qm"


def test_resource_path_finds_existing_resources():
    """저장소의 실제 번역 파일·아이콘이 존재하는 경로로 해석된다."""
    assert os.path.exists(main.resource_path("translations/en_US.qm"))
    assert os.path.exists(main.resource_path("resources/chzzk.ico"))
