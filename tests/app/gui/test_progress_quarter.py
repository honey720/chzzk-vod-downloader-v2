"""0.25초 진행 통지에서 앱이 보이는 것 — 소수 진행률 · 1초에 한 번 바뀌는 글 (#557).

엔진은 진행을 0.25초마다 알린다. 진행률 · 받은 크기 · 막대는 통지마다 바뀌고, 속도 · 남은 시간 글 ·
하단바 모니터링은 1초에 한 번 바뀐다. 시계 · 통지는 테스트가 낸다.
"""

import pytest

import app.viewmodels.content_viewmodel as content_viewmodel
import app.widgets.widget as widget_module
from app.viewmodels import card_display, card_text
from app.viewmodels.download_viewmodel import _file_progress_args, _segment_progress_args
from app.viewmodels.staged_progress import StagedProgress
from core.engine.base import PROGRESS_SECONDS
from core.models.events import ProgressEvent
from tests.app.gui.test_footer_bar import (  # noqa: F401 — answers는 픽스처
    MIB,
    _free_measured,
    _progress,
    _pump,
    _tick,
    _window,
    answers,
)

# ================================================================ 소수 진행률


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (0, "0.0%"),
        (37.4, "37.4%"),
        (37.96, "37.9%"),  # 버린다 — 다음 값이 먼저 보이지 않게
        (99.96, "99.9%"),  # 끝나기 전에 「100」으로 보이지 않게
        (100, "100%"),
        (100.0, "100%"),
    ],
)
def test_progress_text_has_one_decimal_and_shows_done_as_100(value, text) -> None:
    """진행률 글은 소수 한 자리로 버려 적고, 다 받았으면 「100%」여야 한다."""
    assert card_text.progress_text(value) == text


def test_file_progress_is_a_decimal_of_the_received_bytes() -> None:
    """파일 다운로드의 진행률은 받은 바이트 ÷ 전체의 소수여야 한다(정수로 버리지 않음).

    받은 374 · 전체 1000 바이트 -> 37.4
    """
    event = ProgressEvent(downloaded_size=374, total_size=1000, speed=1.0, active_threads=1)

    assert _file_progress_args(event)[3] == pytest.approx(37.4)


def test_segment_progress_is_a_decimal_of_the_finished_segments() -> None:
    """세그먼트 기반의 진행률은 끝낸 세그먼트 ÷ 전체의 소수여야 한다.

    세그먼트 1000개 중 374개 끝남, 병합 전 -> 37.4
    """

    class Counters:
        max_threads, completed_threads, merged_segments, reused_threads = 1000, 374, 0, 0

    class Stage:
        content_type, post_process, is_segment_based = "m3u8", False, True

    event = ProgressEvent(downloaded_size=374, total_size=None, speed=1.0, active_threads=1)

    assert _segment_progress_args(event, Counters(), Stage())[3] == pytest.approx(37.4)


def test_staged_bar_stops_at_99_9_until_every_stage_is_done() -> None:
    """단계 막대는 끝나기 전에는 99.9까지(소수), 모든 단계가 끝나야 100이어야 한다.

    받기 1.0 · 컷 0.9999(예상 1초씩) -> 99.9 이하, 컷 1.0 -> 100
    """
    bar = StagedProgress(("transfer", "cut"))
    almost = bar.update({"transfer": 1.0, "cut": 0.9999}, {"transfer": 1.0, "cut": 1.0})

    assert 99.0 < almost <= 99.9
    assert bar.update({"cut": 1.0}, {"transfer": 1.0, "cut": 1.0}) == 100


# ================================================================ 1초에 한 번 바뀌는 글


class _Model:
    def notifyChanged(self, item) -> None:
        pass


class _Item:
    rate_text_at = None
    download_remain_time = download_speed = download_size = ""
    download_progress = 0.0


def test_rate_texts_change_once_a_second_while_progress_changes_every_notice(monkeypatch) -> None:
    """0.25초마다 통지가 와도 속도 · 남은 시간 글은 1초에 한 번, 진행률 · 크기는 통지마다 바뀌어야 한다.

    시계 0 · 0.25 · 0.5 · 0.75 · 1.0초에 속도 1 · 2 · 3 · 4 · 5 MB/s 통지
    -> 속도 글이 보인 값: 1(0초) · 1 · 1 · 1 · 5(1초), 진행률은 다섯 번 다 바뀜
    """
    clock = [0.0]
    monkeypatch.setattr(content_viewmodel, "_clock", lambda: clock[0])
    viewmodel = content_viewmodel.ContentViewModel.__new__(content_viewmodel.ContentViewModel)
    viewmodel.model = _Model()
    item = _Item()
    shown, progress = [], []
    for step in range(5):
        clock[0] = step * 0.25
        viewmodel.update_progress(
            f"{9 - step}:00", str(step), f"{step + 1}.0 MB/s", 10.0 + step, item
        )
        shown.append((item.download_speed, item.download_remain_time))
        progress.append(item.download_progress)

    assert [speed for speed, _ in shown] == ["1.0 MB/s"] * 4 + ["5.0 MB/s"]
    assert [remain for _, remain in shown] == ["9:00"] * 4 + ["5:00"]
    assert progress == [10.0, 11.0, 12.0, 13.0, 14.0]


def test_footer_changes_once_a_second_not_on_every_notice(qtbot, tmp_path, answers) -> None:  # noqa: F811 — 픽스처
    """하단바 속도는 진행 통지마다가 아니라 1초 갱신 때만 바뀌어야 한다.

    받는 중 2.0 MB/s 통지 → 1초 갱신 → 3.0 · 4.0 · 5.0 MB/s 통지 셋 -> 「↓ 2.0 MB/s」 그대로,
    1초 갱신 -> 「↓ 5.0 MB/s」
    """
    win, _items, engine = _window(tmp_path)
    _free_measured(qtbot, win)
    win.downloadButton.click()
    _pump()
    _progress(engine, 0, 10 * MIB, 2.0, 4)
    _tick(win)
    speed = win.footerMonitor.labels["speed"]
    assert speed.text() == "↓ 2.0 MB/s"

    for value in (3.0, 4.0, 5.0):
        _progress(engine, 0, 10 * MIB, value, 4)
    assert speed.text() == "↓ 2.0 MB/s"

    _tick(win)
    assert speed.text() == "↓ 5.0 MB/s"


# ================================================================ 막대 모션 · 애니메이션 끔


def test_bar_motion_time_is_read_from_the_progress_interval() -> None:
    """카드 막대의 이어 움직임 시간은 엔진의 진행 통지 간격(PROGRESS_SECONDS)에서 셈한 값이어야 한다.

    PROGRESS_SECONDS 0.25초 -> PROGRESS_INTERVAL_MS == TICK_MOTION_MS == 250
    """
    assert PROGRESS_SECONDS == 0.25, "전제: 이 판의 통지 간격"

    assert card_display.PROGRESS_INTERVAL_MS == round(PROGRESS_SECONDS * 1000) == 250
    assert widget_module.TICK_MOTION_MS == card_display.PROGRESS_INTERVAL_MS


def test_animations_off_still_steps_once_a_second() -> None:
    """애니메이션 끔의 막대 갱신 간격은 진행 통지가 0.25초여도 1초(1000ms) 그대로여야 한다."""
    assert widget_module.STILL_STEP_MS == 1000
