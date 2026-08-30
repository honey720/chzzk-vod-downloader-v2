"""pytest 공통 설정.

저장소 루트를 import 경로에 추가해 `content`, `download` 등 최상위 모듈을
어느 위치에서 pytest를 실행하든 import할 수 있게 한다.
"""

import faulthandler
import os
import re
import sys
from pathlib import Path

import pytest

# 전역 QSS 픽스처(app.setStyle(theme.build_style()) 등)를 테스트 하나를
# 넘어 살려두면 macOS CI에서만 프로세스 종료 시점에 SIGSEGV(exit code
# 139)가 난다(#242) — Python 예외가 아니라 네이티브 크래시라 기본적으로는
# 스택이 안 남는다. faulthandler를 세션 시작부터 켜 두니 실제로 크래시
# 스택을 한 번 잡았다 — pytest 자체의 세션 종료 강제 GC
# (_pytest/unraisableexception.py::gc_collect_harder) 도중에 죽는다.
# 상시 켜 둬서 다음에 재발해도 CI 로그만으로 실마리를 잡을 수 있게 한다.
# 상세 경계·가설은 tests/unit/test_widget_theme.py의
# `_apply_dark_card_qss` 픽스처 docstring 참고.
faulthandler.enable()

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config.config as config_module  # noqa: E402 — sys.path 삽입 후에 와야 한다
import core.downloaders.hls_aes_downloader as hls_aes_module  # noqa: E402
import core.downloaders.m3u8_downloader as m3u8_module  # noqa: E402
from core.utils.paths import temp_dir_for  # noqa: E402

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


# ============ 전역 choose_temp_dir 격리 (#192) ============
# choose_temp_dir(core/utils/paths.py)은 실제로 파일을 써서(fsync 포함)
# 속도를 재는 실 디스크 I/O다 — 격리하지 않으면 M3U8Downloader·
# HlsAesDownloader를 생성하는 기존 테스트 수십 건이 매번 이 프로브를
# 타 스위트가 느려지고, tmp_path와 시스템 임시 폴더가 우연히 같은
# 볼륨이 아닌 CI 환경에서는 판정이 흔들려 임시 폴더 위치가 테스트마다
# 달라질 수 있다(재현성 상실). #192 기능 자체를 검증하는 테스트
# (tests/unit/core/test_choose_temp_dir.py)만 이 픽스처를 지역적으로
# 되돌려(monkeypatch로 실제 choose_temp_dir를 복원) 실 프로브를 태운다.
@pytest.fixture(autouse=True)
def _isolate_choose_temp_dir(monkeypatch):
    """기본은 임시 폴더를 산출물 폴더에 그대로 두는 기존 동작으로 고정한다."""

    def _default_temp_dir(output_path: str) -> str:
        return temp_dir_for(output_path)

    monkeypatch.setattr(m3u8_module, "choose_temp_dir", _default_temp_dir)
    monkeypatch.setattr(hls_aes_module, "choose_temp_dir", _default_temp_dir)


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
