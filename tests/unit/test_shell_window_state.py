"""창 크기·위치·최대화 상태 기억 게이트 (#253).

- 저장은 창을 닫을 때(`closeEvent` → `_rememberWindowState`) 한 번, 복원은 창 생성 시
  (`_restoreWindowState`)다 — 실제 배선을 탄다(핸들러 직접 호출 없음).
- 복원값은 그대로 쓰지 않는다: 기록된 위치가 놓인 화면의 작업 영역(`_availableGeometry`)으로
  크기를 줄이고 위치를 안으로 민다. 모니터가 바뀌어 창이 화면 밖으로 나가는 결함 방지.
- 키가 없는 config(업데이트 전 유저)·깨진 값은 첫 실행으로 취급해 초기 크기 규칙으로 뜬다.
- `DEFAULT_CONFIG` 등재 — `reorder_config`가 미등재 키를 지우는 함정(#159 전례).

config는 conftest의 autouse 픽스처가 테스트마다 임시 폴더로 격리한다. 화면은 이음새로
주입한다(절대 px는 주입한 화면과 그 파생값뿐).

⚠️ 파일 이름이 `test_shell_`로 시작하는 이유 — 실행 순서다. Windows offscreen에서
`test_theme.py`·`test_title_hover.py`·`test_widget.py`·`test_widget_theme.py`가 먼저 돈 뒤에
메인 창(`VodDownloader`)을 만들고 파괴하면 teardown의 `gc.collect()`에서 힙 손상
(0xc0000374, #243 계열)이 결정적으로 난다 — main의 `test_shell_layout.py`를 그 뒤로 옮겨도
같다(실측). 이 파일이 `test_t*`보다 앞에 놓이면 나지 않는다. 원인 수정은 #243 몫이다.
"""

import json

import pytest
from PySide6.QtCore import QPoint, QRect
from PySide6.QtWidgets import QApplication

import config.config as config_module
import main as main_module
import theme
from application.mainWindow import VodDownloader, clamp_to_available, parse_saved_window
from tests.unit.card_helpers import drop_new_top_levels, hold_style, resize_to, snapshot_top_levels

#: 테스트가 주입하는 작업 영역 — 주 화면.
AVAILABLE = QRect(0, 0, 1920, 1040)


@pytest.fixture(autouse=True)
def _apply_production_qss(qapp):
    """실제 전역 QSS·스타일을 태운다(최소폭이 QSS padding·폰트에 좌우된다). ⚠️ function scope 유지."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))  # 참조 보관 — 이중 해제 우회 (#243)
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))


@pytest.fixture(autouse=True)
def _destroy_windows():
    """테스트가 만든 최상위 창은 close()가 아니라 파괴한다 — 숨은 채 남으면 다음 setStyle이 죽는다(#248 CI)."""
    before = snapshot_top_levels()
    yield
    drop_new_top_levels(before)


@pytest.fixture(autouse=True)
def _fake_available(monkeypatch):
    """모든 창이 같은 작업 영역을 본다 — 기록 위치가 어느 점이든 주 화면 하나."""
    monkeypatch.setattr(
        VodDownloader, "_availableGeometry", lambda self, near=None: QRect(AVAILABLE)
    )


def _initial_width() -> int:
    """첫 실행 초기 폭 — 테스트가 토큰·주입 화면에서 직접 계산한다."""
    return min(int(AVAILABLE.width() * 0.45), theme.METRICS["initialWidthMax"])


def _saved_window() -> dict:
    """config.json에 기록된 창 상태를 파일에서 직접 읽는다(제품의 load_config 미사용)."""
    with open(config_module.CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f).get("window")


def _write_window(state) -> None:
    """config.json의 window 키를 직접 쓴다 — 기록이 있는 상태·깨진 상태·키 부재를 만든다."""
    config_module.load_config()  # 파일이 없으면 기본값으로 만든다
    with open(config_module.CONFIG_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    if state is None:
        cfg.pop("window", None)
    else:
        cfg["window"] = state
    with open(config_module.CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f)


def _open() -> VodDownloader:
    """실제 메인 창을 만들고 대기 중인 이벤트를 소화한다(복원은 생성 시점에 일어난다)."""
    win = VodDownloader()
    QApplication.processEvents()
    QApplication.processEvents()
    return win


def _in_available(win: VodDownloader) -> bool:
    """창(프레임 위치 + 클라이언트 크기)이 주입한 작업 영역 안에 완전히 들어가는가."""
    rect = QRect(win.pos(), win.size())
    return AVAILABLE.contains(rect)


class TestRoundTrip:
    def test_close_saves_and_the_next_window_restores_the_same_size_and_position(self):
        """실제 배선: 창을 옮기고 줄인 뒤 닫으면 기록되고, 다음 창이 같은 값으로 뜬다."""
        first = _open()
        resize_to(first, _initial_width() - 100)  # 초기 규칙과 다른 값이어야 복원을 가를 수 있다
        first.resize(first.width(), 650)
        first.move(QPoint(120, 90))
        QApplication.processEvents()
        expected = (first.pos().toTuple(), first.size().toTuple())
        assert first.close(), "closeEvent가 닫기를 거부했다"
        QApplication.processEvents()

        saved = _saved_window()
        assert saved == {
            "x": 120,
            "y": 90,
            "width": expected[1][0],
            "height": 650,
            "maximized": False,
        }, saved

        second = _open()
        assert (second.pos().toTuple(), second.size().toTuple()) == expected, (
            f"복원값 {second.pos().toTuple()} {second.size().toTuple()} ≠ 기록값 {expected}"
        )

    def test_the_key_survives_update_config(self):
        """기록된 키는 시작 시 `update_config()`(reorder_config)를 거쳐도 남는다 — DEFAULT_CONFIG 등재의 이유."""
        first = _open()
        first.close()
        QApplication.processEvents()
        assert _saved_window() is not None
        config_module.update_config()
        assert _saved_window() is not None, (
            "reorder_config가 window 키를 지웠다 — DEFAULT_CONFIG에 등재되지 않았다"
        )
        assert "window" in config_module.DEFAULT_CONFIG

    def test_unchanged_state_does_not_rewrite_the_file(self):
        """같은 값이면 다시 쓰지 않는다 — 닫을 때마다 파일이 바뀌는 일이 없다."""
        first = _open()
        first.close()
        QApplication.processEvents()
        import os

        stamp = os.stat(config_module.CONFIG_FILE).st_mtime_ns
        second = _open()
        second.close()
        QApplication.processEvents()
        assert os.stat(config_module.CONFIG_FILE).st_mtime_ns == stamp


class TestRestoreIsClampedToTheCurrentScreen:
    def test_a_saved_rect_bigger_than_the_screen_shrinks_and_moves_inside(self):
        """모니터를 바꾼 상황: 기록이 작업 영역보다 크고 밖에 있으면 안으로 들어온다."""
        _write_window({"x": 3000, "y": 2000, "width": 2500, "height": 1500, "maximized": False})
        win = _open()
        assert _in_available(win), (
            f"복원된 창 {win.pos().toTuple()} {win.size().toTuple()}이 작업 영역 {AVAILABLE} 밖이다"
        )
        assert win.size().toTuple() == (AVAILABLE.width(), AVAILABLE.height())

    def test_a_partly_offscreen_position_is_pushed_back_in(self):
        """크기는 맞는데 오른쪽·아래로 걸쳐 있으면 위치만 민다 — 크기는 그대로."""
        _write_window({"x": 1800, "y": 900, "width": 700, "height": 600, "maximized": False})
        win = _open()
        assert win.size().toTuple() == (700, 600), "크기가 바뀔 이유가 없다"
        assert win.pos().toTuple() == (AVAILABLE.width() - 700, AVAILABLE.height() - 600)
        assert _in_available(win)

    def test_a_negative_position_is_pushed_to_the_edge(self):
        """왼쪽·위로 나간 기록은 작업 영역 가장자리(0, 0)로 민다."""
        _write_window({"x": -400, "y": -50, "width": 700, "height": 600, "maximized": False})
        win = _open()
        assert win.pos().toTuple() == (0, 0)

    def test_a_saved_rect_that_fits_is_left_alone(self):
        """작업 영역 안에 있는 기록은 그대로 되살린다."""
        _write_window({"x": 200, "y": 150, "width": 700, "height": 600, "maximized": False})
        win = _open()
        assert (win.pos().toTuple(), win.size().toTuple()) == ((200, 150), (700, 600))


class TestClampFunction:
    """순수 함수 `clamp_to_available` — 창 없이 사각형만으로 잰다."""

    def test_fits_unchanged(self):
        """안에 들어가는 사각형은 바뀌지 않는다."""
        assert clamp_to_available(QRect(10, 20, 300, 200), QRect(0, 0, 800, 600)) == QRect(
            10, 20, 300, 200
        )

    def test_too_big_becomes_the_available_rect(self):
        """작업 영역보다 큰 사각형은 작업 영역 그 자체가 된다."""
        assert clamp_to_available(QRect(50, 50, 900, 700), QRect(0, 0, 800, 600)) == QRect(
            0, 0, 800, 600
        )

    def test_overhang_is_pushed_in_and_negative_origin_is_pushed_to_the_edge(self):
        """오른쪽·아래로 걸치면 안으로 밀고, 음수 원점은 가장자리로 민다."""
        assert clamp_to_available(QRect(700, 500, 300, 200), QRect(0, 0, 800, 600)) == QRect(
            500, 400, 300, 200
        )
        assert clamp_to_available(QRect(-100, -5, 300, 200), QRect(0, 0, 800, 600)) == QRect(
            0, 0, 300, 200
        )

    def test_respects_a_screen_that_does_not_start_at_the_origin(self):
        """두 번째 모니터처럼 작업 영역이 (1920, 0)에서 시작해도 그 안으로 민다."""
        available = QRect(1920, 0, 1280, 700)
        assert clamp_to_available(QRect(100, 100, 400, 300), available) == QRect(
            1920, 100, 400, 300
        )
        assert clamp_to_available(QRect(3100, 600, 400, 300), available) == QRect(
            2800, 400, 400, 300
        )


class TestFirstRunAndOldConfigs:
    def test_a_config_without_the_key_opens_at_the_initial_rule(self):
        """업데이트 전 유저의 config(키 없음): 초기 크기 규칙으로 뜨고 에러가 없다."""
        _write_window(None)
        win = _open()
        assert win.width() == _initial_width()
        assert win.height() == AVAILABLE.height() // 2

    def test_an_empty_record_opens_at_the_initial_rule(self):
        """DEFAULT_CONFIG의 기본값({}) — 파일이 새로 만들어진 첫 실행."""
        _write_window({})
        win = _open()
        assert win.width() == _initial_width()

    @pytest.mark.parametrize(
        "broken",
        (
            {"x": "a", "y": 0, "width": 700, "height": 600},
            {"x": 0, "y": 0, "width": 0, "height": 600},
            {"x": 0, "y": 0, "width": -5, "height": 600},
            {"width": 700},
            "not a dict",
            None,
            {"x": 1e1000, "y": 0, "width": 700, "height": 600},  # JSON 1e1000 → 무한대
            {"x": 0, "y": 0, "width": 1e1000, "height": 600},
            {"x": 10**30, "y": 0, "width": 700, "height": 600},  # C++ int 범위 밖의 거대 정수
            {"x": 0, "y": 0, "width": 2**31, "height": 600},
            {"x": float("nan"), "y": 0, "width": 700, "height": 600},
            {"x": 0, "y": 0, "width": float("nan"), "height": 600},
            {
                "x": True,
                "y": 0,
                "width": 700,
                "height": 600,
            },  # bool은 int의 하위 타입이지만 좌표가 아니다
            {"x": 0.5, "y": 0, "width": 700, "height": 600},  # 정수값이 아닌 float
            {"x": 0, "y": 0, "width": 700, "height": 600, "maximized": "yes"},
        ),
    )
    def test_a_broken_record_falls_back_to_the_initial_rule(self, broken):
        """깨진 기록(문자열·0/음수·필드 누락·비-dict·null·무한대·거대 정수·NaN·bool·비정수 float·비-bool 최대화)은 첫 실행으로 뜬다."""
        _write_window(broken)
        win = _open()
        assert win.width() == _initial_width(), (
            f"깨진 기록 {broken!r}에서 초기 규칙으로 뜨지 않았다: {win.width()}"
        )


class TestWhitelist:
    """`parse_saved_window` — 원하는 형태만 통과한다. config.json은 손으로 고칠 수 있는 파일이라 깨진 값은 정상 시나리오다."""

    def test_a_proper_record_parses(self):
        """형태가 맞는 기록은 (사각형, 최대화)로 나온다."""
        assert parse_saved_window(
            {"x": 1, "y": 2, "width": 700, "height": 600, "maximized": True}
        ) == (
            QRect(1, 2, 700, 600),
            True,
        )

    def test_integral_floats_are_accepted_and_maximized_defaults_to_false(self):
        """JSON을 거치며 700.0처럼 실수가 된 정수는 받는다 — 정수값이면 형태가 같다."""
        assert parse_saved_window({"x": 1.0, "y": 2.0, "width": 700.0, "height": 600.0}) == (
            QRect(1, 2, 700, 600),
            False,
        )

    @pytest.mark.parametrize(
        "bad",
        (
            {"x": 1e1000, "y": 0, "width": 700, "height": 600},
            {"x": 0, "y": 0, "width": 700, "height": -1e1000},
            {"x": 10**30, "y": 0, "width": 700, "height": 600},
            {"x": -(2**31) - 1, "y": 0, "width": 700, "height": 600},
            {"x": 0, "y": 0, "width": 2**31, "height": 600},
            {"x": float("nan"), "y": 0, "width": 700, "height": 600},
            {"x": True, "y": 0, "width": 700, "height": 600},
            {"x": 0.5, "y": 0, "width": 700, "height": 600},
            {"x": "1", "y": 0, "width": 700, "height": 600},
            {"x": 0, "y": 0, "width": 0, "height": 600},
            {"x": 0, "y": 0, "width": 700},
            {"x": 0, "y": 0, "width": 700, "height": 600, "maximized": 1},
            [],
            "x",
            None,
            {},
        ),
    )
    def test_anything_else_is_rejected(self, bad):
        """화이트리스트 밖의 값은 전부 None — 예외가 아니라 정상 반환이다."""
        assert parse_saved_window(bad) is None, f"화이트리스트가 {bad!r}를 통과시켰다"

    def test_the_boundary_of_the_qt_int_range_is_inclusive(self):
        """C++ int의 양 끝값은 범위 안이다."""
        assert (
            parse_saved_window({"x": 2**31 - 1, "y": -(2**31), "width": 1, "height": 1}) is not None
        )


class TestRestoreClampsAfterTheMinimumSizeIsApplied:
    """기록 크기가 최소 크기보다 작으면 Qt가 resize()에서 키운다 — 위치는 그 **뒤**의 실제 크기로 정해야 한다.

    최소 크기는 이음새(`_contentMinimumWidth`, 작업 영역 높이 × 0.5)로 키운다 — DPI·글꼴·번역이
    바뀌어 최소가 기록보다 커진 상황. 절대 px 대신 작업 영역에서 유도한다.
    """

    def test_a_small_record_at_the_bottom_right_ends_inside_after_growing(self, monkeypatch):
        """오른쪽·아래 끝에 붙은 작은 기록이 최소 크기로 커져도 작업 영역 안에 남는다."""
        grown = AVAILABLE.width() // 2  # 기록 폭보다 훨씬 큰 최소폭
        monkeypatch.setattr(VodDownloader, "_contentMinimumWidth", lambda self: grown)
        small = (120, 100)
        _write_window(
            {
                "x": AVAILABLE.width() - small[0],
                "y": AVAILABLE.height() - small[1],
                "width": small[0],
                "height": small[1],
                "maximized": False,
            }
        )
        win = _open()
        assert win.width() == grown and win.height() == AVAILABLE.height() // 2, (
            "전제: 최소 크기가 기록을 키웠어야 한다"
        )
        assert win.pos().toTuple() == (
            AVAILABLE.width() - grown,
            AVAILABLE.height() - AVAILABLE.height() // 2,
        ), (
            f"커진 크기로 위치를 다시 잡지 않아 창이 걸친다: {win.pos().toTuple()} {win.size().toTuple()}"
        )
        assert _in_available(win)

    def test_a_small_record_that_fits_after_growing_keeps_its_position(self, monkeypatch):
        """커진 뒤에도 자리가 있으면 기록 위치를 옮기지 않는다."""
        grown = AVAILABLE.width() // 2
        monkeypatch.setattr(VodDownloader, "_contentMinimumWidth", lambda self: grown)
        _write_window({"x": 100, "y": 80, "width": 120, "height": 100, "maximized": False})
        win = _open()
        assert win.size().toTuple() == (grown, AVAILABLE.height() // 2)
        assert win.pos().toTuple() == (100, 80), "자리가 있으면 위치는 그대로다"


class TestMaximized:
    def test_maximized_state_is_saved_with_the_normal_geometry_and_restored(self):
        """최대화한 채 닫으면 플래그와 보통 크기가 함께 기록되고, 다음 창은 최대화로 뜬다."""
        first = _open()
        first.resize(first.width(), 600)
        first.move(QPoint(100, 80))
        QApplication.processEvents()
        normal = (first.pos().toTuple(), first.size().toTuple())
        first.showMaximized()
        QApplication.processEvents()
        QApplication.processEvents()
        if not first.isMaximized():
            pytest.skip("이 QPA는 최대화 상태를 보고하지 않는다")
        first.close()
        QApplication.processEvents()
        saved = _saved_window()
        assert saved["maximized"] is True
        assert (saved["width"], saved["height"]) == normal[1], (
            "최대화 크기가 아니라 해제 시 돌아갈 보통 크기를 기록해야 한다"
        )

        second = _open()
        assert second.isMaximized(), "기록이 최대화인데 보통 창으로 떴다"
        second.showNormal()
        QApplication.processEvents()
        assert second.size().toTuple() == normal[1], (
            "최대화를 풀면 기록된 보통 크기로 돌아와야 한다"
        )
