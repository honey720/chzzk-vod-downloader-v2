"""`WrappingLabel` 단위 테스트 — 한 줄 → 두 줄 → 말줄임의 순서와 높이 (#309).

폭은 절대 px가 아니라 라벨의 글꼴로 잰 글 폭에서 유도한다. 라벨은 최상위 창이 아니라 다른
위젯의 자식으로 둔다 — 최상위 창은 플랫폼의 최소 폭에 걸려 요청한 폭이 되지 않을 수 있다.
"""

import pytest
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from app.widgets.eliding_label import WrappingLabel
from tests.unit.card_helpers import shown

TEXT = "Sections add up to 18:30:00.000 — longer than the video (08:00:00.000)"


@pytest.fixture
def label(qapp):
    """보이는 위젯 안에 놓인 라벨 — 글은 TEXT."""
    host = QWidget()
    host.resize(4000, 200)
    made = WrappingLabel(host)
    made.setText(TEXT)
    host.show()
    QApplication.processEvents()
    yield made
    host.hide()


def _whole_width(label: WrappingLabel) -> int:
    """TEXT를 한 줄로 적는 폭(px) — 라벨의 글꼴로 잰다."""
    return QFontMetrics(label.font()).horizontalAdvance(TEXT)


def _set_width(label: WrappingLabel, width: int) -> None:
    """라벨의 폭을 width로 놓고, 실제 폭이 그 값인지 확인한다."""
    label.resize(width, label.sizeHint().height())
    QApplication.processEvents()
    assert label.width() == width


def test_text_that_fits_is_shown_on_one_line(label):
    """폭이 글의 한 줄 폭과 같으면 나누지 않고 전문을 보여야 한다.

    폭 = TEXT의 한 줄 폭
    -> 보이는 글 == TEXT, wordWrap 거짓
    """
    _set_width(label, _whole_width(label))

    assert (shown(label), label.wordWrap()) == (TEXT, False)


def test_text_one_pixel_too_wide_is_split_over_two_lines(label):
    """폭이 한 줄 폭보다 1px 좁으면 말줄임하지 않고 두 줄로 나눠 전문을 보여야 한다.

    폭 = TEXT의 한 줄 폭 − 1 (두 줄이면 들어간다 — 첫 줄에 못 들어간 낱말만 둘째 줄로 간다)
    -> 보이는 글 == TEXT("…" 없음), wordWrap 참, 툴팁 == TEXT
    """
    _set_width(label, _whole_width(label) - 1)

    assert (shown(label), label.wordWrap()) == (TEXT, True)
    assert label.toolTip() == TEXT


def test_text_that_needs_more_than_two_lines_is_elided_on_one_line(label):
    """두 줄에도 안 들어가는 폭이면 한 줄로 말줄임하고 툴팁에 전문을 둬야 한다.

    폭 = TEXT의 한 줄 폭 ÷ 4 (두 줄에 들어가는 글은 많아야 폭의 두 배 — 한 줄 폭의 절반이다)
    -> 보이는 글이 "…"로 끝나고 TEXT보다 짧다, wordWrap 거짓, 툴팁 == TEXT, text() == TEXT
    """
    _set_width(label, _whole_width(label) // 4)

    visible = shown(label)
    assert visible.endswith("…") and len(visible) < len(TEXT)
    assert label.wordWrap() is False
    assert (label.toolTip(), label.text()) == (TEXT, TEXT)


def test_widening_brings_the_text_back_to_one_line(label):
    """말줄임된 뒤 폭을 넓히면 다시 한 줄의 전문이어야 한다.

    폭을 한 줄 폭 ÷ 4로 줄였다가 한 줄 폭으로 넓힘
    -> 보이는 글 == TEXT, wordWrap 거짓
    """
    _set_width(label, _whole_width(label) // 4)
    assert shown(label).endswith("…")  # 전제: 줄였을 때 말줄임됐다

    _set_width(label, _whole_width(label))

    assert (shown(label), label.wordWrap()) == (TEXT, False)


def test_height_asked_is_two_lines_with_or_without_text(qapp):
    """청하는 높이는 글이 있든 없든 두 줄의 높이여야 한다 — 글이 생겨도 줄의 높이가 같다.

    빈 라벨과 TEXT를 넣은 라벨
    -> 둘 다 minimumSizeHint · sizeHint의 높이 == 글꼴의 줄 간격 x 2
    """
    empty, filled = WrappingLabel(), WrappingLabel()
    filled.setText(TEXT)
    lines = QFontMetrics(empty.font()).lineSpacing() * 2

    heights = [
        hint.height()
        for made in (empty, filled)
        for hint in (made.minimumSizeHint(), made.sizeHint())
    ]

    assert heights == [lines] * 4


def test_height_does_not_depend_on_the_width(qapp):
    """폭에 따라 높이가 달라지는 라벨로 다뤄지지 않아야 한다.

    TEXT를 넣은 라벨 -> hasHeightForWidth() 거짓, heightForWidth(10) == -1
    """
    made = WrappingLabel()
    made.setText(TEXT)

    assert (made.hasHeightForWidth(), made.heightForWidth(10)) == (False, -1)


def test_minimum_width_asked_is_small_whatever_the_text(qapp):
    """청하는 최소 폭은 글의 길이와 무관하게 말줄임표 하나 남짓이어야 한다.

    짧은 글 "a"와 TEXT x 5
    -> 두 라벨의 minimumSizeHint 폭이 같고, QLabel이 TEXT에 청하는 폭보다 작다
    """
    short, long = WrappingLabel(), WrappingLabel()
    short.setText("a")
    long.setText(TEXT * 5)
    plain = QLabel(TEXT)

    assert long.minimumSizeHint().width() == short.minimumSizeHint().width()
    assert long.minimumSizeHint().width() < plain.minimumSizeHint().width()
