"""`ContentItemWidget` 렌더 회귀 테스트 (PR #229 오너 실기 확인 — statusLabel·
fileSizeLabel이 폭 0으로 눌려 빈 문자열만 그려지던 회귀).

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


def _make_item(content_type="video", title="제목", channel="채널"):
    return ContentItem(
        "https://chzzk.naver.com/video/1",
        {"title": title, "category": "", "channelName": channel, "createdDate": "", "duration": 3600},
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


def _build_widget(item, qapp, width: int = 600) -> ContentItemWidget:
    """위젯을 만들고 `setData()`까지 반영한 뒤 실제로 보여서 레이아웃을
    확정한다. `show()` 없이는 `topLayout`이 활성화되지 않아 라벨들의
    `width()`가 Qt 기본값(작음)에 머물러 있을 수 있다 — 실제 앱(카드가
    보이는 `QScrollArea` 안)과 다른 조건에서 재는 셈이라 반드시 보여준다.
    """
    widget = ContentItemWidget(item, 0)
    widget.setData(item, 0)
    widget.resize(width, 134)
    widget.show()
    qapp.processEvents()
    return widget


#: 라벨이 완전히 안 잘리는 걸 구조적으로 보장하려는 폭 (#237).
#: `_build_widget()` 기본값(600)에서는 로컬 Windows 오프스크린 대체 폰트가
#: CI 고정 폰트보다 넓어 "1.2 GB"·"00:05:12" 같은 짧은 값조차 elide 문턱을
#: 넘었다(실측: 650px부터 로컬에서도 안 잘림, 900은 그 위에 넉넉한 여유를
#: 더한 값 — 이 폭에서는 특정 문자가 아니라 "잘리지 않는다"는 폭 자체의
#: 성질을 확인하는 것이라 어느 폰트에서도 다시 안 깨진다).
_UNCONSTRAINED_WIDTH = 900


class TestStatusAndFileSizeAreVisible:
    """카드 폭이 넉넉한(창을 좁히지 않은) 정상 상태에서 이 라벨들이 실제로
    보여야 한다 — PR #229 이후 폭 0으로 눌려 전부 빈 문자열이 되던 회귀."""

    def test_waiting_file_type_shows_status_and_total_size(self, qapp):
        item = _make_item(content_type="video")
        item.total_size = "1.2 GB"
        widget = _build_widget(item, qapp, width=_UNCONSTRAINED_WIDTH)

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
        # 절단 지점 대신 "빈 문자열이 아니다(폭 0 회귀)"와 "숫자 부분(123)은
        # 살아있다"만 고정한다.
        assert _rendered(widget.statusLabel) != ""
        assert _rendered(widget.fileSizeLabel) != ""
        assert "123" in _rendered(widget.fileSizeLabel)

    def test_finished_shows_download_time_and_size(self, qapp):
        item = _make_item()
        item.download_size = 800 * 1024 * 1024
        item.downloadState = DownloadState.FINISHED
        item.download_time = "00:05:12"
        widget = _build_widget(item, qapp, width=_UNCONSTRAINED_WIDTH)

        assert _rendered(widget.statusLabel) != ""
        assert "00:05:12" in _rendered(widget.statusLabel)
        assert _rendered(widget.fileSizeLabel) != ""

    def test_failed_shows_failure_reason(self, qapp):
        item = _make_item()
        item.downloadState = DownloadState.FAILED
        item.stateMessage = "Postprocessing failed"
        widget = _build_widget(item, qapp)

        assert _rendered(widget.statusLabel) != ""


class TestChannelNameYieldsBeforeStatusAndFileSize:
    """`topLayout`이 빠듯하면 채널명이 먼저 줄고, 상태·파일 크기는 끝까지
    버텨야 한다(오너 지시 — 채널명은 길이가 임의라 잘려도 손해가 작지만,
    파일 크기·상태는 다운로드를 지켜보는 유저에게 더 중요한 정보다).

    `channelNameLabel`에 준 큰 stretch(100)가 이 우선순위를 만든다 — 채널명이
    `setMaximumWidth(150)` 상한에 걸려도 "Download waiting"·"1.24 GB" 같은
    상태·크기 라벨은 안 줄어드는 걸 확인했다. `setMaximumWidth(150)`도 같이
    거는데, 이게 없으면 stretch가 커서 여유가 있을 때도 채널명이 스페이서
    몫까지 욕심내 폭이 과하게 넓어진다(실측 확인 — 249px까지 늘어났었음).
    """

    def test_narrow_width_elides_channel_name_but_not_status_or_size(self, qapp):
        # 채널명은 넉넉히 길게 잡는다 — 짧은 채널명은 플랫폼 폴백 폰트에서
        # 글리프 폭이 좁아 150px 상한 안에 통째로 들어가 버려(실측: 이
        # 환경에서 채널명 라벨이 99px을 받는데 "우왁굳의 게임방송"이 안
        # 잘리고 그대로 표시됨) 애초에 잘림이 안 생기는 환경이 있다 —
        # 길이를 넉넉히 둬 어떤 폰트 메트릭에서도 반드시 잘리게 한다.
        # `setMaximumWidth(150)`이 폰트와 무관하게 상한을 강제하므로 이
        # 채널명은 카드 폭을 얼마나 늘려도 반드시 잘린다 — 그래서 카드
        # 폭 자체는 `_UNCONSTRAINED_WIDTH`로 넉넉히 줘도 "채널명만 줄고
        # 나머지는 안 준다"는 우선순위 검증은 그대로 유지된다.
        #
        # #237: 원래 460px을 썼는데, 로컬 Windows 오프스크린 대체 폰트가
        # CI 고정 폰트보다 넓어 이 폭에서는 채널명이 다 죽고도 상태·크기
        # 라벨까지 밀려 잘렸다(우선순위 메커니즘 자체는 살아 있었지만
        # 여유가 부족했다) — 로컬에서도 안 잘리는 최소치가 650px이라 700을
        # 썼다. `_UNCONSTRAINED_WIDTH`(900)까지 안 올린 이유: 고장 주입으로
        # 확인해 보니 900에서는 `channelNameLabel`의 stretch를 0으로 빼도
        # (우선순위 메커니즘 자체를 없애도) 이 테스트가 통과했다 — 그만큼
        # 넓으면 아예 경쟁이 안 생겨 무엇이 이 결과를 만드는지 더는 검증하지
        # 못한다. 700은 "로컬에서 안 잘림(650+)"과 "stretch=0이면 여전히
        # 잘림(760까지)"의 교집합이라, 우선순위 메커니즘이 실제로 결과를
        # 만든다는 것 자체를 계속 검증한다.
        item = _make_item(content_type="video", channel="우왁굳의 게임방송 다시보기 풀버전 모음집 전체")
        item.total_size = "1.24 GB"
        widget = _build_widget(item, qapp, width=700)

        assert _rendered(widget.channelNameLabel) != widget.channelNameLabel.text(), (
            "채널명이 안 잘렸다 — 이 폭에서는 채널명이 먼저 줄어야 한다"
        )
        # "정확히 어디까지 잘리는가"가 아니라 "안 잘려야 한다"는 우선순위
        # 자체가 검증 대상이라, 원문과의 완전 일치로 잰다 — 부분 문자열
        # 검사(`"..." in ...`)는 잘림 문턱이 폰트마다 달라 흔들린다.
        assert _rendered(widget.statusLabel) == widget.statusLabel.text(), (
            "상태 문구가 잘렸다 — 채널명보다 먼저 줄면 안 된다"
        )
        assert _rendered(widget.fileSizeLabel) == widget.fileSizeLabel.text(), (
            "파일 크기가 잘렸다 — 채널명보다 먼저 줄면 안 된다"
        )

    def test_wide_width_does_not_over_grow_channel_name(self, qapp):
        """여유가 있을 때 stretch 때문에 채널명이 스페이서 몫까지 삼키면 안 된다."""
        item = _make_item(content_type="video", channel="짧은채널")
        widget = _build_widget(item, qapp, width=600)

        # 실제 내용("짧은채널")이 필요로 하는 폭보다 과하게 넓어지지 않아야 한다
        # — setMaximumWidth(150) 상한 이내여야 한다
        assert widget.channelNameLabel.width() <= 150


class TestRecoversFullTextAfterWidthIncreases:
    """한 번 좁은 폭에서 "..."까지 줄었다가 창을 다시 넓히면 전체 텍스트로
    돌아와야 한다 — 여백이 충분한데도 파일 크기가 항상 "..."으로만 보이던
    회귀(오너 실기 확인). `TestStatusAndFileSizeAreVisible`은 위젯을 처음부터
    넉넉한 폭으로만 만들어 검증하기 때문에 이 시나리오(좁았다가 넓어짐)를
    전혀 못 본다 — 이게 바로 신규 6개 테스트를 추가하고도 이 회귀를 못 잡은
    이유다.

    원인: `ElidingLabel.sizeHint()`를 override하지 않으면 `QLabel` 기본
    구현이 "지금 화면에 그려진(이미 elide된) 텍스트" 기준으로 계산한다.
    한 번이라도 좁은 폭에서 "..."까지 줄어들면, 그 뒤로 레이아웃이 다시
    물어봐도 sizeHint가 계속 "..." 하나 폭만 요구해 — 창을 아무리 넓혀도
    다시는 더 넓은 폭을 받지 못하는 되먹임 루프에 갇힌다.
    """

    def test_file_size_label_recovers_when_widened_after_being_narrow(self, qapp):
        item = _make_item(content_type="video")
        item.total_size = "1.24 GB"
        widget = _build_widget(item, qapp, width=300)

        # sanity check: 이 폭에서 실제로 잘렸는지 확인 — 안 잘렸으면 이
        # 테스트가 애초에 무엇을 검증하는지 의미가 없어진다.
        assert _rendered(widget.fileSizeLabel) != widget.fileSizeLabel.text(), (
            "폭 300에서 파일 크기가 안 잘렸다 — 이 테스트의 전제(좁았다가 넓어짐)가 성립하지 않는다"
        )

        widget.resize(1200, 134)
        qapp.processEvents()
        qapp.processEvents()

        assert "1.24 GB" in _rendered(widget.fileSizeLabel), (
            "창을 넓혔는데도 파일 크기가 회복되지 않았다 — "
            "sizeHint()가 표시 중인(이미 elide된) 텍스트 기준으로 계산되고 있을 가능성"
        )

    def test_status_label_recovers_when_widened_after_being_narrow(self, qapp):
        # channelNameLabel은 stretch=100이 커서 sizeHint와 무관하게 레이아웃이
        # 넓혀줄 때 같이 딸려 늘어난다 — 그래서 sizeHint() 버그가 있어도
        # 우연히 회복되어 이 회귀를 검증하지 못한다(mutation으로 확인:
        # sizeHint() override를 지워도 채널명은 회복됨). stretch가 0인
        # statusLabel·fileSizeLabel만 이 버그에 실제로 걸린다.
        item = _make_item(content_type="video")
        item.downloadState = DownloadState.FINISHED
        item.download_time = "00:05:12"
        item.download_size = 800 * 1024 * 1024
        widget = _build_widget(item, qapp, width=300)

        assert _rendered(widget.statusLabel) != widget.statusLabel.text(), (
            "폭 300에서 상태 문구가 안 잘렸다 — 이 테스트의 전제가 성립하지 않는다"
        )

        widget.resize(1200, 134)
        qapp.processEvents()
        qapp.processEvents()

        assert "00:05:12" in _rendered(widget.statusLabel), (
            "창을 넓혔는데도 상태 문구가 회복되지 않았다"
        )
