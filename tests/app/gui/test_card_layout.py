"""카드 가로 배치 회귀 게이트 (#244 재설계 · #550 C2 배치 — 확정 구조를 기하값으로 고정).

밀도 게이트(`test_card_density.py`)는 세로만 본다 — 가로 배치에 게이트가
없던 동안 "넓은 창에서 요소가 균등 분배로 흩어지는" 결함이 시안 리뷰를
그대로 통과해 실기에서야 잡혔다(1600px에서 해상도 버튼 간격 335px 실측).
이 파일이 그 구멍을 막는다.

**고정하는 불변식(#245 상태별 슬롯 · #550 C2 배치)**:
- 좌측 기준선은 둘뿐 — 썸네일 왼쪽(=cardPadding)과 글 열 왼쪽.
  세 줄(1줄 채널 그림 · 채널명 · 날짜, 2줄 제목, 3줄 상태 아이콘 · 상태 글 ··· 저장 위치 ·
  정보 글) 전부 글 열의 같은 x에서 시작한다. 예외 없다.
- 우측 끝도 하나 — 호버 때 1줄 오른쪽 끝에 겹치는 버튼 상자의 삭제(✕)와 3줄 정보 글의
  오른쪽 끝이 같은 x. 버튼이 늘어도 유지.
- 남는 공간은 각 줄에서 딱 한 곳(가운데 스트레치)만 흡수한다.
- 상태가 바뀌면 3줄 내용 · 버튼이 바뀌지만 줄 위치 · 카드 높이는 불변
  (목록이 들썩이면 안 된다).

좌표는 모두 카드 면(`contentFrame`) 기준으로 옮겨 잰다 — 줄마다 위젯이 따로 있어(#550)
위젯의 `x()`는 그 줄 안의 값이다.

**방법론**: 폰트에 의존하지 않는 순수 기하 *관계*만 잰다 — 라벨 폭 자체는
폰트가 정하지만, "이웃 간격 == 고정 spacing"·"기준선 일치"·"남는 공간이
한 곳"이라는 관계는 폰트와 무관하다.

**⚠️ 반드시 창 폭 여러 값으로 잰다** — 이 부류 결함은 넓은 폭에서만
드러난다. 좁은 폭에서는 요소가 꽉 차 정렬 문제가 안 보인다.
"""

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

import CVDv2 as main_module
import app.theme as theme
from app.viewmodels.data import ContentItem
from app.widgets.widget import ContentItemWidget
from core.models.download_state import DownloadState
from tests.app.gui.card_helpers import resize_to, shown, hold_style
from typing import Never

#: 좁게(요소가 꽉 참)·기본·아주 넓게(실기 와이드 모니터 — 결함이 실제로
#: 발견된 조건).
WIDTHS = (560, 900, 1600)

#: 각 줄 QHBoxLayout의 setSpacing 고정값(app/widgets/contentItemWidget.py — cardRowSpacing).
FIXED_SPACING = 4

#: 기본 위치와 다른 긴 저장 위치 — 3줄에 저장 위치가 보이는 카드를 만든다.
LONG_PATH = "D:/vod/archive/2026/summer/finals/T1-vs-GEN-full-set-highlights-and-interviews"


@pytest.fixture(autouse=True)
def _apply_production_qss(qapp) -> None:
    """실제 전역 QSS(위계 폰트 토큰 포함)를 태운 상태에서 잰다. ⚠️
    `scope="function"` 유지 — 넓히면 macOS 종료 크래시 재발
    (`test_widget_theme.py`의 `_apply_dark_card_qss` 문서 참고)."""
    theme.set_color_scheme("dark")
    qapp.setStyle(
        hold_style(theme.build_style())
    )  # 참조 보관 — 이중 해제 우회 (#243, card_helpers.hold_style)
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))


@pytest.fixture(autouse=True)
def no_network(monkeypatch) -> None:
    class _FailingSession:
        def head(self, *a, **k) -> Never:
            raise RuntimeError("network disabled in tests")

        def get(self, *a, **k) -> Never:
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("app.widgets.widget.get_thread_session", lambda: _FailingSession())
    # 테스트 아이템의 기본 경로를 전역 설정 경로로 등록한다(#245) — 실제
    # 앱은 시작 시 mainWindow가 밀어 넣는 값이라, 안 넣으면 모든 카드가
    # "전역과 다른 경로"로 판정돼 3줄에 저장 위치가 뜬다.
    monkeypatch.setattr("app.widgets.widget._global_download_path", "C:/dl")


def _make_item(reps=("1080", "720", "144"), path="C:/dl") -> ContentItem:
    item = ContentItem(
        "https://chzzk.naver.com/video/1",
        {
            "title": "제목",
            "category": "",
            "channelName": "채널",
            "createdDate": "",
            "duration": 3600,
        },
        [[r, f"u{r}"] for r in reps],
        None,
        "",
        path,
        "video",
        None,
    )
    item.downloadState = DownloadState.WAITING
    item.total_size = "711.02 MB"
    return item


def _make_widget(qapp, reps=("1080", "720", "144"), path="C:/dl") -> ContentItemWidget:
    item = _make_item(reps, path)
    widget = ContentItemWidget(item, 0)
    widget.loadRepresentations()
    widget.setData(item, 0)
    return widget


def _set_state(widget, state, progress=42) -> None:
    widget.item.downloadState = state
    widget.item.download_progress = progress
    widget.setData(widget.item, 0)


def _running_widget(qapp) -> ContentItemWidget:
    """받는 중 + 기본 위치와 다른 긴 경로 — 3줄에 상태 글 · 저장 위치 · 정보 글이 함께 있는 카드."""
    widget = _make_widget(qapp, path=LONG_PATH)
    item = widget.item
    item.downloadState = DownloadState.RUNNING
    item.resolution = "1080"
    item.download_progress, item.download_speed, item.download_remain_time = (
        42,
        "12.3 MB/s",
        "00:03:21",
    )
    widget.setData(item, 0)
    _at_width(widget, 900)
    return widget


def _at_width(widget, width) -> None:
    # 높이는 자연값(sizeHint)을 쓴다 — 임의 고정 높이를 강제하면 세로 여분이
    # 행 사이로 분배돼 실제 목록(카드가 자기 hint 높이를 받는)과 다른
    # 조건에서 재게 된다.
    widget.resize(width, widget.sizeHint().height())
    widget.show()
    QApplication.processEvents()


def _hover(widget) -> None:
    """호버를 켜 카드 버튼 상자를 보인다 — 버튼 기하는 보일 때만 의미가 있다(#550)."""
    widget.setHovered(True)
    QApplication.processEvents()


def _x(widget, w) -> int:
    """`w`의 왼쪽 끝 — 카드 면(`contentFrame`) 좌표."""
    return w.mapTo(widget.contentFrame, QPoint(0, 0)).x()


def _y(widget, w) -> int:
    """`w`의 위끝 — 카드 면(`contentFrame`) 좌표."""
    return w.mapTo(widget.contentFrame, QPoint(0, 0)).y()


def _right(widget, w) -> int:
    """`w`의 오른쪽 끝(마지막 픽셀 다음) — 카드 면 좌표."""
    return _x(widget, w) + w.width()


def _gap(widget, left_widget, right_widget) -> int:
    return _x(widget, right_widget) - _right(widget, left_widget)


class TestTwoLeftBaselines:
    """좌측 기준선은 둘뿐 — 썸네일 왼쪽과 글 열 왼쪽 (#244 확정 · #550)."""

    @staticmethod
    def _row_heads(widget) -> dict[str, int]:
        """줄마다 자리를 차지하는 첫 항목의 카드 면 x.

        숨은 항목(대기 카드의 3줄 상태 아이콘)은 건너뛴다 — 레이아웃 항목이 비지 않았는지
        (`isEmpty()`)로 고르고, 위젯이 아니라 레이아웃이 준 자리(`geometry()`)를 잰다.
        """
        heads = {}
        for name, row, layout in (
            ("1줄", widget.topRow, widget.topLayout),
            ("2줄", widget.titleRow, widget.titleLayout),
            ("3줄", widget.statusRow, widget.resolutionLayout),
        ):
            first = next(
                layout.itemAt(i)
                for i in range(layout.count())
                if layout.itemAt(i).widget() is not None and not layout.itemAt(i).isEmpty()
            )
            heads[f"{name} {first.widget().objectName()}"] = _x(widget, row) + first.geometry().x()
        return heads

    @pytest.mark.parametrize("state", (DownloadState.WAITING, DownloadState.RUNNING))
    @pytest.mark.parametrize("width", WIDTHS)
    def test_all_rows_start_at_the_content_baseline(self, qapp, width, state) -> None:
        """세 줄의 첫 자리가 폭 · 상태와 무관하게 같은 x(글 열 기준선)에서 시작해야 한다.

        대기 · 받는 중 카드, 창 폭 560 · 900 · 1600
        -> 1줄 channelImageLabel · 2줄 titleLabel · 3줄 첫 항목(대기는 statusLabel)의 카드 면 x가 모두 같다
        """
        widget = _make_widget(qapp)
        _set_state(widget, state)
        _at_width(widget, width)
        content_x = _x(widget, widget.titleLabel)
        starts = self._row_heads(widget)
        offenders = {n: x for n, x in starts.items() if x != content_x}
        assert not offenders, (
            f"폭 {width}px에서 글 열 기준선({content_x}px)을 벗어난 줄 시작점: {offenders} — "
            "좌측 기준선은 썸네일·글 열 둘뿐이어야 한다"
        )

    @pytest.mark.parametrize("width", WIDTHS)
    def test_exactly_two_left_baselines(self, qapp, width) -> None:
        """썸네일 왼쪽이 카드 안쪽 여백 + 테두리 1px이고 글 열이 그 오른쪽이어야 한다.

        창 폭 560 · 900 · 1600
        -> thumb_x == cardPadding + 1, thumb_x < 글 열 x
        """
        widget = _make_widget(qapp)
        _at_width(widget, width)
        thumb_x = _x(widget, widget.thumbnailLabel)
        content_x = _x(widget, widget.titleLabel)
        baselines = {thumb_x, content_x}
        assert len(baselines) == 2 and thumb_x < content_x, (
            f"기준선이 둘이 아니다: thumb={thumb_x}, content={content_x}"
        )
        assert thumb_x == theme.METRICS["cardPadding"] + 1

    def test_baselines_do_not_move_as_the_window_widens(self, qapp) -> None:
        """창 폭이 바뀌어도 썸네일 · 글 열 기준선의 x가 같아야 한다.

        창 폭 560 → 900 → 1600
        -> (썸네일 x, 글 열 x)가 세 폭에서 같다
        """
        widget = _make_widget(qapp)
        positions = []
        for width in WIDTHS:
            _at_width(widget, width)
            positions.append((_x(widget, widget.thumbnailLabel), _x(widget, widget.titleLabel)))
        assert positions[0] == positions[1] == positions[2], (
            f"창 폭에 따라 기준선이 움직인다: {positions}"
        )


class TestOneRightEdge:
    """우측 끝은 하나 — 호버 때 보이는 삭제(1줄)와 정보 글(3줄)의 오른쪽 끝이 같은 x (#550)."""

    @pytest.mark.parametrize("width", WIDTHS)
    def test_delete_and_info_share_the_right_edge(self, qapp, width) -> None:
        """호버한 대기 카드에서 삭제 버튼과 정보 글의 오른쪽 끝이 같은 x여야 한다.

        창 폭 560 · 900 · 1600, setHovered(True)
        -> deleteButton 오른쪽 끝 == fileSizeLabel 오른쪽 끝(카드 면 좌표)
        """
        widget = _make_widget(qapp)
        _at_width(widget, width)
        _hover(widget)
        assert widget.deleteButton.isVisible(), "전제: 호버 때 삭제 버튼이 보인다"
        delete_right = _right(widget, widget.deleteButton)
        info_right = _right(widget, widget.fileSizeLabel)
        assert delete_right == info_right, (
            f"폭 {width}px에서 삭제({delete_right})와 정보 글({info_right})의 오른쪽 끝이 다르다"
        )


class TestRowOneClusters:
    """1줄 — 왼쪽(채널 그림 · 채널명 · 날짜)은 붙어 있고, 호버 때 버튼 상자는 오른쪽 끝에
    붙는다. 남는 공간은 그 사이 한 곳만 흡수한다(#245 · #550)."""

    @pytest.mark.parametrize("width", WIDTHS)
    def test_left_cluster_gap_is_fixed(self, qapp, width) -> None:
        """채널 그림 → 채널명 간격이 폭과 무관하게 고정 간격이어야 한다.

        창 폭 560 · 900 · 1600
        -> channelImageLabel ↔ channelNameLabel 간격 == 4
        """
        widget = _make_widget(qapp)
        _at_width(widget, width)
        gap = _gap(widget, widget.channelImageLabel, widget.channelNameLabel)
        assert gap == FIXED_SPACING, (
            f"폭 {width}px에서 채널이미지→채널명 간격이 {gap}px — 군집이 흩어졌다"
        )

    def test_free_space_lives_only_between_the_clusters(self, qapp) -> None:
        """폭이 커질수록 채널명 → 호버 버튼 상자 사이 한 곳만 벌어져야 한다.

        실패 카드(↻ · ✕), setHovered(True), 창 폭 560 → 900 → 1600
        -> channelNameLabel ↔ retryButton 간격이 단조 증가
        """
        widget = _make_widget(qapp)
        _set_state(widget, DownloadState.FAILED, 0)
        middle_gaps = []
        for width in WIDTHS:
            _at_width(widget, width)
            _hover(widget)
            assert widget.retryButton.isVisible(), "전제: 호버 때 다시 시도 버튼이 보인다"
            middle_gaps.append(_gap(widget, widget.channelNameLabel, widget.retryButton))
        assert middle_gaps[0] < middle_gaps[1] < middle_gaps[2], (
            f"군집 사이 가운데 간격이 폭을 따라 늘지 않는다: {middle_gaps} — "
            "남는 공간이 다른 곳으로 새고 있다"
        )

    def test_button_gap_does_not_grow_as_the_window_widens(self, qapp) -> None:
        """창 폭이 바뀌어도 호버 버튼 사이 간격이 같아야 한다.

        실패 카드(↻ · ✕), setHovered(True), 창 폭 560 → 900 → 1600
        -> retryButton ↔ deleteButton 간격이 세 폭에서 같다
        """
        widget = _make_widget(qapp)
        _set_state(widget, DownloadState.FAILED, 0)
        gaps = []
        for width in WIDTHS:
            _at_width(widget, width)
            _hover(widget)
            gaps.append(_gap(widget, widget.retryButton, widget.deleteButton))
        assert gaps[0] == gaps[1] == gaps[2], f"창 폭에 따라 버튼 간격이 변한다: {gaps}"

    @pytest.mark.parametrize("width", WIDTHS)
    def test_right_edge_holds_with_every_button_shown(self, qapp, width) -> None:
        """카드 버튼 다섯이 한꺼번에 보여도 우측 끝선과 카드 높이가 그대로여야 한다.

        호버한 대기 카드에서 editButton · pauseButton · openDirectoryButton · retryButton을 강제로
        보이고 카드 폭을 560 · 900 · 1600(+1)으로 놓음
        -> deleteButton 오른쪽 끝 == fileSizeLabel 오른쪽 끝, 카드 높이 불변
        """
        widget = _make_widget(qapp)
        _at_width(widget, width)
        height_before = widget.height()
        _hover(widget)
        for button in (
            widget.editButton,
            widget.pauseButton,
            widget.openDirectoryButton,
            widget.retryButton,
        ):
            button.setVisible(True)
        resize_to(widget, width + 1)  # 버튼 상자가 보이는 동안의 크기 바뀜 — 상자를 다시 놓는다
        assert len(widget.visibleCardButtons()) == 5, "전제: 버튼 다섯이 상자에 놓였다"
        assert widget.deleteButton.isVisible(), "전제: 호버 때 삭제 버튼이 보인다"
        assert _right(widget, widget.deleteButton) == _right(widget, widget.fileSizeLabel), (
            "버튼 다섯에서 우측 끝선이 깨졌다"
        )
        assert widget.height() == height_before, "버튼 다섯에서 카드 높이가 변했다"


class TestStatusRow:
    """3줄 — 상태 아이콘 · 상태 글은 왼쪽에 고정 간격으로, 정보 글은 우측 끝, 남는 공간은
    가운데 한 곳 (#245 · #550)."""

    @pytest.mark.parametrize("width", WIDTHS)
    def test_icon_to_status_gap_is_fixed(self, qapp, width) -> None:
        """상태 아이콘 → 상태 글 간격이 폭과 무관하게 고정 간격이어야 한다.

        받는 중 카드, 창 폭 560 · 900 · 1600
        -> statusIcon ↔ statusLabel 간격 == 4
        """
        widget = _make_widget(qapp)
        _set_state(widget, DownloadState.RUNNING)
        _at_width(widget, width)
        gap = _gap(widget, widget.statusIcon, widget.statusLabel)
        assert gap == FIXED_SPACING, f"폭 {width}px에서 상태 아이콘→상태 글 간격이 {gap}px"

    def test_status_position_does_not_move_as_the_window_widens(self, qapp) -> None:
        """창 폭이 바뀌어도 상태 글의 x가 같아야 한다.

        받는 중 카드, 창 폭 560 → 900 → 1600
        -> statusLabel 카드 면 x가 세 폭에서 같다
        """
        widget = _make_widget(qapp)
        _set_state(widget, DownloadState.RUNNING)
        positions = []
        for width in WIDTHS:
            _at_width(widget, width)
            positions.append(_x(widget, widget.statusLabel))
        assert positions[0] == positions[1] == positions[2], (
            f"창 폭에 따라 상태 글 x가 움직인다: {positions} — 왼쪽 고정이 깨졌다"
        )

    def test_free_space_lives_between_status_and_info(self, qapp) -> None:
        """폭이 커질수록 상태 글 → 정보 글 사이만 벌어져야 한다.

        받는 중 카드, 창 폭 560 → 900 → 1600
        -> statusLabel ↔ fileSizeLabel 간격이 단조 증가
        """
        widget = _make_widget(qapp)
        _set_state(widget, DownloadState.RUNNING)
        middle_gaps = []
        for width in WIDTHS:
            _at_width(widget, width)
            middle_gaps.append(_gap(widget, widget.statusLabel, widget.fileSizeLabel))
        assert middle_gaps[0] < middle_gaps[1] < middle_gaps[2], (
            f"3줄 가운데 간격이 폭을 따라 늘지 않는다: {middle_gaps}"
        )


class TestResolutionOrder:
    """해상도 목록은 **내림차순**(높은 해상도가 앞)이고 기본 선택은 첫 항목이다 (#245 · #550).

    카드에는 해상도 버튼이 없다 — 정렬 · 기본 선택은 카드가 하고 편집 창이 그 목록을 읽는다.
    """

    def test_representations_are_ordered_high_to_low(self, qapp) -> None:
        """해상도가 480 · 1080 · 720 순으로 오면 카드가 높은 것부터 정렬해야 한다.

        unique_reps 해상도 (480, 1080, 720)
        -> unique_reps 해상도 차례 ["1080", "720", "480"]
        """
        widget = _make_widget(qapp, reps=("480", "1080", "720"))  # API 입력 순서와 무관
        assert [rep[0] for rep in widget.item.unique_reps] == ["1080", "720", "480"]

    def test_default_selection_is_the_highest(self, qapp) -> None:
        """해상도가 480 · 1080 · 720 순으로 오면 기본 선택이 1080(목록 0번)이어야 한다.

        unique_reps 해상도 (480, 1080, 720)
        -> pickedIndex() == 0, item.resolution == "1080"
        """
        widget = _make_widget(qapp, reps=("480", "1080", "720"))
        assert widget.pickedIndex() == 0
        assert str(widget.item.resolution) == "1080"


class TestRowThreePriority:
    """3줄 우선순위(#245 · #550): 정보 글은 잘리지 않고, 상태 글이 그다음, **먼저 줄어드는 것은
    저장 위치다.** 측정 폭은 절대 px가 아니라 임계 폭 T(3줄 구성 요소의 독립 합산) 기준이다 —
    폭을 px로 박으면 폰트에 따라 카드가 최소폭에 클램프돼 게이트가 아무것도 재지 않는다."""

    def _threshold(self, widget) -> int:
        """임계 폭 T — 3줄에 상태 아이콘 · 상태 글 · 저장 위치(최소 힌트) · 정보 글이 **딱** 들어가는 카드 폭.

        ⚠️ 제품의 계산 함수(`_fitInfo`)를 부르지 않고 테스트가 구성 요소를 **독립 합산**한다.
        요소: 상태 아이콘 · 폴더 아이콘 폭, 상태 글 자연 폭, 저장 위치 라벨의 최소 힌트,
        정보 글 자연 폭(넓은 폭에서 잰 긴 꼴), 간격 4개(빈칸은 간격을 받지 않는다), 그리고
        카드 폭 ↔ 3줄 폭의 차(썸네일 · 여백, 순수 기하로 실측).
        """
        _at_width(widget, 900)
        offset = widget.width() - widget.statusRow.width()
        parts = (
            widget.statusIcon.width()
            + widget.statusLabel.sizeHint().width()
            + widget.pathIcon.width()
            + widget.directoryLabel.minimumSizeHint().width()
            + widget.fileSizeLabel.sizeHint().width()
        )
        return offset + parts + 4 * FIXED_SPACING

    def _points(self, widget) -> dict[str, int]:
        """측정점 — T−1 / T / T+경로 자연 폭÷2(저장 위치가 글이되 일부) / T+200."""
        threshold = self._threshold(widget)
        return {
            "T-1": threshold - 1,
            "T": threshold,
            "T+path/2": threshold + widget.directoryLabel.sizeHint().width() // 2,
            "T+200": threshold + 200,
        }

    @pytest.mark.parametrize("point", ("T-1", "T", "T+path/2", "T+200"))
    def test_info_is_never_elided(self, qapp, point) -> None:
        """받는 중 카드의 정보 글은 임계 폭 근처 어느 폭에서도 잘리지 않아야 한다.

        받는 중 · 긴 저장 위치 카드, 폭 T−1 · T · T+경로÷2 · T+200
        -> fileSizeLabel 표시 문자열 == 원문(말줄임 없음)
        """
        widget = _running_widget(qapp)
        width = self._points(widget)[point]
        assert width >= widget.minimumSizeHint().width(), "전제: 측정 폭이 카드 최소폭 이상"
        resize_to(widget, width)
        assert shown(widget.fileSizeLabel) == widget.fileSizeLabel.text(), (
            f"{point}에서 정보 글이 잘렸다: {shown(widget.fileSizeLabel)!r}"
        )

    @pytest.mark.parametrize("point", ("T", "T+path/2", "T+200"))
    def test_status_keeps_its_text_and_the_path_yields(self, qapp, point) -> None:
        """받는 중 카드의 상태 글은 T 이상에서 잘리지 않아야 한다 — 저장 위치가 먼저 양보한다.

        받는 중 · 긴 저장 위치 카드, 폭 T · T+경로÷2 · T+200
        -> statusLabel 표시 문자열 == 원문, 저장 위치 이름이 숨거나(아이콘만 — #554) 그 오른쪽 끝 <
        정보 글 왼쪽 끝
        """
        widget = _running_widget(qapp)
        resize_to(widget, self._points(widget)[point])
        assert shown(widget.statusLabel) == widget.statusLabel.text(), (
            f"{point}에서 상태 글이 잘렸다: {shown(widget.statusLabel)!r} — "
            "줄어드는 것은 저장 위치가 먼저여야 한다"
        )
        if widget.directoryLabel.isVisible():
            assert _right(widget, widget.directoryLabel) < _x(widget, widget.fileSizeLabel)

    @pytest.mark.parametrize("point", ("T", "T+path/2", "T+200"))
    def test_segment_info_is_never_elided(self, qapp, point) -> None:
        """세그먼트 대기 카드의 정보 글(해상도 · 길이)도 임계 폭 근처에서 잘리지 않아야 한다.

        content_type="m3u8" 대기 · 긴 저장 위치 카드, 폭 T · T+경로÷2 · T+200
        -> fileSizeLabel 원문 "1080p · 1:00:00", 표시 문자열 == 원문
        """
        widget = _make_widget(qapp, path=LONG_PATH)
        widget.item.content_type = "m3u8"
        widget.setData(widget.item, 0)
        assert widget.fileSizeLabel.text() == "1080p · 1:00:00", "전제: 세그먼트는 크기 없이 길이"
        resize_to(widget, self._points(widget)[point])
        assert shown(widget.fileSizeLabel) == widget.fileSizeLabel.text(), (
            f"{point}에서 정보 글이 잘렸다: {shown(widget.fileSizeLabel)!r}"
        )

    def test_only_the_path_shrinks_as_the_card_narrows(self, qapp) -> None:
        """폭을 계속 줄일 때 저장 위치만 짧아지고 상태 글 · 정보 글은 잘리지 않아야 한다.

        받는 중 · 긴 저장 위치 카드, 폭 T+600 → T+300 → T+경로÷2 → T+40 → T
        -> 상태 글 · 정보 글 표시 == 원문, 저장 위치 폭은 단조 감소하고 처음 > 끝
        """
        widget = _running_widget(qapp)
        threshold = self._threshold(widget)
        half_path = widget.directoryLabel.sizeHint().width() // 2
        widths = (
            threshold + 600,
            threshold + 300,
            threshold + half_path,
            threshold + 40,
            threshold,
        )
        path_widths = []
        for width in widths:
            resize_to(widget, width)
            assert shown(widget.statusLabel) == widget.statusLabel.text(), (
                f"폭 {width}px에서 상태 글이 잘렸다"
            )
            assert shown(widget.fileSizeLabel) == widget.fileSizeLabel.text(), (
                f"폭 {width}px에서 정보 글이 잘렸다"
            )
            path_widths.append(widget.directoryLabel.width())
        assert all(a >= b for a, b in zip(path_widths, path_widths[1:])), (
            f"저장 위치 폭이 단조 감소하지 않는다: {path_widths}"
        )
        assert path_widths[0] > path_widths[-1], f"좁혀도 저장 위치가 줄지 않았다: {path_widths}"

    def test_fit_is_decided_on_the_very_first_show(self, qapp) -> None:
        """이미 보이는 컨테이너에 받는 중 카드가 들어오면 첫 표시부터 3줄이 그 폭에 맞춰져야 한다.

        받는 중 · 긴 저장 위치 카드, 컨테이너 폭 T + 저장 위치 자연 폭 + 200(견본 카드로 유도),
        카드를 보이는 컨테이너에 더함
        -> 카드 폭 == 컨테이너 폭, statusLabel · fileSizeLabel 표시 문자열 == 원문,
        directoryLabel 표시 문자열 == "T1-vs-GEN-full-set-highlights-and-interviews"

        첫 표시에서 카드의 resizeEvent는 자식 프레임 레이아웃이 잡히기 **전에** 도착해 3줄
        폭이 아직 틀린 값이다(#245) — 거기서 끝내면 자리가 넉넉해도 저장 위치가 0폭으로 남고
        창을 흔들어야 고쳐진다.
        """
        probe = _running_widget(qapp)  # 폭을 알아내기 위한 견본
        threshold = self._threshold(probe) + probe.directoryLabel.sizeHint().width() + 200
        height = probe.sizeHint().height()
        probe.hide()
        box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)
        box.resize(threshold, height)
        box.show()
        QApplication.processEvents()
        item = _make_item(path=LONG_PATH)
        widget = ContentItemWidget(item, 0)
        widget.loadRepresentations()
        item.downloadState = DownloadState.RUNNING
        item.resolution = "1080"
        item.download_progress, item.download_speed, item.download_remain_time = (
            42,
            "12.3 MB/s",
            "00:03:21",
        )
        widget.setData(item, 0)
        column.addWidget(widget)  # 보이는 컨테이너가 카드를 (큐로) 처음 보인다
        for _ in range(3):
            QApplication.processEvents()
        try:
            assert widget.isVisible() and widget.width() == box.width(), (
                "전제: 카드가 컨테이너 폭으로 보인다"
            )
            assert shown(widget.statusLabel) == widget.statusLabel.text(), (
                "첫 표시에서 상태 글이 잘렸다 — 창을 흔들어야 고쳐지는 결함"
            )
            assert shown(widget.fileSizeLabel) == widget.fileSizeLabel.text()
            assert shown(widget.directoryLabel) == "T1-vs-GEN-full-set-highlights-and-interviews", (
                "첫 표시에서 자리가 넉넉한데 저장 위치가 줄어 있다 — 창을 흔들어야 고쳐지는 결함"
            )
        finally:
            box.hide()
            box.deleteLater()

    def test_path_text_recovers_when_the_card_widens_again(self, qapp) -> None:
        """T까지 줄였다가 넓히면 저장 위치가 마지막 폴더 이름 전부로 돌아와야 한다.

        받는 중 · 긴 저장 위치 카드, 폭 T → 1600
        -> directoryLabel 표시 문자열 == "T1-vs-GEN-full-set-highlights-and-interviews"
        """
        widget = _running_widget(qapp)
        resize_to(widget, self._threshold(widget))
        label = widget.directoryLabel
        assert not label.isVisible() or shown(label) != label.text(), (
            "전제: 좁은 폭에서 저장 위치가 줄었다(이름이 숨거나 줄어듦 — #554)"
        )
        _at_width(widget, 1600)
        QApplication.processEvents()
        assert shown(widget.directoryLabel) == "T1-vs-GEN-full-set-highlights-and-interviews", (
            f"넓혔는데 저장 위치가 회복되지 않았다: {shown(widget.directoryLabel)!r}"
        )


class TestPathVisibilityRule:
    """저장 위치는 **기본 위치와 다를 때만** 3줄에 보인다 — 상태와 무관하다(#245 · #550).

    같은 값을 카드마다 되풀이하는 것은 정보가 아니다. 저장 위치를 바꾸는 길은 카드 편집 창이라
    대기 카드에 클릭 대상을 남길 까닭이 없어졌다.
    """

    @pytest.mark.parametrize(
        "state",
        (
            DownloadState.WAITING,
            DownloadState.RUNNING,
            DownloadState.PAUSED,
            DownloadState.FINISHED,
            DownloadState.FAILED,
        ),
    )
    def test_path_is_hidden_when_it_matches_global(self, qapp, state) -> None:
        """저장 위치가 기본 위치와 같으면 어느 상태에서도 3줄에 저장 위치가 없어야 한다.

        download_path == 기본 위치("C:/dl"), 상태 다섯
        -> directoryLabel · pathIcon 숨김
        """
        widget = _make_widget(qapp)
        _set_state(widget, state)
        _at_width(widget, 900)
        assert widget.directoryLabel.isHidden() and widget.pathIcon.isHidden(), (
            f"{state.name}: 기본 위치와 같은 저장 위치가 반복 표시된다"
        )

    @pytest.mark.parametrize(
        "state", (DownloadState.WAITING, DownloadState.RUNNING, DownloadState.FINISHED)
    )
    def test_path_shown_when_it_differs(self, qapp, state) -> None:
        """저장 위치가 기본 위치와 다르면 3줄에 보이고 정보 글 왼쪽에 고정 간격으로 붙어야 한다.

        download_path="D:/다른/폴더", 대기 · 받는 중 · 완료, 창 폭 900
        -> directoryLabel 보임, directoryLabel ↔ fileSizeLabel 간격 == 4
        """
        widget = _make_widget(qapp, path="D:/다른/폴더")
        _set_state(widget, state)
        _at_width(widget, 900)
        assert widget.directoryLabel.isVisible() and widget.pathIcon.isVisible()
        assert _gap(widget, widget.directoryLabel, widget.fileSizeLabel) == FIXED_SPACING


class TestPathAbbreviationAndTooltip:
    """카드의 저장 위치 글은 마지막 폴더 이름, 전문은 툴팁(#550). 축약 규칙
    (`abbreviate_path`)은 헤더바 · 편집 창이 쓴다 — app/widgets/widget.py 참고."""

    HOME = "C:/Users/me"

    @staticmethod
    def _abbr(path, home) -> str:
        from app.widgets.widget import abbreviate_path

        return abbreviate_path(path, home=home)

    @pytest.mark.parametrize(
        "path,expected",
        [
            ("C:/Users/me/Downloads/vod/lck/2026", "~/…/2026"),  # 홈 아래, 3단계 이상 → 접음
            ("C:/Users/me/Downloads/vod", "~/Downloads/vod"),  # 홈 아래, 2단계 → 전부
            ("C:/Users/me/Downloads", "~/Downloads"),
            ("C:/Users/me", "~"),
            ("D:/vod/lck/2026/finals", "D:/…/finals"),  # 홈 밖(드라이브), 접음
            ("D:/vod/lck", "D:/vod/lck"),  # 홈 밖, 2단계 → 전부
            ("C:\\Users\\me\\Videos\\a\\b", "~/…/b"),  # 역슬래시 입력도 /로 통일
            ("/srv/media/vod/2026", "/…/2026"),  # POSIX 루트
            ("", ""),
        ],
    )
    def test_abbreviation_rule(self, path, expected) -> None:
        assert self._abbr(path, home=self.HOME) == expected

    def test_label_shows_the_last_folder_and_tooltip_carries_the_full_path(self, qapp) -> None:
        """저장 위치가 기본 위치와 다르면 글은 마지막 폴더 이름, 툴팁은 전체 경로여야 한다.

        download_path="D:/vod/lck/2026/finals"
        -> directoryLabel.text() == "finals", toolTip() == "D:/vod/lck/2026/finals"
        """
        widget = _make_widget(qapp, path="D:/vod/lck/2026/finals")
        _at_width(widget, 900)
        assert widget.directoryLabel.text() == "finals"
        assert widget.directoryLabel.toolTip() == "D:/vod/lck/2026/finals"

    def test_long_path_does_not_break_the_row(self, qapp) -> None:
        """긴 저장 위치도 3줄을 넘치게 하지 않아야 한다 — 우측 끝선 유지.

        download_path 깊이 10, 카드 폭 640, setHovered(True)
        -> fileSizeLabel 오른쪽 끝 == deleteButton 오른쪽 끝, 저장 위치 이름이 보이면(폭이 모자라면 아이콘으로
        접힌다 — #554) directoryLabel 오른쪽 끝 < fileSizeLabel 왼쪽 끝
        """
        widget = _make_widget(qapp, path="D:/아주/긴/경로/구조/를/가진/폴더/이름/마지막")
        resize_to(widget, 640)
        _hover(widget)
        shown(widget.fileSizeLabel)
        assert _right(widget, widget.fileSizeLabel) == _right(widget, widget.deleteButton)
        if widget.directoryLabel.isVisible():  # 보이는 라벨의 기하만 의미가 있다
            assert _right(widget, widget.directoryLabel) < _x(widget, widget.fileSizeLabel)

    @pytest.mark.parametrize(
        "path,expected",
        [
            ("D:/archive-2026/x", ("D:/archive-2026/x", "D:/…/", "x")),
            ("D:/x", ("D:/x", "D:/", "x")),
            ("D:/a/b/c/x", ("D:/…/x", "D:/…/", "x")),
            ("C:/Users/me/x", ("~/x", "~/", "x")),
            ("C:/Users/me", ("~", "", "~")),
            ("/x", ("/x", "/", "x")),
            ("/srv/x", ("/srv/x", "/…/", "x")),
            ("/srv/media/x", ("/…/x", "/…/", "x")),
        ],
    )
    def test_display_parts(self, path, expected) -> None:
        from app.widgets.widget import path_display_parts

        assert path_display_parts(path, home="C:/Users/me") == expected


class TestStateSwapKeepsCardHeight:
    """상태가 바뀌면 3줄 내용 · 버튼이 바뀌지만 **카드 높이와 줄 위치는 안 변한다**
    (#245 확정 — 목록이 들썩이면 안 된다). 버튼 교체 · 정보 글 숨김(실패) · 진행바 표시
    (받는 중 · 일시정지)가 전부 일어나는 **다섯 상태**를 순회한다."""

    STATES = (
        DownloadState.WAITING,
        DownloadState.RUNNING,
        DownloadState.PAUSED,
        DownloadState.FINISHED,
        DownloadState.FAILED,
    )

    def test_card_height_is_identical_across_all_states(self, qapp) -> None:
        """대기 · 받는 중 · 일시정지 · 완료 · 실패에서 카드 높이가 같아야 한다.

        창 폭 900, 상태 다섯
        -> widget.height()가 모두 같다
        """
        widget = _make_widget(qapp)
        heights = {}
        for state in self.STATES:
            _set_state(widget, state)
            _at_width(widget, 900)
            heights[state] = widget.height()
        assert len(set(heights.values())) == 1, (
            f"상태에 따라 카드 높이가 변한다: {heights} — 목록이 들썩인다"
        )

    def test_row3_stays_at_the_same_y_across_states(self, qapp) -> None:
        """상태 다섯에서 3줄과 상태 글의 세로 위치가 같아야 한다.

        창 폭 900, 상태 다섯
        -> (statusRow y, statusLabel y)가 모두 같다(카드 면 좌표)
        """
        widget = _make_widget(qapp)
        ys = {}
        for state in self.STATES:
            _set_state(widget, state)
            _at_width(widget, 900)
            ys[state] = (_y(widget, widget.statusRow), _y(widget, widget.statusLabel))
        assert len(set(ys.values())) == 1, f"상태에 따라 3줄 세로 위치가 흔들린다: {ys}"


class TestLabelsDoNotGrow:
    """텍스트 라벨들은 창이 더 넓어져도 자기 텍스트 폭에 머물러야 한다.

    간격(edge-to-edge)만 재면 못 잡는 흩어짐 모드 — 라벨이 stretch/
    Expanding을 받으면 이웃 간격은 고정값 그대로인데 라벨 폭이 부풀어
    좌측 정렬 텍스트 뒤로 빈 공간이 생긴다(실기에서 실측된 바로 그 모드).
    여유가 생긴 900px의 폭(자연 텍스트 폭)과 1600px의 폭이 같아야 한다.
    """

    LABELS = ("statusLabel", "fileSizeLabel")

    def test_labels_do_not_grow_when_the_window_widens(self, qapp) -> None:
        # 상태 텍스트가 실제로 보이는 진행 상태로 잰다(#245 슬롯)
        widget = _make_widget(qapp)
        _set_state(widget, DownloadState.RUNNING)
        _at_width(widget, 900)
        at_900 = {name: getattr(widget, name).width() for name in self.LABELS}
        _at_width(widget, 1600)
        for name in self.LABELS:
            width_1600 = getattr(widget, name).width()
            assert width_1600 == at_900[name], (
                f"{name} 폭이 900px→1600px에서 {at_900[name]}→{width_1600}으로 변한다 — "
                "라벨이 남는 공간을 나눠 먹고 있다"
            )


class TestProgressBarOnlyWithProgress:
    """하단 진행바는 **진행분이 있을 때**(진행·일시정지) 보인다(#245 정정 —
    "진행 중일 때만"이 아니다. 일시정지는 멈췄을 뿐 받은 양이 있고 그
    양을 바가 계속 보여줘야 한다; 색만 muted로 바뀐다). 대기의 빈 막대는
    정보가 없고 자리만 먹는다. 완료 · 실패는 끝까지 찬 상태 색 막대다(#554).
    보일 때는 카드 아래 가장자리 근처 전체 폭이고, 양끝은 카드 곡률로 잘려 있다."""

    def test_bar_hidden_while_waiting(self, qapp) -> None:
        """대기 카드면 진행률 표시줄이 숨어야 한다."""
        widget = _make_widget(qapp)
        _set_state(widget, DownloadState.WAITING, 0)
        _at_width(widget, 900)
        assert not widget.progressBar.isVisible()

    @pytest.mark.parametrize("state", (DownloadState.FINISHED, DownloadState.FAILED))
    def test_bar_is_full_when_finished_or_failed(self, qapp, state) -> None:
        """완료 · 실패 카드면 진행률 표시줄이 끝까지 차 있어야 한다 (#554).

        완료(진행 100) · 실패(진행 0) -> 보임, value == maximum == 100
        """
        widget = _make_widget(qapp)
        _set_state(widget, state, 100 if state == DownloadState.FINISHED else 0)
        _at_width(widget, 900)
        bar = widget.progressBar
        assert bar.isVisible()
        assert bar.value() == bar.maximum() == 100

    @pytest.mark.parametrize(
        "state,bar_state",
        [
            (DownloadState.RUNNING, "running"),
            (DownloadState.PAUSED, "paused"),
        ],
    )
    def test_bar_visible_and_full_width_with_progress(self, qapp, state, bar_state) -> None:
        widget = _make_widget(qapp)
        _set_state(widget, state)
        _at_width(widget, 900)
        assert widget.progressBar.isVisible(), f"{state}에서 진행바가 안 보인다"
        assert widget.progressBar.property("state") == bar_state, (
            f"{state}의 진행바 색 규칙이 {widget.progressBar.property('state')!r} — "
            f"{bar_state!r}이어야 한다(일시정지는 muted로 '돌고 있지 않다'를 알린다)"
        )
        assert widget.progressBar.value() == 42
        # 전체 폭(테두리 안쪽 1px 여백 허용) + 카드 바닥에 붙어 있다
        frame = widget.contentFrame
        assert widget.progressBar.width() >= frame.width() - 4
        bar_bottom = widget.progressBar.mapTo(frame, widget.progressBar.rect().bottomLeft()).y()
        assert frame.height() - bar_bottom <= 3, (
            f"진행바가 카드 바닥에서 {frame.height() - bar_bottom}px 떠 있다"
        )

    @pytest.mark.parametrize("width", WIDTHS)
    def test_bar_is_clipped_to_the_card_corners(self, qapp, width) -> None:
        """막대 양끝이 카드의 둥근 모서리 바깥으로 튀어나오지 않는다.

        바 자체의 QSS border-radius는 높이(4px)에 눌려 카드 반지름(cardRadius)을
        못 따라가 실기 렌더에서 바닥 모서리 밖에 트랙·진행색 조각이 남았다
        (#245 오너 실기 제보 — 카드 왼쪽 바깥의 짧은 선). 그래서 카드
        곡률 마스크를 건다. 마스크 안/밖을 순수 기하로 잰다(폰트 무관).
        """
        widget = _make_widget(qapp)
        _set_state(widget, DownloadState.RUNNING)
        _at_width(widget, width)
        bar = widget.progressBar
        mask = bar.mask()
        assert not mask.isEmpty(), "진행바에 카드 곡률 마스크가 없다"
        w, h = bar.width(), bar.height()

        # 바닥 모서리 픽셀은 잘려 있고(카드 몸통 밖), 가운데는 살아 있다
        assert not mask.contains(QPoint(0, h - 1)), (
            f"폭 {width}px: 바 왼쪽 아래 모서리가 카드 곡률 밖에 남는다"
        )
        assert not mask.contains(QPoint(w - 1, h - 1)), (
            f"폭 {width}px: 바 오른쪽 아래 모서리가 카드 곡률 밖에 남는다"
        )
        assert mask.contains(QPoint(w // 2, h - 1))
        assert mask.contains(QPoint(w // 2, 0))
        # 마스크가 바 안쪽 대부분은 살려 둔다 — 잘려 나가는 것은 모서리뿐
        radius = theme.METRICS["cardRadius"]
        assert mask.contains(QPoint(radius, h - 1)) and mask.contains(QPoint(w - 1 - radius, h - 1))
