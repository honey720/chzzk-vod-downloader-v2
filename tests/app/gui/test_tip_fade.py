"""툴팁 말풍선 페이드 — 나타날 때 투명 → 불투명, 닫힐 때 불투명 → 투명 뒤 숨음 (#610).

시간은 테스트가 짧게 건다(`FADE_IN_MS` · `FADE_OUT_MS`) — 제품 값(120 · 80)을 기다리지 않는다. 들어감 · 나감은
테스트가 보낸 이벤트만 쓴다(포인터를 옮기지 않는다).
"""

from collections.abc import Iterator

import pytest
from pytestqt.qtbot import QtBot
from PySide6.QtCore import QEvent, QVariantAnimation
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLineEdit, QPushButton, QWidget

import app.viewmodels.card_display as card_display
from app.widgets import tip_bubble
from app.widgets.tip_bubble import ErrorTip, TipBubble, install_tips
from tests.app.gui.card_helpers import enter, leave

FADE_IN_MS = 60  # 테스트가 거는 나타남 시간(ms)
FADE_OUT_MS = 40  # 테스트가 거는 닫힘 시간(ms)


@pytest.fixture(autouse=True)
def _fade(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """페이드 시간을 짧게 걸고 애니메이션 켬으로 둔다 — 앞뒤로 말풍선을 바로 닫는다."""
    monkeypatch.setattr(tip_bubble, "TIP_FADE_IN_MS", FADE_IN_MS)
    monkeypatch.setattr(tip_bubble, "TIP_FADE_OUT_MS", FADE_OUT_MS)
    monkeypatch.setattr(tip_bubble, "HOVER_TIP_DELAY_MS", 30)
    card_display.use(card_display.CardDisplay(animations=True))
    _hide_now()
    yield
    _hide_now()
    card_display.use(None)


def _hide_now() -> None:
    bubble = tip_bubble.current_bubble()
    if bubble is not None:
        bubble.hideNow()


@pytest.fixture
def buttons(qtbot: QtBot) -> Iterator[tuple[QPushButton, QPushButton]]:
    """툴팁이 있는 버튼 둘을 든 창 — 말풍선 필터를 건다. 창은 테스트가 끝날 때까지 붙잡는다.

    `qtbot.addWidget`은 창을 붙잡지 않는다 — 지역 변수만 두면 픽스처가 끝날 때 창이 지워진다.
    """
    host = QWidget()
    qtbot.addWidget(host)
    layout = QHBoxLayout(host)
    first, second = QPushButton("첫째"), QPushButton("둘째")
    first.setToolTip("첫째 툴팁")
    second.setToolTip("둘째 툴팁")
    layout.addWidget(first)
    layout.addWidget(second)
    host.show()
    QTest.qWaitForWindowExposed(host)
    install_tips(host)
    yield first, second
    host.hide()


def _bubble() -> TipBubble:
    bubble = tip_bubble.current_bubble()
    assert bubble is not None, "전제: 말풍선이 만들어졌다"
    return bubble


def _show_fully(qtbot: QtBot, button: QPushButton) -> TipBubble:
    enter(button)
    bubble = _bubble()
    qtbot.waitUntil(lambda: bubble.fade() is None, timeout=2000)
    return bubble


def _animations(bubble: QWidget) -> list[QVariantAnimation]:
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    return bubble.findChildren(QVariantAnimation)


def test_bubble_appears_from_transparent(qtbot: QtBot, buttons) -> None:
    """버튼에 들어가면 말풍선이 불투명도 0으로 보이기 시작해 페이드 뒤 1이어야 한다.

    나타남 60ms · 첫째 버튼 들어감 -> 바로 보임 · 불투명도 0 · 페이드 있음, 끝나면 불투명도 1 · 페이드 없음
    """
    first, _second = buttons
    enter(first)
    bubble = _bubble()
    assert bubble.isVisible() and bubble.target is first
    assert bubble.opacity() == 0.0
    assert bubble.fade() is not None
    qtbot.waitUntil(lambda: bubble.fade() is None, timeout=2000)
    assert bubble.opacity() == 1.0


def test_bubble_fades_out_then_hides(qtbot: QtBot, buttons) -> None:
    """다 뜬 말풍선은 나가면 바로 숨지 않고 옅어진 뒤 숨어야 한다.

    닫힘 40ms · 첫째 버튼 다 뜸 → 나감 -> 보임 · 닫히는 중, 불투명도 1 미만을 지나 숨음 · 대상 없음
    """
    first, _second = buttons
    bubble = _show_fully(qtbot, first)
    leave(first)
    assert bubble.isVisible() and bubble.isFadingOut()
    fade = bubble.fade()
    assert fade is not None, "전제: 닫힘 페이드가 걸렸다"
    steps: list[float] = []
    fade.valueChanged.connect(lambda _value: steps.append(bubble.opacity()))
    qtbot.waitUntil(lambda: not bubble.isVisible(), timeout=2000)
    assert any(step < 1.0 for step in steps)
    assert bubble.target is None


def test_new_tip_while_fading_out_replaces_the_old_one_at_once(qtbot: QtBot, buttons) -> None:
    """닫히는 중에 다른 버튼에 들어가면 앞 말풍선은 바로 그만두고 새 말풍선만 투명에서 나타나야 한다.

    첫째 다 뜸 → 나감(닫히는 중) → 둘째 들어감 -> 닫히는 중 아님 · 대상 둘째 · 글 「둘째 툴팁」 · 불투명도 0,
    보이는 말풍선 창 하나
    """
    first, second = buttons
    bubble = _show_fully(qtbot, first)
    leave(first)
    assert bubble.isFadingOut(), "전제: 닫히는 중이다"
    enter(second)
    assert not bubble.isFadingOut()
    assert bubble.target is second and bubble.text == "둘째 툴팁"
    assert bubble.opacity() == 0.0
    shown = [
        w for w in QApplication.topLevelWidgets() if isinstance(w, TipBubble) and w.isVisible()
    ]
    assert shown == [bubble]
    qtbot.waitUntil(lambda: bubble.fade() is None, timeout=2000)
    assert bubble.isVisible() and bubble.opacity() == 1.0


def test_animations_off_shows_and_hides_at_once(qtbot: QtBot, buttons) -> None:
    """설정의 애니메이션 끔이면 말풍선이 불투명도 1로 바로 뜨고 나가면 바로 숨어야 한다.

    animations=False · 첫째 들어감 -> 불투명도 1 · 페이드 없음, 나감 -> 숨음
    """
    card_display.use(card_display.CardDisplay(animations=False))
    first, _second = buttons
    enter(first)
    bubble = _bubble()
    assert bubble.isVisible() and bubble.opacity() == 1.0 and bubble.fade() is None
    leave(first)
    assert not bubble.isVisible()


def test_fade_animation_lives_only_while_moving(qtbot: QtBot, buttons) -> None:
    """페이드 애니메이션 객체는 나타나고 닫히는 동안만 있어야 한다(#571 계약).

    첫째 들어감 -> 애니메이션 1개, 끝난 뒤 0개; 나감 -> 1개, 숨은 뒤 0개
    """
    first, _second = buttons
    enter(first)
    bubble = _bubble()
    assert len(_animations(bubble)) == 1
    qtbot.waitUntil(lambda: bubble.fade() is None, timeout=2000)
    assert _animations(bubble) == []
    leave(first)
    assert len(_animations(bubble)) == 1
    qtbot.waitUntil(lambda: not bubble.isVisible(), timeout=2000)
    assert _animations(bubble) == []


def test_delayed_tip_fades_in_after_the_delay(
    qtbot: QtBot, buttons, monkeypatch: pytest.MonkeyPatch
) -> None:
    """지연을 건 위젯은 지연 동안 말풍선이 없고, 지연 뒤 투명에서 나타나야 한다(#599 지연 + #610 페이드).

    지연 30ms · 첫째에 지연 걸고 들어감 -> 말풍선 없음 · 기다리는 위젯 = 첫째,
    지연 끝에 말풍선을 띄운 바로 그때 보임 · 불투명도 0 · 페이드 있음, 페이드 끝나면 1
    """
    first, _second = buttons
    first.setProperty("tipDelayed", True)
    at_show: list[tuple[bool, float, bool]] = []
    show_tip = tip_bubble.show_tip

    def show_and_record(target: QWidget, text: str, rect=None) -> None:
        show_tip(target, text, rect)
        bubble = _bubble()
        at_show.append((bubble.isVisible(), bubble.opacity(), bubble.fade() is not None))

    monkeypatch.setattr(tip_bubble, "show_tip", show_and_record)
    enter(first)
    bubble = tip_bubble.current_bubble()
    assert bubble is None or not bubble.isVisible()
    assert tip_bubble.tip_filter().pending() is first
    qtbot.waitUntil(lambda: bool(at_show), timeout=2000)
    assert at_show == [(True, 0.0, True)]
    bubble = _bubble()
    qtbot.waitUntil(lambda: bubble.fade() is None, timeout=2000)
    assert bubble.opacity() == 1.0


def test_error_tip_fades_and_reads_closed_while_fading(qtbot: QtBot) -> None:
    """등록 오류 말풍선(창의 자식)도 투명에서 나타나고, 닫으면 바로 닫힌 것으로 읽힌 뒤 옅어져 숨어야 한다.

    입력칸 하나 창 · 오류 「잘못된 주소」 띄움 -> 불투명도 0에서 시작 · 다 뜨면 1,
    닫음 -> isShowing 거짓 · 글 "" · 아직 보임(닫히는 중), 그 뒤 숨음
    """
    host = QWidget()
    qtbot.addWidget(host)
    layout = QHBoxLayout(host)
    edit = QLineEdit()
    layout.addWidget(edit)
    host.resize(400, 200)
    host.show()
    QTest.qWaitForWindowExposed(host)
    error = ErrorTip(edit)
    error.show("잘못된 주소")
    bubble = error.bubble()
    assert bubble is not None and bubble.isVisible()
    assert bubble.opacity() == 0.0
    qtbot.waitUntil(lambda: bubble.fade() is None, timeout=2000)
    assert bubble.opacity() == 1.0
    error.hide()
    assert not error.isShowing() and error.text() == ""
    assert bubble.isVisible() and bubble.isFadingOut()
    qtbot.waitUntil(lambda: not bubble.isVisible(), timeout=2000)
