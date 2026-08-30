"""카드가 다운로드 상태에 맞는 색을 실제로 입는지 검증한다 (#227, #240 2단계).

전역 `.qss`의 `#contentFrame[state="..."]` 규칙이 옳은 토큰을 쓰는지는
`test_theme.py`가 실제 파일 소스로 본다. 여기서는 **위젯이 그걸 실제로
렌더링하는지** — 상태가 바뀔 때마다 `setData()`가 카드 테두리와 진행바를
같이 갱신하는지를, `widget.grab()`으로 실제 렌더된 픽셀을 읽어 확인한다.

카드 테두리·진행바 색 둘 다 위젯 스타일시트가 아니라 동적 속성(`state`) +
전역 QSS의 `[state="..."]` 규칙으로 정해진다(#240 2단계 — 카드 프레임도
위젯별 `setStyleSheet`에서 전역 QSS로 옮겼다). QSS는 `.className` 선택자를
지원하지 않고 조용히 무시하므로 이 배선이 유일한 경로다 — 속성이 상태를
따라가지 않으면 색이 안 변하는데 **아무 에러도 안 난다**. `styleSheet()`
문자열을 파싱해 검증하던 이전 버전은 전역 QSS로 옮기며 전부 빈 문자열만
돌려주게 됐다 — 텍스트가 아니라 실제 렌더 픽셀을 보는 쪽으로 바꿨다.
"""

import pytest
from PySide6.QtGui import QColor

import main as main_module
import theme
from app.viewmodels.item_state import ItemState
from content.data import ContentItem
from content.widget import ContentItemWidget
from core.models.download_state import DownloadState


@pytest.fixture(autouse=True)
def _apply_dark_card_qss(qapp):
    """이 파일의 테스트에만 실제 프로덕션 스타일시트를 적용한다.

    카드 프레임 지오메트리·색 검증은 전역 QSS가 실제로 태워진 상태를
    재야 의미가 있다(모듈 docstring 참고) — 이 요구가 있는 파일에만
    국소적으로 적용한다.

    ⚠️⚠️⚠️ **반드시 `scope="function"`(기본값)으로 유지할 것 — 범위를
    넓히면(session/module 스코프로 바꾸거나 이 파일 밖으로 확대) macOS
    CI에서 프로세스 종료 시점에 SIGSEGV가 재발한다.** 매 테스트 함수마다
    새로 지어 다시 건다는 게 핵심이다 — "몇 개 파일/몇 개 테스트에 적용
    되는가"(범위)가 아니라 "만든 `theme.build_style()` 객체를 테스트
    하나가 끝난 뒤에도 `qapp`에 계속 걸어 둔 채로 살려두는가"(수명)가
    실제 경계선이다(아래 실측 참고). 다음에 "국소 픽스처를 매번 다시
    만드는 게 낭비 같으니 파일/세션 스코프로 캐싱하자"는 생각이 들면
    바로 이 경고를 볼 것 — 그 리팩터링이 정확히 크래시를 재현한다.

    **경계 실측(2026-08-30, #242)**: 스위트 586개 전체에 세션 스코프로
    걸었을 때도, 이 두 파일에만 좁히고 스코프만 `module`로 바꿨을 때도
    (약 28개 테스트, 파일당 인스턴스 1개) 둘 다 macOS에서 크래시가
    재현됐다 — 즉 **몇 개 테스트에 적용하는지(범위)는 무관했고, 인스턴스가
    테스트 하나를 넘어 살아있는지(수명)가 경계였다.** `scope="function"`
    (지금 이 상태, 테스트마다 새 인스턴스를 짓고 버림)만 두 차례 연속
    macOS 통과를 재현했다.

    **크래시 스택(`faulthandler` 도입 후 처음 확보, module 스코프 실험에서
    수집)**: `Fatal Python error: Segmentation fault`가 pytest 자체의
    `_pytest/unraisableexception.py::gc_collect_harder`(세션 종료 시
    `pytest_unconfigure`가 미처리 unraisable exception을 잡으려고 강제로
    `gc.collect()`를 여러 번 도는 지점, 우리 테스트 코드가 아니라 pytest
    내부) 안에서 죽는다. **추정(확정 아님)**: `theme.build_style()`이
    만드는 `_DropDownComboBoxStyle(QProxyStyle)`(Fusion을 감싼 것)가
    `qapp.setStyle()`로 소유권이 Qt C++ 쪽으로 넘어간 뒤, 그 파이썬
    래퍼가 한 테스트를 넘어 오래 살아있으면 강제 GC 사이클 수집 시점에
    shiboken/Qt 쪽 소유권과 충돌해(정확한 메커니즘 미확인 — lldb 등 더
    깊은 도구 없이는 이 이상 못 판다) 크래시하는 것으로 보인다. Windows·
    Linux에서는 재현 안 됨 — macOS의 Qt/shiboken 오브젝트 수명 처리가
    더 엄격하거나 다른 것으로 추정.

    `main.apply_theme()`을 그대로 안 쓰는 이유: 그 함수의
    `theme.detect_color_scheme(app)`이 오프스크린 QPA 팔레트 폴백에서
    "light"로 나온다(실측 확인) — 그대로 쓰면 이 파일의 `theme.DARK[...]`
    기대값이 전부 깨진다. 스킴 감지는 건너뛰고 "dark"만 명시 고정한다.
    """
    theme.set_color_scheme("dark")
    qapp.setStyle(theme.build_style())
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))


def _card_border_colour(frame) -> QColor:
    """카드 프레임의 실제 렌더 테두리 색 — 좌측 변, 세로 중앙 지점.

    모서리(border-radius)를 피해야 한다 — 둥근 모서리 부근은 배경색과
    안티앨리어싱이 섞여 순수 테두리 색이 안 나온다(실측 확인). 좌측 변
    중앙은 카드 높이 전체에서 곡률이 없는 안전한 지점이다.
    """
    img = frame.grab().toImage()
    return img.pixelColor(0, img.height() // 2)


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
    expected = QColor(theme.DARK["state" + card_state.capitalize()])
    assert _card_border_colour(widget.contentFrame) == expected


@pytest.mark.parametrize("download_state,card_state", STATE_MAP)
def test_progress_bar_property_follows_the_state(qapp, download_state, card_state):
    widget = _widget(qapp, download_state, progress=50)
    assert widget.progressBar.property("state") == card_state


def test_state_change_repaints_an_existing_card(qapp):
    """같은 위젯이 상태를 갈아탈 때도 따라와야 한다 — 카드는 재사용된다."""
    widget = _widget(qapp, DownloadState.WAITING)
    assert _card_border_colour(widget.contentFrame) == QColor(theme.DARK["stateWaiting"])

    widget.item.downloadState = DownloadState.RUNNING
    widget.item.download_progress = 30
    widget.setData(widget.item, 0)
    assert _card_border_colour(widget.contentFrame) == QColor(theme.DARK["stateRunning"])
    assert widget.progressBar.property("state") == "running"
    assert widget.progressBar.value() == 30

    widget.item.downloadState = DownloadState.FAILED
    widget.setData(widget.item, 0)
    assert _card_border_colour(widget.contentFrame) == QColor(theme.DARK["stateFailed"])
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

    **#237 추가 — "순수 기하값"이되 요청 폭이 아니라 실제 폭을 써야
    한다.** `resize(300, ...)`을 줘도 `ElidingLabel.minimumSizeHint()`가
    폰트 메트릭 기반이라 위젯이 요청보다 넓게 클램프될 수 있다(로컬
    Windows 오프스크린 대체 폰트에서 300px 요청 → 실제 308px). 값 자체는
    폰트가 정하지만 "위젯 폭 − 20px = 안쪽 폭"이라는 **관계**는 폰트와
    무관해서, `widget.width()`(실제 폭) 기준으로 재면 어느 폰트에서든
    깨지지 않는다.
    """

    #: `contentItemLayout`의 좌우 여백(9) + 카드 테두리(1), 양쪽 합계.
    EXPECTED_HORIZONTAL_INSET = 20

    @pytest.mark.parametrize("width", [300, 460, 600])
    def test_content_width_matches_the_baseline_budget(self, qapp, width):
        widget = _widget(qapp, DownloadState.WAITING)
        widget.resize(width, 134)
        widget.show()
        qapp.processEvents()

        # `resize(width, ...)`가 요청한 폭 그대로 받는다는 보장은 없다 —
        # `ElidingLabel.minimumSizeHint()`가 폰트 메트릭으로 최소 폭을
        # 요구해서, 요청 폭이 그 합보다 좁으면 Qt가 위젯을 더 넓게
        # 클램프한다(로컬 Windows 오프스크린 대체 폰트에서 300px 요청 시
        # 실측 308px — #237에서 확인). 그래서 요청값(`width`)이 아니라
        # 실제로 받은 폭(`widget.width()`)을 기준으로 잰다 — 클램프가
        # 얼마가 됐든 "위젯 폭 − 20px = 카드 안쪽 폭"이라는 진짜 불변식은
        # 폰트와 무관하게 그대로 성립한다(테두리 1px + 레이아웃 여백 9px,
        # 양쪽 합 20px은 명시 상수라 플랫폼·폰트에 안 흔들린다).
        actual_width = widget.width()
        inner = widget.contentFrame.contentsRect().width()
        assert inner == actual_width - self.EXPECTED_HORIZONTAL_INSET, (
            f"카드 안쪽 가용 폭이 {actual_width - inner}px 줄었다(기대 "
            f"{self.EXPECTED_HORIZONTAL_INSET}px, 실제 위젯 폭 {actual_width}px) — "
            "라벨 잘림 위치가 밀려 3-OS 폰트 메트릭 회귀 테스트가 깨진다"
        )

    @pytest.mark.parametrize(
        "download_state",
        [DownloadState.WAITING, DownloadState.RUNNING, DownloadState.FINISHED, DownloadState.FAILED],
    )
    def test_no_horizontal_padding_in_any_state(self, qapp, download_state):
        """상태가 달라져도 가로 여백은 0이어야 한다 — 폭이 상태에 따라 흔들리면 안 된다.

        (#240 2단계) padding은 이제 상태와 무관한 공통 규칙 하나뿐이라
        `theme.card_style()`처럼 상태별 문자열을 만드는 함수 자체가 없다 —
        텍스트를 읽는 대신 상태별로 실제 위젯을 만들어 지오메트리가
        흔들리지 않는지 직접 잰다(테두리 색만 다르고 폭에 영향을 주면 안
        되는 게 진짜 불변식이다).
        """
        widget = _widget(qapp, download_state)
        widget.resize(460, 134)
        widget.show()
        qapp.processEvents()
        inner = widget.contentFrame.contentsRect().width()
        assert inner == widget.width() - self.EXPECTED_HORIZONTAL_INSET
