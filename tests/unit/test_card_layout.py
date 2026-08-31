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
    """3행 — 해상도 pill 좌측 고정 간격, 남는 공간은 가운데 한 곳,
    파일 크기는 우측 끝."""

    @pytest.mark.parametrize("width", WIDTHS)
    def test_pill_gaps_are_fixed(self, qapp, width):
        widget = _make_widget(qapp)
        _at_width(widget, width)
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


class TestPathShownOnlyWhenDifferent:
    """경로는 전역 설정 경로와 다를 때만 3행에 보인다(#245) — 같은 값을
    카드마다 반복하는 것이 정보 과다의 큰 몫이었다. 다르다는 것 자체가
    정보다."""

    def test_path_hidden_when_it_matches_the_global_default(self, qapp):
        widget = _make_widget(qapp)  # 아이템 경로 == 전역("C:/dl", 픽스처 주입)
        _at_width(widget, 900)
        assert not widget.directoryLabel.isVisible()

    def test_path_shown_when_it_differs(self, qapp):
        widget = _make_widget(qapp)
        widget.item.download_path = "D:/다른/폴더"
        widget.setData(widget.item, 0)
        _at_width(widget, 900)
        assert widget.directoryLabel.isVisible()
        # 경로는 파일 크기 왼쪽에 고정 간격으로 붙는다(우측 끝선 유지)
        assert _gap(widget.directoryLabel, widget.fileSizeLabel) == FIXED_SPACING


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
