"""창 복원이 프레임(제목 표시줄 · 테두리)까지 작업 영역 안에 넣는지 재는 게이트 (#487).

위치는 프레임 기준(`pos()`)이고 크기는 클라이언트 기준(`size()`)이라, 프레임 여백을 세지 않으면
아래 끝에 닿는 기록에서 창 아래가 제목 표시줄 높이만큼 작업 영역 밖으로 나간다.

- offscreen: 프레임 여백을 이음새(`_frameMargins`)로 주입해 "프레임 포함 사각형이 작업 영역 안"을 잰다.
  여백이 0으로 나오는 플랫폼의 보완(표시 뒤 `frameGeometry()`로 다시 맞추기)은 offscreen이 표시 뒤에
  주는 실제 테두리 여백으로 잰다.
- 실제 화면(`CVDV2_TEST_NATIVE=1`에서만): 실제 화면 · 실제 여백으로 `show()` 뒤 `frameGeometry()`가
  `availableGeometry()` 안인지 잰다. 배율은 `QT_SCALE_FACTOR`로 바꿔 돌린다.

⚠️ 파일 이름이 `test_shell_`로 시작하는 이유는 test_shell_window_state.py와 같다(실행 순서, #243).
"""

import os

import pytest
from PySide6.QtCore import QMargins, QRect, QSize
from PySide6.QtWidgets import QApplication

import CVDv2 as main_module
import app.theme as theme
from app.views.mainWindow import VodDownloader
from tests.app.gui.card_helpers import drop_new_top_levels, hold_style, snapshot_top_levels
from tests.qt_platform import NATIVE_SWITCH
from tests.support.settings_db import write_settings

#: 테스트가 주입하는 작업 영역 — 주 화면.
AVAILABLE = QRect(0, 0, 1920, 1040)
#: 주입하는 프레임 여백(왼쪽 · 위 · 오른쪽 · 아래) — 위는 제목 표시줄.
MARGINS = QMargins(1, 31, 1, 1)
#: 기록 크기(클라이언트) — 작업 영역 높이의 절반(최소 높이)보다 작아 최소 높이로 커진다.
RECORD = QSize(700, 600)

NATIVE = os.environ.get(NATIVE_SWITCH) == "1"


@pytest.fixture(autouse=True)
def _apply_production_qss(qapp):
    """실제 전역 QSS·스타일을 태운다(최소폭이 QSS padding·폰트에 좌우된다). ⚠️ function scope 유지."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))  # 참조 보관 — 이중 해제 우회 (#243)
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))


@pytest.fixture(autouse=True)
def _destroy_windows():
    """테스트가 만든 최상위 창은 close()가 아니라 파괴한다(#248 CI)."""
    before = snapshot_top_levels()
    yield
    drop_new_top_levels(before)


def _cases(available: QRect) -> dict[str, dict]:
    """이슈 #487의 네 경우 + 맞는 기록 — 작업 영역에서 유도한다."""
    right, bottom = available.x() + available.width(), available.y() + available.height()
    w, h = RECORD.width(), RECORD.height()
    return {
        "bottom-right edge": {"x": right - w, "y": bottom - h, "width": w, "height": h},
        "overhang": {"x": right - 100, "y": bottom - 100, "width": w, "height": h},
        "bigger than screen": {
            "x": right + 80,
            "y": bottom + 60,
            "width": available.width() + 500,
            "height": available.height() + 400,
        },
        "negative": {"x": available.x() - 400, "y": available.y() - 50, "width": w, "height": h},
        "fits": {"x": available.x() + 200, "y": available.y() + 150, "width": w, "height": h},
    }


def _open(record: dict) -> VodDownloader:
    """기록을 쓰고 실제 메인 창을 만든다(복원은 생성 시점, 표시까지 생성자가 한다)."""
    write_settings({"window": {**record, "maximized": False}})
    win = VodDownloader()
    QApplication.processEvents()
    QApplication.processEvents()
    return win


class TestInjectedMargins:
    """offscreen — 프레임 여백을 주입하고 작업 영역도 주입한다."""

    @pytest.fixture(autouse=True)
    def _inject(self, monkeypatch):
        monkeypatch.setattr(
            VodDownloader, "_availableGeometry", lambda self, near=None: QRect(AVAILABLE)
        )
        monkeypatch.setattr(VodDownloader, "_frameMargins", lambda self: QMargins(MARGINS))

    @pytest.mark.parametrize("case", list(_cases(AVAILABLE)))
    def test_the_frame_including_rect_is_inside_the_available_area(self, case):
        """복원한 창의 프레임 포함 사각형(위치 + 클라이언트 크기 + 주입한 여백)이 작업 영역 안이어야 한다.

        작업 영역 (0, 0, 1920, 1040) · 여백 (1, 31, 1, 1)
        -> QRect(pos(), size() + (2, 32))가 작업 영역 안
        """
        win = _open(_cases(AVAILABLE)[case])
        extra = QSize(MARGINS.left() + MARGINS.right(), MARGINS.top() + MARGINS.bottom())
        frame = QRect(win.pos(), win.size() + extra)
        assert AVAILABLE.contains(frame), (
            f"{case}: 프레임 {frame.getRect()}이 작업 영역 {AVAILABLE.getRect()} 밖이다"
        )

    def test_a_record_touching_the_bottom_ends_one_title_bar_higher(self):
        """아래 · 오른쪽 끝에 붙은 기록이면 창 위치가 제목 표시줄 · 아래 테두리만큼 올라가야 한다.

        기록 y = 1040 − 높이 -> 복원 y = 1040 − (높이 + 31 + 1)
        """
        win = _open(_cases(AVAILABLE)["bottom-right edge"])
        bottom = AVAILABLE.y() + AVAILABLE.height()
        assert win.pos().y() == bottom - (win.height() + MARGINS.top() + MARGINS.bottom())


class TestZeroMarginsAreCorrectedAfterShow:
    """표시 전에 여백이 0으로 나오는 플랫폼 — 표시 뒤 실제 `frameGeometry()`로 다시 맞춘다."""

    @pytest.fixture(autouse=True)
    def _inject(self, monkeypatch):
        monkeypatch.setattr(
            VodDownloader, "_availableGeometry", lambda self, near=None: QRect(AVAILABLE)
        )
        monkeypatch.setattr(VodDownloader, "_frameMargins", lambda self: QMargins())

    def test_the_frame_after_show_is_inside_the_available_area(self):
        """표시 전 여백이 0이고 표시 뒤 프레임 여백이 있으면 frameGeometry()가 작업 영역 안이어야 한다.

        여백 주입 0 · 아래 · 오른쪽 끝에 붙은 기록 -> 표시 뒤 frameGeometry() ⊂ 작업 영역
        """
        win = _open(_cases(AVAILABLE)["bottom-right edge"])
        frame = win.frameGeometry()
        assert frame != win.geometry(), "전제: 이 플랫폼은 표시 뒤 프레임 여백을 준다"
        assert AVAILABLE.contains(frame), (
            f"프레임 {frame.getRect()}이 작업 영역 {AVAILABLE.getRect()} 밖이다"
        )


@pytest.mark.skipif(not NATIVE, reason=f"실제 화면에서만 — {NATIVE_SWITCH}=1")
class TestNativeScreen:
    """실제 화면 · 실제 여백 — 배율은 QT_SCALE_FACTOR로 바꿔 돌린다."""

    @pytest.mark.parametrize(
        "case", ["bottom-right edge", "overhang", "bigger than screen", "fits"]
    )
    def test_frame_geometry_after_show_is_inside_the_screen(self, qapp, case):
        """실제 화면에서 복원한 창의 frameGeometry()가 그 화면의 availableGeometry() 안이어야 한다.

        기록 = 주 화면 작업 영역에서 유도한 경우 -> show() 뒤 frameGeometry() ⊂ availableGeometry()
        """
        available = qapp.primaryScreen().availableGeometry()
        win = _open(_cases(available)[case])
        frame = win.frameGeometry()
        screen = (win.screen() or qapp.primaryScreen()).availableGeometry()
        assert screen.contains(frame), (
            f"{case}: 프레임 {frame.getRect()}이 작업 영역 {screen.getRect()} 밖이다"
        )
