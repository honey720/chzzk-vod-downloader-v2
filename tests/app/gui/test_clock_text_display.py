"""24시간 넘는 시간이 카드 · 하단바에 날짜를 버리지 않고 보이는지 (#511).

실제 메인 창 · 카드 위젯으로 잰다. 엔진 자리는 하단바 테스트의 서비스 대역을 쓴다.
"""

import pytest
from PySide6.QtWidgets import QApplication

from app.viewmodels.data import ContentItem
from app.widgets.widget import ContentItemWidget
from core.models.download_state import DownloadState
from tests.app.gui import test_footer_bar as footer
from tests.app.gui.test_footer_bar import _environment, answers  # noqa: F401 — 같은 환경 · 확인 창 대역

MIB = 1024 * 1024
DAY_AND_A_QUARTER = 108000  # 30시간(초) — 날짜를 버리면 6시간으로 보인다


@pytest.mark.usefixtures("answers")
def test_footer_and_card_show_a_time_left_past_a_day(qtbot, tmp_path):
    """남은 시간이 30시간이면 하단바와 카드 모두 "30:00:00 left"여야 한다.

    받을 크기 108000 MiB, 받은 0, 속도 1.0 MB/s 통지 -> 하단바 남은 시간 칸 · 카드 상태 글에 "30:00:00 left"
    """
    win, items, engine = footer._window(tmp_path)
    win.downloadButton.click()
    footer._pump()
    footer._progress(engine, 0, DAY_AND_A_QUARTER * MIB, 1.0, 4)
    footer._tick(win)
    assert win.footerMonitor.labels["remain"].text() == "30:00:00 left"
    assert "30:00:00 left" in win.listView.widgetFor(items[0]).statusLabel.text()


@pytest.mark.usefixtures("answers")
def test_card_shows_an_elapsed_time_past_a_day(qtbot, tmp_path):
    """받는 데 30시간 걸린 카드의 완료 글은 "· 30:00:00"으로 끝나야 한다.

    서비스 대역의 걸린 시간 108000초 → 완료 -> 카드 상태 글 끝 "· 30:00:00"
    """
    win, items, engine = footer._window(tmp_path)
    engine.elapsed_seconds = lambda: float(DAY_AND_A_QUARTER)
    win.downloadButton.click()
    footer._pump()
    footer._finish(engine)
    assert items[0].downloadState == DownloadState.FINISHED, "전제: 완료됐다"
    assert win.listView.widgetFor(items[0]).statusLabel.text().endswith("· 30:00:00")


def _waiting_segment_card(duration: int | None) -> ContentItemWidget:
    """영상 길이가 duration인 대기 세그먼트 카드 — 세그먼트 카드는 대기에서 길이 칸에 길이를 적는다."""
    item = ContentItem(
        "https://chzzk.naver.com/video/1",
        {
            "title": "방송",
            "category": "",
            "channelName": "채널",
            "createdDate": "",
            "duration": duration,
        },
        [["1080", "u1"]],
        None,
        "",
        "C:/dl",
        "m3u8",
        None,
    )
    item.downloadState = DownloadState.WAITING
    widget = ContentItemWidget(item, 0)
    widget.setData(item, 0)
    assert item.is_segment_based, "전제: 세그먼트 카드는 대기에서 길이를 적는다"
    return widget


def test_waiting_card_shows_a_video_length_past_a_day(qapp: QApplication) -> None:
    """영상 길이가 30시간인 대기 세그먼트 카드는 길이를 "30:00:00"으로 보여야 한다."""
    widget = _waiting_segment_card(DAY_AND_A_QUARTER)
    assert widget.fileSizeLabel.text() == "30:00:00"


@pytest.mark.parametrize("duration", [None, 0], ids=["null", "키 없음"])
def test_waiting_card_leaves_an_unknown_video_length_empty(
    qapp: QApplication, duration: int | None
) -> None:
    """영상 길이를 모르는 대기 카드는 길이 칸이 비어 있어야 한다 — 지금 시각 · "00:00:00"을 보이지 않는다 (#515).

    응답의 길이 null(None) · 키 없음(0) -> 길이 칸 글 "", 확보 폭은 길이를 아는 카드(3600초)와 같다
    """
    widget = _waiting_segment_card(duration)
    known = _waiting_segment_card(3600)
    assert widget.fileSizeLabel.text() == ""
    assert widget.fileSizeLabel.minimumWidth() == known.fileSizeLabel.minimumWidth()


def test_waiting_card_shows_the_length_once_it_is_known(qapp: QApplication) -> None:
    """길이를 모르던 대기 카드에 길이가 채워지면 다시 그릴 때 길이가 보여야 한다.

    길이 None 카드 → 길이 3600초를 채워 다시 그림 -> "01:00:00"
    """
    widget = _waiting_segment_card(None)
    widget.item.duration = 3600
    widget.setData(widget.item, 0)
    assert widget.fileSizeLabel.text() == "01:00:00"


@pytest.mark.usefixtures("answers")
def test_a_negative_time_left_still_updates_the_card_and_the_footer(qtbot, tmp_path):
    """받은 크기가 받을 크기보다 큰 통지가 와도 카드 · 하단바가 예외 없이 고쳐져야 한다(#513 리뷰).

    받을 크기 1 MiB, 받은 2 MiB, 속도 1.0 MB/s 통지 -> 카드 상태 글에 "0:00 left", 하단바 속도 칸 보임
    """
    win, items, engine = footer._window(tmp_path)
    win.downloadButton.click()
    footer._pump()
    footer._progress(engine, 2 * MIB, MIB, 1.0, 4)
    footer._tick(win)
    assert "0:00 left" in win.listView.widgetFor(items[0]).statusLabel.text()
    assert win.footerMonitor.labels["speed"].text() == "↓ 1.0 MB/s"
