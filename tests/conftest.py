"""pytest 공통 설정.

저장소 루트를 import 경로에 추가해 `content`, `download` 등 최상위 모듈을
어느 위치에서 pytest를 실행하든 import할 수 있게 한다.
"""

import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# 외부 API 응답을 흉내 내는 픽스처 파일 저장 위치
MOCK_RESPONSES_DIR = Path(__file__).resolve().parent / "fixtures" / "mock_responses"


@pytest.fixture
def load_mock_response():
    """mock_responses 디렉토리의 픽스처 파일 내용을 문자열로 읽어 오는 헬퍼를 반환한다."""

    def _load(name: str) -> str:
        return (MOCK_RESPONSES_DIR / name).read_text(encoding="utf-8")

    return _load
