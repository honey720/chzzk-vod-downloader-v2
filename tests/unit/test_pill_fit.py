"""해상도 pill의 글자가 폭에 맞춰지는지 검증 — 보조 글자와 본 글자가 겹치지 않는다 (#309).

접힌 pill은 크기 글(말줄임하지 않는다)에 밀려 자연 폭보다 좁아질 수 있다. 그때 보조 글자
("60fps")를 먼저 그리지 않고, 그래도 모자라면 본 글자를 말줄임한다. 줄였으면 툴팁의 첫 줄이
전체 글자다.

겹침은 두 가지로 잰다 — 폰트 치수로 계산한 글자 자리(제품의 계산을 부르지 않는다)와, 그린
그림이 "그 글자만 가진 pill"의 그림과 같은지다.
"""

import pytest
from PySide6.QtCore import QEvent, QPoint
from PySide6.QtGui import QFontMetrics, QHelpEvent
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

import app.theme as theme
import main as main_module
from app.viewmodels.data import ContentItem
from app.widgets.pill import CARET_GAP, CARET_WIDTH, EDGE_PADDING, SECONDARY_GAP, ResolutionPill
from app.widgets.widget import ContentItemWidget
from core.api.playback_tracks import list_streams, playback_tracks
from core.models.download_state import DownloadState
from core.models.plan import TimeRange
from tests.unit.card_helpers import drop_new_top_levels, hold_style, snapshot_top_levels
from tests.unit.stream_samples import S1

MAIN = "1080p(source)"
SECONDARY = "60fps"
ELLIPSIS = "…"


@pytest.fixture(autouse=True)
def _apply_production_qss(qapp):
    """실제 전역 QSS를 태운 상태에서 잰다 — pill의 여백은 QSS가 준다."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))


@pytest.fixture(autouse=True)
def _drop_windows(qapp):
    """이 파일의 테스트가 띄운 창을 테스트 끝에 파괴한다."""
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


def _pump() -> None:
    for _ in range(3):
        QApplication.processEvents()


def _pill(text: str, secondary: str = "", *, caret: bool = True, selected: bool = True):
    """레이아웃 없는 창 안의 pill 하나 — 폭은 테스트가 정한다."""
    window = QWidget()
    window.resize(400, 60)
    pill = ResolutionPill(text, window)
    pill.setSecondaryText(secondary)
    pill.setFixedHeight(theme.METRICS["pillHeight"])
    pill.setSelected(selected)
    pill.setCaret(caret)
    pill.move(10, 10)
    window.show()
    _pump()
    return window, pill


def _set_width(pill: ResolutionPill, width: int) -> None:
    pill.resize(width, pill.height())
    _pump()
    assert pill.width() == width  # resize()는 요청이다 — 그 폭이 됐는지 본다


def _text_room(pill: ResolutionPill) -> int:
    """pill 안에서 글자가 쓸 수 있는 폭 — 좌우 여백과 ▾ 몫을 뺀 것. pill.py의 상수만으로 센다."""
    caret = CARET_WIDTH + CARET_GAP if pill.hasCaret() else 0
    return pill.width() - 2 * EDGE_PADDING - caret


def _advance(pill: ResolutionPill, text: str) -> int:
    return QFontMetrics(pill.font()).horizontalAdvance(text)


@pytest.mark.parametrize("caret", [True, False], ids=["접힌 pill(▾)", "▾ 없는 pill"])
def test_texts_drawn_at_any_width_fit_without_overlapping(qapp, caret):
    """pill이 그리는 본 글자와 보조 글자는 어떤 폭에서도 겹치지 않고 글자 자리 안에 있어야 한다.

    "1080p(source)" + "60fps". 폭을 자연 폭에서 1px까지 1px씩 줄임
    -> 폭마다: 본 글자 폭 + (보조 글자가 있으면 간격 + 보조 글자 폭) <= 글자 자리,
       말줄임한 폭이 하나 이상 있고 보조 글자를 뺀 폭이 하나 이상 있다
    """
    window, pill = _pill(MAIN, SECONDARY, caret=caret)
    natural = pill.sizeHint().width()
    seen = set()

    for width in range(natural, 0, -1):
        _set_width(pill, width)
        main, secondary = pill.shownTexts()
        used = _advance(pill, main)
        if secondary:
            used += SECONDARY_GAP + _advance(pill, secondary)
        if main or secondary:
            assert used <= _text_room(pill), f"폭 {width}px: 글자 {used}px이 자리를 넘는다"
        seen.add((main == MAIN, bool(secondary)))

    assert seen >= {(True, True), (True, False), (False, False)}


def test_narrowing_hides_the_secondary_text_first_and_then_elides_the_main_text(qapp):
    """폭이 줄면 먼저 보조 글자가 사라지고, 그 뒤에 본 글자가 말줄임돼야 한다 — 거꾸로는 없다.

    "1080p(source)" + "60fps". 폭을 자연 폭에서 1px까지 줄임
    -> 자연 폭: 둘 다 그대로. 단계는 (둘 다) → (본 글자만) → (말줄임)의 순서로만 바뀐다.
       보조 글자가 보이는 동안 본 글자는 늘 그대로다. 말줄임한 글자는 "…"로 끝나고 본 글자의 앞부분이다
    """
    window, pill = _pill(MAIN, SECONDARY)
    natural = pill.sizeHint().width()
    stages = []

    for width in range(natural, 0, -1):
        _set_width(pill, width)
        main, secondary = pill.shownTexts()
        if secondary:
            assert (main, secondary) == (MAIN, SECONDARY)
            stage = 0
        elif main == MAIN:
            stage = 1
        else:
            stage = 2
            if main:
                assert main.endswith(ELLIPSIS) and MAIN.startswith(main[: -len(ELLIPSIS)])
        stages.append(stage)

    assert stages[0] == 0, "자연 폭에서는 둘 다 그린다"
    assert stages == sorted(stages)
    assert set(stages) == {0, 1, 2}


def _picture(pill: ResolutionPill):
    return pill.grab().toImage()


@pytest.mark.parametrize("squeeze", [1, 12], ids=["1px 모자람", "12px 모자람"])
def test_pill_too_narrow_for_the_secondary_text_is_drawn_like_a_pill_without_it(qapp, squeeze):
    """보조 글자가 들어가지 않는 폭의 pill은 보조 글자가 없는 pill과 똑같이 그려져야 한다.

    "1080p(source)" + "60fps"인 pill을 자연 폭 − squeeze px로, 본 글자만 가진 pill을 같은 폭으로
    -> 두 그림이 같다(보조 글자가 본 글자 위에 그려지지 않았다). 자연 폭에서는 두 그림이 다르다
    """
    window, pill = _pill(MAIN, SECONDARY)
    other_window, plain = _pill(MAIN)
    natural = pill.sizeHint().width()

    _set_width(pill, natural)
    _set_width(plain, natural)
    assert _picture(pill) != _picture(plain), "전제: 자연 폭에서는 보조 글자가 그려진다"

    _set_width(pill, natural - squeeze)
    _set_width(plain, natural - squeeze)

    assert pill.shownTexts() == (MAIN, "")
    assert _picture(pill) == _picture(plain)


def test_pill_too_narrow_for_the_main_text_is_drawn_like_a_pill_with_the_elided_text(qapp):
    """본 글자도 들어가지 않는 폭의 pill은 말줄임한 글자만 가진 pill과 똑같이 그려져야 한다.

    "1080p(source)" + "60fps"인 pill을 (보조 글자 없는 자연 폭 − 20px)로
    -> 그린 글자가 "…"로 끝나고, 그 글자만 가진 pill을 같은 폭으로 그린 그림과 같다
    """
    window, pill = _pill(MAIN, SECONDARY)
    whole_window, whole = _pill(MAIN)
    width = whole.sizeHint().width() - 20  # 본 글자만 가진 pill의 자연 폭보다 20px 좁다
    _set_width(pill, width)
    main, secondary = pill.shownTexts()
    assert secondary == "" and main.endswith(ELLIPSIS) and main != MAIN

    elided_window, elided = _pill(main)
    _set_width(elided, width)
    assert elided.shownTexts() == (main, ""), "전제: 견줄 pill은 그 글자를 그대로 그린다"

    assert _picture(pill) == _picture(elided)
    assert pill.text() == MAIN  # 버튼의 글자는 바뀌지 않는다 — 그리는 것만 줄인다


class _TipSpy:
    """pill이 QToolTip에 한 요청을 기록한다 — 실제 툴팁 창은 띄우지 않는다.

    실제 QToolTip은 플랫폼의 애니메이션 설정에 따라 바로 보이지 않는다. pill이 무엇을 띄우라고
    했는지만 잰다.
    """

    def __init__(self):
        self.shown: list[str] = []

    def showText(self, _position, text, _widget=None):
        self.shown.append(text)

    def hideText(self):
        pass


def _tooltips_requested_by(pill: ResolutionPill, monkeypatch) -> list[str]:
    """pill에 툴팁 이벤트를 보내고, pill이 띄우라고 한 글들을 돌려준다."""
    spy = _TipSpy()
    monkeypatch.setattr("app.widgets.pill.QToolTip", spy)
    spot = QPoint(2, 2)
    QApplication.sendEvent(pill, QHelpEvent(QEvent.Type.ToolTip, spot, pill.mapToGlobal(spot)))
    _pump()
    return spy.shown


@pytest.mark.parametrize(
    ("given", "narrow", "expected"),
    [
        ("", False, ""),
        ("1.23 GB", False, "1.23 GB"),
        ("", True, "1080p(source) 60fps"),
        ("1.23 GB", True, "1080p(source) 60fps\n1.23 GB"),
    ],
    ids=["자연 폭 · 툴팁 없음", "자연 폭 · 크기 툴팁", "좁음 · 툴팁 없음", "좁음 · 크기 툴팁"],
)
def test_tooltip_of_a_shortened_pill_starts_with_the_whole_text(
    qapp, monkeypatch, given, narrow, expected
):
    """글자를 줄인 pill의 툴팁은 첫 줄이 전체 글자여야 하고, 줄이지 않은 pill의 툴팁은 받은 글 그대로여야 한다.

    "1080p(source)" + "60fps", setToolTip(given). 자연 폭 / 자연 폭 − 5px에서 툴팁 이벤트
    -> pill이 띄우라고 한 글 == [expected] (빈 글이면 띄우지 않는다)
    """
    window, pill = _pill(MAIN, SECONDARY)
    pill.setToolTip(given)
    _set_width(pill, pill.sizeHint().width() - (5 if narrow else 0))

    assert _tooltips_requested_by(pill, monkeypatch) == ([expected] if expected else [])


def test_tooltip_of_an_elided_pill_without_secondary_text_is_the_whole_text(qapp, monkeypatch):
    """보조 글자가 없는 pill도 본 글자를 말줄임했으면 툴팁이 전체 글자여야 한다.

    "1080p(source)"만, 자연 폭 − 20px -> pill이 띄우라고 한 글 == ["1080p(source)"]
    """
    window, pill = _pill(MAIN)
    _set_width(pill, pill.sizeHint().width() - 20)
    assert pill.shownTexts()[0].endswith(ELLIPSIS)

    assert _tooltips_requested_by(pill, monkeypatch) == [MAIN]


# ================================================================ 카드 안에서


def _card(sections=()):
    """원본 1080p 60fps가 든 대기 카드(인코딩 전 다시보기) — 보이는 창 안에 놓는다."""
    reps = list_streams(S1.master, playback_tracks(S1.playback))
    item = ContentItem(
        "https://chzzk.naver.com/video/1",
        {
            "title": "제목",
            "category": "",
            "channelName": "채널",
            "createdDate": "",
            "duration": 28800,
        },
        reps,
        reps[-1][0],
        reps[-1][1],
        "C:/dl",
        "m3u8",
        None,
    )
    item.downloadState = DownloadState.WAITING
    widget = ContentItemWidget(item, 0)
    widget.addRepresentationButtons()
    item.selections = tuple(sections)
    widget.setData(item, 0)
    window = QWidget()
    column = QVBoxLayout(window)
    column.setContentsMargins(0, 0, 0, 0)
    column.addWidget(widget)
    window.resize(1400, widget.sizeHint().height())
    window.show()
    _pump()
    return window, widget


@pytest.mark.parametrize(
    "sections",
    [(), (TimeRange(600.0, 1200.0), TimeRange(1800.0, 2400.0))],
    ids=["구간 없음", "구간 둘"],
)
def test_collapsed_pill_squeezed_by_the_size_text_never_overlaps_its_texts(qapp, sections):
    """카드가 좁아 접힌 pill이 눌려도 pill의 글자는 서로 겹치지 않고, pill은 크기 글과 겹치지 않아야 한다.

    원본 1080p 60fps 대기 카드. 창을 1px씩 좁혀 최소 폭까지 감
    -> 접힌 pill이 자연 폭보다 좁아지는 폭이 있고(전제), 그 뒤의 모든 폭에서:
       글자 폭의 합 <= 글자 자리, pill의 오른쪽 끝 <= 크기 글의 왼쪽 끝, 줄인 pill의 툴팁 첫 줄이 전체 글자다.
       pill이 눌리기 시작한 첫 폭에서는 보조 글자만 사라진다(본 글자는 그대로)
    """
    window, widget = _card(sections)
    squeezed_at = []
    width = window.width()
    while True:
        window.resize(width, window.height())
        _pump()
        if window.width() != width:
            break  # 최소 폭에 닿았다
        pill = widget._selectedButton
        assert pill is not None and pill.isVisibleTo(window)
        assert widget.fileSizeLabel.isVisibleTo(window)
        if widget.pillMode() == "collapsed" and pill.width() < pill.sizeHint().width():
            main, secondary = pill.shownTexts()
            if not squeezed_at:
                assert (main, secondary) == (pill.text(), "")
            squeezed_at.append(width)
            used = _advance(pill, main)
            if secondary:
                used += SECONDARY_GAP + _advance(pill, secondary)
            if main or secondary:
                assert used <= _text_room(pill), f"창 {width}px: 글자 {used}px이 자리를 넘는다"
            assert pill.x() + pill.width() <= widget.fileSizeLabel.x()
            assert pill.shownToolTip().splitlines()[0] == f"{pill.text()} {pill.secondaryText()}"
        width -= 1

    assert squeezed_at, "전제: 이 카드는 최소 폭에 닿기 전에 접힌 pill이 눌린다"
