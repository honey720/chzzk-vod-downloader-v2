"""카드 완료 · 실패 모션 · 색조 걷기 · 완료 · 실패 카드의 막대 없음 (#571).

시간은 테스트가 정한다(`QVariantAnimation.setCurrentTime`) — 실제 시간에 기대지 않는다. 색은 theme 토큰과
화소를 비교한다.
"""

from collections.abc import Iterator
from typing import Never

import pytest
from PySide6.QtCore import QEvent, QVariantAnimation
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

import CVDv2 as main_module
import app.theme as theme
from app.widgets.contentItemWidget import CardFace
from app.widgets.widget import (
    BAR_FADE_MS,
    FAIL_MOTION_MS,
    FINISH_MOTION_MS,
    TINT_CLEAR_MS,
    ContentItemWidget,
)
from core.models.download_state import DownloadState
from tests.app.gui.card_helpers import drop_new_top_levels, hold_style, snapshot_top_levels
from tests.app.gui.test_card_c3 import DEFAULT_DIR, _card, _close, _display, _item, _pixel, _token

D = DownloadState
SCALE = 10000  # 막대 범위(0.01% 단위) — widget.PROGRESS_SCALE와 같은 값을 테스트가 따로 둔다
TIMES = (
    0,
    50,
    100,
    150,
    200,
    250,
    300,
    350,
    399,
)  # 모션 시간을 옮겨 보는 자리(ms) — 400은 끝이라 뺀다


@pytest.fixture(autouse=True)
def _apply_production_qss(qapp) -> Iterator[None]:
    """실제 전역 QSS · 다크 테마로 잰다."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))
    yield


@pytest.fixture(autouse=True)
def _drop_windows(qapp) -> Iterator[None]:
    """테스트가 띄운 창을 테스트 끝에 파괴한다."""
    before = snapshot_top_levels()
    yield
    drop_new_top_levels(before)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FailingSession:
        def head(self, *a: object, **k: object) -> Never:
            raise RuntimeError("network disabled in tests")

        def get(self, *a: object, **k: object) -> Never:
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("app.widgets.widget.get_thread_session", lambda: _FailingSession())
    monkeypatch.setattr("app.widgets.widget._global_download_path", DEFAULT_DIR)


def _running(progress: int = 40, state: DownloadState = D.RUNNING) -> ContentItemWidget:
    """받는 중(또는 일시정지) `progress`% 카드를 띄운다 — 틱 모션 없이 그 값에 선 막대."""
    item = _item(state)
    item.download_progress = progress
    return _card(item)


def _become(card: ContentItemWidget, state: DownloadState) -> QVariantAnimation | None:
    """카드 아이템의 상태를 바꿔 알린다 — 그때 도는 모션을 돌려준다."""
    card.item.downloadState = state
    card.setData(card.item, 0)
    return card.motion()


def _at(motion: QVariantAnimation, ms: int) -> None:
    """모션 시간을 `ms`로 옮긴다 — 그 시점의 모양을 그린다."""
    motion.setCurrentTime(ms)


def _drained(card: ContentItemWidget) -> list:
    """지우기로 미룬 객체를 지운 뒤 카드에 남은 모션 객체."""
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    return card.findChildren(QVariantAnimation)


def _face_mid(card: ContentItemWidget) -> QColor:
    """카드 면 가로 가운데, 막대 자리 높이의 화소."""
    frame = card.contentFrame
    return _pixel(frame, frame.width() // 2, card.progressBar.geometry().top())


class TestFinishMotionBar:
    """막대 방식 완료 모션 — 끝까지 차며 완료 색 · 색조가 나타나고, 막대가 흐려져 사라진다."""

    @pytest.mark.parametrize("before", (D.RUNNING, D.PAUSED))
    def test_bar_fills_to_the_end_without_going_back(self, qapp, before: DownloadState) -> None:
        """받는 중 · 일시정지 40% 카드가 완료되면 모션 동안 막대 값이 줄지 않고 400ms에 끝에 닿아야 한다.

        막대 방식 · 애니메이션 켬, 40% -> 완료, 0 · 50 · … · 399 · 400ms -> 값이 줄지 않음, 0ms 값 4000,
        400ms 값 10000, 막대 보임
        """
        _display(animations=True)
        card = _running(40, before)
        motion = _become(card, D.FINISHED)
        assert motion is not None
        values = []
        for ms in TIMES:
            _at(motion, ms)
            assert card.progressBar.isVisible()
            values.append(card.progressBar.value())
        _at(motion, FINISH_MOTION_MS)
        values.append(card.progressBar.value())
        assert values == sorted(values), values
        assert values[0] == 4000 and values[-1] == SCALE

    def test_duration_is_the_fill_time_plus_the_fade(self, qapp) -> None:
        """막대 방식 완료 모션의 시간이 FINISH_MOTION_MS + BAR_FADE_MS여야 한다."""
        _display(animations=True)
        card = _running()
        motion = _become(card, D.FINISHED)
        assert motion is not None
        assert motion.duration() == FINISH_MOTION_MS + BAR_FADE_MS

    def test_card_tint_grows_while_the_bar_fills(self, qapp) -> None:
        """완료 모션 동안 카드 색조 세기가 0에서 늘어 400ms에 1이 되고, QSS 색조는 끝날 때까지 없어야 한다.

        40% -> 완료, 0 · 200 · 400ms -> 세기 0 < 200ms 값 < 1 == 400ms 값, tint 속성 ""
        """
        _display(animations=True)
        card = _running()
        motion = _become(card, D.FINISHED)
        strengths = []
        for ms in (0, 200, FINISH_MOTION_MS):
            _at(motion, ms)
            token, strength = card.contentFrame.wash()
            assert token in ("", "cardTintFinished")
            strengths.append(strength)
            assert card.contentFrame.property("tint") == ""
        assert strengths[0] == 0.0 < strengths[1] < 1.0 == strengths[2]

    def test_bar_fades_after_it_is_full(self, qapp) -> None:
        """끝까지 찬 막대의 불투명도가 400ms까지 1이고, 흐려지는 동안 줄어야 한다.

        40% -> 완료, 400 · 400 + BAR_FADE_MS/2ms -> 불투명도 1 · 0 < 값 < 1
        """
        _display(animations=True)
        card = _running()
        motion = _become(card, D.FINISHED)
        _at(motion, FINISH_MOTION_MS)
        assert card.progressBar.look()[2] == 1.0
        _at(motion, FINISH_MOTION_MS + BAR_FADE_MS // 2)
        assert 0.0 < card.progressBar.look()[2] < 1.0

    def test_after_the_motion_the_bar_is_gone_and_the_tint_is_set(self, qapp) -> None:
        """완료 모션이 끝나면 막대가 숨고 QSS 색조가 걸리고 모션 객체가 남지 않아야 한다.

        40% -> 완료, 모션 끝 -> 막대 숨음 · tint "finished" · 면 칠하기 없음 · 막대 덧칠 없음 ·
        motion() None · QVariantAnimation 자식 0개 · 막대 자리 화소 == cardTintFinished
        """
        _display(animations=True)
        card = _running()
        motion = _become(card, D.FINISHED)
        _at(motion, motion.duration())
        assert not card.progressBar.isVisible()
        assert card.contentFrame.property("tint") == "finished"
        assert card.contentFrame.wash() == ("", 0.0)
        assert card.progressBar.look() == ("", 0.0, 1.0)
        assert card.motion() is None
        assert _drained(card) == []
        assert _close(_face_mid(card), _token("cardTintFinished"))


class TestFinishMotionFill:
    """배경 채움 방식 완료 모션 — 채움이 끝까지 차며 완료 색조가 된다."""

    def test_fill_grows_to_the_end_and_turns_into_the_tint(self, qapp) -> None:
        """채움 40% 카드가 완료되면 채움이 줄지 않고 400ms에 끝까지 차며 색이 완료 색조로 섞여야 한다.

        배경 채움 · 애니메이션 켬, 40% -> 완료, 0 · 50 · … · 399ms -> 채움 비율이 줄지 않음 · 399ms에 ≈ 1,
        섞기 0ms 0 · 400ms 1(섞는 토큰 cardTintFinished), 모션 시간 FINISH_MOTION_MS, 막대 숨음
        """
        _display(progress="fill", animations=True)
        card = _running()
        motion = _become(card, D.FINISHED)
        assert motion is not None and motion.duration() == FINISH_MOTION_MS
        fills = []
        for ms in TIMES:
            _at(motion, ms)
            assert not card.progressBar.isVisible()
            fills.append(card.contentFrame.fill()[0])
        assert fills == sorted(fills) and fills[-1] == pytest.approx(1.0), fills
        _at(motion, 0)
        assert card.contentFrame.fillLook()[1] == 0.0
        _at(motion, FINISH_MOTION_MS - 1)
        toward, blend, _ = card.contentFrame.fillLook()
        assert toward == "cardTintFinished" and blend > 0.99

    def test_waiting_card_shows_the_tint_growing(self, qapp) -> None:
        """채움 방식에서 대기 카드가 완료되면 모션 가운데에 카드 색조 세기가 0과 1 사이여야 한다.

        배경 채움 · 애니메이션 켬, 대기 -> 완료, 200ms -> 색조 cardTintFinished · 세기 0 < 값 < 1
        """
        _display(progress="fill", animations=True)
        card = _card(_item(D.WAITING))
        motion = _become(card, D.FINISHED)
        assert motion is not None
        _at(motion, 200)
        token, strength = card.contentFrame.wash()
        assert token == "cardTintFinished" and 0.0 < strength < 1.0

    def test_after_the_motion_the_whole_card_is_tinted(self, qapp) -> None:
        """채움 방식 완료 모션이 끝나면 채움이 없고 QSS 색조가 걸리고 막대 자리 화소가 완료 색조여야 한다."""
        _display(progress="fill", animations=True)
        card = _running()
        motion = _become(card, D.FINISHED)
        _at(motion, motion.duration())
        assert card.contentFrame.fill() == (0.0, "")
        assert card.contentFrame.property("tint") == "finished"
        assert card.motion() is None
        assert _close(_face_mid(card), _token("cardTintFinished"))


class TestFailMotion:
    """실패 모션 — 끝까지 채우지 않고 제자리에서 실패 색이 되며 사라진다. 카드 색조가 나타난다."""

    def test_bar_value_does_not_grow(self, qapp) -> None:
        """받는 중 40% 카드가 실패하면 모션 동안 막대 값이 4000 그대로이고 불투명도가 줄어야 한다.

        막대 방식 · 애니메이션 켬, 40% -> 실패, 0 · 50 · … · 399ms -> 값 4000, 0ms 불투명도 1 > 200ms
        불투명도 > 0, 덧칠 토큰 stateFailed, 모션 시간 FAIL_MOTION_MS
        """
        _display(animations=True)
        card = _running()
        motion = _become(card, D.FAILED)
        assert motion is not None and motion.duration() == FAIL_MOTION_MS
        for ms in TIMES:
            _at(motion, ms)
            assert card.progressBar.value() == 4000, ms
        _at(motion, 0)
        assert card.progressBar.look()[2] == 1.0
        _at(motion, 200)
        toward, _, opacity = card.progressBar.look()
        assert toward == "stateFailed" and 0.0 < opacity < 1.0

    def test_fill_does_not_grow_and_fades(self, qapp) -> None:
        """채움 40% 카드가 실패하면 모션 동안 채움 비율이 0.4 그대로이고 채움 불투명도가 줄어야 한다.

        배경 채움 · 애니메이션 켬, 40% -> 실패, 0 · 50 · … · 399ms -> 채움 0.4, 200ms 불투명도 0 < 값 < 1,
        200ms 색조 세기 0 < 값 < 1
        """
        _display(progress="fill", animations=True)
        card = _running()
        motion = _become(card, D.FAILED)
        assert motion is not None and motion.duration() == FAIL_MOTION_MS
        for ms in TIMES:
            _at(motion, ms)
            assert card.contentFrame.fill()[0] == pytest.approx(0.4), ms
        _at(motion, 200)
        assert 0.0 < card.contentFrame.fillLook()[2] < 1.0
        token, strength = card.contentFrame.wash()
        assert token == "cardTintFailed" and 0.0 < strength < 1.0

    @pytest.mark.parametrize("progress", ("bar", "fill"))
    def test_after_the_motion_the_card_has_the_failed_tint(self, qapp, progress: str) -> None:
        """실패 모션이 끝나면 막대 · 채움이 없고 tint "failed"이고 막대 자리 화소가 실패 색조여야 한다."""
        _display(progress=progress, animations=True)
        card = _running()
        motion = _become(card, D.FAILED)
        _at(motion, motion.duration())
        assert not card.progressBar.isVisible()
        assert card.contentFrame.fill() == (0.0, "")
        assert card.contentFrame.property("tint") == "failed"
        assert card.motion() is None
        assert _drained(card) == []
        assert _close(_face_mid(card), _token("cardTintFailed"))


class TestAnimationsOff:
    @pytest.mark.parametrize("progress", ("bar", "fill"))
    @pytest.mark.parametrize("state,tint", [(D.FINISHED, "finished"), (D.FAILED, "failed")])
    def test_no_motion_and_the_last_look_at_once(
        self, qapp, progress: str, state: DownloadState, tint: str
    ) -> None:
        """애니메이션 끔이면 받는 중 카드가 완료 · 실패될 때 모션 없이 바로 막대 없음 · 색조여야 한다."""
        _display(progress=progress, animations=False)
        card = _running()
        assert _become(card, state) is None
        assert not card.progressBar.isVisible()
        assert card.contentFrame.fill() == (0.0, "")
        assert card.contentFrame.property("tint") == tint
        assert card.contentFrame.wash() == ("", 0.0)

    def test_retry_clears_the_tint_at_once(self, qapp) -> None:
        """애니메이션 끔이면 실패 카드가 대기가 될 때 모션 없이 바로 색조가 없어야 한다."""
        _display(animations=False)
        card = _card(_item(D.FAILED))
        assert _become(card, D.WAITING) is None
        assert card.contentFrame.property("tint") == ""
        assert card.contentFrame.wash() == ("", 0.0)


class TestClearMotion:
    def test_retry_fades_the_tint_out(self, qapp) -> None:
        """실패 카드가 대기가 되면 색조 세기가 1에서 줄어 TINT_CLEAR_MS에 걷히고 모션 객체가 남지 않아야 한다.

        애니메이션 켬, 실패 카드 -> 대기, 0 · 100ms · 끝 -> 세기 1 > 100ms 값 > 0, 끝에서 칠하기 없음 ·
        tint "" · 막대 자리 화소 == cardBg, 모션 시간 TINT_CLEAR_MS
        """
        _display(animations=True)
        card = _card(_item(D.FAILED))
        motion = _become(card, D.WAITING)
        assert motion is not None and motion.duration() == TINT_CLEAR_MS
        assert card.contentFrame.property("tint") == ""
        _at(motion, 0)
        assert card.contentFrame.wash() == ("cardTintFailed", 1.0)
        _at(motion, TINT_CLEAR_MS // 2)
        assert 0.0 < card.contentFrame.wash()[1] < 1.0
        _at(motion, TINT_CLEAR_MS)
        assert card.contentFrame.wash() == ("", 0.0)
        assert card.motion() is None
        assert _drained(card) == []
        assert _close(_face_mid(card), _token("cardBg"))


class TestInterruptions:
    """모션 도중 상태 바뀜 · 숨음 · 선택 모드 — 모션을 멈추고 마지막 모양으로 바로 간다."""

    def test_retry_during_the_fail_motion_goes_straight_to_waiting(self, qapp) -> None:
        """실패 모션 200ms에 다시 시도(대기)하면 모션이 없고 색조 · 칠하기 · 막대가 없어야 한다."""
        _display(animations=True)
        card = _running()
        _at(_become(card, D.FAILED), 200)
        assert _become(card, D.WAITING) is None
        assert card.contentFrame.property("tint") == ""
        assert card.contentFrame.wash() == ("", 0.0)
        assert not card.progressBar.isVisible()
        assert _drained(card) == []

    def test_a_repeated_update_keeps_the_motion(self, qapp) -> None:
        """완료 모션 200ms에 같은 완료 갱신이 한 번 더 오면 같은 모션이 이어져야 한다."""
        _display(animations=True)
        card = _running()
        motion = _become(card, D.FINISHED)
        _at(motion, 200)
        card.setData(card.item, 0)
        assert card.motion() is motion

    def test_hiding_the_card_ends_the_motion(self, qapp) -> None:
        """완료 모션 200ms에 카드가 숨으면 모션이 없고 막대 없음 · tint "finished"여야 한다."""
        _display(animations=True)
        card = _running()
        _at(_become(card, D.FINISHED), 200)
        card.hide()
        assert card.motion() is None
        assert not card.progressBar.isVisible()
        assert card.contentFrame.property("tint") == "finished"
        assert card.contentFrame.wash() == ("", 0.0)

    @pytest.mark.parametrize("picking", (True, False))
    def test_select_mode_change_ends_the_motion(self, qapp, picking: bool) -> None:
        """실패 모션 200ms에 선택 모드를 드나들면 모션이 없고 막대 없음 · tint "failed"여야 한다."""
        _display(animations=True)
        card = _running()
        if not picking:
            card.setPicking(True)
        _at(_become(card, D.FAILED), 200)
        card.setPicking(picking)
        assert card.motion() is None
        assert not card.progressBar.isVisible()
        assert card.contentFrame.property("tint") == "failed"

    def test_a_card_that_starts_finished_does_not_move(self, qapp) -> None:
        """처음부터 완료인 카드는 모션이 없어야 한다(되살린 카드 · 되돌린 카드와 같은 길)."""
        _display(animations=True)
        card = _card(_item(D.FINISHED))
        assert card.motion() is None
        assert card.contentFrame.property("tint") == "finished"


class TestHoveredMotion:
    """호버한 카드의 모션 색 — 끝난 뒤 QSS 색조(호버 단계)와 같은 색으로 간다."""

    def test_hovered_card_reaches_the_hover_tint_before_the_motion_ends(self, qapp) -> None:
        """호버한 받는 중 카드가 완료되면 400ms(색조 세기 1)의 면 화소가 cardTintFinishedHover여야 한다.

        막대 방식 · 애니메이션 켬 · 호버, 40% -> 완료, 400ms · 끝 -> 막대 위 빈 자리 화소 ==
        cardTintFinishedHover
        """
        _display(animations=True)
        card = _running()
        card.setHovered(True)
        motion = _become(card, D.FINISHED)
        frame = card.contentFrame
        y = card.progressBar.geometry().top() - 2  # 막대 바로 위 안쪽 여백 — 글 · 썸네일 · 막대 밖
        _at(motion, FINISH_MOTION_MS)
        assert _close(_pixel(frame, frame.width() // 2, y), _token("cardTintFinishedHover"))
        _at(motion, motion.duration())
        assert card.contentFrame.property("hover") is True, "전제: 호버가 남아 있다"
        assert _close(_pixel(frame, frame.width() // 2, y), _token("cardTintFinishedHover"))

    def test_hovered_fill_card_turns_into_the_hover_tint(self, qapp) -> None:
        """배경 채움 방식에서 호버한 카드가 완료되면 399ms 채움 화소가 cardTintFinishedHover여야 한다."""
        _display(progress="fill", animations=True)
        card = _running()
        card.setHovered(True)
        motion = _become(card, D.FINISHED)
        frame = card.contentFrame
        _at(motion, FINISH_MOTION_MS - 1)
        y = card.progressBar.geometry().top() - 2
        assert _close(_pixel(frame, frame.width() // 2, y), _token("cardTintFinishedHover"))


class TestGeometryKept:
    @pytest.mark.parametrize("state", (D.FINISHED, D.FAILED))
    def test_card_height_and_minimum_width_do_not_change(self, qapp, state: DownloadState) -> None:
        """받는 중 카드가 완료 · 실패가 되어 모션이 끝나도 카드 높이 힌트 · 최소폭 힌트가 같아야 한다."""
        _display(animations=True)
        card = _running()
        before = (card.sizeHint().height(), card.minimumSizeHint().width())
        motion = _become(card, state)
        _at(motion, motion.duration())
        assert (card.sizeHint().height(), card.minimumSizeHint().width()) == before


class TestRepaintOnlyOnChange:
    def test_same_wash_level_does_not_repaint(self, qapp, monkeypatch: pytest.MonkeyPatch) -> None:
        """색조 세기가 같은 1/255 단계 안에서만 바뀌면 카드 면 다시 그리기를 청하지 않아야 한다.

        setWash(cardTintFinished, 0.5) 뒤 0.5 + 1/1000 -> 청함 0번, 이어 0.6 -> 청함 1번
        """
        card = _running()
        frame: CardFace = card.contentFrame
        frame.setWash("cardTintFinished", 0.5)
        calls: list[tuple] = []
        monkeypatch.setattr(frame, "update", lambda *a: calls.append(a))
        frame.setWash("cardTintFinished", 0.5 + 1 / 1000)
        assert calls == []
        frame.setWash("cardTintFinished", 0.6)
        assert len(calls) == 1
