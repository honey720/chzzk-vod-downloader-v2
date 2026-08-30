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
import core.downloaders.hls_aes_downloader as hls_aes_module  # noqa: E402
import core.downloaders.m3u8_downloader as m3u8_module  # noqa: E402
import main as main_module  # noqa: E402
import theme  # noqa: E402
from core.utils.paths import temp_dir_for  # noqa: E402

# 외부 API 응답을 흉내 내는 픽스처 파일 저장 위치
MOCK_RESPONSES_DIR = Path(__file__).resolve().parent / "fixtures" / "mock_responses"


# ============ 전역 카드 QSS 적용 (#240 2단계) ============
# 카드 테두리(#contentFrame)가 위젯별 setStyleSheet에서 전역 .qss로
# 옮겨가면서, 테스트가 스타일시트를 안 태우면 `contentFrame`의 지오메트리
# 게이트(TestCardInnerWidthIsUnchanged)가 실제로는 아무것도 안 보게 된다
# — 실측 확인: Fusion의 스타일 기본 프레임 두께(PM_DefaultFrameWidth)가
# 우리가 QSS에 명시한 1px 테두리와 우연히 같아서, 카드 QSS 규칙을 통째로
# 지워도 이 게이트가 계속 통과해버렸다(고장 주입으로 재현). 그래서
# 전역 .qss를 세션 시작에 한 번 실제로 적용해 게이트가 진짜 실물(프로덕션이
# 쓰는 것과 같은 스타일시트)을 재게 한다 — "테스트가 검증하는 것이 실물과
# 다르다"는 대안(스타일 없는 값으로 게이트를 다시 잡는 것)은 게이트를
# 무력화하는 셈이라 기각했다.
#
# `main.apply_theme()`을 그대로 쓰지 않는 이유: 그 함수는
# `theme.detect_color_scheme(app)`으로 OS 테마를 감지해 스킴을 정하는데,
# 오프스크린 QPA의 기본 팔레트 밝기 폴백이 이 환경에서 "light"로 나온다
# (실측 확인) — 그대로 쓰면 테스트 스위트 전체의 암묵적 전제("아무도
# set_color_scheme()을 안 부르면 DARK", theme.py 모듈 docstring이 명시)가
# 깨져 `theme.DARK[...]`를 기대하는 수십 개 어서션이 전부 LIGHT 값과
# 비교하게 된다. 그래서 스킴 감지는 건너뛰고 명시적으로 "dark"로 고정한
# 뒤 팔레트·스타일·스타일시트만 프로덕션과 같은 방식으로 적용한다.
@pytest.fixture(scope="session", autouse=True)
def _apply_global_card_qss(qapp):
    """전역 스타일시트를 세션에 한 번 적용해 카드 QSS 지오메트리 게이트가
    실제 프로덕션 스타일시트를 재게 한다."""
    theme.set_color_scheme("dark")
    qapp.setStyle(theme.build_style())
    qapp.setPalette(theme.build_palette())
    qss_path = main_module.resource_path(theme.QSS_RELATIVE_PATH)
    qapp.setStyleSheet(theme.load_stylesheet(qss_path))


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
