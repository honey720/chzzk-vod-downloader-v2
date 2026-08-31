"""카드 상태 매트릭스 게이트 (#245 — 상태는 넷이 아니라 다섯이다).

오너 확정 표를 그대로 코드로 옮겨 **셀 하나씩** 단언한다:

| 상태     | 3행 슬롯              | 조작        | 진행바        |
|----------|-----------------------|-------------|---------------|
| 대기     | 해상도 pill           | —           | 없음          |
| 진행     | 42% · 8.2 MB/s · 남음 | 일시정지    | 있음          |
| 일시정지 | 54% · 일시정지됨      | 재개        | 있음(muted)   |
| 완료     | ✓ 완료                | 폴더 열기   | 없음          |
| 실패     | ✕ 사유                | 재시도      | 없음          |

`DownloadState.PAUSED`가 첫 슬롯 설계에서 빠진 채 "진행 중일 때만 바 /
일시정지에도 일시정지 버튼"으로 나갔던 것을 되돌린 게이트다 — 멈춰 있는
카드가 이미 한 일(일시정지)을 또 권하면 안 되고, 받은 양은 바가 계속
보여줘야 한다(색만 muted).

조작 아이콘은 폰트 글리프가 아니라 `content/icons.py`가 그리는 도형이라
`iconName()`으로 폰트 무관하게 단언한다. 슬롯 텍스트는 `text()`(모델 값)로
본다 — 렌더 픽셀은 폰트에 좌우된다.
"""

import pytest
from PySide6.QtWidgets import QApplication

import main as main_module
import theme
from content import icons
from content.data import ContentItem
from content.widget import ContentItemWidget
from core.models.download_state import DownloadState


@pytest.fixture(autouse=True)
def _apply_production_qss(qapp):
    """실제 전역 QSS를 태운다. ⚠️ `scope="function"` 유지(macOS 종료 크래시 —
    `test_widget_theme.py`의 `_apply_dark_card_qss` 문서 참고)."""
    theme.set_color_scheme("dark")
    qapp.setStyle(theme.build_style())
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    class _FailingSession:
        def head(self, *a, **k):
            raise RuntimeError("network disabled in tests")

        def get(self, *a, **k):
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("content.widget.get_thread_session", lambda: _FailingSession())
    monkeypatch.setattr("content.widget._global_download_path", "C:/dl")


def _make_widget(qapp, state, progress=0) -> ContentItemWidget:
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
        "C:/dl",
        "video",
        None,
    )
    item.downloadState = DownloadState.WAITING
    item.total_size = "595.34 MB"
    widget = ContentItemWidget(item, 0)
    widget.addRepresentationButtons()  # 대기에서 pill 생성(크기 조회는 네트워크 차단으로 안 돈다)
    # 유저가 1080p pill을 골라 확정한 상태를 재현 — 실제 앱은 크기 조회 콜백이
    # 마지막 해상도를 기본 선택하고 유저 클릭이 바꾼다
    widget.setresolutionUrlSize("1080", "u1", 0, widget.buttons[0])
    item.total_size = "595.34 MB"
    item.downloadState = state
    item.download_progress = progress
    item.download_speed = "8.2 MB/s"
    item.download_remain_time = "00:12:34"
    item.download_size = 624_000_000
    item.download_time = "00:05:12"  # 어댑터가 주는 전체 소요(전송+후처리) "HH:MM:SS"
    item.stateMessage = "Failed to save file" if state == DownloadState.FAILED else ""
    widget.setData(item, 0)
    widget.resize(900, widget.sizeHint().height())
    widget.show()
    QApplication.processEvents()
    return widget


#: 상태 → (조작 버튼 이름 또는 None, 도형 이름, 진행바 표시, 진행바 색 상태)
MATRIX = {
    DownloadState.WAITING: (None, None, False, "waiting"),
    DownloadState.RUNNING: ("pauseButton", "pause", True, "running"),
    DownloadState.PAUSED: ("pauseButton", "resume", True, "paused"),
    DownloadState.FINISHED: ("openDirectoryButton", "folder", False, "finished"),
    DownloadState.FAILED: ("retryButton", "retry", False, "failed"),
}

ACTION_BUTTONS = ("pauseButton", "openDirectoryButton", "retryButton")
PROGRESS = {DownloadState.RUNNING: 42, DownloadState.PAUSED: 54, DownloadState.FINISHED: 100}


class TestSlotColumn:
    """3행 슬롯 — 상태마다 다른 질문에 답한다."""

    def test_waiting_shows_resolution_pills_only(self, qapp):
        widget = _make_widget(qapp, DownloadState.WAITING)
        assert widget.buttons and all(b.isVisible() for b in widget.buttons)
        assert not widget.statusLabel.isVisible()

    def test_running_shows_percent_speed_and_remaining(self, qapp):
        widget = _make_widget(qapp, DownloadState.RUNNING, 42)
        text = widget.statusLabel.text()
        assert widget.statusLabel.isVisible()
        assert text.startswith("42%"), text
        assert "8.2 MB/s" in text and "12:34" in text

    def test_paused_shows_percent_first_then_paused(self, qapp):
        widget = _make_widget(qapp, DownloadState.PAUSED, 54)
        text = widget.statusLabel.text()
        assert widget.statusLabel.isVisible()
        assert text.startswith("54%"), f"진행분이 앞에 와야 한다: {text!r}"
        assert text.endswith("Paused"), f"뒤 문구는 '일시정지됨'(Paused)이어야 한다: {text!r}"
        assert "Download paused" not in text, (
            "옛 문구('다운로드 정지')가 남아 있다 — 정지는 완전 중단으로 읽힌다"
        )
        assert "left" not in text, "일시정지에 남은 시간이 붙어 있다 — 멈춘 상태에 남은 시간은 없다"

    def test_finished_shows_check_completed_and_elapsed(self, qapp):
        """"✓ 완료 · 5:12" — 소요 시간은 진행 중의 남은 시간과 같은 짧은 포맷.
        전송 성능에 들인 공이 유저에게 보이는 유일한 자리다(#245)."""
        widget = _make_widget(qapp, DownloadState.FINISHED, 100)
        assert widget.statusLabel.text() == "✓ Completed · 5:12", widget.statusLabel.text()

    def test_finished_elapsed_uses_the_same_short_format_as_remaining(self, qapp):
        widget = _make_widget(qapp, DownloadState.FINISHED, 100)
        widget.item.download_time = "01:02:03"
        widget.setData(widget.item, 0)
        assert widget.statusLabel.text().endswith("· 1:02:03")
        widget.item.download_time = "00:00:07"
        widget.setData(widget.item, 0)
        assert widget.statusLabel.text().endswith("· 0:07")

    def test_finished_without_elapsed_shows_completed_only(self, qapp):
        """값이 없으면 억지로 채우지 않는다 — "✓ 완료"만."""
        widget = _make_widget(qapp, DownloadState.FINISHED, 100)
        widget.item.download_time = ""
        widget.setData(widget.item, 0)
        assert widget.statusLabel.text() == "✓ Completed"

    def test_failed_shows_cross_and_reason(self, qapp):
        widget = _make_widget(qapp, DownloadState.FAILED)
        assert widget.statusLabel.text() == "✕ Failed to save file"


class TestActionColumn:
    """1행 우측 조작 — "지금 할 수 있는 것" 하나만 보이고 삭제는 항상."""

    @pytest.mark.parametrize("state", list(MATRIX))
    def test_exactly_the_matrix_action_is_visible(self, qapp, state):
        button_name, icon, _, _ = MATRIX[state]
        widget = _make_widget(qapp, state, PROGRESS.get(state, 0))
        visible = [name for name in ACTION_BUTTONS if getattr(widget, name).isVisible()]
        assert visible == ([button_name] if button_name else []), (
            f"{state.name}: 보이는 조작 {visible} — 매트릭스는 {button_name}"
        )
        assert widget.deleteButton.isVisible(), f"{state.name}: 삭제는 항상 보여야 한다"
        if button_name:
            assert getattr(widget, button_name).iconName() == icon, (
                f"{state.name}: 조작 도형이 {getattr(widget, button_name).iconName()!r} — {icon!r}이어야 한다"
            )

    def test_paused_offers_resume_not_pause_again(self, qapp):
        """멈춰 있는데 일시정지 버튼이 그대로면 이미 한 일을 또 권하는 것이다."""
        widget = _make_widget(qapp, DownloadState.PAUSED, 54)
        assert widget.pauseButton.iconName() == "resume"
        assert widget.pauseButton.toolTip() == "Resume"

    def test_running_offers_pause(self, qapp):
        widget = _make_widget(qapp, DownloadState.RUNNING, 42)
        assert widget.pauseButton.iconName() == "pause"
        assert widget.pauseButton.toolTip() == "Pause"

    def test_pause_and_resume_toggle_back_and_forth(self, qapp):
        """같은 버튼이 상태를 따라 도형·툴팁을 오간다 — 한쪽으로 굳으면 회귀다."""
        widget = _make_widget(qapp, DownloadState.RUNNING, 42)
        seen = []
        for state in (DownloadState.PAUSED, DownloadState.RUNNING, DownloadState.PAUSED):
            widget.item.downloadState = state
            widget.setData(widget.item, 0)
            seen.append(widget.pauseButton.iconName())
        assert seen == ["resume", "pause", "resume"], seen

    def test_delete_highlights_with_the_failure_colour_on_hover(self, qapp):
        """삭제는 평소 muted, 호버에서만 실패색 — 항상 빨간 ✕는 위계 역전(#244)."""
        widget = _make_widget(qapp, DownloadState.WAITING)
        assert widget.deleteButton.hoverToken() == "stateFailed"
        assert widget.deleteButton.colorToken() == "textMuted"
        for name in ACTION_BUTTONS:
            assert getattr(widget, name).hoverToken() == "text"


class TestProgressBarColumn:
    """진행바 — 진행분이 있을 때(진행·일시정지)만, 일시정지는 muted."""

    @pytest.mark.parametrize("state", list(MATRIX))
    def test_bar_visibility_and_colour_state_follow_the_matrix(self, qapp, state):
        _, _, bar_visible, bar_state = MATRIX[state]
        widget = _make_widget(qapp, state, PROGRESS.get(state, 0))
        assert widget.progressBar.isVisible() == bar_visible, (
            f"{state.name}: 진행바 표시 {widget.progressBar.isVisible()} — 매트릭스는 {bar_visible}"
        )
        assert widget.progressBar.property("state") == bar_state
        assert widget.statusLabel.property("state") == bar_state

    def test_paused_bar_keeps_the_progress_value(self, qapp):
        widget = _make_widget(qapp, DownloadState.PAUSED, 54)
        assert widget.progressBar.value() == 54

    def test_paused_colour_is_distinct_from_running_and_waiting(self):
        """muted는 "다른 색"이어야 정보다 — 진행 파랑·대기 회색 둘 다와 달라야 한다."""
        for table in (theme.DARK, theme.LIGHT):
            assert table["statePaused"] not in (table["stateRunning"], table["stateWaiting"])


class TestRightColumnResolutionAndSize:
    """3행 우측 — 확정 해상도를 파일 크기 옆에 붙인다("1080p · 595.34 MB").
    한 자리라 행이 늘지 않는다. 대기에서는 pill이 선택을 보여주므로 안 붙인다."""

    @pytest.mark.parametrize("state", (DownloadState.RUNNING, DownloadState.PAUSED))
    def test_running_and_paused_show_resolution_with_total_size(self, qapp, state):
        widget = _make_widget(qapp, state, PROGRESS[state])
        assert widget.fileSizeLabel.isVisible()
        assert widget.fileSizeLabel.text() == "1080p · 595.34 MB", widget.fileSizeLabel.text()

    def test_finished_shows_resolution_with_downloaded_size(self, qapp):
        widget = _make_widget(qapp, DownloadState.FINISHED, 100)
        assert widget.fileSizeLabel.text().startswith("1080p · ")
        assert "MB" in widget.fileSizeLabel.text()

    def test_waiting_shows_size_only(self, qapp):
        widget = _make_widget(qapp, DownloadState.WAITING)
        assert "1080p" not in widget.fileSizeLabel.text()

    def test_failed_hides_the_size_slot(self, qapp):
        widget = _make_widget(qapp, DownloadState.FAILED)
        assert not widget.fileSizeLabel.isVisible()


class TestHeightIsInvariantAcrossFiveStates:
    """다섯 상태 전부에서 카드 높이가 같다 — 목록이 들썩이면 안 된다."""

    def test_same_height_in_all_five_states(self, qapp):
        heights = {}
        for state in MATRIX:
            widget = _make_widget(qapp, state, PROGRESS.get(state, 0))
            heights[state.name] = widget.height()
        assert len(set(heights.values())) == 1, f"상태별 카드 높이가 다르다: {heights}"

    def test_same_height_when_one_card_walks_through_all_five_states(self, qapp):
        widget = _make_widget(qapp, DownloadState.WAITING)
        heights = {}
        for state in MATRIX:
            widget.item.downloadState = state
            widget.item.download_progress = PROGRESS.get(state, 0)
            widget.setData(widget.item, 0)
            widget.resize(900, widget.sizeHint().height())
            QApplication.processEvents()
            heights[state.name] = widget.height()
        assert len(set(heights.values())) == 1, (
            f"한 카드가 상태를 옮겨갈 때 높이가 변한다: {heights}"
        )


class TestDrawnIconsAreSharedAndFontFree:
    """조작 아이콘은 폰트 글리프가 아니라 그린 도형이고, 아이콘당 한 번만
    그려 공유한다 — 카드마다 페인트 객체가 붙으면 O(1) 삽입이 무너진다."""

    def test_every_icon_paints_something_without_a_font(self, qapp):
        for name in icons.ICON_NAMES:
            image = icons.action_pixmap(
                name, theme.DARK["text"], theme.METRICS["actionGlyph"]
            ).toImage()
            painted = sum(
                1
                for x in range(image.width())
                for y in range(image.height())
                if image.pixelColor(x, y).alpha() > 0
            )
            assert painted > 0, f"{name} 도형이 비어 있다"

    def test_icon_pixmaps_are_shared_across_cards(self, qapp):
        before = icons.cache_size()
        widgets = [_make_widget(qapp, DownloadState.FAILED) for _ in range(5)]
        for widget in widgets:
            widget.retryButton.grab()  # paintEvent를 실제로 태운다
        after = icons.cache_size()
        # 카드 5장이 그려도 캐시는 (retry·delete) × 색 조합만큼만 는다 — 카드 수와 무관
        assert after - before <= 4, f"카드마다 픽스맵을 새로 만들고 있다: +{after - before}"
        first = icons.action_pixmap(
            "retry", theme.DARK["textMuted"], theme.METRICS["actionGlyph"], 1.0
        )
        assert first is icons.action_pixmap(
            "retry", theme.DARK["textMuted"], theme.METRICS["actionGlyph"], 1.0
        )

    def test_buttons_carry_no_text_glyph(self, qapp):
        widget = _make_widget(qapp, DownloadState.RUNNING, 42)
        for name in ACTION_BUTTONS + ("deleteButton",):
            assert getattr(widget, name).text() == "", f"{name}이 아직 문자 글리프를 쓴다"

    def test_unknown_icon_name_is_rejected(self, qapp):
        with pytest.raises(ValueError):
            icons.action_pixmap("emoji", theme.DARK["text"], 10)
