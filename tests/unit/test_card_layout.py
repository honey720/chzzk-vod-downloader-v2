"""카드 가로 배치 회귀 게이트 (#244 재설계 — 오너 확정 구조를 기하값으로 고정).

밀도 게이트(`test_card_density.py`)는 세로만 본다 — 가로 배치에 게이트가
없던 동안 "넓은 창에서 요소가 균등 분배로 흩어지는" 결함이 시안 리뷰를
그대로 통과해 실기에서야 잡혔다(1600px에서 해상도 버튼 간격 335px 실측).
이 파일이 그 구멍을 막는다.

**고정하는 불변식(#244 확정 설계)**:
- 좌측 기준선은 둘뿐 — 썸네일 왼쪽(=cardPadding)과 컨텐츠 열 왼쪽.
  4개 행(1행 채널·2행 제목·3행 해상도·4행 경로) 전부 컨텐츠 열의 같은
  x에서 시작한다. 예외 없다.
- 우측 끝도 하나 — 삭제(1행)와 파일 크기(3행)의 오른쪽 끝이 같은 x.
- 남는 공간은 각 행에서 딱 한 곳(가운데 스트레치/경로 라벨)만 흡수한다.
  요소 사이 간격은 고정값이라 창이 넓어져도 벌어지지 않는다.

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
    ROW_HEADS = ("channelImageLabel", "titleLabel", "openDirectoryButton")

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
        폭이 그 16/9배인지를 직접 잰다. 글자·간격 토큰이 바뀌어도 이
        관계는 유지돼야 한다(고정 크기를 박으면 여기서 잡힌다).
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
    """1행 — 좌측(채널이미지·채널명·종류)·우측(상태아이콘·상태·진행률·삭제)
    군집이 각자 붙고, 남는 공간은 그 사이 스트레치 한 곳만 흡수한다."""

    LEFT_CLUSTER = ("channelImageLabel", "channelNameLabel", "contentTypeLabel")
    RIGHT_CLUSTER = ("stateIconLabel", "statusLabel", "progressLabel", "deleteButton")

    @pytest.mark.parametrize("width", WIDTHS)
    def test_intra_cluster_gaps_are_fixed(self, qapp, width):
        widget = _make_widget(qapp)
        _at_width(widget, width)
        for cluster in (self.LEFT_CLUSTER, self.RIGHT_CLUSTER):
            for left_name, right_name in zip(cluster, cluster[1:]):
                gap = _gap(getattr(widget, left_name), getattr(widget, right_name))
                assert gap == FIXED_SPACING, (
                    f"폭 {width}px에서 {left_name}→{right_name} 간격이 {gap}px "
                    f"(고정값 {FIXED_SPACING}px 기대) — 군집 내부가 흩어졌다"
                )

    def test_free_space_lives_only_between_the_clusters(self, qapp):
        widget = _make_widget(qapp)
        middle_gaps = []
        for width in WIDTHS:
            _at_width(widget, width)
            middle_gaps.append(_gap(widget.contentTypeLabel, widget.stateIconLabel))
        assert middle_gaps[0] < middle_gaps[1] < middle_gaps[2], (
            f"군집 사이 가운데 간격이 폭을 따라 늘지 않는다: {middle_gaps} — "
            "남는 공간이 다른 곳으로 새고 있다"
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


class TestDirectoryRow:
    """4행 — 폴더 버튼이 경로 왼쪽에 한 덩어리로 붙는다(#244 — 우측 끝에
    혼자 떠 있지 않게). 남는 공간은 경로 라벨이 흡수한다."""

    @pytest.mark.parametrize("width", WIDTHS)
    def test_folder_button_is_glued_to_the_path(self, qapp, width):
        widget = _make_widget(qapp)
        _at_width(widget, width)
        gap = _gap(widget.openDirectoryButton, widget.directoryLabel)
        assert gap == FIXED_SPACING, (
            f"폭 {width}px에서 폴더 버튼→경로 간격이 {gap}px — 한 덩어리가 아니다"
        )


class TestLabelsDoNotGrow:
    """텍스트 라벨들은 창이 더 넓어져도 자기 텍스트 폭에 머물러야 한다.

    간격(edge-to-edge)만 재면 못 잡는 흩어짐 모드 — 라벨이 stretch/
    Expanding을 받으면 이웃 간격은 고정값 그대로인데 라벨 폭이 부풀어
    좌측 정렬 텍스트 뒤로 빈 공간이 생긴다(실기에서 실측된 바로 그 모드).
    여유가 생긴 900px의 폭(자연 텍스트 폭)과 1600px의 폭이 같아야 한다.
    """

    LABELS = ("contentTypeLabel", "statusLabel", "progressLabel", "fileSizeLabel")

    def test_labels_do_not_grow_when_the_window_widens(self, qapp):
        widget = _make_widget(qapp)
        _at_width(widget, 900)
        at_900 = {name: getattr(widget, name).width() for name in self.LABELS}
        _at_width(widget, 1600)
        for name in self.LABELS:
            width_1600 = getattr(widget, name).width()
            assert width_1600 == at_900[name], (
                f"{name} 폭이 900px→1600px에서 {at_900[name]}→{width_1600}으로 변한다 — "
                "라벨이 남는 공간을 나눠 먹고 있다"
            )


class TestProgressBarOnlyWhenRunning:
    """하단 진행바는 진행 중일 때만 보인다(#244 확정 — 빈 막대는 정보가
    없고 자리만 먹는다). 보일 때는 카드 아래 가장자리 근처 전체 폭이다."""

    def test_bar_hidden_for_idle_states(self, qapp):
        for state in (DownloadState.WAITING, DownloadState.FINISHED, DownloadState.FAILED):
            widget = _make_widget(qapp)
            widget.item.downloadState = state
            widget.setData(widget.item, 0)
            _at_width(widget, 900)
            assert not widget.progressBar.isVisible(), (
                f"{state}에서 진행바가 보인다 — 진행 중일 때만 보여야 한다"
            )

    def test_bar_visible_and_full_width_when_running(self, qapp):
        widget = _make_widget(qapp)
        widget.item.downloadState = DownloadState.RUNNING
        widget.item.download_progress = 42
        widget.setData(widget.item, 0)
        _at_width(widget, 900)
        assert widget.progressBar.isVisible()
        # 전체 폭(테두리 안쪽 1px 여백 허용) + 카드 바닥에 붙어 있다
        frame = widget.contentFrame
        assert widget.progressBar.width() >= frame.width() - 4
        bar_bottom = widget.progressBar.mapTo(frame, widget.progressBar.rect().bottomLeft()).y()
        assert frame.height() - bar_bottom <= 3, (
            f"진행바가 카드 바닥에서 {frame.height() - bar_bottom}px 떠 있다"
        )
