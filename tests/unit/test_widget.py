"""`ContentItemWidget` 렌더 회귀 테스트 (#232 — statusLabel·fileSizeLabel이
폭 0으로 눌려 빈 문자열만 그려지던 회귀).

`#229`(가로 스크롤 수정)가 `statusLabel`·`fileSizeLabel`을 `ElidingLabel`로
바꾸면서 `topLayout`(index·type·channelImage·channelName·스페이서·status·
progress·fileSize·delete)에 `Ignored` 정책 라벨이 둘 이상 + `Expanding`
스페이서가 같이 놓이는 조합이 생겼다 — 실기에서 이 둘이 폭 0으로 눌려
빈 문자열만 그려지는 회귀가 났다(오너 실기 확인).

**왜 기존 테스트(583개)가 못 잡았는가 — 이게 이 파일의 존재 이유다.**
`content/widget.py`(`ContentItemWidget`)를 직접 겨냥한 테스트가 지금까지
하나도 없었다 — `test_failure_display.py`·`test_content_manager.py` 등은
전부 `widget.statusLabel.text()`로 검증하는데, `ElidingLabel.text()`는
*의도적으로* 화면에 그려지는 값이 아니라 논리적 원문 전체를 돌려주도록
설계했다(호출부가 "지금 제목이 뭐야"를 물을 때 잘린 값을 받으면 안 되므로).
그 결과 **`.text()`를 쓰는 검증은 라벨이 화면에서 완전히 안 보여도 항상
통과한다** — 라벨 종류를 바꾼 게 검증 대상 자체를 무력화한 셈이다. 이
파일은 `QLabel.text(widget)`(부모 클래스 접근자, 실제 렌더링 문자열)로
검증해 이 사각지대를 없앤다.
"""

import pytest
from PySide6.QtWidgets import QLabel

from content.data import ContentItem
from content.widget import ContentItemWidget
from core.models.download_state import DownloadState
from app.viewmodels.item_state import ItemState


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    class _FailingSession:
        def head(self, *a, **k):
            raise RuntimeError("network disabled in tests")

        def get(self, *a, **k):
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("content.widget.get_thread_session", lambda: _FailingSession())


def _make_item(content_type="video", title="제목"):
    return ContentItem(
        "https://chzzk.naver.com/video/1",
        {"title": title, "category": "", "channelName": "채널", "createdDate": "", "duration": 3600},
        [], None, "", "C:/Users/LeeDH/Downloads", content_type, None,
    )


def _rendered(label) -> str:
    """실제 화면에 그려지는 문자열 — `ElidingLabel`이면 부모 접근자를 쓴다.

    `label.text()`를 쓰면 `ElidingLabel`의 경우 원문 전체가 돌아와 이
    테스트의 목적(렌더 회귀 검출) 자체가 무력화된다 — 절대 쓰지 않는다.
    """
    if type(label).__name__ == "ElidingLabel":
        return QLabel.text(label)
    return label.text()


def _build_widget(item, qapp) -> ContentItemWidget:
    """위젯을 만들고 `setData()`까지 반영한 뒤 실제로 보여서 레이아웃을
    확정한다. `show()` 없이는 `topLayout`이 활성화되지 않아 라벨들의
    `width()`가 Qt 기본값(작음)에 머물러 있을 수 있다 — 실제 앱(카드가
    보이는 `QScrollArea` 안)과 다른 조건에서 재는 셈이라 반드시 보여준다.
    """
    widget = ContentItemWidget(item, 0)
    widget.setData(item, 0)
    widget.resize(600, 134)
    widget.show()
    qapp.processEvents()
    return widget


class TestStatusAndFileSizeAreVisible:
    """카드 폭이 넉넉한(창을 좁히지 않은) 정상 상태에서 이 라벨들이 실제로
    보여야 한다 — #232에서 폭 0으로 눌려 전부 빈 문자열이 되던 회귀."""

    def test_waiting_file_type_shows_status_and_total_size(self, qapp):
        item = _make_item(content_type="video")
        item.total_size = "1.2 GB"
        widget = _build_widget(item, qapp)

        assert _rendered(widget.statusLabel) != ""
        assert _rendered(widget.fileSizeLabel) != ""
        assert "1.2 GB" in _rendered(widget.fileSizeLabel)

    def test_waiting_segment_type_shows_status_and_duration(self, qapp):
        item = _make_item(content_type="m3u8")
        widget = _build_widget(item, qapp)

        assert _rendered(widget.statusLabel) != ""
        assert _rendered(widget.fileSizeLabel) != ""

    def test_loading_placeholder_shows_status(self, qapp):
        item = _make_item()
        item.downloadState = ItemState.LOADING
        widget = _build_widget(item, qapp)

        assert _rendered(widget.statusLabel) != ""

    def test_running_shows_progress_status_and_size(self, qapp):
        item = _make_item()
        item.total_size = "500 MB"
        item.downloadState = DownloadState.RUNNING
        item.download_remain_time = "00:01:23"
        item.download_size = 123 * 1024 * 1024  # setSize()가 바이트 숫자를 "123.00 MB"로 포맷한다
        item.download_speed = "3.2 MB/s"
        item.download_progress = 42
        widget = _build_widget(item, qapp)

        # 카드 폭 600px에 라벨이 여럿 몰려 있어 정확히 어디까지 잘리는지는
        # 폰트 메트릭에 좌우된다(#229 후속 CI 실패로 이미 겪은 함정) — 정확한
        # 절단 지점 대신 "빈 문자열이 아니다(#232 회귀)"와 "숫자 부분(123)은
        # 살아있다"만 고정한다.
        assert _rendered(widget.statusLabel) != ""
        assert _rendered(widget.fileSizeLabel) != ""
        assert "123" in _rendered(widget.fileSizeLabel)

    def test_finished_shows_download_time_and_size(self, qapp):
        item = _make_item()
        item.download_size = 800 * 1024 * 1024
        item.downloadState = DownloadState.FINISHED
        item.download_time = "00:05:12"
        widget = _build_widget(item, qapp)

        assert _rendered(widget.statusLabel) != ""
        assert "00:05:12" in _rendered(widget.statusLabel)
        assert _rendered(widget.fileSizeLabel) != ""

    def test_failed_shows_failure_reason(self, qapp):
        item = _make_item()
        item.downloadState = DownloadState.FAILED
        item.stateMessage = "Postprocessing failed"
        widget = _build_widget(item, qapp)

        assert _rendered(widget.statusLabel) != ""
