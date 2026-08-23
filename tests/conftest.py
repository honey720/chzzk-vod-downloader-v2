"""pytest 공통 설정.

저장소 루트를 import 경로에 추가해 `content`, `download` 등 최상위 모듈을
어느 위치에서 pytest를 실행하든 import할 수 있게 한다.
"""

import os
import re
import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config.config as config_module  # noqa: E402 — sys.path 삽입 후에 와야 한다

# 외부 API 응답을 흉내 내는 픽스처 파일 저장 위치
MOCK_RESPONSES_DIR = Path(__file__).resolve().parent / "fixtures" / "mock_responses"


@pytest.fixture
def load_mock_response():
    """mock_responses 디렉토리의 픽스처 파일 내용을 문자열로 읽어 오는 헬퍼를 반환한다."""

    def _load(name: str) -> str:
        return (MOCK_RESPONSES_DIR / name).read_text(encoding="utf-8")

    return _load


# ============ 전역 config.json 격리 (#199) ============
# 개별 테스트가 CONFIG_DIR/CONFIG_FILE을 각자 격리하지 않으면
# application.mainWindow.VodDownloader 등 앱 계층을 실배선으로 태우는
# 테스트가 실유저 config.json을 조용히 덮어쓴다 — #150이 만든 경로 찾기
# 로깅 테스트가 그 배선을 탔는데, #166이 같은 지점("입력 확정"·"창 닫기")에
# 실제 저장까지 발동하는 보존 로직을 얹으면서 오염이 시작됐다. #166은
# 자기가 새로 만든 테스트만 메모리로 격리했고, 먼저 있던 테스트는 새는
# 지점으로 남았다(tests/unit/test_path_action_logging.py::window). 개별
# 수정 대신 전역 autouse로 막아, 다음에 또 새 진입점이 생겨도 기본이
# 안전하게 유지되게 한다. 개별 테스트가 이미 갖고 있는 자체 격리
# (config_store 등, 저장 이력 검증 같은 자기 목적이 있다)는 그대로 둔다 —
# 같은 값으로 다시 monkeypatch되어도 무해하다.
@pytest.fixture(autouse=True)
def _isolate_real_config(tmp_path, monkeypatch):
    """모든 테스트에서 config.json 읽기·쓰기(및 CONFIG_DIR/logs 로그 파일)를 임시 위치로 돌린다."""
    isolated_dir = tmp_path / "_isolated_config"
    monkeypatch.setattr(config_module, "CONFIG_DIR", str(isolated_dir))
    monkeypatch.setattr(config_module, "CONFIG_FILE", str(isolated_dir / "config.json"))


# ============ 실패 GUI 테스트 스크린샷 (#154) ============
# 실패한 테스트의 최상위 위젯을 grab해 PNG로 남긴다. 기본은 실패 시에만,
# CVDV2_SHOT_ALL=1이면 성공 테스트도 찍는다(전체 갤러리용).
# offscreen에서도 실픽셀이 렌더됨은 실측 확인 — 단 폰트는 QT_QPA_FONTDIR 필요.
SHOT_DIR = Path(os.environ.get("CVDV2_SHOT_DIR", "test-screenshots"))
SHOT_ALL = os.environ.get("CVDV2_SHOT_ALL") == "1"


def _screenshot_slug(nodeid: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", nodeid)[:120]


def _capture_screenshots(nodeid: str, outcome: str) -> list[str]:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        return []
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    saved = []
    for n, w in enumerate(app.topLevelWidgets()):
        if not w.isVisible() or w.width() == 0 or w.height() == 0:
            continue
        name = f"{sys.platform}-{outcome}-{_screenshot_slug(nodeid)}-{n}.png"
        if w.grab().save(str(SHOT_DIR / name), "PNG"):
            saved.append(name)
    return saved


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if rep.when != "call":
        return
    if rep.failed or SHOT_ALL:
        saved = _capture_screenshots(item.nodeid, "FAIL" if rep.failed else "PASS")
        if saved:
            rep.sections.append(("screenshots", "\n".join(saved)))
