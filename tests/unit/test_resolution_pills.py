"""카드 3행 해상도 pill 게이트 (#244 3행 정리).

- 같은 높이 트랙이 둘인 매니페스트 → pill은 높이당 하나 (core/api/representations.py)
- 해상도 인라인 확장 — 접힘/펼침, 고르면 접힘, 기하 복원, 줄바꿈 임계 T, 한 번에 하나
"""

import pytest
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

import main as main_module
import theme
from content.data import ContentItem
from content.model import ContentListModel
from content.view import ContentListView
from content.widget import ContentItemWidget
from core.api.dash import parse_dash_manifest
from core.models.download_state import DownloadState
from tests.unit.card_helpers import hold_style


@pytest.fixture(autouse=True)
def _apply_production_qss(qapp):
    """실제 전역 QSS를 태운 상태에서 잰다(scope=function 유지 — test_widget_theme 참고)."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))
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


# ======================= [1] 해상도 인라인 확장 =======================
#
# 평소:   [1080p ▾]  ·····  경로  ·····  크기
# 펼침:   1080p  720p  480p  360p  144p        ← 경로·크기는 잠깐 숨는다
# 고른 뒤: [720p ▾]  ·····  경로  ·····  크기
#
# 팝업이 아니다 — 그 자리에서 펼쳐지고 고르면 접힌다. 안 들어가면 줄을 바꾼다.
# 카드 높이가 잠깐 변하는 것은 허용하되 접히면 원래 높이로 **정확히** 돌아온다.
#
# 고장 주입(확인됨): content/widget.py::_packPills에서 "비게 된 추가 행은 없앤다"
# 루프를 건너뛰면 TestGeometryRestoresAfterCollapse의 좁은 폭 게이트가 잡는다
# (접힌 뒤 카드 높이가 늘어난 채 남는다).

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
    return box, widget


def resize_box(box, widget, width):
    """컨테이너 폭을 바꾸고 카드가 그 폭을 실제로 받았는지 단언한다(클램프 = 즉시 실패)."""
    box.resize(width, 400)
    _pump()
    assert widget.width() == width, (
        f"요청 폭 {width}px인데 실제 {widget.width()}px — 최소폭({widget.minimumSizeHint().width()}px)에 클램프됐다"
    )


def expanded_threshold(widget) -> int:
    """펼친 pill 전부가 3행 한 줄에 **딱** 들어가는 카드 폭 T — 구성 요소 독립 합산.

    카드 폭↔3행 폭 차(offset, 접힌 상태에서 실측) + 펼친 pill 자연 폭 합(▾ 없음)
    + 간격 × pill 개수(pill 사이 n−1개 + 행 끝 세로 버팀목 앞 1개). 제품의 판정
    함수(_packPills)는 부르지 않는다.
    """
    assert not widget.isExpanded()
    offset = widget.width() - widget.resolutionLayout.geometry().width()
    widget.setExpanded(True)
    _pump()
    pills = sum(b.sizeHint().width() for b in widget.buttons)
    widget.setExpanded(False)
    _pump()
    return offset + pills + len(widget.buttons) * FIXED_SPACING


def _rows(widget) -> int:
    """보이는 pill이 차지한 줄 수 — 서로 다른 y의 개수."""
    return len({b.y() for b in widget.buttons if b.isVisible()})


def _row3_snapshot(widget) -> dict:
    """3행 기하 스냅샷 — 접힘 전후 비교용(보이는 것만 잰다)."""
    selected = next(b for b in widget.buttons if b.isVisible())
    return {
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


class TestCollapsedAndExpandedStates:
    def test_collapsed_shows_only_the_selected_pill_with_a_caret(self, qapp):
        box, widget = make_boxed()
        assert _visible_pills(widget) == ["1080p"]
        pill = widget.buttons[0]
        assert pill.isSelected() and pill.hasCaret() and pill.isEnabled(), "접힌 선택 pill은 ▾가 있고 눌 수 있어야 한다"
        assert widget.fileSizeLabel.isVisible() and widget.directoryLabel.isVisible()

    def test_expanding_shows_all_pills_and_hides_path_and_size(self, qapp):
        box, widget = make_boxed()
        widget.buttons[0].click()  # 접힌 pill을 누른다 = 펼치기
        _pump()
        assert widget.isExpanded()
        assert _visible_pills(widget) == [f"{r}p" for r in FIVE]
        assert not any(b.hasCaret() for b in widget.buttons), "펼치면 ▾는 사라진다"
        assert not widget.fileSizeLabel.isVisible(), "펼치는 동안 크기는 숨는다"
        assert not widget.directoryLabel.isVisible() and not widget.pathIconButton.isVisible(), "펼치는 동안 경로는 숨는다"
        assert widget.buttons[0].isSelected(), "펼쳐도 선택 표시는 유지된다"

    def test_picking_a_pill_selects_it_and_collapses(self, qapp):
        box, widget = make_boxed()
        events = []
        widget.expandedChanged.connect(events.append)
        widget.buttons[0].click()
        _pump()
        widget.buttons[2].click()  # 480p
        _pump()
        assert not widget.isExpanded() and events == [True, False]
        assert _visible_pills(widget) == ["480p"] and widget.buttons[2].hasCaret()
        assert str(widget.item.resolution) == "480" and widget.item.base_url == "u480"
        assert widget.fileSizeLabel.isVisible() and widget.directoryLabel.isVisible(), "접히면 경로·크기가 돌아온다"

    def test_clicking_the_selected_pill_while_expanded_only_collapses(self, qapp):
        box, widget = make_boxed()
        widget.buttons[0].click()
        _pump()
        widget.buttons[0].click()  # 이미 선택된 1080p
        _pump()
        assert not widget.isExpanded() and _visible_pills(widget) == ["1080p"]
        assert str(widget.item.resolution) == "1080"

    def test_starting_the_download_collapses_and_hides_the_pills(self, qapp):
        box, widget = make_boxed()
        widget.setExpanded(True)
        _pump()
        widget.item.downloadState = DownloadState.RUNNING
        widget.item.download_progress, widget.item.download_speed, widget.item.download_remain_time = 1, "1 MB/s", "00:10:00"
        widget.setData(widget.item, 0)
        _pump()
        assert not widget.isExpanded() and _visible_pills(widget) == []
        assert widget.statusLabel.isVisible() and widget.fileSizeLabel.isVisible()
        assert widget.contentLayout.count() == 3, "추가 행이 남아 있다"
        widget.setExpanded(True)  # 대기가 아니면 펼쳐지지 않는다
        assert not widget.isExpanded()


class TestGeometryRestoresAfterCollapse:
    """펼침 → 고름 → 접힘 뒤 3행 기하가 펼치기 전과 같다 — 넓은 폭(한 줄)과 좁은 폭(줄바꿈) 둘 다."""

    def _round_trip(self, qapp, width, pick):
        box, widget = make_boxed(width=900)
        threshold = expanded_threshold(widget)
        target = width(threshold)
        resize_box(box, widget, target)
        before = _row3_snapshot(widget)
        widget.buttons[0].click()  # 펼침
        _pump()
        expanded_h = widget.height()
        widget.buttons[pick].click()  # 고름 → 접힘
        _pump()
        after = _row3_snapshot(widget)
        return before, after, expanded_h

    def test_wide_card_round_trip_keeps_row_three_geometry(self, qapp):
        before, after, expanded_h = self._round_trip(qapp, lambda t: t + 200, pick=0)
        assert after == before, f"접힌 뒤 3행 기하가 달라졌다:\n{before}\n{after}"
        assert expanded_h == before["card_h"], "한 줄에 들어가는 폭에서는 펼쳐도 높이가 변하지 않는다"

    def test_narrow_card_round_trip_returns_to_the_original_height(self, qapp):
        before, after, expanded_h = self._round_trip(qapp, lambda t: t - 1, pick=0)
        assert expanded_h > before["card_h"], "전제: T−1에서는 줄바꿈으로 카드가 잠깐 자란다"
        assert after == before, f"접힌 뒤 원래 높이·기하로 돌아오지 않았다:\n{before}\n{after}"

    def test_picking_a_different_pill_restores_everything_but_the_pill_width(self, qapp):
        before, after, _ = self._round_trip(qapp, lambda t: t - 1, pick=2)
        for key in ("card_h", "hint_h", "row3", "size", "path", "icon", "thumb", "extra_rows"):
            assert after[key] == before[key], f"{key}: {before[key]} → {after[key]}"
        assert after["pill_xy"][0] == before["pill_xy"][0], "선택 pill의 x(기준선)가 달라졌다"
        assert after["pill_xy"][1:] == before["pill_xy"][1:]


class TestWrapThreshold:
    """어느 폭에서 줄바꿈이 시작되는지 — 절대 px가 아니라 유도한 T 기준."""

    def test_wraps_below_the_threshold_and_recovers_above_it(self, qapp):
        box, widget = make_boxed(width=900)
        threshold = expanded_threshold(widget)
        base_h = widget.height()
        widget.setExpanded(True)
        _pump()
        resize_box(box, widget, threshold)
        assert _rows(widget) == 1 and widget.height() == base_h, "T에서는 한 줄이어야 한다"
        resize_box(box, widget, threshold - 1)
        assert _rows(widget) == 2, "T−1에서 줄바꿈이 시작돼야 한다"
        assert widget.height() > base_h
        last = widget.buttons[-1]
        assert last.x() == widget.buttons[0].x(), "둘째 줄도 컨텐츠 열 기준선에서 시작한다"
        assert last.y() > widget.buttons[0].y()
        resize_box(box, widget, threshold + 200)
        assert _rows(widget) == 1 and widget.height() == base_h, "넓히면 한 줄로 회복돼야 한다"
        assert widget.contentLayout.count() == 3, "추가 행이 남았다"

    def test_expanded_pills_never_overflow_the_row(self, qapp):
        box, widget = make_boxed(width=900)
        threshold = expanded_threshold(widget)
        widget.setExpanded(True)
        _pump()
        widest = max(b.sizeHint().width() for b in widget.buttons)
        floor = widget.minimumSizeHint().width()  # 펼친 동안의 최소폭(접힘 기준)
        widths = (threshold - 1, threshold - 1 - widest, max(floor, threshold - 1 - 2 * widest))
        for width in widths:
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
        first.buttons[0].click()
        _pump()
        assert first.isExpanded() and not second.isExpanded()
        second.buttons[0].click()
        _pump()
        assert second.isExpanded() and not first.isExpanded(), "한 번에 하나만 펼쳐져야 한다"
        assert _visible_pills(first) == ["1080p"]
        view.deleteLater()
