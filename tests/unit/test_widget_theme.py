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


class TestCardInnerWidthIsUnchanged:
    """카드 안쪽 가로 가용 폭은 "카드 폭 − 20px"로 고정이다.

    **이 파일에서 가장 중요한 테스트다.** 스타일을 입히면서 테두리나 가로
    여백을 늘리면 제목·경로·상태 라벨이 받는 폭이 그만큼 줄고, 라벨이
    "어디서 잘리는가"를 고정해 둔 회귀 테스트들(`test_widget.py`,
    `test_content_view.py::TestNoHorizontalOverflow`)이 3-OS 폰트 메트릭
    차이에 걸려 깨진다. 그 테스트들은 잘림 위치를 직접 보기 때문에
    **로컬 한 OS에서는 통과하고 CI 3-OS에서 실패**할 수 있다 — PR #234의
    첫 CI가 정확히 그랬다(가로 padding 10px + 테두리 1px = 22px 손실:
    카드 460px에서 채널명 92→70px, 뷰 300px에서 제목 118→96px·경로
    88→66px. Windows 로컬 528 passed, CI는 3-OS 전부 실패).

    폰트에 의존하는 잘림 위치가 아니라 **순수 기하값**을 재기 때문에 이
    테스트는 어느 OS에서든 같은 답을 낸다 — 그래서 위 회귀들보다 먼저,
    로컬에서 원인을 짚어준다. 카드에 여백이 더 필요하면 세로로 주고,
    가로를 꼭 늘려야 한다면 이 상수와 위 회귀 테스트들을 함께 다시 볼 것.
    """

    #: `contentItemLayout`의 좌우 여백(9) + 카드 테두리(1), 양쪽 합계.
    EXPECTED_HORIZONTAL_INSET = 20

    @pytest.mark.parametrize("width", [300, 460, 600])
    def test_content_width_matches_the_baseline_budget(self, qapp, width):
        widget = _widget(qapp, DownloadState.WAITING)
        widget.resize(width, 134)
        widget.show()
        qapp.processEvents()

        inner = widget.contentFrame.contentsRect().width()
        assert inner == width - self.EXPECTED_HORIZONTAL_INSET, (
            f"카드 안쪽 가용 폭이 {width - inner}px 줄었다(기대 "
            f"{self.EXPECTED_HORIZONTAL_INSET}px) — 라벨 잘림 위치가 밀려 "
            "3-OS 폰트 메트릭 회귀 테스트가 깨진다"
        )

    @pytest.mark.parametrize("state", ["waiting", "running", "finished", "failed"])
    def test_no_horizontal_padding_in_any_state(self, state):
        """상태가 달라져도 가로 여백은 0이어야 한다 — 폭이 상태에 따라 흔들리면 안 된다."""
        css = theme.card_style(state)
        padding = [ln for ln in css.splitlines() if "padding" in ln]
        assert padding, "카드 규칙에 padding 선언이 사라졌다 — 이 테스트의 전제를 확인할 것"
        assert padding[0].strip().rstrip(";").split(":")[1].split()[-1] == "0px", (
            f"가로 여백이 0이 아니다: {padding[0].strip()!r}"
        )
