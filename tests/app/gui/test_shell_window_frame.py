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
from collections.abc import Iterator

import pytest
from PySide6.QtCore import QMargins, QRect, QSize
from PySide6.QtWidgets import QApplication, QMainWindow
from pytestqt.qtbot import QtBot

import CVDv2 as main_module
import app.theme as theme
from app.views.mainWindow import VodDownloader
from tests.app.gui.card_helpers import drop_new_top_levels, hold_style, snapshot_top_levels
from tests.qt_platform import NATIVE_SWITCH
from tests.support.settings_db import write_settings

#: 주입하는 프레임 여백(왼쪽 · 위 · 오른쪽 · 아래) — 위는 제목 표시줄.
MARGINS = QMargins(1, 31, 1, 1)
#: 이슈 #487의 네 경우 + 맞는 기록.
CASES = ("bottom-right edge", "overhang", "bigger than screen", "negative", "fits")

NATIVE = os.environ.get(NATIVE_SWITCH) == "1"


@pytest.fixture(autouse=True)
def _apply_production_qss(qapp: QApplication) -> None:
    """실제 전역 QSS·스타일을 태운다(최소폭이 QSS padding·폰트에 좌우된다). ⚠️ function scope 유지."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))  # 참조 보관 — 이중 해제 우회 (#243)
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))


@pytest.fixture(autouse=True)
def _destroy_windows() -> Iterator[None]:
    """테스트가 만든 최상위 창은 close()가 아니라 파괴한다(#248 CI)."""
    before = snapshot_top_levels()
    yield
    drop_new_top_levels(before)


def _screen_area(qapp: QApplication) -> QRect:
    """주입할 작업 영역 — 실제 주 화면의 작업 영역 그대로(고정값을 두지 않는다, #487).

    실제 화면보다 큰 영역을 주입하면 macOS가 `show()` 안에서 창을 화면 안으로 옮겨 앱의 계산이 아니라 OS의 보정을
    재게 된다(#486과 같은 까닭). offscreen은 그 플랫폼의 가상 화면이다.
    """
    return QRect(qapp.primaryScreen().availableGeometry())


def _cases(available: QRect) -> dict[str, dict]:
    """이슈 #487의 네 경우 + 맞는 기록 — 작업 영역에서 유도한다(기록 크기 = 폭 60% · 높이 절반)."""
    right, bottom = available.x() + available.width(), available.y() + available.height()
    w, h = available.width() * 6 // 10, available.height() // 2
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


def _wide_enough(area: QRect) -> QRect:
    """작업 영역이 창 최소폭(+ 여백)보다 좁으면 넓힌다 — offscreen 가상 화면(800px)에서만.

    헤더바 2줄(#495) 뒤 최소폭이 폰트에 따라 800px대라, 800px 화면에는 창이 어떻게 놓아도 들어가지 않는다
    (좁은 폭 모양은 셸 S3). 그 화면에서는 "프레임까지 안에 넣는다"를 잴 수 없으므로 최소폭에서 유도한
    폭으로 넓혀 잰다. offscreen은 OS가 창을 옮기지 않아 넓혀도 앱의 계산을 잰다(`_screen_area`의 주의는
    실제 화면 이야기다).
    """
    write_settings({})
    probe = VodDownloader()
    QApplication.processEvents()
    need = probe.minimumWidth() + MARGINS.left() + MARGINS.right()
    if NATIVE or area.width() >= need * 10 // 6 + 100:
        return area
    return QRect(area.x(), area.y(), need * 10 // 6 + 100, area.height())  # 기록 폭(60%)도 최소폭 위


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
    def _inject(self, qapp: QApplication, monkeypatch: pytest.MonkeyPatch) -> None:
        self.area = _wide_enough(_screen_area(qapp))
        monkeypatch.setattr(
            VodDownloader, "_availableGeometry", lambda _self, near=None: QRect(self.area)
        )
        monkeypatch.setattr(VodDownloader, "_frameMargins", lambda _self: QMargins(MARGINS))

    @pytest.mark.parametrize("case", CASES)
    def test_the_frame_including_rect_is_inside_the_available_area(self, case: str) -> None:
        """복원한 창의 프레임 포함 사각형(위치 + 클라이언트 크기 + 주입한 여백)이 작업 영역 안이어야 한다.

        작업 영역 = 실제 주 화면의 작업 영역 · 여백 (1, 31, 1, 1)
        -> QRect(pos(), size() + (2, 32))가 작업 영역 안
        """
        win = _open(_cases(self.area)[case])
        extra = QSize(MARGINS.left() + MARGINS.right(), MARGINS.top() + MARGINS.bottom())
        frame = QRect(win.pos(), win.size() + extra)
        assert self.area.contains(frame), (
            f"{case}: 프레임 {frame.getRect()}이 작업 영역 {self.area.getRect()} 밖이다"
        )

    def test_a_record_touching_the_bottom_ends_one_title_bar_higher(self) -> None:
        """아래 · 오른쪽 끝에 붙은 기록이면 창 위치가 제목 표시줄 · 아래 테두리만큼 올라가야 한다.

        기록 y = 작업 영역 아래 끝 − 높이 -> 복원 y = 아래 끝 − (높이 + 31 + 1)
        """
        win = _open(_cases(self.area)["bottom-right edge"])
        bottom = self.area.y() + self.area.height()
        assert win.pos().y() == bottom - (win.height() + MARGINS.top() + MARGINS.bottom())


class TestZeroMarginsAreCorrectedAfterShow:
    """표시 전에 여백이 0으로 나오는 플랫폼 — 표시 뒤 실제 `frameGeometry()`로 다시 맞춘다."""

    @pytest.fixture(autouse=True)
    def _inject(self, qapp: QApplication, monkeypatch: pytest.MonkeyPatch) -> None:
        self.area = _wide_enough(_screen_area(qapp))
        monkeypatch.setattr(
            VodDownloader, "_availableGeometry", lambda _self, near=None: QRect(self.area)
        )
        monkeypatch.setattr(VodDownloader, "_frameMargins", lambda _self: QMargins())

    def test_the_frame_after_show_is_inside_the_available_area(self) -> None:
        """표시 전 여백이 0이고 표시 뒤 프레임 여백이 있으면 frameGeometry()가 작업 영역 안이어야 한다.

        여백 주입 0 · 아래 · 오른쪽 끝에 붙은 기록 -> 표시 뒤 frameGeometry() ⊂ 작업 영역
        """
        win = _open(_cases(self.area)["bottom-right edge"])
        frame = win.frameGeometry()
        if (
            frame == win.geometry()
        ):  # 창 관리자가 없는 X 서버(CI의 xvfb)는 표시 뒤에도 여백을 주지 않는다
            pytest.skip("이 플랫폼은 표시 뒤에도 프레임 여백을 주지 않는다 — 다시 맞출 것이 없다")
        assert self.area.contains(frame), (
            f"프레임 {frame.getRect()}이 작업 영역 {self.area.getRect()} 밖이다"
        )

    def test_a_frame_that_appears_later_is_corrected_then(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """표시 직후 프레임 여백이 없다가 나중에 생기면 그때 frameGeometry()가 작업 영역 안으로 맞춰져야 한다.

        frameGeometry()가 처음 두 번은 클라이언트와 같음(창 관리자가 테두리를 늦게 붙임)
        -> 다시 보기(FRAME_RETRY_MS 간격) 뒤 frameGeometry() ⊂ 작업 영역
        """
        real = QMainWindow.frameGeometry
        calls = {"n": 0}

        def late(win: VodDownloader) -> QRect:
            calls["n"] += 1
            return win.geometry() if calls["n"] <= 2 else real(win)

        monkeypatch.setattr(VodDownloader, "frameGeometry", late)
        win = _open(_cases(self.area)["bottom-right edge"])
        if real(win) == win.geometry():  # 창 관리자가 없는 X 서버(CI의 xvfb)
            pytest.skip("이 플랫폼은 표시 뒤에도 프레임 여백을 주지 않는다 — 다시 맞출 것이 없다")
        # 다시 본 뒤의 프레임을 잰다 — OS가 show() 안에서 창을 옮기면 첫 검사부터 안일 수 있다
        qtbot.waitUntil(lambda: calls["n"] >= 3 and self.area.contains(real(win)), timeout=2000)


@pytest.mark.skipif(not NATIVE, reason=f"실제 화면에서만 — {NATIVE_SWITCH}=1")
class TestNativeScreen:
    """실제 화면 · 실제 여백 — 배율은 QT_SCALE_FACTOR로 바꿔 돌린다."""

    @pytest.mark.parametrize(
        "case", ["bottom-right edge", "overhang", "bigger than screen", "fits"]
    )
    def test_frame_geometry_after_show_is_inside_the_screen(
        self, qapp: QApplication, case: str
    ) -> None:
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
