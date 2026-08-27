"""카드가 다운로드 상태에 맞는 색을 실제로 입는지 검증한다 (#227).

`theme.card_style()`이 옳은 문자열을 만드는지는 test_theme.py가 본다.
여기서는 **위젯이 그걸 실제로 붙이는지** — 상태가 바뀔 때마다 `setData()`가
카드 테두리와 진행바를 같이 갱신하는지를 본다.

진행바 색은 위젯 스타일시트가 아니라 동적 속성(`state`) + 전역 QSS의
`[state="..."]` 규칙으로 정해진다. QSS는 `.className` 선택자를 지원하지
않고 조용히 무시하므로 이 배선이 유일한 경로다 — 속성이 상태를 따라가지
않으면 진행바 색이 안 변하는데 **아무 에러도 안 난다**.
"""

import pytest

import theme
from app.viewmodels.item_state import ItemState
from content.data import ContentItem
from content.widget import ContentItemWidget
from core.models.download_state import DownloadState


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    class _FailingSession:
        def head(self, *a, **k):
            raise RuntimeError("network disabled in tests")

        def get(self, *a, **k):
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("content.widget.get_thread_session", lambda: _FailingSession())


def _make_item():
    return ContentItem(
        "https://chzzk.naver.com/video/1",
        {"title": "제목", "category": "", "channelName": "채널", "createdDate": "", "duration": 3600},
        [], None, "", "C:/Users/LeeDH/Downloads", "video", None,
    )


def _widget(qapp, state, progress=0):
    item = _make_item()
    item.downloadState = state
    item.download_progress = progress
    item.download_size = 1024
    item.total_size = "1.0 GB"
    widget = ContentItemWidget(item, 0)
    widget.setData(item, 0)
    qapp.processEvents()
    return widget


STATE_MAP = [
    (DownloadState.WAITING, "waiting"),
    (DownloadState.PAUSED, "waiting"),
    (ItemState.LOADING, "waiting"),
    (DownloadState.RUNNING, "running"),
    (DownloadState.FINISHED, "finished"),
    (DownloadState.FAILED, "failed"),
]


@pytest.mark.parametrize("download_state,card_state", STATE_MAP)
def test_card_frame_gets_the_state_colour(qapp, download_state, card_state):
    widget = _widget(qapp, download_state, progress=50)
    assert theme.DARK["state" + card_state.capitalize()] in widget.contentFrame.styleSheet()


@pytest.mark.parametrize("download_state,card_state", STATE_MAP)
def test_progress_bar_property_follows_the_state(qapp, download_state, card_state):
    widget = _widget(qapp, download_state, progress=50)
    assert widget.progressBar.property("state") == card_state


def test_state_change_repaints_an_existing_card(qapp):
    """같은 위젯이 상태를 갈아탈 때도 따라와야 한다 — 카드는 재사용된다."""
    widget = _widget(qapp, DownloadState.WAITING)
    assert theme.DARK["stateWaiting"] in widget.contentFrame.styleSheet()

    widget.item.downloadState = DownloadState.RUNNING
    widget.item.download_progress = 30
    widget.setData(widget.item, 0)
    assert theme.DARK["stateRunning"] in widget.contentFrame.styleSheet()
    assert widget.progressBar.property("state") == "running"
    assert widget.progressBar.value() == 30

    widget.item.downloadState = DownloadState.FAILED
    widget.setData(widget.item, 0)
    assert theme.DARK["stateFailed"] in widget.contentFrame.styleSheet()
    assert widget.progressBar.property("state") == "failed"


def test_waiting_card_shows_no_progress(qapp):
    """대기 중인 카드에 이전 진행률이 남아 보이면 안 된다."""
    widget = _widget(qapp, DownloadState.WAITING, progress=70)
    assert widget.progressBar.value() == 0


def test_resolution_buttons_are_marked_for_the_pill_rule(qapp):
    item = _make_item()
    item.unique_reps = [["1080", "https://x/1080"], ["720", "https://x/720"]]
    widget = ContentItemWidget(item, 0)
    widget.setData(item, 0)
    widget.addRepresentationButtons()
    qapp.processEvents()

    assert widget.buttons, "해상도 버튼이 만들어지지 않았다"
    assert all(b.property("role") == "resolution" for b in widget.buttons)


def test_icon_buttons_are_marked(qapp):
    widget = _widget(qapp, DownloadState.WAITING)
    assert widget.deleteButton.property("role") == "icon"
    assert widget.openDirectoryButton.property("role") == "icon"
