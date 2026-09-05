"""카드 3행 해상도 pill 게이트 (#244 3행 정리).

- 같은 높이 트랙이 둘인 매니페스트 → pill은 높이당 하나 (core/api/representations.py)
- 해상도 인라인 확장 — 접힘/펼침, 고르면 접힘, 기하 복원, 줄바꿈 임계 T, 한 번에 하나
- 3행 접힘 순서 한 방향(① 경로 줄임 → ② 아이콘 → ③ 해상도 접힘, 역방향 없음)
- 늦게 온 크기 조회가 유저 선택을 덮지 않는다(자동 선택은 안 골랐을 때만)
"""

import time

import pytest
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

import main as main_module
import theme
from app.viewmodels.data import ContentItem
from app.viewmodels.model import ContentListModel
from app.widgets.view import ContentListView
from app.widgets.widget import ContentItemWidget
from core.api.dash import parse_dash_manifest
from core.models.download_state import DownloadState
from tests.unit.card_helpers import drop_new_top_levels, hold_style, shown, snapshot_top_levels


@pytest.fixture(autouse=True)
def _apply_production_qss(qapp):
    """실제 전역 QSS를 태운 상태에서 잰다(scope=function 유지 — test_widget_theme 참고)."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))


@pytest.fixture(autouse=True)
def _drop_windows(qapp):
    """이 파일의 테스트가 띄운 창을 테스트 끝에 확실히 파괴한다(card_helpers.drop_new_top_levels)."""
    before = snapshot_top_levels()
    yield
    drop_new_top_levels(before)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    class _FailingSession:
        def head(self, *a, **k):
            raise RuntimeError("network disabled in tests")

        def get(self, *a, **k):
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("app.widgets.widget.get_thread_session", lambda: _FailingSession())
    monkeypatch.setattr("app.widgets.widget._global_download_path", "C:/dl")


def make_waiting_widget(reps, width=900, path="C:/dl") -> ContentItemWidget:
    """대기 카드 — `reps`는 파서가 돌려주는 `[해상도, url]` 목록(또는 그 튜플)."""
    item = ContentItem(
        "https://chzzk.naver.com/video/1",
        {"title": "제목", "category": "", "channelName": "채널", "createdDate": "", "duration": 3600},
        [list(r) for r in reps], None, "", path, "video", None,
    )
    item.downloadState = DownloadState.WAITING
    item.total_size = "711.02 MB"
    widget = ContentItemWidget(item, 0)
    widget.addRepresentationButtons()
    widget.setData(item, 0)
    widget.resize(width, widget.sizeHint().height())
    widget.show()
    QApplication.processEvents()
    return widget


_DUPLICATE_HEIGHTS = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011">
  <Period><AdaptationSet mimeType="video/mp4">
    <Representation id="a" width="1920" height="1080" bandwidth="5000000"><BaseURL>https://v.invalid/1080_low.mp4</BaseURL></Representation>
    <Representation id="b" width="1920" height="1080" bandwidth="6000000"><BaseURL>https://v.invalid/1080_high.mp4</BaseURL></Representation>
    <Representation id="c" width="1280" height="720" bandwidth="3000000"><BaseURL>https://v.invalid/720.mp4</BaseURL></Representation>
    <Representation id="d" width="1280" height="720" bandwidth="2000000"><BaseURL>https://v.invalid/720_low.mp4</BaseURL></Representation>
  </AdaptationSet></Period>
</MPD>"""


class TestSameHeightTracksMakeOnePill:
    """같은 높이 트랙이 둘인 매니페스트에서 pill이 하나만 생긴다 — [3] 게이트."""

    def test_one_pill_per_height_from_a_manifest_with_duplicate_heights(self, qapp):
        reps, _, _ = parse_dash_manifest(_DUPLICATE_HEIGHTS)
        widget = make_waiting_widget(reps)
        texts = [b.text() for b in widget.buttons]
        assert texts == ["1080p", "720p"], f"높이당 pill 하나여야 한다: {texts}"
        # 남은 것은 비트레이트가 높은 트랙 — pill이 가리키는 URL로 확인
        assert widget.item.unique_reps[0][1] == "https://v.invalid/1080_high.mp4"
        assert widget.item.unique_reps[1][1] == "https://v.invalid/720.mp4"


# ======================= [1]·[P-3] 해상도 표시 — 폭에 반응 =======================
#
# 들어가면:   1080p  720p  480p  360p  144p ····· 경로 ····· 크기   (클릭 한 번에 고른다)
# 안 들어가면: [1080p ▾] ····· 경로 ····· 크기                      (누르면 그 자리에서 펼침)
# 펼침:       1080p  720p  480p  360p  144p        ← 경로·크기는 잠깐 숨는다, 모자라면 줄바꿈
# 고른 뒤:    [720p ▾] ····· 경로 ····· 크기
#
# 판정은 content/widget.py::_layoutRowThree 한 곳 — 경로가 텍스트→아이콘으로 바뀌는
# 것과 같은 방식(3행 폭 − 자연 폭들). 절대 px 임계값은 없다 — 아래 T는 테스트가
# 구성 요소를 독립 합산해 유도한다([D]의 T 유도 그대로).
#
# 고장 주입(확인됨):
# - _pillsFit의 `need <= row_width`를 뒤집으면 TestResponsiveMode가 잡는다
# - _packPills의 "비게 된 추가 행은 없앤다" 루프를 건너뛰면 TestGeometryRestoresAfterCollapse
#   ·TestWrapThreshold가 잡는다(접힌 뒤 카드가 늘어난 채 남는다)

FIVE = (1080, 720, 480, 360, 144)
FIXED_SPACING = 4
LONG_PATH = "D:/vod/archive/2026/summer/finals/T1-vs-GEN-full-set-highlights-and-interviews"


def _visible_pills(widget):
    return [b.text() for b in widget.buttons if b.isVisible()]


def _pump():
    for _ in range(3):
        QApplication.processEvents()


def make_boxed(reps=FIVE, width=900, path=LONG_PATH):
    """카드를 목록처럼 세로 레이아웃 컨테이너에 넣는다 — 카드 높이는 컨테이너가 sizeHint대로 준다.

    최상위 카드를 직접 resize하면 접혀도 높이가 저절로 줄지 않아 "원래 높이로
    돌아오는가"를 잴 수 없다. 실제 목록(ContentListView 컨테이너)과 같은 조건이다.
    """
    box = QWidget()
    column = QVBoxLayout(box)
    column.setContentsMargins(0, 0, 0, 0)
    column.setSpacing(0)
    item = ContentItem(
        "https://chzzk.naver.com/video/1",
        {"title": "제목", "category": "", "channelName": "채널", "createdDate": "", "duration": 3600},
        [[r, f"u{r}"] for r in reps], None, "", path, "video", None,
    )
    item.downloadState = DownloadState.WAITING
    item.total_size = "711.02 MB"
    widget = ContentItemWidget(item, 0)
    widget.addRepresentationButtons()
    widget.setData(item, 0)
    column.addWidget(widget)
    column.addStretch(1)
    box.resize(width, 400)
    box.show()
    _pump()
    assert widget.width() == width, f"전제: 카드가 컨테이너 폭 {width}px을 받았다(실제 {widget.width()}px)"
    assert widget.pillMode() == "all", "전제: 900px에서는 pill 전부가 들어간다"
    return box, widget


def resize_box(box, widget, width):
    """컨테이너 폭을 바꾸고 카드가 그 폭을 실제로 받았는지 단언한다(클램프 = 즉시 실패)."""
    box.resize(width, 400)
    _pump()
    assert widget.width() == width, (
        f"요청 폭 {width}px인데 실제 {widget.width()}px — 최소폭({widget.minimumSizeHint().width()}px)에 클램프됐다"
    )


def _offset(widget) -> int:
    """카드 폭 ↔ 3행 폭 차(썸네일·패딩·테두리) — 순수 기하 실측."""
    return widget.width() - widget.resolutionLayout.geometry().width()


def fit_threshold(widget) -> int:
    """pill 전부 + 경로 아이콘 + 크기가 3행 한 줄에 **딱** 들어가는 카드 폭 T — 독립 합산.

    offset + pill 자연 폭 합(전부 보이는 모드에서의 sizeHint = ▾ 없음) + 크기 확보 폭 +
    아이콘 폭 + 간격 × (pill 개수 + 1)(pill 사이 n−1, 아이콘 앞 1, 크기 앞 1).
    제품의 판정 함수(_pillsFit)는 부르지 않는다.
    """
    assert widget.pillMode() == "all", "T는 전부 보이는 모드에서 잰다(▾ 없는 자연 폭)"
    pills = sum(b.sizeHint().width() for b in widget.buttons)
    size = max(widget.fileSizeLabel.minimumWidth(), widget.fileSizeLabel.sizeHint().width())
    icon = widget.pathIconButton.minimumWidth()
    return _offset(widget) + pills + size + icon + (len(widget.buttons) + 1) * FIXED_SPACING


def expanded_threshold(widget) -> int:
    """펼친 pill 전부가 3행 한 줄에 **딱** 들어가는 카드 폭 — offset + pill 자연 폭 합 +
    간격 × pill 개수(pill 사이 n−1개 + 행 끝 세로 버팀목 앞 1개)."""
    assert widget.pillMode() == "all"
    pills = sum(b.sizeHint().width() for b in widget.buttons)
    return _offset(widget) + pills + len(widget.buttons) * FIXED_SPACING


def _rows(widget) -> int:
    """보이는 pill이 차지한 줄 수 — 서로 다른 y의 개수."""
    return len({b.y() for b in widget.buttons if b.isVisible()})


def _row3_snapshot(widget) -> dict:
    """3행 기하 스냅샷 — 접힘 전후 비교용(보이는 것만 잰다)."""
    selected = next(b for b in widget.buttons if b.isSelected())
    return {
        "mode": widget.pillMode(),
        "card_h": widget.height(),
        "hint_h": widget.sizeHint().height(),
        "row3": widget.resolutionLayout.geometry(),
        "pill_xy": (selected.x(), selected.y(), selected.height()),
        "size": (widget.fileSizeLabel.isVisible(), widget.fileSizeLabel.geometry()),
        # 경로는 선택 pill 폭이 바뀌면 그만큼 x·폭이 달라지는 것이 정상(남는 폭을 다 받는다) —
        # 우측 끝·y·높이만 잰다. 같은 pill을 고르는 왕복에서는 이것도 완전히 같다.
        "path": (widget.directoryLabel.isVisible(), widget.directoryLabel.geometry().right(),
                 widget.directoryLabel.y(), widget.directoryLabel.height()),
        "icon": (widget.pathIconButton.isVisible(), widget.pathIconButton.geometry()),
        "thumb": widget.thumbnailLabel.size(),
        "extra_rows": widget.contentLayout.count(),
    }


class TestResponsiveMode:
    """들어가면 전부, 안 들어가면 접힘 — 임계 폭 T±ε, 되먹임 없음, 전환 순간 선택 유지."""

    def test_all_pills_just_above_t_and_collapsed_just_below(self, qapp):
        box, widget = make_boxed()
        threshold = fit_threshold(widget)
        resize_box(box, widget, threshold + 1)
        assert widget.pillMode() == "all" and _visible_pills(widget) == [f"{r}p" for r in FIVE]
        assert not any(b.hasCaret() for b in widget.buttons)
        assert widget.fileSizeLabel.isVisible() and widget.pathIconButton.isVisible(), "T+1: 크기·경로 아이콘이 함께 있다"
        resize_box(box, widget, threshold - 1)
        assert widget.pillMode() == "collapsed" and _visible_pills(widget) == ["1080p"], "T−1에서 접혀야 한다"
        assert widget.buttons[0].hasCaret()
        assert widget.fileSizeLabel.isVisible(), "접혀도 크기는 그대로 보인다"
        assert widget.directoryLabel.isVisible() or widget.pathIconButton.isVisible(), "접혀도 경로 진입점은 남는다"

    def test_exactly_at_t_everything_fits(self, qapp):
        box, widget = make_boxed()
        threshold = fit_threshold(widget)
        resize_box(box, widget, threshold)
        assert widget.pillMode() == "all"
        assert widget.pathIconButton.isVisible() and not widget.directoryLabel.isVisible(), "T에서 경로는 아이콘 한 칸"

    def test_widening_shows_all_pills_again_without_feedback(self, qapp):
        box, widget = make_boxed()
        threshold = fit_threshold(widget)
        for _ in range(3):
            resize_box(box, widget, threshold - 1)
            assert widget.pillMode() == "collapsed"
            resize_box(box, widget, threshold + 1)
            assert widget.pillMode() == "all" and _visible_pills(widget) == [f"{r}p" for r in FIVE], (
                "넓혔는데 다시 펼쳐지지 않았다 — 되먹임 루프"
            )

    def test_selection_survives_every_transition(self, qapp):
        """⚠️ 제일 중요 — 전부↔접힘↔펼침을 오가도 고른 해상도가 바뀌지 않는다."""
        box, widget = make_boxed()
        threshold = fit_threshold(widget)
        widget.buttons[2].click()  # 전부 보이는 폭에서 480p를 고른다
        _pump()
        assert widget.pillMode() == "all" and str(widget.item.resolution) == "480"
        resize_box(box, widget, threshold - 1)
        assert widget.pillMode() == "collapsed" and _visible_pills(widget) == ["480p"], "접히며 선택이 바뀌었다"
        assert str(widget.item.resolution) == "480" and widget.item.base_url == "u480"
        widget.buttons[2].click()  # 접힌 pill을 눌러 펼친다
        _pump()
        assert widget.pillMode() == "expanded" and widget.buttons[2].isSelected()
        resize_box(box, widget, threshold + 1)
        assert widget.pillMode() == "all" and [b.isSelected() for b in widget.buttons] == [False, False, True, False, False]
        assert str(widget.item.resolution) == "480" and widget.item.base_url == "u480"

    def test_widening_while_expanded_drops_the_expansion(self, qapp):
        """정의: 펼친 채 넓혀 전부 들어가게 되면 펼침은 풀린다(전부 보이는 모드, 경로·크기
        복귀, expandedChanged(False)). 펼침은 기억되지 않는다 — 다시 좁히면 접힘이다."""
        box, widget = make_boxed()
        threshold = fit_threshold(widget)
        events = []
        widget.expandedChanged.connect(events.append)
        resize_box(box, widget, threshold - 1)
        widget.buttons[0].click()
        _pump()
        assert widget.isExpanded() and events == [True]
        assert not widget.fileSizeLabel.isVisible()
        resize_box(box, widget, threshold + 1)
        assert widget.pillMode() == "all" and not widget.isExpanded() and events == [True, False]
        assert widget.fileSizeLabel.isVisible() and widget.pathIconButton.isVisible(), "펼침이 풀리면 경로·크기가 돌아온다"
        resize_box(box, widget, threshold - 1)
        assert widget.pillMode() == "collapsed" and not widget.isExpanded(), "펼침은 기억되지 않는다"
        assert _visible_pills(widget) == ["1080p"]

    def test_card_height_is_the_same_in_every_mode(self, qapp):
        """전부 / 접힘 / 펼침(한 줄)에서 카드 높이·3행 y가 같다 — 모드가 바뀐다고 목록이 들썩이면 안 된다."""
        box, widget = make_boxed()
        threshold = fit_threshold(widget)
        one_row = expanded_threshold(widget)
        resize_box(box, widget, threshold + 1)
        h_all, y_all = widget.height(), widget.buttons[0].y()
        resize_box(box, widget, threshold - 1)
        assert threshold - 1 >= one_row, "전제: T−1에서 펼친 pill이 한 줄에 들어간다"
        h_collapsed, y_collapsed = widget.height(), widget.buttons[0].y()
        widget.buttons[0].click()
        _pump()
        assert widget.pillMode() == "expanded" and _rows(widget) == 1
        h_expanded, y_expanded = widget.height(), widget.buttons[0].y()
        assert h_all == h_collapsed == h_expanded, f"모드별 카드 높이가 다르다: {h_all}/{h_collapsed}/{h_expanded}"
        assert y_all == y_collapsed == y_expanded, f"모드별 3행 y가 다르다: {y_all}/{y_collapsed}/{y_expanded}"

    def test_fewer_pills_fit_where_more_do_not(self, qapp):
        """임계는 pill 개수를 따라 움직인다 — 5개가 접히는 폭에서 2개(클립)는 전부 보인다."""
        box5, five = make_boxed()
        threshold = fit_threshold(five)
        resize_box(box5, five, threshold - 1)
        assert five.pillMode() == "collapsed"
        box2, two = make_boxed(reps=(1080, 720))
        resize_box(box2, two, threshold - 1)
        assert two.pillMode() == "all" and _visible_pills(two) == ["1080p", "720p"]


class TestCollapsedAndExpandedStates:
    def _collapsed(self, qapp):
        box, widget = make_boxed()
        resize_box(box, widget, fit_threshold(widget) - 1)
        assert widget.pillMode() == "collapsed"
        return box, widget

    def test_collapsed_shows_only_the_selected_pill_with_a_caret(self, qapp):
        box, widget = self._collapsed(qapp)
        assert _visible_pills(widget) == ["1080p"]
        pill = widget.buttons[0]
        assert pill.isSelected() and pill.hasCaret() and pill.isEnabled(), "접힌 선택 pill은 ▾가 있고 눌 수 있어야 한다"
        assert widget.fileSizeLabel.isVisible()

    def test_expanding_shows_all_pills_and_hides_path_and_size(self, qapp):
        box, widget = self._collapsed(qapp)
        widget.buttons[0].click()  # 접힌 pill을 누른다 = 펼치기
        _pump()
        assert widget.isExpanded() and widget.pillMode() == "expanded"
        assert _visible_pills(widget) == [f"{r}p" for r in FIVE]
        assert not any(b.hasCaret() for b in widget.buttons), "펼치면 ▾는 사라진다"
        assert not widget.fileSizeLabel.isVisible(), "펼치는 동안 크기는 숨는다"
        assert not widget.directoryLabel.isVisible() and not widget.pathIconButton.isVisible(), "펼치는 동안 경로는 숨는다"
        assert widget.buttons[0].isSelected(), "펼쳐도 선택 표시는 유지된다"

    def test_picking_a_pill_selects_it_and_collapses(self, qapp):
        box, widget = self._collapsed(qapp)
        events = []
        widget.expandedChanged.connect(events.append)
        widget.buttons[0].click()
        _pump()
        widget.buttons[2].click()  # 480p
        _pump()
        assert not widget.isExpanded() and events == [True, False]
        assert _visible_pills(widget) == ["480p"] and widget.buttons[2].hasCaret()
        assert str(widget.item.resolution) == "480" and widget.item.base_url == "u480"
        assert widget.fileSizeLabel.isVisible(), "접히면 크기가 돌아온다"
        assert widget.directoryLabel.isVisible() or widget.pathIconButton.isVisible(), "접히면 경로가 돌아온다"

    def test_clicking_the_selected_pill_while_expanded_only_collapses(self, qapp):
        box, widget = self._collapsed(qapp)
        widget.buttons[0].click()
        _pump()
        widget.buttons[0].click()  # 이미 선택된 1080p
        _pump()
        assert not widget.isExpanded() and _visible_pills(widget) == ["1080p"]
        assert str(widget.item.resolution) == "1080"

    def test_set_expanded_is_a_no_op_when_everything_fits(self, qapp):
        box, widget = make_boxed()
        widget.setExpanded(True)
        _pump()
        assert not widget.isExpanded() and widget.pillMode() == "all", "전부 보이는데 펼칠 것은 없다"

    def test_starting_the_download_collapses_and_hides_the_pills(self, qapp):
        box, widget = self._collapsed(qapp)
        widget.setExpanded(True)
        _pump()
        widget.item.downloadState = DownloadState.RUNNING
        widget.item.download_progress, widget.item.download_speed, widget.item.download_remain_time = 1, "1 MB/s", "00:10:00"
        widget.setData(widget.item, 0)
        _pump()
        assert not widget.isExpanded() and widget.pillMode() == "hidden" and _visible_pills(widget) == []
        assert widget.statusLabel.isVisible() and widget.fileSizeLabel.isVisible()
        assert widget.contentLayout.count() == 3, "추가 행이 남아 있다"
        widget.setExpanded(True)  # 대기가 아니면 펼쳐지지 않는다
        assert not widget.isExpanded()


class TestGeometryRestoresAfterCollapse:
    """펼침 → 고름 → 접힘 뒤 3행 기하가 펼치기 전과 같다 — 한 줄로 펼쳐지는 폭과 줄바꿈 폭 둘 다."""

    def _round_trip(self, qapp, width, pick):
        box, widget = make_boxed(width=900)
        target = width(fit_threshold(widget), expanded_threshold(widget))
        resize_box(box, widget, target)
        assert widget.pillMode() == "collapsed", "전제: 접힌 폭"
        before = _row3_snapshot(widget)
        widget.buttons[0].click()  # 펼침
        _pump()
        expanded_h = widget.height()
        widget.buttons[pick].click()  # 고름 → 접힘
        _pump()
        after = _row3_snapshot(widget)
        return before, after, expanded_h

    def test_one_row_round_trip_keeps_row_three_geometry(self, qapp):
        before, after, expanded_h = self._round_trip(qapp, lambda fit, one: fit - 1, pick=0)
        assert after == before, f"접힌 뒤 3행 기하가 달라졌다:\n{before}\n{after}"
        assert expanded_h == before["card_h"], "한 줄에 들어가는 폭에서는 펼쳐도 높이가 변하지 않는다"

    def test_wrapped_round_trip_returns_to_the_original_height(self, qapp):
        before, after, expanded_h = self._round_trip(qapp, lambda fit, one: one - 1, pick=0)
        assert expanded_h > before["card_h"], "전제: 한 줄 임계 아래에서는 줄바꿈으로 카드가 잠깐 자란다"
        assert after == before, f"접힌 뒤 원래 높이·기하로 돌아오지 않았다:\n{before}\n{after}"

    def test_picking_a_different_pill_restores_everything_but_the_pill_width(self, qapp):
        before, after, _ = self._round_trip(qapp, lambda fit, one: one - 1, pick=2)
        for key in ("mode", "card_h", "hint_h", "row3", "size", "path", "icon", "thumb", "extra_rows"):
            assert after[key] == before[key], f"{key}: {before[key]} → {after[key]}"
        assert after["pill_xy"][0] == before["pill_xy"][0], "선택 pill의 x(기준선)가 달라졌다"
        assert after["pill_xy"][1:] == before["pill_xy"][1:]


class TestWrapThreshold:
    """펼친 pill이 어느 폭에서 줄바꿈되는지 — 절대 px가 아니라 유도한 임계 기준.

    줄바꿈이 여전히 필요한 이유: 접힘 판정은 "pill + 경로 아이콘 + 크기"이고 펼침은
    경로·크기를 숨기므로 그 사이 폭에서는 한 줄이지만, 그보다 좁으면(창 콘텐츠
    최소폭 근처) 펼친 pill이 한 줄에 안 들어간다 — 가로 오버플로는 금지다.
    """

    def test_wraps_below_the_one_row_threshold_and_recovers_above_it(self, qapp):
        box, widget = make_boxed(width=900)
        fit = fit_threshold(widget)
        one = expanded_threshold(widget)
        assert one < fit, "전제: 펼친 한 줄 임계는 전부 들어가는 임계보다 좁다"
        base_h = widget.height()
        resize_box(box, widget, one)
        widget.buttons[0].click()  # 접힘 → 펼침
        _pump()
        assert widget.pillMode() == "expanded" and _rows(widget) == 1 and widget.height() == base_h, "한 줄 임계에서는 한 줄"
        resize_box(box, widget, one - 1)
        assert _rows(widget) == 2, "한 줄 임계 −1에서 줄바꿈이 시작돼야 한다"
        assert widget.height() > base_h
        last = widget.buttons[-1]
        assert last.x() == widget.buttons[0].x(), "둘째 줄도 컨텐츠 열 기준선에서 시작한다"
        assert last.y() > widget.buttons[0].y()
        resize_box(box, widget, one + 20)
        assert widget.pillMode() == "expanded" and _rows(widget) == 1 and widget.height() == base_h, "넓히면 한 줄로 회복돼야 한다"
        assert widget.contentLayout.count() == 3, "추가 행이 남았다"

    def test_expanded_pills_never_overflow_the_row(self, qapp):
        box, widget = make_boxed(width=900)
        one = expanded_threshold(widget)
        widest = max(b.sizeHint().width() for b in widget.buttons)
        resize_box(box, widget, one - 1)
        widget.buttons[0].click()
        _pump()
        assert widget.pillMode() == "expanded"
        floor = widget.minimumSizeHint().width()
        for width in (one - 1, one - 1 - widest, max(floor, one - 1 - 2 * widest)):
            resize_box(box, widget, width)
            right_edge = widget.resolutionLayout.geometry().right()
            for pill in widget.buttons:
                assert pill.isVisible() and pill.x() + pill.width() - 1 <= right_edge, (
                    f"폭 {width}px에서 {pill.text()}가 3행 밖으로 나갔다 — 가로 오버플로 금지"
                )
        assert _rows(widget) >= 2


class TestOneExpandedAtATime:
    def test_expanding_another_card_collapses_the_first(self, qapp):
        view = ContentListView()
        model = ContentListModel()
        view.setModel(model)
        view.resize(900, 600)
        view.show()
        items = []
        for i in range(2):
            item = ContentItem(
                f"https://chzzk.naver.com/video/{i}",
                {"title": f"제목{i}", "category": "", "channelName": "채널", "createdDate": "", "duration": 3600},
                [[r, f"u{r}"] for r in FIVE], None, "", "C:/dl", "video", None,
            )
            item.downloadState = DownloadState.WAITING
            model.addItem(item)
            items.append(item)
        _pump()
        first, second = (view.widgetFor(it) for it in items)
        # 접히는 폭까지 목록을 좁힌다 — 카드 폭 = 뷰 폭 − (뷰↔카드 차, 실측)
        threshold = fit_threshold(first)
        view.resize(threshold - 1 + (view.width() - first.width()), 600)
        _pump()
        assert first.width() == threshold - 1 and first.pillMode() == second.pillMode() == "collapsed", "전제: 두 카드가 접혔다"
        first.buttons[0].click()
        _pump()
        assert first.isExpanded() and not second.isExpanded()
        second.buttons[0].click()
        _pump()
        assert second.isExpanded() and not first.isExpanded(), "한 번에 하나만 펼쳐져야 한다"
        assert _visible_pills(first) == ["1080p"]
        view.deleteLater()


# ======================= [P-4·1] 접힘 순서는 한 방향 =======================
#
# 폭이 줄수록: ① 경로 ElideMiddle → ② 경로 아이콘만 → ③ 해상도 접힘([▾]).
# ③ 뒤에 자리가 남아도 ①·②로 돌아가지 않는다(오너 확정). 파일 크기·재생 시간은 어떤
# 폭에서도 접지 않는다. 실기에서 5-pill 카드는 "접힘+경로 텍스트", 3-pill 카드는
# "전부+아이콘"으로 우선순위가 반대로 보였다 — pill을 접어 생긴 자리로 경로가 다시
# 펴진 것. 단조 판정이어야 같은 폭에서 항상 같은 모양이다.
#
# 고장 주입(확인됨): _layoutRowThree의 `icon_only = mode == "collapsed" or ...`에서
# 접힘 조건을 빼면 TestShrinkOrderIsMonotonic이 잡는다(③에서 경로 텍스트가 살아난다).



def _stage(widget) -> int:
    """3행 모양을 단계 번호로 — 0 전부+경로 전문 / 1 전부+경로 줄임 / 2 전부+아이콘 / 3 접힘+아이콘.

    정의 밖의 조합(접힘+경로 텍스트, 크기 숨김 등)은 -1 — 있으면 안 되는 모양이다.
    """
    mode = widget.pillMode()
    text = widget.directoryLabel.isVisible()
    icon = widget.pathIconButton.isVisible()
    if not widget.fileSizeLabel.isVisible() or text == icon:
        return -1
    if mode == "all" and text:
        return 0 if QLabel.text(widget.directoryLabel) == widget.directoryLabel.text() else 1
    if mode == "all" and icon:
        return 2
    if mode == "collapsed" and icon:
        return 3
    return -1


class TestShrinkOrderIsMonotonic:
    def _sweep(self, box, widget, widths) -> dict:
        seen = {}
        for width in widths:
            resize_box(box, widget, width)
            seen[width] = _stage(widget)
            assert seen[width] != -1, f"폭 {width}px: 정의 밖의 3행 모양(접힘+경로 텍스트 등)"
            assert shown(widget.fileSizeLabel) == widget.fileSizeLabel.text(), f"폭 {width}px: 파일 크기가 잘렸다"
        return seen

    def _widths(self, widget):
        """T(전부 들어가는 임계)를 사이에 두고 넓은 폭에서 접힘 폭까지 4px씩 — 절대 px 없음."""
        fit = fit_threshold(widget)
        one = expanded_threshold(widget)
        return list(range(fit + 400, one - 40, -4))

    def test_narrowing_only_moves_forward_through_the_stages(self, qapp):
        box, widget = make_boxed(width=1400)
        seen = self._sweep(box, widget, self._widths(widget))
        stages = list(seen.values())
        assert stages[0] == 0 and stages[-1] == 3, f"전제: 넓게 ①전문 → 좁게 ③접힘까지 내려간다: {stages[0]}…{stages[-1]}"
        assert all(a <= b for a, b in zip(stages, stages[1:])), f"역방향 전이가 있다: {stages}"
        assert {1, 2} <= set(stages), f"①줄임·②아이콘 단계를 거치지 않았다: {sorted(set(stages))}"

    def test_collapsed_keeps_the_path_icon_even_with_room_to_spare(self, qapp):
        box, widget = make_boxed(width=900)
        fit = fit_threshold(widget)
        resize_box(box, widget, fit - 1)
        assert widget.pillMode() == "collapsed"
        # 접힘으로 생긴 자리: 경로 텍스트 최소치보다 훨씬 넓다 — 그래도 아이콘이다
        used = widget.buttons[0].sizeHint().width() + widget.fileSizeLabel.width() + 3 * FIXED_SPACING
        room = widget.resolutionLayout.geometry().width() - used
        assert room > widget.fontMetrics().horizontalAdvance("~/…/abcdef"), "전제: 텍스트를 다시 펼 자리가 남아 있다"
        assert _stage(widget) == 3 and widget.pathIconButton.isVisible() and not widget.directoryLabel.isVisible(), (
            "③ 접힘 뒤 자리가 남는다고 경로가 다시 펴졌다 — 순서는 한 방향이다"
        )
        assert widget.pathIconButton.toolTip() == LONG_PATH, "아이콘이어도 전문은 툴팁으로 남는다"

    def test_widening_recovers_in_reverse_and_the_same_width_always_looks_the_same(self, qapp):
        """회복은 되돌아감이 아니다 — 같은 판정을 다시 하는 것. 같은 폭이면 같은 모양이어야 한다."""
        box, widget = make_boxed(width=1400)
        widths = self._widths(widget)
        narrowing = self._sweep(box, widget, widths)
        widening = self._sweep(box, widget, list(reversed(widths)))
        assert widening == narrowing, "같은 폭에서 좁힐 때와 넓힐 때 모양이 다르다"
        stages_up = [widening[w] for w in reversed(widths)]
        assert all(a >= b for a, b in zip(stages_up, stages_up[1:])), f"넓힐 때 역순 회복이 아니다: {stages_up}"

    def test_bouncing_around_the_threshold_never_oscillates(self, qapp):
        box, widget = make_boxed(width=900)
        fit = fit_threshold(widget)
        expected = {}
        for _ in range(4):
            for width in (fit + 1, fit, fit - 1, fit - 30):
                resize_box(box, widget, width)
                stage = _stage(widget)
                assert stage != -1
                assert expected.setdefault(width, stage) == stage, f"폭 {width}px에서 모양이 진동한다: {expected[width]} → {stage}"
        assert expected[fit + 1] <= 2 and expected[fit - 1] == 3


# ======================= [P-4·2] 늦게 온 크기 조회가 유저 선택을 덮지 않는다 =======================
#
# 카드가 만들어질 때 pill마다 크기 조회 스레드가 뜨고, 첫 pill(최고 해상도)의 결과가
# 도착하면 그것을 기본 선택으로 확정한다(_onRepSizeFetched). 유저가 그 사이에 720p를
# 골랐으면 늦게 온 결과가 선택을 1080p로 되돌렸다 — 접힘 모드에서는 pill이 하나만 보여
# 뒤집혀도 알아챌 표면이 없다. 카드가 "유저가 직접 고른 적이 있는가"를 기억하고 있으면
# 자동 선택을 건너뛴다. 자동 선택 자체는 남는다(안 골랐으면 최고화질).
#
# 응답이 늦는 조건은 time.sleep()으로 실제로 만든다 — GIL에 가려 우연히 안 깨지는 것을
# 안 깨진다고 읽지 않기 위해서다.
#
# 고장 주입(확인됨): _onRepSizeFetched의 `if self._userPicked:` 분기를 지우면
# TestLateSizeFetchKeepsTheUsersPick가 잡는다.



class _SlowResponse:
    def __init__(self, size: int) -> None:
        self.headers = {"content-length": str(size)}

    def raise_for_status(self) -> None:
        pass


class _SlowSession:
    """head()가 delay초 자고 나서 크기를 준다 — 조회가 유저 조작보다 늦게 끝나는 창을 벌린다."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.sizes = {"u1080": 700_000_000, "u720": 400_000_000, "u480": 200_000_000, "u360": 100_000_000, "u144": 50_000_000}

    def head(self, url, timeout=None):
        time.sleep(self.delay)
        return _SlowResponse(self.sizes[url])

    def get(self, *a, **k):
        raise RuntimeError("get은 쓰이지 않아야 한다 — content-length가 있다")


class TestLateSizeFetchKeepsTheUsersPick:
    DELAY = 0.3

    def _slow(self, monkeypatch):
        session = _SlowSession(self.DELAY)
        monkeypatch.setattr("app.widgets.widget.get_thread_session", lambda: session)

    def _all_sizes_arrived(self, widget) -> bool:
        return all(b.toolTip() != "" for b in widget.buttons)

    def test_a_pick_made_before_the_size_arrives_survives(self, qapp, qtbot, monkeypatch):
        self._slow(monkeypatch)
        box, widget = make_boxed()  # 조회 스레드 5개가 자는 동안
        assert widget.buttons[0].toolTip() == "", "전제: 아직 아무 크기도 도착하지 않았다"
        widget.buttons[1].click()  # 720p — 전부 보이는 모드, 클릭 한 번
        _pump()
        assert str(widget.item.resolution) == "720"
        qtbot.waitUntil(lambda: self._all_sizes_arrived(widget), timeout=5000)
        _pump()
        assert str(widget.item.resolution) == "720" and widget.item.base_url == "u720", "늦게 온 조회가 선택을 덮었다"
        assert [b.isSelected() for b in widget.buttons] == [False, True, False, False, False]
        assert "381.47 MB" in widget.fileSizeLabel.text(), "고른 해상도(720p)의 크기로 표시가 채워져야 한다"

    def test_without_a_pick_the_default_selection_still_runs(self, qapp, qtbot, monkeypatch):
        self._slow(monkeypatch)
        box, widget = make_boxed()
        assert "Checking" in widget.fileSizeLabel.text() or widget.fileSizeLabel.text().strip() in ("", "711.02 MB")
        qtbot.waitUntil(lambda: self._all_sizes_arrived(widget), timeout=5000)
        _pump()
        assert str(widget.item.resolution) == "1080" and widget.buttons[0].isSelected(), "안 골랐으면 최고화질이 잡혀야 한다"
        assert "667.57 MB" in widget.fileSizeLabel.text(), "자동 선택의 크기가 표시에 채워져야 한다"
        assert widget.item.total_size == widget.item.unique_reps[0][-1]

    def test_pick_survives_collapsing_before_the_size_arrives(self, qapp, qtbot, monkeypatch):
        self._slow(monkeypatch)
        box, widget = make_boxed()
        widget.buttons[1].click()  # 전부 보이는 모드에서 720p
        _pump()
        resize_box(box, widget, fit_threshold(widget) - 1)  # 접힘 — pill은 [720p ▾] 하나
        assert widget.pillMode() == "collapsed" and _visible_pills(widget) == ["720p"]
        qtbot.waitUntil(lambda: self._all_sizes_arrived(widget), timeout=5000)
        _pump()
        assert _visible_pills(widget) == ["720p"] and str(widget.item.resolution) == "720", "접힌 채로 선택이 뒤집혔다"
