"""카드 가로 배치 회귀 게이트 (#244 재설계 — 오너 확정 구조를 기하값으로 고정).

밀도 게이트(`test_card_density.py`)는 세로만 본다 — 가로 배치에 게이트가
없던 동안 "넓은 창에서 요소가 균등 분배로 흩어지는" 결함이 시안 리뷰를
그대로 통과해 실기에서야 잡혔다(1600px에서 해상도 버튼 간격 335px 실측).
이 파일이 그 구멍을 막는다.

**고정하는 불변식(#245 상태별 슬롯 확정 설계)**:
- 좌측 기준선은 둘뿐 — 썸네일 왼쪽(=cardPadding)과 컨텐츠 열 왼쪽.
  3개 행(1행 채널·조작, 2행 제목, 3행 상태 슬롯·크기) 전부 컨텐츠 열의
  같은 x에서 시작한다. 예외 없다.
- 우측 끝도 하나 — 삭제(1행)와 파일 크기(3행)의 오른쪽 끝이 같은 x.
  #178로 조작이 3개로 늘어도 유지(강제 3-조작 가시화 게이트).
- 남는 공간은 각 행에서 딱 한 곳(가운데 스트레치)만 흡수한다.
- 상태가 바뀌면 3행 내용·조작이 바뀌지만 행 높이·카드 높이는 불변
  (목록이 들썩이면 안 된다).

**방법론**: 폰트에 의존하지 않는 순수 기하 *관계*만 잰다 — 라벨 폭 자체는
폰트가 정하지만, "이웃 간격 == 고정 spacing"·"기준선 일치"·"남는 공간이
한 곳"이라는 관계는 폰트와 무관하다.

**⚠️ 반드시 창 폭 여러 값으로 잰다** — 이 부류 결함은 넓은 폭에서만
드러난다. 좁은 폭에서는 요소가 꽉 차 정렬 문제가 안 보인다.
"""

import pytest
from PySide6.QtWidgets import QApplication

import main as main_module
import theme
from content.data import ContentItem
from content.widget import ContentItemWidget
from core.models.download_state import DownloadState
from tests.unit.card_helpers import resize_to, shown, hold_style

#: 좁게(요소가 꽉 참)·기본·아주 넓게(실기 와이드 모니터 — 결함이 실제로
#: 발견된 조건). 카드 최소폭이 썸네일(16:9) 때문에 커져 하한을 520→560으로.
WIDTHS = (560, 900, 1600)

#: 각 행 QHBoxLayout의 setSpacing 고정값(ui/contentItemWidget.py).
FIXED_SPACING = 4


@pytest.fixture(autouse=True)
def _apply_production_qss(qapp):
    """실제 전역 QSS(위계 폰트 토큰 포함)를 태운 상태에서 잰다. ⚠️
    `scope="function"` 유지 — 넓히면 macOS 종료 크래시 재발
    (`test_widget_theme.py`의 `_apply_dark_card_qss` 문서 참고)."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))  # 참조 보관 — 이중 해제 우회 (#243, card_helpers.hold_style)
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
    # 테스트 아이템의 기본 경로를 전역 설정 경로로 등록한다(#245) — 실제
    # 앱은 시작 시 mainWindow가 밀어 넣는 값이라, 안 넣으면 모든 카드가
    # "전역과 다른 경로"로 판정돼 경로 라벨이 떠서 3행 슬롯을 밀어낸다.
    monkeypatch.setattr("content.widget._global_download_path", "C:/dl")


def _make_widget(qapp) -> ContentItemWidget:
    item = ContentItem(
        "https://chzzk.naver.com/video/1",
        {"title": "제목", "category": "", "channelName": "채널", "createdDate": "", "duration": 3600},
        [["1080", "u1"], ["720", "u2"], ["144", "u3"]], None, "", "C:/dl", "video", None,
    )
    item.downloadState = DownloadState.WAITING
    item.total_size = "711.02 MB"
    widget = ContentItemWidget(item, 0)
    widget.addRepresentationButtons()
    widget.setData(item, 0)
    return widget


def _at_width(widget, width):
    # 높이는 자연값(sizeHint)을 쓴다 — 임의 고정 높이를 강제하면 세로 여분이
    # 행 사이로 분배돼 실제 목록(카드가 자기 hint 높이를 받는)과 다른
    # 조건에서 재게 된다(썸네일 가득 채움 검증이 3px 어긋나는 것으로 실측).
    widget.resize(width, widget.sizeHint().height())
    widget.show()
    QApplication.processEvents()


def _right(w) -> int:
    return w.x() + w.width()


def _gap(left_widget, right_widget) -> int:
    return right_widget.x() - _right(left_widget)


class TestTwoLeftBaselines:
    """좌측 기준선은 둘뿐 — 썸네일 왼쪽과 컨텐츠 열 왼쪽 (#244 확정)."""

    #: 각 행의 첫 요소 — 전부 컨텐츠 열의 같은 x에서 시작해야 한다.
    #: (3행의 첫 요소는 대기 카드에선 pill이라 아래에서 buttons[0]로 더한다)
    ROW_HEADS = ("channelImageLabel", "titleLabel")

    @pytest.mark.parametrize("width", WIDTHS)
    def test_all_rows_start_at_the_content_baseline(self, qapp, width):
        widget = _make_widget(qapp)
        _at_width(widget, width)
        content_x = widget.titleLabel.x()
        starts = {name: getattr(widget, name).x() for name in self.ROW_HEADS}
        starts["resolution_first_button"] = widget.buttons[0].x()
        offenders = {n: x for n, x in starts.items() if x != content_x}
        assert not offenders, (
            f"폭 {width}px에서 컨텐츠 열 기준선({content_x}px)을 벗어난 행 시작점: {offenders} — "
            "좌측 기준선은 썸네일·컨텐츠 열 둘뿐이어야 한다"
        )

    @pytest.mark.parametrize("width", WIDTHS)
    def test_exactly_two_left_baselines(self, qapp, width):
        widget = _make_widget(qapp)
        _at_width(widget, width)
        thumb_x = widget.thumbnailLabel.x()
        content_x = widget.titleLabel.x()
        baselines = {thumb_x, content_x}
        assert len(baselines) == 2 and thumb_x < content_x, (
            f"기준선이 둘이 아니다: thumb={thumb_x}, content={content_x}"
        )
        # 썸네일 기준선 = 카드 안쪽 여백(cardPadding) + 테두리 1px
        assert thumb_x == theme.METRICS["cardPadding"] + 1

    def test_baselines_do_not_move_as_the_window_widens(self, qapp):
        widget = _make_widget(qapp)
        positions = []
        for width in WIDTHS:
            _at_width(widget, width)
            positions.append((widget.thumbnailLabel.x(), widget.titleLabel.x()))
        assert positions[0] == positions[1] == positions[2], (
            f"창 폭에 따라 기준선이 움직인다: {positions}"
        )

    def test_thumbnail_fills_the_content_height_at_16_9(self, qapp):
        """썸네일은 컨텐츠 열 높이를 가득 채우고 폭은 16:9로 나온다(#244 확정).

        "카드 높이가 썸네일을 결정하는 게 아니라, 원하는 카드 높이에서
        16:9로 폭이 나온다" — 썸네일 높이가 우측 4행의 실제 높이와 같고
        폭이 그 16/9배인지를 직접 잰다(#245: 3행으로 줄어 높이·폭이 함께
        작아진다). 글자·간격 토큰이 바뀌어도 이 관계는 유지돼야 한다
        (고정 크기를 박으면 여기서 잡힌다).
        """
        widget = _make_widget(qapp)
        _at_width(widget, 900)
        thumb = widget.thumbnailLabel
        content_height = widget.contentLayout.contentsRect().height()
        assert abs(thumb.height() - content_height) <= 1, (
            f"썸네일 높이 {thumb.height()}px ≠ 컨텐츠 열 높이 {content_height}px — 가득 채우지 않는다"
        )
        assert thumb.width() == round(thumb.height() * 16 / 9), (
            f"썸네일이 16:9가 아니다: {thumb.width()}x{thumb.height()}"
        )


class TestOneRightEdge:
    """우측 끝은 하나 — 삭제(1행)와 파일 크기(3행)의 오른쪽 끝이 같은 x."""

    @pytest.mark.parametrize("width", WIDTHS)
    def test_delete_and_file_size_share_the_right_edge(self, qapp, width):
        widget = _make_widget(qapp)
        _at_width(widget, width)
        assert _right(widget.deleteButton) == _right(widget.fileSizeLabel), (
            f"폭 {width}px에서 삭제({_right(widget.deleteButton)})와 "
            f"파일 크기({_right(widget.fileSizeLabel)})의 오른쪽 끝이 다르다"
        )


class TestRowOneClusters:
    """1행 — 좌측(채널이미지·채널명)과 우측(상태별 조작·삭제) 군집이 각자
    붙고, 남는 공간은 그 사이 스트레치 한 곳만 흡수한다(#245). 삭제 앞은
    파괴적 조작 분리를 위한 추가 고정 간격(4+8=12px)이다."""

    #: 삭제(✕) 앞 간격 — spacing 4 + addSpacing 8. 실수 방지용 분리다.
    DELETE_EXTRA_GAP = FIXED_SPACING + 8

    @pytest.mark.parametrize("width", WIDTHS)
    def test_left_cluster_gap_is_fixed(self, qapp, width):
        widget = _make_widget(qapp)
        _at_width(widget, width)
        gap = _gap(widget.channelImageLabel, widget.channelNameLabel)
        assert gap == FIXED_SPACING, (
            f"폭 {width}px에서 채널이미지→채널명 간격이 {gap}px — 군집이 흩어졌다"
        )

    def test_free_space_lives_only_between_the_clusters(self, qapp):
        """폭이 커질수록 채널명→우측 조작 사이 한 곳만 벌어져야 한다 —
        실패 카드(↻ 조작 가시)로 재서 우측 군집의 첫 요소를 고정한다."""
        widget = _make_widget(qapp)
        widget.item.downloadState = DownloadState.FAILED
        widget.setData(widget.item, 0)
        middle_gaps = []
        for width in WIDTHS:
            _at_width(widget, width)
            middle_gaps.append(_gap(widget.channelNameLabel, widget.retryButton))
        assert middle_gaps[0] < middle_gaps[1] < middle_gaps[2], (
            f"군집 사이 가운데 간격이 폭을 따라 늘지 않는다: {middle_gaps} — "
            "남는 공간이 다른 곳으로 새고 있다"
        )

    @pytest.mark.parametrize("width", WIDTHS)
    def test_delete_is_separated_by_the_extra_gap(self, qapp, width):
        """삭제(✕)는 앞 조작과 추가 간격으로 떨어져 있다 — 파괴적 조작이
        같은 무게로 붙어 있으면 실수로 누른다(#245)."""
        widget = _make_widget(qapp)
        widget.item.downloadState = DownloadState.FAILED
        widget.setData(widget.item, 0)
        _at_width(widget, width)
        gap = _gap(widget.retryButton, widget.deleteButton)
        assert gap == self.DELETE_EXTRA_GAP, (
            f"폭 {width}px에서 조작→삭제 간격이 {gap}px "
            f"(기대 {self.DELETE_EXTRA_GAP}px)"
        )

    @pytest.mark.parametrize("width", WIDTHS)
    def test_right_edge_holds_with_three_actions(self, qapp, width):
        """#178 구간 버튼 자리 확보 게이트 — 조작이 3개로 늘어도 우측
        끝선(삭제==파일크기)과 카드 높이가 안 깨진다. 버튼을 새로 만들지
        않고(눌러도 아무 일 없는 버튼은 노이즈) 기존 3개(⏸·📁·↻)를 강제로
        동시 가시화해 그 폭 조건을 재현한다."""
        widget = _make_widget(qapp)
        _at_width(widget, width)
        height_before = widget.height()
        for button in (widget.pauseButton, widget.openDirectoryButton, widget.retryButton):
            button.setVisible(True)
        _at_width(widget, width)
        assert _right(widget.deleteButton) == _right(widget.fileSizeLabel), (
            "조작 3개에서 우측 끝선이 깨졌다 — #178 확장 시 재발할 결함"
        )
        assert widget.height() == height_before, (
            "조작 3개에서 카드 높이가 변했다 — #178 확장 시 목록이 들썩인다"
        )


class TestResolutionRow:
    """3행 — 해상도 pill 좌측 고정 간격, 남는 공간은 가운데 한 곳, 파일 크기는 우측 끝.

    세 폭 모두 pill 3개가 경로 아이콘·크기와 함께 들어가므로 pill 전부가 보이는
    모드다(#244 3행 정리 — 안 들어갈 때만 접힌다. 그 게이트는
    tests/unit/test_resolution_pills.py)."""

    @pytest.mark.parametrize("width", WIDTHS)
    def test_pill_gaps_are_fixed(self, qapp, width):
        widget = _make_widget(qapp)
        _at_width(widget, width)
        assert widget.pillMode() == "all", "전제: 이 폭에서는 pill 전부가 보인다"
        for left, right in zip(widget.buttons, widget.buttons[1:]):
            gap = _gap(left, right)
            assert gap == FIXED_SPACING, (
                f"폭 {width}px에서 해상도 pill 간격이 {gap}px (고정값 "
                f"{FIXED_SPACING}px 기대) — 넓은 창 흩어짐 결함(1600px에서 "
                "335px 실측)의 재발이다"
            )

    def test_pill_positions_do_not_move_as_the_window_widens(self, qapp):
        widget = _make_widget(qapp)
        positions = []
        for width in WIDTHS:
            _at_width(widget, width)
            positions.append([b.x() for b in widget.buttons])
        assert positions[0] == positions[1] == positions[2], (
            f"창 폭에 따라 pill x가 움직인다: {positions} — 왼쪽 고정이 깨졌다"
        )

    def test_free_space_lives_between_pills_and_file_size(self, qapp):
        widget = _make_widget(qapp)
        middle_gaps = []
        for width in WIDTHS:
            _at_width(widget, width)
            middle_gaps.append(_gap(widget.buttons[-1], widget.fileSizeLabel))
        assert middle_gaps[0] < middle_gaps[1] < middle_gaps[2], (
            f"3행 가운데 간격이 폭을 따라 늘지 않는다: {middle_gaps}"
        )


def _make_widget_with_reps(qapp, reps, width=900) -> ContentItemWidget:
    """해상도 목록을 지정해 대기 카드를 만든다 — 개수가 다른 카드를 섞어 재기 위함."""
    item = ContentItem(
        "https://chzzk.naver.com/video/1",
        {"title": "제목", "category": "", "channelName": "채널", "createdDate": "", "duration": 3600},
        [[r, f"u{r}"] for r in reps], None, "", "C:/dl", "video", None,
    )
    item.downloadState = DownloadState.WAITING
    item.total_size = "711.02 MB"
    widget = ContentItemWidget(item, 0)
    widget.addRepresentationButtons()
    widget.setData(item, 0)
    _at_width(widget, width)
    return widget


def _visible_pills(widget) -> list[str]:
    return [b.text() for b in widget.buttons if b.isVisible()]


def _selected(widget):
    return [b for b in widget.buttons if b.isSelected()]


class TestResolutionPillsDescending:
    """해상도 pill은 **내림차순**(높은 해상도가 왼쪽) — #245 오너 확정.

    기본 선택이 최고 해상도라, 오름차순이면 선택 pill이 맨 오른쓸에 놓여
    해상도 개수가 다른 카드(VOD 3개 / 클립 2개) 사이에서 선택 표시의 x가
    지그재그로 흩어졸다. 내림차순이면 기본 상태에서 항상 맨 앞 한 줄이다.
    ⚠️ 순서는 고정 — 클릭해도 pill이 움직이지 않는다(연속으로 눌러볼 수
    있어야 한다). 선택만 바뀐다.
    """

    def test_pills_are_ordered_high_to_low(self, qapp):
        widget = _make_widget_with_reps(qapp, (480, 1080, 720))  # API 입력 순서와 무관
        assert [b.text() for b in widget.buttons] == ["1080p", "720p", "480p"]

    def test_default_selection_is_the_first_pill(self, qapp):
        widget = _make_widget_with_reps(qapp, (480, 1080, 720))
        # 선택 표시는 `isSelected()`(content/pill.py) — 이전의 "선택 = 비활성"은
        # 접힌 pill을 눌러 펼쳐야 하므로 폐기됐다(#244 3행 정리)
        assert _selected(widget) == [widget.buttons[0]], "기본 선택(최고 해상도)이 첫 pill이 아니다"
        assert str(widget.item.resolution) == "1080"

    @pytest.mark.parametrize("width", WIDTHS)
    def test_selected_pill_x_is_identical_across_cards_with_different_counts(self, qapp, width):
        """3개짜리(VOD)와 2개짜리(클립)를 섞어도 기본 선택 pill의 x가 같다 —
        목록에서 선택 표시가 한 줄로 선다."""
        three = _make_widget_with_reps(qapp, (1080, 720, 480), width)
        two = _make_widget_with_reps(qapp, (1080, 720), width)
        sel3 = _selected(three)[0]
        sel2 = _selected(two)[0]
        assert sel3.x() == sel2.x(), (
            f"폭 {width}px에서 선택 pill x가 3개 카드 {sel3.x()} / 2개 카드 {sel2.x()} — "
            "지그재그다(오름차순이면 선택이 맨 오른쓸이라 개수에 따라 움직인다)"
        )
        assert sel3.x() == three.titleLabel.x(), "선택 pill이 컨텐츠 열 기준선에 있지 않다"

    def test_clicking_another_pill_does_not_reorder(self, qapp):
        """전부 보이는 폭 — 클릭 한 번에 480p가 고르고 pill은 움직이지 않는다."""
        widget = _make_widget_with_reps(qapp, (1080, 720, 480))
        assert widget.pillMode() == "all"
        order_before = [b.text() for b in widget.buttons]
        xs_before = [b.x() for b in widget.buttons]
        widget.buttons[2].click()  # 480p 선택
        QApplication.processEvents()
        assert widget.pillMode() == "all" and _visible_pills(widget) == order_before, "고른다고 접히면 안 된다"
        assert [b.text() for b in widget.buttons] == order_before
        assert [b.x() for b in widget.buttons] == xs_before, "선택했다고 pill이 움직였다 — 순서는 고정이다"
        assert _selected(widget) == [widget.buttons[2]]


class TestPillsCollapseOnlyWhenTheyDoNotFit:
    """해상도 pill은 **자리가 있으면 전부**, 안 들어가면 선택 하나로 접힌다 (#244 3행 정리 P-3).

    #245의 "어떤 폭에서도 전부 보인다(접지 않는다)"는 오너가 2026-08-31에
    뒤집었다(SPEC §9). 이 클래스는 넓은 폭의 "전부" 쪽만 고정한다 — 임계 폭 T
    유도, T±ε 전환, 선택 유지, 펼침은 tests/unit/test_resolution_pills.py가 잰다.
    """

    MANY = (2160, 1440, 1080, 720, 480, 360, 240, 144)

    def test_all_pills_show_when_they_fit(self, qapp):
        widget = _make_widget_with_reps(qapp, self.MANY, 1600)
        assert widget.pillMode() == "all"
        assert _visible_pills(widget) == [f"{r}p" for r in self.MANY]
        assert not any(b.hasCaret() for b in widget.buttons), "전부 보이는데 ▾가 있다"

    def test_pill_widths_do_not_shrink_when_the_card_narrows_but_still_fits(self, qapp):
        widget = _make_widget_with_reps(qapp, self.MANY, 1600)
        wide = [b.width() for b in widget.buttons]
        _at_width(widget, 900)
        assert widget.pillMode() == "all", "전제: 900px에서도 8개가 들어간다(offscreen·실기 모두)"
        assert [b.width() for b in widget.buttons] == wide, "좁아지자 pill이 쥐어짜였다 — 안 들어가면 접혀야지 줄면 안 된다"


class TestRowThreePriority:
    """3행 우선순위(#245 확정): ①우측 군집(파일 크기/재생 시간) 확보 → ②해상도
    pill 전부 → ③남는 폭은 전부 경로(ElideMiddle) → ④최소치 아래면 경로는
    아이콘만(클릭 대상 유지). **줄어드는 것은 다운로드 경로 하나뿐이다.**"""

    FIVE = (2160, 1440, 1080, 720, 480)
    LONG_PATH = "D:/vod/archive/2026/summer/finals/T1-vs-GEN-full-set-highlights-and-interviews"

    def _tight(self, qapp, width, *, segment=False, path=LONG_PATH):
        """`width=None`이면 임계 폭 T(`_threshold_waiting`)까지 줄인다 — 그때 경로
        자리는 아이콘 하나 폭이라 아이콘 모드가 폰트와 무관하게 보장된다.
        `minimumSizeHint()`는 표시 모드(텍스트/아이콘)에 따라 몇 px 움직여 고정점이
        아니다(resize_to가 잡아낸 실측). 폭은 `resize_to`로 놓는다 — 클램프되면
        조용히 넘어가지 않는다."""
        widget = _make_widget_with_reps(qapp, self.FIVE, 900)
        if segment:
            widget.item.content_type = "m3u8"  # 크기 조회 전 → 그 자리에 재생 시간
        widget.item.download_path = path
        widget.setData(widget.item, 0)
        resize_to(widget, width or self._threshold_waiting(widget))
        return widget

    def _rendered(self, label) -> str:
        """보이는지 단언한 뒤의 표시 문자열 — 숨은 라벨의 낡은 값은 읽지 않는다."""
        return shown(label)

    def _threshold_waiting(self, widget) -> int:
        """대기 카드의 임계 폭 T — 3행에 pill 전부·아이콘·크기가 **딱** 들어가는 카드 폭.

        [D]와 같은 방식으로 테스트가 구성 요소를 **독립 합산**한다(제품 계산 함수
        미사용): pill 자연 폭 합 + 크기 확보 폭 + 아이콘 폭 + 간격(pill 개수+2) +
        카드 폭↔3행 폭 차(실측). 여유 간격 하나(+4)가 있어 T에서는 pill 전부가
        아직 들어간다(#244 3행 정리 — 접힘은 그 아래 폭에서만). 절대 px(560/640)는 offscreen 폰트에서 카드 최소폭
        (681)에 클램프돼 세 폭이 한 점으로 붕괴했었다(E-3) — T 기준이면 어떤
        폰트에서도 같은 상대 위치에서 잰다.
        """
        layout = widget.resolutionLayout
        offset = widget.width() - layout.geometry().width()
        visible = [b for b in widget.buttons if b.isVisible()]
        pills = sum(b.sizeHint().width() for b in visible)
        size = max(widget.fileSizeLabel.minimumWidth(), widget.fileSizeLabel.sizeHint().width())
        # 경로 자리: 아이콘(20px)과 텍스트 라벨의 최소 힌트("…"+4 — offscreen 폰트에선
        # 아이콘보다 넓다) 중 큰 쪽. 작은 쪽을 쓰면 텍스트 모드에서 T가 카드 최소폭보다
        # 작아져 resize_to가 클램프를 잡는다(실측 offscreen 3px).
        slot = max(widget.pathIconButton.minimumWidth(), widget.directoryLabel.minimumSizeHint().width())
        return offset + pills + size + slot + (len(visible) + 2) * FIXED_SPACING

    def _points(self, widget) -> dict:
        """측정점 — T(경로 아이콘) / T+경로 자연 폭÷2(경로 텍스트이되 부분) / T+200."""
        threshold = self._threshold_waiting(widget)
        return {"T": threshold, "T+path/2": threshold + widget.directoryLabel.sizeHint().width() // 2, "T+200": threshold + 200}

    @pytest.mark.parametrize("point", ("T", "T+path/2", "T+200"))
    def test_file_size_is_never_elided_with_five_pills(self, qapp, point):
        """지금 실패하던 조건 — 해상도 5개에서 파일 크기가 잘리면 안 된다."""
        widget = self._tight(qapp, 900)
        resize_to(widget, self._points(widget)[point])
        assert self._rendered(widget.fileSizeLabel) == widget.fileSizeLabel.text(), (
            f"{point}에서 파일 크기가 잘렸다: {self._rendered(widget.fileSizeLabel)!r}"
        )

    @pytest.mark.parametrize("point", ("T", "T+path/2", "T+200"))
    def test_duration_is_never_elided_either(self, qapp, point):
        """크기 조회 전에는 같은 자리에 재생 시간이 들어온다 — 그것도 온전해야 한다."""
        widget = self._tight(qapp, 900, segment=True)
        assert widget.fileSizeLabel.text().count(":") == 2, "세그먼트 기반 대기 카드는 재생 시간을 보여야 한다(전제)"
        resize_to(widget, self._points(widget)[point])
        assert self._rendered(widget.fileSizeLabel) == widget.fileSizeLabel.text(), (
            f"{point}에서 재생 시간이 잘렸다: {self._rendered(widget.fileSizeLabel)!r}"
        )

    def test_reserved_width_covers_the_longest_case(self, qapp):
        """확보 폭은 "가장 긴 경우"(재생 시간 vs 크기) 기준이다 — 텍스트가 크기에서
        시간으로 바뀌어도 라벨 폭이 늘 필요가 없다."""
        widget = self._tight(qapp, 900)
        shown(widget.fileSizeLabel)  # 보이는 라벨의 폭만 의미가 있다
        metrics = widget.fileSizeLabel.fontMetrics()
        duration = metrics.horizontalAdvance("00:00:00")
        assert widget.fileSizeLabel.minimumWidth() >= duration, "재생 시간 폭을 확보하지 않았다"
        assert widget.fileSizeLabel.width() >= widget.fileSizeLabel.minimumWidth()

    def test_only_the_path_shrinks_as_the_card_narrows(self, qapp):
        """폭을 계속 줄일 때 경로만 짧아지고 크기·pill은 불변이다.

        폭은 T 기준으로 유도한다(T+600 → T+300 → T+경로÷2 → T+40 → T). 이전의
        절대 px(1600/900/700/640/560/min)는 offscreen에서 640·560이 최소폭에
        클램프돼 폭이 안 변했고, 단조 감소 단언이 아무것도 재지 않은 채 통과했다.
        `resize_to`가 그 경우를 즉시 실패시킨다.
        """
        widget = self._tight(qapp, 900)
        threshold = self._threshold_waiting(widget)
        half_path = widget.directoryLabel.sizeHint().width() // 2
        widths = (threshold + 600, threshold + 300, threshold + half_path, threshold + 40, threshold)
        resize_to(widget, widths[0])
        size_w = widget.fileSizeLabel.width()
        visible = [b for b in widget.buttons if b.isVisible()]  # 접힘: 선택 pill 하나
        pills = [(b.x() - widget.buttons[0].x(), b.width()) for b in visible]
        path_widths = []
        for width in widths:
            resize_to(widget, width)
            assert widget.fileSizeLabel.width() == size_w, f"폭 {width}px에서 파일 크기 폭이 변했다"
            assert self._rendered(widget.fileSizeLabel) == widget.fileSizeLabel.text()
            assert [shown(b) for b in visible] == [f"{r}p" for r in self.FIVE], "pill이 숨었다 — T 이상에서는 전부 보인다"
            assert [(b.x() - widget.buttons[0].x(), b.width()) for b in visible] == pills, (
                f"폭 {width}px에서 pill 배치가 변했다"
            )
            path_widths.append(widget.directoryLabel.width() if widget.directoryLabel.isVisible() else 0)
        assert all(a >= b for a, b in zip(path_widths, path_widths[1:])), (
            f"경로 폭이 단조 감소하지 않는다: {path_widths}"
        )
        assert path_widths[0] > path_widths[2] > path_widths[-1], f"좁혀도 경로가 줄지 않았다: {path_widths}"
        assert path_widths[-1] == 0 and widget.pathIconButton.isVisible(), "T에서는 경로가 아이콘이어야 한다"

    def test_path_collapses_to_an_icon_but_stays_clickable(self, qapp, monkeypatch):
        """남는 폭이 최소치 아래면 경로는 아이콘만 남는다 — 텍스트가 사라져도
        폴더 선택 진입점은 산다. 전역과 다르면 점 표시(folder_dot)."""
        widget = self._tight(qapp, None)  # T — 경로 자리가 아이콘 하나 폭 < 최소 텍스트 폭
        assert [shown(b) for b in widget.buttons] == [f"{r}p" for r in self.FIVE], "전제: pill 5개가 전부 보인다(압력의 근원)"
        assert widget.pathIconButton.isVisible(), "경로가 아이콘으로 접히지 않았다 — 전제(최소폭, 5 pill) 확인"
        assert not widget.directoryLabel.isVisible()
        assert widget.pathIconButton.iconName() == "folder_dot", "전역과 다른 경로인데 점 표시가 없다"
        assert widget.pathIconButton.toolTip() == self.LONG_PATH
        from content import widget as widget_mod

        calls = []
        monkeypatch.setattr(
            widget_mod.QFileDialog, "getExistingDirectory",
            staticmethod(lambda parent, caption, start, *a, **k: calls.append(start) or "E:/picked"),
        )
        widget.pathIconButton.click()
        assert calls == [self.LONG_PATH], "아이콘 모드에서 클릭이 폴더 선택으로 이어지지 않는다"
        assert widget.item.download_path == "E:/picked"

    def test_icon_mode_is_decided_on_the_very_first_show(self, qapp):
        """**이미 보이는 목록에 카드가 들어올 때**(실제 앱의 삽입 경로) 좁으면 첫
        화면부터 아이콘이어야 한다.

        첫 표시에서 카드의 resizeEvent는 자식 프레임 레이아웃이 활성화되기
        **전에** 도착해 3행 폭이 0이다(Qt show_helper가 대기 중 Resize를 자식
        표시 전에, showEvent를 자식 표시 후에 보낸다). 거기서 "배치 전"으로
        끝내면 폭이 다시 안 바뀌는 한 판정이 영영 안 돈다 — 실기 갤러리 560px
        에서 경로가 43px 텍스트로 남은 채 발견됐다. 창을 살짝 흔들면 고쳐지는
        것이 증상이다(windows·offscreen 둘 다 재현)."""
        from PySide6.QtWidgets import QVBoxLayout, QWidget

        box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)
        probe = _make_widget_with_reps(qapp, self.FIVE, 900)  # 최소폭을 알아내기 위한 견본
        box.resize(probe.minimumSizeHint().width(), probe.sizeHint().height())
        box.show()
        QApplication.processEvents()
        item = ContentItem(
            "https://chzzk.naver.com/video/1",
            {"title": "제목", "category": "", "channelName": "채널", "createdDate": "", "duration": 3600},
            [[r, f"u{r}"] for r in self.FIVE], None, "", self.LONG_PATH, "video", None,
        )
        item.downloadState = DownloadState.WAITING
        item.total_size = "711.02 MB"
        widget = ContentItemWidget(item, 0)
        widget.addRepresentationButtons()
        widget.setData(item, 0)
        column.addWidget(widget)  # 보이는 컨테이너가 카드를 (큐로) 처음 보인다
        for _ in range(3):
            QApplication.processEvents()
        assert widget.isVisible() and widget.width() == box.width(), "전제: 카드가 컨테이너 폭으로 보인다"
        assert widget.pathIconButton.isVisible() and not widget.directoryLabel.isVisible(), (
            "첫 표시에서 경로가 아이콘으로 접히지 않았다 — 창을 흔들어야 고쳐지는 결함"
        )

    def _running_widget(self, qapp):
        """진행 중 + 전역과 다른 긴 경로 — 3행에 슬롯·경로·크기가 함께 있는 카드."""
        widget = _make_widget_with_reps(qapp, self.FIVE, 900)
        item = widget.item
        item.download_path = self.LONG_PATH  # 전역과 다름 → 진행 중에도 경로가 보인다
        item.downloadState = DownloadState.RUNNING
        item.resolution = "1080"
        item.download_progress, item.download_speed, item.download_remain_time = 42, "12.3 MB/s", "00:03:21"
        widget.setData(item, 0)
        _at_width(widget, 900)
        return widget

    def _threshold(self, widget) -> int:
        """임계 폭 T — 3행에 슬롯·아이콘·크기가 **딱** 들어가는 카드 폭.

        ⚠️ 제품의 계산 함수(_layoutPathLabel·_pathMinTextWidth)를 부르지 않고
        테스트가 구성 요소를 **독립적으로 합산**한다 — 제품이 틀리면 테스트도
        같이 틀리는 동어반복을 피하기 위함. 요소: 슬롯 자연 폭 · 크기 라벨
        확보 폭 · 아이콘 폭 · 간격 3개, 그리고 카드 폭↔3행 폭의 차(썸네일·
        패딩, 순수 기하로 실측). 폰트가 달라도 T가 따라 움직이므로 T±에서는
        어떤 QPA에서도 전제("접으면 슬롯 자리 있음")가 항상 참이다.
        """
        layout = widget.resolutionLayout
        offset = widget.width() - layout.geometry().width()
        slot = widget.statusLabel.sizeHint().width()
        size = max(widget.fileSizeLabel.minimumWidth(), widget.fileSizeLabel.sizeHint().width())
        icon = widget.pathIconButton.minimumWidth()
        return offset + slot + size + icon + 3 * FIXED_SPACING

    @pytest.mark.parametrize("point", ("T", "T+path/2", "T+200"))
    def test_progress_slot_keeps_its_text_and_the_path_yields(self, qapp, point):
        """대기 밖 상태에서도 같은 규칙 — 진행 슬롯("42% · 속도 · 남은 시간")은
        잘리지 않고 **경로가 먼저 양보**한다(560px 실기에서 슬롯이 잘리고 경로가
        남던 결함). 측정점은 절대 px가 아니라 임계 폭 T 기준 — 폭을 px로 박으면
        폰트에 따라(offscreen은 크기 라벨이 실기의 2배) 카드가 최소폭 미만이 되어
        게이트가 CI에서 안 돈다(A-1 실측). 가운데 점은 T+40 같은 고정 여유가
        아니라 **경로 자연 폭의 절반**이다 — 고정 여유는 폰트에 따라 경로가
        아이콘 모드(캡이 관여하지 않는 구간)에 떨어져 캡 제거 주입이 한쪽 QPA에서만
        잡혔다. 경로가 텍스트이되 다 못 들어가는 구간이 캡이 실제로 작동하는
        유일한 구간이고, 자연 폭의 절반은 어떤 폰트에서도 그 안에 있다."""
        widget = self._running_widget(qapp)
        threshold = self._threshold(widget)
        extra = {"T": 0, "T+path/2": widget.directoryLabel.sizeHint().width() // 2, "T+200": 200}[point]
        assert threshold > widget.minimumSizeHint().width(), "전제: T가 카드 최소폭보다 커야 T±에서 실제로 잰다"
        _at_width(widget, threshold + extra)
        assert widget.width() == threshold + extra, "전제: 카드가 요청 폭을 받았다"
        assert self._rendered(widget.statusLabel) == widget.statusLabel.text(), (
            f"{point}(+{extra}px)에서 진행 슬롯이 잘렸다: {self._rendered(widget.statusLabel)!r} — 줄어드는 것은 경로뿐이어야 한다"
        )
        assert self._rendered(widget.fileSizeLabel) == widget.fileSizeLabel.text()
        assert widget.directoryLabel.isVisible() or widget.pathIconButton.isVisible(), "경로 진입점이 사라졌다"

    def test_slot_longer_than_the_row_still_keeps_size_and_folds_the_path_to_an_icon(self, qapp):
        """T−1 — 슬롯·아이콘·크기가 1px 모자라는 "슬롯이 행보다 긴 상황"의 별도
        게이트(완화 조건에 섞지 않는다). 그때도 크기는 온전하고 경로는 아이콘까지
        완전히 양보한다(텍스트를 붙들고 있으면 안 된다)."""
        widget = self._running_widget(qapp)
        threshold = self._threshold(widget)
        assert threshold - 1 >= widget.minimumSizeHint().width(), "전제: T−1이 카드 최소폭 이상"
        _at_width(widget, threshold - 1)
        assert widget.width() == threshold - 1
        assert self._rendered(widget.fileSizeLabel) == widget.fileSizeLabel.text(), "크기가 잘렸다"
        assert widget.pathIconButton.isVisible() and not widget.directoryLabel.isVisible(), (
            "슬롯이 행보다 긴데 경로가 텍스트를 붙들고 있다 — 경로가 먼저 양보해야 한다"
        )

    def test_icon_has_no_dot_when_the_path_matches_global(self, qapp):
        widget = self._tight(qapp, None, path="C:/dl")  # 전역과 같음
        widget.pathIconButton.setVisible(True)  # 판정과 무관하게 도형만 확인
        assert widget.pathIconButton.iconName() == "folder"

    def test_path_text_recovers_when_the_card_widens_again(self, qapp):
        """되먹임 루프 회귀 게이트 — 한 번 아이콘/말줄임까지 줄었다가 넓히면
        경로가 원래 길이(축약형 전문)로 돌아온다."""
        widget = self._tight(qapp, None)
        assert widget.pathIconButton.isVisible()
        _at_width(widget, 1600)
        QApplication.processEvents()
        assert widget.directoryLabel.isVisible() and not widget.pathIconButton.isVisible()
        assert self._rendered(widget.directoryLabel) == widget.directoryLabel.text(), (
            f"넓혔는데 경로가 회복되지 않았다: {self._rendered(widget.directoryLabel)!r}"
        )
        assert widget.directoryLabel.text() == "D:/…/T1-vs-GEN-full-set-highlights-and-interviews"


class TestPathVisibilityRule:
    """경로는 **대기면 항상, 그 외엔 전역 설정 경로와 다를 때만** 보인다(#245 정정).

    첫 규칙 "다를 때만"은 대기에서 라벨을 숨겨 클릭 대상이 없어졌다 — 카드별
    경로 변경 진입점이 사라진 회귀. 받기 전에는 "어디에 받을지"가 유효한
    정보이고 바꾸는 시점이 바로 대기다. 받기 시작한 뒤에는 같은 값을
    카드마다 반복하는 것이 정보 과다라 다를 때만 남긴다.
    """

    def test_waiting_always_shows_the_path_even_when_it_matches_global(self, qapp):
        widget = _make_widget(qapp)  # 아이템 경로 == 전역("C:/dl", 픽스처 주입), WAITING
        _at_width(widget, 900)
        assert widget.directoryLabel.isVisible(), "대기 카드에서 경로가 숨었다 — 클릭할 대상이 없다"
        # 경로는 파일 크기 왼쪽에 고정 간격으로 붙는다(우측 끝선 유지)
        assert _gap(widget.directoryLabel, widget.fileSizeLabel) == FIXED_SPACING

    @pytest.mark.parametrize("state", (DownloadState.RUNNING, DownloadState.PAUSED,
                                       DownloadState.FINISHED, DownloadState.FAILED))
    def test_other_states_hide_the_path_when_it_matches_global(self, qapp, state):
        widget = _make_widget(qapp)
        widget.item.downloadState = state
        widget.item.download_progress = 42
        widget.setData(widget.item, 0)
        _at_width(widget, 900)
        assert not widget.directoryLabel.isVisible(), f"{state.name}: 전역과 같은 경로가 반복 표시된다"

    @pytest.mark.parametrize("state", (DownloadState.WAITING, DownloadState.RUNNING, DownloadState.FINISHED))
    def test_path_shown_when_it_differs(self, qapp, state):
        widget = _make_widget(qapp)
        widget.item.downloadState = state
        widget.item.download_path = "D:/다른/폴더"
        widget.setData(widget.item, 0)
        _at_width(widget, 900)
        assert widget.directoryLabel.isVisible()


class TestPathAbbreviationAndTooltip:
    """표시는 "뿌리/…/마지막폴더"로 축약, 전문은 툴팁(#245). 규칙과 근거는
    content/widget.py::abbreviate_path 참고."""

    HOME = "C:/Users/me"

    @staticmethod
    def _abbr(path, home):
        from content.widget import abbreviate_path

        return abbreviate_path(path, home=home)

    @pytest.mark.parametrize("path,expected", [
        ("C:/Users/me/Downloads/vod/lck/2026", "~/…/2026"),        # 홈 아래, 3단계 이상 → 접음
        ("C:/Users/me/Downloads/vod", "~/Downloads/vod"),           # 홈 아래, 2단계 → 전부
        ("C:/Users/me/Downloads", "~/Downloads"),
        ("C:/Users/me", "~"),
        ("D:/vod/lck/2026/finals", "D:/…/finals"),                   # 홈 밖(드라이브), 접음
        ("D:/vod/lck", "D:/vod/lck"),                                # 홈 밖, 2단계 → 전부
        ("C:\\Users\\me\\Videos\\a\\b", "~/…/b"),                    # 역슬래시 입력도 /로 통일
        ("/srv/media/vod/2026", "/…/2026"),                          # POSIX 루트
        ("", ""),
    ])
    def test_abbreviation_rule(self, path, expected):
        assert self._abbr(path, home=self.HOME) == expected

    def test_label_shows_abbreviation_and_tooltip_carries_the_full_path(self, qapp):
        widget = _make_widget(qapp)
        widget.item.download_path = "D:/vod/lck/2026/finals"
        widget.setData(widget.item, 0)
        _at_width(widget, 900)
        assert widget.directoryLabel.text() == "D:/…/finals"
        assert widget.directoryLabel.toolTip() == "D:/vod/lck/2026/finals"

    def test_abbreviated_label_does_not_break_the_row(self, qapp):
        """긴 경로도 3행이 터지지 않는다 — 카드 폭 안에서 우측 끝선 유지."""
        widget = _make_widget(qapp)
        widget.item.download_path = "D:/아주/긴/경로/구조/를/가진/폴더/이름/마지막"
        widget.setData(widget.item, 0)
        resize_to(widget, 640)
        shown(widget.directoryLabel)  # 보이는 라벨의 기하만 의미가 있다
        shown(widget.fileSizeLabel)
        assert _right(widget.fileSizeLabel) == _right(widget.deleteButton)
        assert _right(widget.directoryLabel) < widget.fileSizeLabel.x()


class TestPathShrinkOrder:
    """경로가 줄어드는 **순서**(#245 확정): ①전체 → ②중간 폴더 접기(`뿌리/…/마지막`,
    마지막 폴더 온전) → ③마지막 폴더에만 ElideMiddle(접두 고정) → ④아이콘.
    파일이 실제로 들어가는 곳은 마지막 폴더라 **가장 늦게** 잘린다. 중간 폴더가
    하나뿐(깊이 2)이어도 ②를 거친다 — 이전엔 깊이 ≤2를 전부 표시한 채 문자열
    전체에 ElideMiddle을 걸어 `~/scratch/c…wnloader-v2`처럼 중간 폴더가 살고
    마지막 폴더가 잘렸다. 폰트 무의존 — 문자열 관계만 잰다."""

    LAST = "chzzk-vod-downloader-v2"

    def _rendered_while_narrowing(self, qapp, path):
        """1400px에서 카드 최소폭까지 4px씩 좁히며 경로 라벨의 실제 표시 문자열을
        순서대로 모은다(연속 중복 제거). 아이콘 모드로 접히면 멈춘다."""
        from PySide6.QtWidgets import QLabel

        widget = _make_widget(qapp)
        widget.item.download_path = path
        widget.setData(widget.item, 0)
        seen = []
        for width in range(1400, widget.minimumSizeHint().width() - 1, -4):
            _at_width(widget, width)
            if not widget.directoryLabel.isVisible():
                break
            shown = QLabel.text(widget.directoryLabel)
            if not seen or seen[-1] != shown:
                seen.append(shown)
        return widget, seen

    def test_depth_two_folds_the_middle_folder_before_touching_the_last(self, qapp):
        widget, seen = self._rendered_while_narrowing(qapp, f"D:/archive-2026/{self.LAST}")
        assert seen[0] == f"D:/archive-2026/{self.LAST}", "넓을 때는 전체가 보여야 한다(①)"
        folded = f"D:/…/{self.LAST}"
        assert folded in seen, f"②(중간 폴더 접기) 단계가 없다 — 표시 순서: {seen}"
        fold_at = seen.index(folded)
        assert fold_at == 1, f"①에서 ②로 바로 가야 한다 — 중간에 다른 표시가 끼었다: {seen[:fold_at + 1]}"
        for text in seen[fold_at + 1:]:
            assert text.startswith("D:/…/"), f"③에서 접두 `D:/…/`가 고정되지 않았다: {text!r}"
            assert "…" in text[len("D:/…/"):], f"③은 마지막 폴더 안에서만 말줄임한다: {text!r}"
        cut_at = next((i for i, t in enumerate(seen) if self.LAST not in t), None)
        assert cut_at is not None and cut_at > fold_at, "마지막 폴더는 중간 폴더가 접힌 **뒤에** 잘려야 한다"

    def test_depth_one_keeps_the_root_and_elides_inside_the_folder(self, qapp):
        widget, seen = self._rendered_while_narrowing(qapp, f"D:/{self.LAST}")
        assert seen[0] == f"D:/{self.LAST}"
        assert all(t.startswith("D:/") and "…" in t[len("D:/"):] for t in seen[1:]), seen

    def test_depth_five_starts_folded_and_then_elides_the_last_folder_only(self, qapp):
        widget, seen = self._rendered_while_narrowing(qapp, f"D:/a/b/c/d/{self.LAST}")
        assert seen[0] == f"D:/…/{self.LAST}", "깊이 3 이상은 ①이 곧 ②(뿌리/…/마지막)"
        assert all(t.startswith("D:/…/") and "…" in t[len("D:/…/"):] for t in seen[1:]), seen
        assert len(seen) >= 2, "좁혀도 ③으로 내려가지 않았다 — 전제 확인"

    def test_widening_recovers_the_full_text(self, qapp):
        widget, seen = self._rendered_while_narrowing(qapp, f"D:/archive-2026/{self.LAST}")
        assert len(seen) >= 3, "전제: ①→②→③까지 내려갔다"
        _at_width(widget, 1400)
        from PySide6.QtWidgets import QLabel

        assert QLabel.text(widget.directoryLabel) == f"D:/archive-2026/{self.LAST}", "넓혀도 ①로 돌아오지 않는다"

    @pytest.mark.parametrize("path,expected", [
        ("D:/archive-2026/x", ("D:/archive-2026/x", "D:/…/", "x")),
        ("D:/x", ("D:/x", "D:/", "x")),
        ("D:/a/b/c/x", ("D:/…/x", "D:/…/", "x")),
        ("C:/Users/me/x", ("~/x", "~/", "x")),
        ("C:/Users/me", ("~", "", "~")),
        ("/x", ("/x", "/", "x")),
        ("/srv/x", ("/srv/x", "/…/", "x")),
        ("/srv/media/x", ("/…/x", "/…/", "x")),
    ])
    def test_display_parts(self, path, expected):
        from content.widget import path_display_parts

        assert path_display_parts(path, home="C:/Users/me") == expected


class TestPathClickOpensFolderPicker:
    """경로를 클릭하면 폴더 선택 대화상자 → 고른 값이 그 카드에 적용된다(#245).
    인라인 편집의 교체 — 대화상자는 monkeypatch로 막고 반환값을 주입한다
    (상단 [경로 찾기] 테스트와 같은 방식)."""

    def _patch_dialog(self, monkeypatch, result):
        calls = []
        from content import widget as widget_mod

        def fake(parent, caption, start_dir, *a, **k):
            calls.append((caption, start_dir))
            return result

        monkeypatch.setattr(widget_mod.QFileDialog, "getExistingDirectory", staticmethod(fake))
        return calls

    def test_click_on_waiting_card_applies_the_chosen_folder(self, qapp, monkeypatch):
        widget = _make_widget(qapp)
        _at_width(widget, 900)
        calls = self._patch_dialog(monkeypatch, "D:/new/folder")
        emitted = []
        widget.textChanged.connect(emitted.append)
        widget.choosePath(None)  # 실배선: directoryLabel.mousePressEvent → choosePath (아래에서 확인)
        assert calls and calls[0][1] == "C:/dl", "대화상자가 현재 경로에서 열리지 않는다"
        assert widget.item.download_path == "D:/new/folder"
        assert widget.directoryLabel.text() == "D:/new/folder"
        assert widget.directoryLabel.toolTip() == "D:/new/folder"
        assert emitted == ["D:/new/folder"], "모델 반영 시그널(textChanged)이 안 나갔다"

    def test_label_click_is_wired_to_the_picker(self, qapp, monkeypatch):
        """핸들러 직접 호출이 아니라 라벨 클릭 배선 자체를 확인한다."""
        widget = _make_widget(qapp)
        _at_width(widget, 900)
        calls = self._patch_dialog(monkeypatch, "")
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        shown(widget.directoryLabel)  # 숨은 라벨은 유저가 누를 수 없다 — 클릭 전에 가시성 단언
        QTest.mouseClick(widget.directoryLabel, Qt.MouseButton.LeftButton)
        assert calls, "경로 라벨 클릭이 폴더 선택으로 이어지지 않는다"

    def test_cancel_keeps_the_path(self, qapp, monkeypatch):
        widget = _make_widget(qapp)
        self._patch_dialog(monkeypatch, "")
        widget.choosePath(None)
        assert widget.item.download_path == "C:/dl"

    def test_no_picker_once_the_download_has_started(self, qapp, monkeypatch):
        widget = _make_widget(qapp)
        widget.item.downloadState = DownloadState.RUNNING
        widget.item.download_path = "D:/다른"  # 진행 중이라도 다르면 라벨은 보인다
        widget.setData(widget.item, 0)
        calls = self._patch_dialog(monkeypatch, "E:/x")
        widget.choosePath(None)
        assert not calls and widget.item.download_path == "D:/다른"


class TestStateSwapKeepsCardHeight:
    """상태가 바뀌면 3행 내용·조작이 바뀌지만 **카드 높이는 안 변한다**
    (#245 확정 — 목록이 들썩이면 안 된다). 슬롯 교체(pill↔텍스트)·조작
    교체(일시정지/재개/폴더/재시도)·크기 숨김(실패)·진행바 표시(진행·
    일시정지)가 전부 일어나는 **다섯 상태**(PAUSED 포함)를 순회한다."""

    def test_card_height_is_identical_across_all_states(self, qapp):
        widget = _make_widget(qapp)
        heights = {}
        for state in (DownloadState.WAITING, DownloadState.RUNNING, DownloadState.PAUSED,
                      DownloadState.FINISHED, DownloadState.FAILED):
            widget.item.downloadState = state
            widget.item.download_progress = 42
            widget.setData(widget.item, 0)
            _at_width(widget, 900)
            heights[state] = widget.height()
        assert len(set(heights.values())) == 1, (
            f"상태에 따라 카드 높이가 변한다: {heights} — 목록이 들썩인다"
        )

    def test_row3_stays_at_the_same_y_across_states(self, qapp):
        """슬롯이 pill↔텍스트로 바뀌어도 3행의 세로 위치가 같아야 한다."""
        widget = _make_widget(qapp)
        _at_width(widget, 900)
        pill_y = widget.buttons[0].y()
        widget.item.downloadState = DownloadState.RUNNING
        widget.setData(widget.item, 0)
        _at_width(widget, 900)
        assert abs(widget.statusLabel.y() - pill_y) <= 4, (
            f"슬롯 교체로 3행 세로 위치가 흔들린다: pill y={pill_y}, 텍스트 y={widget.statusLabel.y()}"
        )


class TestLabelsDoNotGrow:
    """텍스트 라벨들은 창이 더 넓어져도 자기 텍스트 폭에 머물러야 한다.

    간격(edge-to-edge)만 재면 못 잡는 흩어짐 모드 — 라벨이 stretch/
    Expanding을 받으면 이웃 간격은 고정값 그대로인데 라벨 폭이 부풀어
    좌측 정렬 텍스트 뒤로 빈 공간이 생긴다(실기에서 실측된 바로 그 모드).
    여유가 생긴 900px의 폭(자연 텍스트 폭)과 1600px의 폭이 같아야 한다.
    """

    LABELS = ("statusLabel", "fileSizeLabel")

    def test_labels_do_not_grow_when_the_window_widens(self, qapp):
        # 상태 텍스트가 실제로 보이는 진행 상태로 잰다(#245 슬롯)
        widget = _make_widget(qapp)
        widget.item.downloadState = DownloadState.RUNNING
        widget.item.download_progress = 42
        widget.setData(widget.item, 0)
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
    양을 바가 계속 보여줘야 한다; 색만 muted로 바뀐다). 대기·완료·실패의
    빈 막대는 정보가 없고 자리만 먹는다. 보일 때는 카드 아래 가장자리
    근처 전체 폭이고, 양끝은 카드 곡률로 잘려 있다."""

    def test_bar_hidden_for_states_without_progress(self, qapp):
        for state in (DownloadState.WAITING, DownloadState.FINISHED, DownloadState.FAILED):
            widget = _make_widget(qapp)
            widget.item.downloadState = state
            widget.item.download_progress = 100 if state == DownloadState.FINISHED else 0
            widget.setData(widget.item, 0)
            _at_width(widget, 900)
            assert not widget.progressBar.isVisible(), (
                f"{state}에서 진행바가 보인다 — 진행분이 있는 상태(진행·일시정지)만 보여야 한다"
            )

    @pytest.mark.parametrize("state,bar_state", [
        (DownloadState.RUNNING, "running"),
        (DownloadState.PAUSED, "paused"),
    ])
    def test_bar_visible_and_full_width_with_progress(self, qapp, state, bar_state):
        widget = _make_widget(qapp)
        widget.item.downloadState = state
        widget.item.download_progress = 42
        widget.setData(widget.item, 0)
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
    def test_bar_is_clipped_to_the_card_corners(self, qapp, width):
        """막대 양끝이 카드의 둥근 모서리 바깥으로 튀어나오지 않는다.

        바 자체의 QSS border-radius는 높이(4px)에 눌려 카드 반지름(12px)을
        못 따라가 실기 렌더에서 바닥 모서리 밖에 트랙·진행색 조각이 남았다
        (#245 오너 실기 제보 — 카드 왼쪽 바깥의 짧은 선). 그래서 카드
        곡률 마스크를 건다. 마스크 안/밖을 순수 기하로 잰다(폰트 무관).
        """
        widget = _make_widget(qapp)
        widget.item.downloadState = DownloadState.RUNNING
        widget.item.download_progress = 42
        widget.setData(widget.item, 0)
        _at_width(widget, width)
        bar = widget.progressBar
        mask = bar.mask()
        assert not mask.isEmpty(), "진행바에 카드 곡률 마스크가 없다"
        w, h = bar.width(), bar.height()
        from PySide6.QtCore import QPoint
        # 바닥 모서리 픽셀은 잘려 있고(카드 몸통 밖), 가운데는 살아 있다
        assert not mask.contains(QPoint(0, h - 1)), f"폭 {width}px: 바 왼쪽 아래 모서리가 카드 곡률 밖에 남는다"
        assert not mask.contains(QPoint(w - 1, h - 1)), f"폭 {width}px: 바 오른쪽 아래 모서리가 카드 곡률 밖에 남는다"
        assert mask.contains(QPoint(w // 2, h - 1))
        assert mask.contains(QPoint(w // 2, 0))
        # 마스크가 바 안쪽 대부분은 살려 둔다 — 잘려 나가는 것은 모서리뿐
        radius = theme.METRICS["cardRadius"]
        assert mask.contains(QPoint(radius, h - 1)) and mask.contains(QPoint(w - 1 - radius, h - 1))
