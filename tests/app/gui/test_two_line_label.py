"""`TwoLineLabel` 단위 테스트 — 한 줄로 잇기 → 두 줄 → 둘째 줄만 말줄임, 그리고 높이 (#309).

폭은 절대 px가 아니라 라벨의 글꼴로 잰 글 폭에서 유도한다. 라벨은 최상위 창이 아니라 다른
위젯의 자식으로 둔다 — 최상위 창은 플랫폼의 최소 폭에 걸려 요청한 폭이 되지 않을 수 있다.
"""

import pytest
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from app.widgets.eliding_label import TwoLineLabel
from tests.app.gui.card_helpers import shown

HEAD = "Sections add up to more than the video"
TAIL = "Sections 18:30:00.000 · Video 08:00:00.000"  # HEAD보다 길다
WHOLE = HEAD + " · " + TAIL


@pytest.fixture
def label(qapp):
    """보이는 위젯 안에 놓인 라벨 — 두 문장은 HEAD · TAIL."""
    host = QWidget()
    host.resize(4000, 200)
    made = TwoLineLabel(host)
    made.setLines(HEAD, TAIL)
    host.show()
    QApplication.processEvents()
    yield made
    host.hide()


def _width_of(label: TwoLineLabel, text: str) -> int:
    """그 글을 한 줄로 적는 폭(px) — 라벨의 글꼴로 잰다."""
    return QFontMetrics(label.font()).horizontalAdvance(text)


def _set_width(label: TwoLineLabel, width: int) -> None:
    """라벨의 폭을 width로 놓고, 실제 폭이 그 값인지 확인한다."""
    label.resize(width, label.sizeHint().height())
    QApplication.processEvents()
    assert label.width() == width


def test_two_sentences_that_fit_are_joined_on_one_line(label):
    """폭이 이은 글의 한 줄 폭과 같으면 두 문장을 " · "로 이어 한 줄로 보여야 한다.

    폭 = WHOLE의 한 줄 폭 -> 보이는 글 == HEAD + " · " + TAIL
    """
    _set_width(label, _width_of(label, WHOLE))

    assert shown(label) == WHOLE


def test_one_pixel_too_narrow_puts_each_sentence_on_its_own_line(label):
    """폭이 한 줄 폭보다 1px 좁으면 두 문장을 한 줄씩, 줄이지 않고 보여야 한다.

    폭 = WHOLE의 한 줄 폭 − 1 (TAIL 하나는 들어간다)
    -> 보이는 글 == HEAD + 줄바꿈 + TAIL, 툴팁 == WHOLE, text() == WHOLE
    """
    _set_width(label, _width_of(label, WHOLE) - 1)

    assert shown(label) == HEAD + "\n" + TAIL
    assert (label.toolTip(), label.text()) == (WHOLE, WHOLE)


def test_second_line_that_exactly_fits_is_not_elided(label):
    """폭이 둘째 문장의 폭과 같으면 둘째 줄을 줄이지 않아야 한다.

    폭 = TAIL의 한 줄 폭 -> 보이는 글 == HEAD + 줄바꿈 + TAIL
    """
    _set_width(label, _width_of(label, TAIL))

    assert shown(label) == HEAD + "\n" + TAIL


def test_only_the_second_line_is_elided_when_it_does_not_fit(label):
    """둘째 문장이 폭에 안 들어가면 첫 줄은 그대로 두고 둘째 줄만 말줄임해야 한다.

    폭 = TAIL의 한 줄 폭 − 1
    -> 첫 줄 == HEAD(그대로), 둘째 줄이 "…"로 끝나고 TAIL보다 짧다, 툴팁 == WHOLE
    """
    _set_width(label, _width_of(label, TAIL) - 1)

    first, second = shown(label).split("\n")
    assert first == HEAD
    assert second.endswith("…") and len(second) < len(TAIL)
    assert label.toolTip() == WHOLE


def test_first_line_is_kept_whole_even_when_it_is_wider_than_the_label(label):
    """첫 문장이 폭보다 넓어도 첫 줄은 줄이지 않아야 한다.

    폭 = HEAD의 한 줄 폭 ÷ 2 -> 첫 줄 == HEAD, 둘째 줄이 "…"로 끝난다
    """
    _set_width(label, _width_of(label, HEAD) // 2)

    first, second = shown(label).split("\n")
    assert first == HEAD
    assert second.endswith("…")


def test_widening_joins_the_sentences_back_on_one_line(label):
    """둘째 줄이 말줄임된 뒤 폭을 넓히면 다시 한 줄로 이은 글이어야 한다.

    폭을 TAIL의 폭 − 1로 줄였다가 WHOLE의 폭으로 넓힘 -> 보이는 글 == WHOLE
    """
    _set_width(label, _width_of(label, TAIL) - 1)
    assert shown(label).endswith("…")  # 전제: 줄였을 때 말줄임됐다

    _set_width(label, _width_of(label, WHOLE))

    assert shown(label) == WHOLE


def test_empty_sentences_show_nothing(label):
    """두 문장이 모두 비면 보이는 글 · 툴팁 · text()가 모두 비어야 한다.

    setLines("", "") -> 보이는 글 "", 툴팁 "", text() ""
    """
    label.setLines("", "")
    QApplication.processEvents()

    assert (shown(label), label.toolTip(), label.text()) == ("", "", "")


def test_single_sentence_is_elided_like_a_plain_eliding_label(label):
    """문장 하나만 넣으면 한 줄로 두고, 폭에 안 들어가면 그 줄을 말줄임해야 한다.

    setText(HEAD), 폭 = HEAD의 한 줄 폭 − 1 -> 줄바꿈 없음, "…"로 끝난다, 툴팁 == HEAD
    """
    label.setText(HEAD)

    _set_width(label, _width_of(label, HEAD) - 1)

    visible = shown(label)
    assert "\n" not in visible and visible.endswith("…")
    assert label.toolTip() == HEAD


def test_height_asked_is_two_lines_with_or_without_text(qapp):
    """청하는 높이는 글이 있든 없든 두 줄의 높이여야 한다 — 글이 생겨도 줄의 높이가 같다.

    빈 라벨과 두 문장을 넣은 라벨
    -> 둘 다 minimumSizeHint · sizeHint의 높이 == 글꼴의 줄 간격 x 2
    """
    empty, filled = TwoLineLabel(), TwoLineLabel()
    filled.setLines(HEAD, TAIL)
    lines = QFontMetrics(empty.font()).lineSpacing() * 2

    heights = [
        hint.height()
        for made in (empty, filled)
        for hint in (made.minimumSizeHint(), made.sizeHint())
    ]

    assert heights == [lines] * 4


def test_width_asked_is_small_at_least_and_the_joined_line_at_best(qapp):
    """청하는 최소 폭은 글과 무관하게 작고, 바라는 폭은 한 줄로 이은 글을 담는 폭이어야 한다.

    짧은 두 문장("a", "b")과 HEAD · TAIL
    -> 두 라벨의 minimumSizeHint 폭이 같고 QLabel이 WHOLE에 청하는 폭보다 작다,
       HEAD · TAIL 라벨의 sizeHint 폭 >= WHOLE의 한 줄 폭
    """
    short, long = TwoLineLabel(), TwoLineLabel()
    short.setLines("a", "b")
    long.setLines(HEAD, TAIL)
    plain = QLabel(WHOLE)

    assert long.minimumSizeHint().width() == short.minimumSizeHint().width()
    assert long.minimumSizeHint().width() < plain.minimumSizeHint().width()
    assert long.sizeHint().width() >= _width_of(long, WHOLE)
