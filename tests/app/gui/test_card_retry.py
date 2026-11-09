"""실패 카드의 재시도 = 대기로 되돌리기 게이트 (#261) — 시작은 유저가 따로 누른다.

재시도(↻)가 즉시 다시 받기 시작하면 실패 원인을 고칠 틈이 없다(쿠키 만료: 같은 쿠키로 바로
또 실패). 이 파일이 고정하는 것:

1. 재시도 → 카드는 대기(`DownloadState.WAITING` — app 계층 `ContentItem`의 표시 상태)가 되고
   **다운로드가 시작되지 않는다**(핸들이 생기지 않는다, `DownloadViewModel.start` 미호출).
2. 되돌린 뒤 전역 다운로드 버튼을 누르면 **기존 시작 경로**를 탄다 — 재시도 전용 경로가 없다.
3. 전역 버튼은 **되돌린 카드는 집어 가고, 실패인 채인 카드는 집어 가지 않는다** — 정본의
   "실패 카드 자동 포함 반대"와의 경계다.
4. 조작 표면이 상태를 따라온다(§3.4): ↻가 사라지고 대기의 조작(✎ — 카드 편집 창, #550)이
   나온다 — 카드 버튼 · 툴팁.
5. 대기로 되돌릴 때 후처리 실패로 보존된 세그먼트(#185)가 지워지지 않는다.

core `DownloadState`의 `FAILED → WAITING` 불허(#135)는 tests/core/models/test_download_task_model.py가
그대로 지킨다 — 여기서는 그 계약을 **건드리지 않았음**을 층 분리로 증명한다(실패한 core
모델은 FAILED로 끝나고, 되돌린 카드가 시작될 때 새 모델이 WAITING부터 만들어진다).

실제 배선을 탄다: 카드의 ↻ 클릭 → 위젯 시그널 → 뷰 릴레이 → `VodDownloader.onCardRetry`,
전역 버튼 클릭 → `onDownloadPause` → `downloadItem` → `downloadRequested` → `startDownload`.
`DownloadViewModel.start`만 대역으로 바꿔 실제 네트워크·엔진은 돌리지 않는다.
config는 conftest가 격리한다. 카드 버튼은 호버 때만 보인다 — 누르기 전에 `setHovered(True)`로 보인다.
"""

import os
from collections.abc import Iterator

import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import CVDv2 as main_module
import app.theme as theme
from app.views import mainWindow as mw_mod
from app.views.mainWindow import VodDownloader
from app.viewmodels.data import ContentItem
from app.widgets.widget import ContentItemWidget
from core.models.download_state import DownloadState
from core.models.plan import TimeRange
from core.models.section_resume import SectionResume
from core.utils.paths import build_output_path, temp_dir_for
from tests.app.gui.card_helpers import (
    drop_new_top_levels,
    hold_style,
    snapshot_top_levels,
)
from typing import Never


@pytest.fixture(autouse=True)
def _apply_production_qss(qapp) -> None:
    """실제 전역 QSS·스타일(호버 규칙 포함)을 태운다. ⚠️ function scope 유지."""
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


@pytest.fixture(autouse=True)
def _no_network(monkeypatch) -> None:
    """카드의 크기 조회·메타데이터 조회가 네트워크를 타지 않게 막는다."""

    class _FailingSession:
        def head(self, *a, **k) -> Never:
            raise RuntimeError("network disabled in tests")

        def get(self, *a, **k) -> Never:
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("app.widgets.widget.get_thread_session", lambda: _FailingSession())


@pytest.fixture
def started(monkeypatch) -> list[ContentItem]:
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


def _pump() -> None:
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


def open_window_with_a_failed_card(
    tmp_path,
) -> tuple[VodDownloader, ContentItem, ContentItemWidget]:
    """실제 메인 창에 실패 카드 한 장 — 대기로 넣어 해상도가 골라진 뒤 실패로 바꾼다(제품 순서)."""
    win = VodDownloader()
    win.resize(900, win.height())
    win.show()
    QTest.qWaitForWindowExposed(win)
    item = _make_item(str(tmp_path))
    win.contentManager.model.addItem(item)
    _pump()
    widget = win.listView.widgetFor(item)
    assert widget is not None, "전제: 카드가 만들어져야 한다"
    assert item.resolution == "1080", "전제: 가장 높은 해상도가 기본으로 골라진다"
    item.download_progress = 37
    item.stateMessage = "Failed to save file\nDetail line"
    item.downloadState = DownloadState.FAILED
    win.contentManager.model.notifyChanged(item)
    _pump()
    assert widget.visibleCardButtons() == ["retryButton", "deleteButton"], (
        "전제: 실패 카드에는 ↻가 놓여야 한다"
    )
    return win, item, widget


def hover_card(widget) -> None:
    """카드를 호버로 둬 카드 버튼을 보인다 — 목록의 호버 다시 맞추기가 끈 뒤에도 다시 켠다."""
    widget.setHovered(True)
    _pump()
    widget.setHovered(True)


def click_retry(widget) -> None:
    """카드에 호버해 ↻를 보인 뒤 실제로 누른다 — 위젯 시그널 → 뷰 릴레이 → 메인 창 핸들러."""
    hover_card(widget)
    assert widget.retryButton.isVisible(), "전제: 호버한 실패 카드에는 ↻가 보여야 한다"
    widget.retryButton.click()
    _pump()


class TestRetryOnlyReturnsToWaiting:
    def test_retry_makes_the_card_waiting_and_starts_nothing(self, tmp_path, started) -> None:
        """★ ↻ → 대기. `DownloadViewModel.start`가 불리지 않고 핸들도 없다."""
        win, item, widget = open_window_with_a_failed_card(tmp_path)
        click_retry(widget)
        assert item.downloadState is DownloadState.WAITING
        assert item.stateMessage == "" and item.download_progress == 0
        assert started == [], f"재시도가 다운로드를 시작했다: {started}"
        assert not win.downloadViewModel.isDownloading(), "재시도 뒤 다운로드 핸들이 생겼다"
        assert win.downloadButton.toolTip() == "Download", "전역 버튼이 진행 중 표기로 바뀌었다"

    def test_the_global_button_then_takes_the_reverted_card_through_the_normal_path(
        self, tmp_path, started
    ) -> None:
        """되돌린 뒤 전역 다운로드 버튼 → 기존 시작 경로(`startDownload`)가 그 카드로 불린다."""
        win, item, widget = open_window_with_a_failed_card(tmp_path)
        click_retry(widget)
        win.downloadButton.click()
        _pump()
        assert started == [item], f"전역 버튼이 되돌린 카드를 집어 가지 않았다: {started}"
        assert item.output_path, "기존 경로라면 산출물 경로가 조립돼 있어야 한다"
        assert os.path.dirname(item.output_path) == str(tmp_path)

    def test_a_card_left_failed_is_not_taken_by_the_global_button(self, tmp_path, started) -> None:
        """실패인 채인 카드는 전역 버튼이 집어 가지 않는다 — 자동 포함 반대(정본)의 경계.

        실패 카드 1장뿐 -> 다운로드 버튼이 꺼져 있고(시작할 것이 없음 — #504), 눌러도 시작 · 안내 창 없음
        """
        win, item, widget = open_window_with_a_failed_card(tmp_path)
        assert not win.downloadButton.isEnabled()
        win.downloadButton.click()
        _pump()
        assert item.downloadState is DownloadState.FAILED
        assert started == []

    def test_retry_does_not_touch_preserved_segments(self, tmp_path, started) -> None:
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
    """§3.4 — 상태가 바뀌면 카드 버튼 · 3줄 글 · 툴팁이 전부 따라온다."""

    def test_retry_button_goes_and_the_waiting_controls_come(self, tmp_path, started) -> None:
        """실패 카드가 ↻로 되돌려지면 카드 버튼이 ✎ · ✕가 되고 3줄이 대기 글이어야 한다.

        실패 카드(버튼 ↻ · ✕, 사유 "Failed to save file") → ↻
        -> 버튼 ["editButton", "deleteButton"], 상태 글 "Download waiting", 정보 칸 보임,
        진행바 상태 "waiting"
        """
        win, item, widget = open_window_with_a_failed_card(tmp_path)
        assert widget.statusLabel.text() == "Failed to save file"
        click_retry(widget)
        assert widget.visibleCardButtons() == ["editButton", "deleteButton"]
        hover_card(widget)
        assert widget.editButton.isVisible(), "호버한 대기 카드에 ✎가 안 보인다"
        assert not widget.retryButton.isVisible(), "대기 카드에 ↻가 남아 있다"
        assert widget.statusLabel.text() == "Download waiting", "실패 사유가 3줄에 남아 있다"
        assert widget.fileSizeLabel.isVisible(), "대기에서는 정보 칸이 다시 보인다"
        assert widget.progressBar.property("state") == "waiting"

    def test_failure_tooltip_is_not_exposed_after_the_revert(self, tmp_path, started) -> None:
        """되돌린 대기 카드는 보이는 라벨 · 카드 버튼의 툴팁에 실패 문구가 없어야 한다.

                사유 "Failed to save file
        Detail line"인 실패 카드 → ↻ -> 보이는 툴팁에 "Failed" 없음
        """
        win, item, widget = open_window_with_a_failed_card(tmp_path)
        assert "Failed to save file" in widget.statusLabel.toolTip()
        click_retry(widget)
        hover_card(widget)
        visible_tooltips = [
            w.toolTip() for w in widget.findChildren(type(widget.statusLabel)) if w.isVisible()
        ] + [
            getattr(widget, name).toolTip()
            for name in widget.visibleCardButtons()
            if getattr(widget, name).isVisible()
        ]
        assert not any("Failed" in t for t in visible_tooltips), visible_tooltips

    def test_edit_dialog_picks_a_resolution_again_after_the_revert(self, tmp_path, started) -> None:
        """되돌린 대기 카드의 ✎를 누르면 편집 창에서 해상도를 다시 고를 수 있어야 한다.

        해상도 1080 · 720, 1080을 고른 채 실패한 카드 → ↻ → ✎ → 편집 창에서 720 · 저장
        -> 아이템 해상도 "720"
        """
        win, item, widget = open_window_with_a_failed_card(tmp_path)
        click_retry(widget)
        hover_card(widget)
        assert widget.editButton.isVisible(), "전제: 호버한 대기 카드에 ✎가 보여야 한다"
        widget.editButton.click()
        _pump()
        dialog = win._editDialog
        assert dialog is not None, "되돌린 카드의 ✎가 편집 창을 열지 않았다"
        combo = dialog.resolutionCombo
        row = next(
            r for r in range(combo.count()) if item.unique_reps[combo.itemData(r)][0] == "720"
        )
        combo.setCurrentIndex(row)
        dialog.saveButton.click()
        _pump()
        assert item.resolution == "720", "되돌린 카드에서 해상도를 다시 고를 수 없다"


class TestRetryChecksTheFinishedSections:
    def test_retry_press_drops_a_finished_section_whose_file_is_gone(
        self, tmp_path, started
    ) -> None:
        """실패 카드의 ↻를 누르면 끝낸 구간 가운데 파일이 없는 것을 끝나지 않은 것으로 돌려야 한다 (#309).

        실패 카드에 남긴 것: 구간 셋, 끝낸 구간 {0, 2}. 0번 파일은 있고 2번 파일은 없다
        -> ↻ 뒤 아이템에 남은 끝낸 구간 == {0}, 다운로드는 시작되지 않는다
        """
        win, item, widget = open_window_with_a_failed_card(tmp_path)
        selections = (TimeRange(1.0, 2.0), TimeRange(3.0, 4.0), TimeRange(5.0, 6.0))
        paths = tuple(str(tmp_path / f"제목 1080p_{number}.mp4") for number in (1, 2, 3))
        with open(paths[0], "wb") as f:
            f.write(b"made")
        resume = SectionResume(selections=selections, paths=paths, done=frozenset({0, 2}))
        item.section_retry = ("stream", resume)

        click_retry(widget)

        assert item.section_retry[1].done == frozenset({0})
        assert started == []
