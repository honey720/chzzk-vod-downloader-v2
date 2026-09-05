"""실패 카드의 재시도 = 대기로 되돌리기 게이트 (#261) — 시작은 유저가 따로 누른다.

재시도(↻)가 즉시 다시 받기 시작하면 실패 원인을 고칠 틈이 없다(쿠키 만료: 같은 쿠키로 바로
또 실패). 이 파일이 고정하는 것:

1. 재시도 → 카드는 대기(`DownloadState.WAITING` — app 계층 `ContentItem`의 표시 상태)가 되고
   **다운로드가 시작되지 않는다**(핸들이 생기지 않는다, `DownloadViewModel.start` 미호출).
2. 되돌린 뒤 전역 다운로드 버튼을 누르면 **기존 시작 경로**를 탄다 — 재시도 전용 경로가 없다.
3. 전역 버튼은 **되돌린 카드는 집어 가고, 실패인 채인 카드는 집어 가지 않는다** — 정본의
   "실패 카드 자동 포함 반대"와의 경계다.
4. 조작 표면이 상태를 따라온다(§3.4): ↻가 사라지고 대기의 조작(pill·제목 편집·경로 변경)이
   나온다 — 가시성·호버 강조·커서·툴팁.
5. 대기로 되돌릴 때 후처리 실패로 보존된 세그먼트(#185)가 지워지지 않는다.

core `DownloadState`의 `FAILED → WAITING` 불허(#135)는 tests/unit/core/test_download_task_model.py가
그대로 지킨다 — 여기서는 그 계약을 **건드리지 않았음**을 층 분리로 증명한다(실패한 core
모델은 FAILED로 끝나고, 되돌린 카드가 시작될 때 새 모델이 WAITING부터 만들어진다).

실제 배선을 탄다: 카드의 ↻ 클릭 → 위젯 시그널 → 뷰 릴레이 → `VodDownloader.onCardRetry`,
전역 버튼 클릭 → `onDownloadPause` → `downloadItem` → `downloadRequested` → `startDownload`.
`DownloadViewModel.start`만 대역으로 바꿔 실제 네트워크·엔진은 돌리지 않는다.
config는 conftest가 격리한다. 호버는 실제 커서를 옮기지 않고 `QWindow` 합성 이벤트로 보낸다.
"""

import os

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import main as main_module
import theme
from app.views import mainWindow as mw_mod
from app.views.mainWindow import VodDownloader
from app.viewmodels.data import ContentItem
from core.models.download_state import DownloadState
from core.utils.paths import build_output_path, temp_dir_for
from tests.unit.card_helpers import (
    drop_new_top_levels,
    hold_style,
    resize_to,
    shown,
    snapshot_top_levels,
)


@pytest.fixture(autouse=True)
def _apply_production_qss(qapp):
    """실제 전역 QSS·스타일(호버 규칙 포함)을 태운다. ⚠️ function scope 유지."""
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


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """카드의 크기 조회·메타데이터 조회가 네트워크를 타지 않게 막는다."""

    class _FailingSession:
        def head(self, *a, **k):
            raise RuntimeError("network disabled in tests")

        def get(self, *a, **k):
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("app.widgets.widget.get_thread_session", lambda: _FailingSession())


@pytest.fixture
def started(monkeypatch):
    """`DownloadViewModel.start`를 기록 대역으로 — 핸들을 만들지 않고 호출만 남긴다.

    isDownloading()은 그대로 둔다(핸들이 안 생겼으니 False) — 시작 경로가 실제로 여기까지
    왔는지를 이 목록이 말한다.
    """
    calls: list[ContentItem] = []
    monkeypatch.setattr(mw_mod.DownloadViewModel, "start", lambda self, item: calls.append(item))
    # 시작 전 경로 검사의 안내 모달이 offscreen 이벤트 루프를 매달지 않게 기록으로 대체한다
    monkeypatch.setattr(
        mw_mod.QMessageBox, "warning", lambda *a, **k: calls.append(("warning", a[2]))
    )
    return calls


def _pump():
    for _ in range(3):
        QApplication.processEvents()


def _make_item(download_path: str) -> ContentItem:
    """대기 카드 하나 — 해상도 둘, 저장 경로는 실존하는 임시 폴더(시작 전 쓰기 검사 통과)."""
    item = ContentItem(
        "https://chzzk.naver.com/video/1",
        {
            "title": "제목",
            "category": "",
            "channelName": "채널",
            "createdDate": "",
            "duration": 3600,
        },
        [["1080", "u1"], ["720", "u2"]],
        None,
        "",
        download_path,
        "video",
        None,
    )
    item.total_size = "595.34 MB"
    return item


def open_window_with_a_failed_card(tmp_path):
    """실제 메인 창에 실패 카드 한 장 — 대기로 넣어 pill이 만들어진 뒤 실패로 바꾼다(제품 순서)."""
    win = VodDownloader()
    win.resize(900, win.height())
    win.show()
    QTest.qWaitForWindowExposed(win)
    item = _make_item(str(tmp_path))
    win.contentManager.model.addItem(item)
    _pump()
    widget = win.listView.widgetFor(item)
    assert widget is not None and widget.buttons, "전제: 카드와 해상도 pill이 만들어져야 한다"
    widget.setresolutionUrlSize("1080", "u1", 0, widget.buttons[0])  # 유저가 해상도를 고른 상태
    item.download_progress = 37
    item.stateMessage = "Failed to save file\nDetail line"
    item.downloadState = DownloadState.FAILED
    win.contentManager.model.notifyChanged(item)
    _pump()
    assert widget.retryButton.isVisible(), "전제: 실패 카드에는 ↻가 보여야 한다"
    return win, item, widget


def click_retry(widget):
    """카드의 ↻를 실제로 누른다 — 위젯 시그널 → 뷰 릴레이 → 메인 창 핸들러."""
    widget.retryButton.click()
    _pump()


def hover(qtbot, window, target, on: bool) -> None:
    """`target` 위(on) 또는 창의 빈 바닥(off)으로 합성 이동 — 실제 커서는 옮기지 않는다."""
    if on:
        pos = target.mapTo(window, target.rect().center())
    else:
        pos = QPoint(window.width() - 2, window.height() - 2)
    QTest.mouseMove(window.windowHandle(), pos + QPoint(1, 1))
    QTest.mouseMove(window.windowHandle(), pos)
    qtbot.waitUntil(lambda: target.underMouse() == on, timeout=2000)


def highlights_on_hover(qtbot, window, target) -> bool:
    """호버 전후 렌더가 다른가 — `shown()`으로 가시성을 먼저 단언한다."""
    hover(qtbot, window, target, on=False)
    shown(target)
    idle = target.grab().toImage()
    hover(qtbot, window, target, on=True)
    hovered = target.grab().toImage()
    hover(qtbot, window, target, on=False)
    return hovered != idle


class TestRetryOnlyReturnsToWaiting:
    def test_retry_makes_the_card_waiting_and_starts_nothing(self, tmp_path, started):
        """★ ↻ → 대기. `DownloadViewModel.start`가 불리지 않고 핸들도 없다."""
        win, item, widget = open_window_with_a_failed_card(tmp_path)
        click_retry(widget)
        assert item.downloadState is DownloadState.WAITING
        assert item.stateMessage == "" and item.download_progress == 0
        assert started == [], f"재시도가 다운로드를 시작했다: {started}"
        assert not win.downloadViewModel.isDownloading(), "재시도 뒤 다운로드 핸들이 생겼다"
        assert win.downloadButton.text() != win.tr("Pause"), "전역 버튼이 진행 중 표기로 바뀌었다"

    def test_the_global_button_then_takes_the_reverted_card_through_the_normal_path(
        self, tmp_path, started
    ):
        """되돌린 뒤 전역 다운로드 버튼 → 기존 시작 경로(`startDownload`)가 그 카드로 불린다."""
        win, item, widget = open_window_with_a_failed_card(tmp_path)
        click_retry(widget)
        win.downloadButton.click()
        _pump()
        assert started == [item], f"전역 버튼이 되돌린 카드를 집어 가지 않았다: {started}"
        assert item.output_path, "기존 경로라면 산출물 경로가 조립돼 있어야 한다"
        assert os.path.dirname(item.output_path) == str(tmp_path)

    def test_a_card_left_failed_is_not_taken_by_the_global_button(self, tmp_path, started):
        """실패인 채인 카드는 전역 버튼이 집어 가지 않는다 — 자동 포함 반대(정본)의 경계."""
        win, item, widget = open_window_with_a_failed_card(tmp_path)
        win.downloadButton.click()
        _pump()
        assert item.downloadState is DownloadState.FAILED
        assert item not in started
        assert any(isinstance(c, tuple) and c[0] == "warning" for c in started), (
            "대기 카드가 없으면 안내 모달이 떠야 한다(기존 동작)"
        )

    def test_retry_does_not_touch_preserved_segments(self, tmp_path, started):
        """후처리 실패로 보존된 세그먼트 폴더(#185)는 대기로 되돌려도 그대로 남는다."""
        win, item, widget = open_window_with_a_failed_card(tmp_path)
        item.output_path = build_output_path(item.download_path, item.title, "1080")
        preserved = temp_dir_for(item.output_path)
        os.makedirs(preserved)
        segment = os.path.join(preserved, "00001.m4v")
        with open(segment, "wb") as f:
            f.write(b"\x00" * 16)
        click_retry(widget)
        assert item.downloadState is DownloadState.WAITING
        assert os.path.isdir(preserved) and os.path.exists(segment), (
            "대기로 되돌리면서 보존된 세그먼트가 지워졌다 — 재시도가 처음부터 받게 된다"
        )


class TestControlsFollowTheState:
    """§3.4 — 상태가 바뀌면 3행의 조작 요소가 전부 따라온다: 가시성·호버·커서·툴팁."""

    def test_retry_button_goes_and_the_waiting_controls_come(self, tmp_path, started):
        """↻·실패 사유가 사라지고 해상도 pill이 나온다 — 대기 매트릭스 그대로."""
        win, item, widget = open_window_with_a_failed_card(tmp_path)
        assert widget.statusLabel.isVisible() and not any(b.isVisible() for b in widget.buttons)
        click_retry(widget)
        assert not widget.retryButton.isVisible(), "대기 카드에 ↻가 남아 있다"
        assert not widget.pauseButton.isVisible() and not widget.openDirectoryButton.isVisible()
        assert not widget.statusLabel.isVisible(), "실패 사유가 3행에 남아 있다"
        assert widget.buttons and all(b.isVisible() for b in widget.buttons), (
            "대기의 조작(pill)이 안 나왔다"
        )
        assert widget.fileSizeLabel.isVisible(), "대기에서는 크기 슬롯이 다시 보인다"
        assert widget.progressBar.property("state") == "waiting"

    def test_failure_tooltip_is_not_exposed_after_the_revert(self, tmp_path, started):
        """실패 사유 툴팁을 들고 있던 라벨은 숨겨지고, 대기 카드의 보이는 표면에 실패 문구가 없다."""
        win, item, widget = open_window_with_a_failed_card(tmp_path)
        assert "Failed to save file" in widget.statusLabel.toolTip()
        click_retry(widget)
        visible_tooltips = [
            w.toolTip() for w in widget.findChildren(type(widget.statusLabel)) if w.isVisible()
        ] + [b.toolTip() for b in widget.buttons if b.isVisible()]
        assert not any("Failed" in t for t in visible_tooltips), visible_tooltips

    def test_title_and_path_surfaces_switch_to_waiting(self, tmp_path, started, qtbot):
        """제목은 편집 가능(IBeam·호버 강조), 경로 라벨은 손가락 커서 — 실패에서는 둘 다 아니었다."""
        win, item, widget = open_window_with_a_failed_card(tmp_path)
        qtbot.addWidget(win)
        assert widget.titleLabel.cursor().shape() == Qt.CursorShape.ArrowCursor
        assert not highlights_on_hover(qtbot, win, widget.titleLabel)
        click_retry(widget)
        assert widget.titleLabel.cursor().shape() == Qt.CursorShape.IBeamCursor, (
            "대기 제목은 편집 가능해야 한다"
        )
        assert highlights_on_hover(qtbot, win, widget.titleLabel), (
            "대기 제목의 호버 강조가 안 따라왔다"
        )
        # 경로 표면 — 전역 경로와 다르므로 라벨이 보인다(같으면 아예 숨는다)
        if widget.directoryLabel.isVisible():
            assert widget.directoryLabel.cursor().shape() == Qt.CursorShape.PointingHandCursor
        else:
            assert widget.pathIconButton.isVisible()

    def test_pills_are_clickable_again_after_the_revert(self, tmp_path, started, qtbot):
        """대기 카드의 pill은 눌러서 해상도를 고를 수 있다 — 호버 강조와 클릭 반응."""
        win, item, widget = open_window_with_a_failed_card(tmp_path)
        qtbot.addWidget(win)
        click_retry(widget)
        resize_to(win, win.width())  # 폭을 확정한 뒤 pill 배치를 다시 잰다
        other = widget.buttons[1]
        shown(other)
        assert highlights_on_hover(qtbot, win, other), "대기 pill이 호버에 반응하지 않는다"
        other.click()
        _pump()
        assert item.resolution == "720", "되돌린 카드에서 해상도를 다시 고를 수 없다"
