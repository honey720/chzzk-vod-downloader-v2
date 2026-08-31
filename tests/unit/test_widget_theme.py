"""카드가 다운로드 상태에 맞는 색·슬롯을 실제로 입는지 검증한다 (#227→#245).

전역 `.qss`의 `#statusLabel[state="..."]` 규칙이 옳은 토큰을 쓰는지는
`test_theme.py`가 실제 파일 소스로 본다. 여기서는 **위젯이 그 규칙을 타는
동적 속성·가시성을 실제로 세팅하는지**를 본다.

**#245 상태별 슬롯**: 3행은 상태마다 다른 것을 보여준다 — 대기=해상도
pill, 진행=%·속도·남은시간, 완료=✓ 완료, 실패=✕ 사유. 슬롯 텍스트
(statusLabel)의 색은 동적 속성(`state`) + 전역 QSS `[state="..."]` 규칙,
1행 우측 조작(⏸/📁/↻)은 상태별 가시성으로 정해진다. QSS는 `.className`
선택자를 지원하지 않고 조용히 무시하므로 이 배선이 유일한 경로다 —
속성이 상태를 따라가지 않으면 색이 안 변하는데 **아무 에러도 안 난다**.
"""

import pytest
from PySide6.QtGui import QColor

import main as main_module
import theme
from app.viewmodels.item_state import ItemState
from content.data import ContentItem
from content.widget import STATE_ICON, ContentItemWidget
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
    # 테스트 아이템의 기본 경로를 전역 설정 경로로 등록한다(#245) — 실제
    # 앱은 시작 시 mainWindow가 밀어 넣는 값이라, 안 넣으면 모든 카드가
    # "전역과 다른 경로"로 판정돼 경로 라벨이 떠서 3행 슬롯을 밀어낸다.
    monkeypatch.setattr("content.widget._global_download_path", "C:/Users/LeeDH/Downloads")


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
    # 일시정지는 자기 색이다(#245 정정 — 진행바가 남아 있는 상태라 muted
    # 색으로 "돌고 있지 않다"를 알려야 한다. 대기 회색 재사용은 옛 매핑)
    (DownloadState.PAUSED, "paused"),
    (ItemState.LOADING, "waiting"),
    (DownloadState.RUNNING, "running"),
    (DownloadState.FINISHED, "finished"),
    (DownloadState.FAILED, "failed"),
]


@pytest.mark.parametrize("download_state,card_state", STATE_MAP)
def test_slot_label_gets_the_state_colour_property(qapp, download_state, card_state):
    """3행 슬롯 라벨이 옳은 색 규칙(`state` 동적 속성)을 받는지 (#245).

    ⚠️ 글리프 렌더 픽셀은 직접 샘플링하지 않는다 — 폰트 가용성에 좌우돼
    CI 헤드리스 이미지에서 글리프가 안 그려지는 걸 실측했다(#245 첫 CI).
    `[state="..."]` 규칙이 옳은 토큰을 쓰는지는 `test_theme.py`가 실제
    QSS 소스로(폰트 무관) 확인하고, 여기서는 위젯이 그 규칙을 타는 동적
    속성을 실제로 세팅했는지(폰트 무관, 진행바와 같은 패턴)만 본다.
    """
    widget = _widget(qapp, download_state, progress=50)
    assert widget.statusLabel.property("state") == card_state


@pytest.mark.parametrize("download_state,card_state", STATE_MAP)
def test_slot_swaps_between_pills_and_status_text(qapp, download_state, card_state):
    """3행 슬롯 — 대기에만 pill, 그 외에는 상태 텍스트가 보인다 (#245).

    isVisible()은 조상이 안 보이면 항상 False라 위젯을 show()한 뒤 잰다.
    """
    widget = _widget(qapp, download_state, progress=50)
    widget.show()
    pills_expected = download_state == DownloadState.WAITING
    assert widget.statusLabel.isVisible() == (not pills_expected)


@pytest.mark.parametrize(
    "download_state,pause,folder,retry",
    [
        (DownloadState.WAITING, False, False, False),
        (DownloadState.RUNNING, True, False, False),
        (DownloadState.PAUSED, True, False, False),
        (DownloadState.FINISHED, False, True, False),
        (DownloadState.FAILED, False, False, True),
    ],
)
def test_state_actions_visibility(qapp, download_state, pause, folder, retry):
    """1행 우측 조작 — 진행·일시정지=pauseButton(도형은 pause↔resume,
    `test_card_state_matrix.py`가 본다), 완료=폴더, 실패=재시도, 삭제는
    항상 (#245 확정 매트릭스)."""
    widget = _widget(qapp, download_state, progress=50)
    widget.show()
    assert widget.pauseButton.isVisible() == pause
    assert widget.openDirectoryButton.isVisible() == folder
    assert widget.retryButton.isVisible() == retry
    assert widget.deleteButton.isVisible()


def test_failed_hides_file_size_for_the_reason(qapp):
    """실패 카드의 3행 우측은 파일 크기 대신 사유가 자리를 쓴다 (#245)."""
    widget = _widget(qapp, DownloadState.FAILED, progress=0)
    widget.show()
    assert not widget.fileSizeLabel.isVisible()
    widget2 = _widget(qapp, DownloadState.WAITING)
    widget2.show()
    assert widget2.fileSizeLabel.isVisible()


@pytest.mark.parametrize("download_state,card_state", STATE_MAP)
def test_progress_bar_property_follows_the_state(qapp, download_state, card_state):
    widget = _widget(qapp, download_state, progress=50)
    assert widget.progressBar.property("state") == card_state


def test_card_border_stays_neutral_regardless_of_state(qapp):
    """카드 테두리는 상태와 무관하게 항상 중립색이어야 한다(#244).

    상태 신호를 "테두리·진행바·텍스트" 3중에서 "아이콘·진행바" 2가지로
    줄인 결정의 핵심 불변식 — 테두리가 다시 상태색을 따라가면 그 결정이
    조용히 되돌아간 것이다.
    """
    neutral = QColor(theme.DARK["border"])
    for download_state, _ in STATE_MAP:
        widget = _widget(qapp, download_state, progress=50)
        assert _card_border_colour(widget.contentFrame) == neutral


def test_state_change_repaints_an_existing_card(qapp):
    """같은 위젯이 상태를 갈아탈 때도 따라와야 한다 — 카드는 재사용된다."""
    widget = _widget(qapp, DownloadState.WAITING)
    widget.show()
    assert widget.statusLabel.property("state") == "waiting"
    assert not widget.statusLabel.isVisible()  # 대기 슬롯은 pill 몫

    widget.item.downloadState = DownloadState.RUNNING
    widget.item.download_progress = 30
    widget.setData(widget.item, 0)
    assert widget.statusLabel.property("state") == "running"
    assert widget.statusLabel.isVisible()
    assert widget.progressBar.property("state") == "running"
    assert widget.progressBar.value() == 30

    widget.item.downloadState = DownloadState.FAILED
    widget.setData(widget.item, 0)
    assert widget.statusLabel.property("state") == "failed"
    assert widget.statusLabel.text().startswith(STATE_ICON["failed"])
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


def test_resolution_buttons_get_their_own_row_not_the_title_row(qapp):
    """해상도 버튼은 제목과 같은 줄을 공유하지 않는다(#244 — 무관 정보 분리).

    이전엔 `titleLayout`에 얹혀 "제목과 해상도라는 서로 무관한 정보가
    같은 줄에 섞인다"는 문제였다.
    """
    item = _make_item()
    item.unique_reps = [["1080", "https://x/1080"], ["720", "https://x/720"]]
    widget = ContentItemWidget(item, 0)
    widget.setData(item, 0)
    widget.addRepresentationButtons()
    qapp.processEvents()

    assert widget.titleLayout.count() == 2, "titleLayout에는 제목 라벨·편집창만 있어야 한다"
    # 3행 구조(#245 상태별 슬롯): [pill들..., statusLabel(슬롯 텍스트),
    # 가운데 스트레치, 경로 라벨, 파일 크기]. pill은 왼쪽부터 순서대로,
    # 스트레치가 없으면 넓은 창에서 흩어지고(오너 실기 확인), 파일 크기는
    # 우측 끝 — 간격·기준선은 tests/unit/test_card_layout.py가 폭 3종으로
    # 게이트한다. (경로 편집 QLineEdit은 폴더 선택으로 교체돼 항목에서 빠졌다)
    assert widget.resolutionLayout.count() == len(widget.buttons) + 4
    for position, button in enumerate(widget.buttons):
        assert widget.resolutionLayout.indexOf(button) == position, (
            "해상도 버튼이 왼쪽부터 순서대로 놓이지 않았다"
        )
    stretch_index = widget.resolutionLayout.indexOf(widget.statusLabel) + 1
    stretch_item = widget.resolutionLayout.itemAt(stretch_index)
    assert stretch_item.widget() is None and stretch_item.spacerItem() is not None, (
        "슬롯과 파일 크기 사이의 가운데 스트레치가 없다 — 넓은 창에서 흩어진다"
    )
    last = widget.resolutionLayout.itemAt(widget.resolutionLayout.count() - 1)
    assert last.widget() is widget.fileSizeLabel, "파일 크기가 3행 우측 끝이 아니다"


def test_icon_buttons_are_marked(qapp):
    widget = _widget(qapp, DownloadState.WAITING)
    assert widget.deleteButton.property("role") == "icon"
    assert widget.openDirectoryButton.property("role") == "icon"


class TestCardInnerWidthIsUnchanged:
    """카드 안쪽 가로 가용 폭은 "카드 폭 − (테두리 2 + cardPadding×2)"로 고정이다.

    **이 파일에서 가장 중요한 테스트다.** 스타일을 입히면서 테두리나 가로
    여백을 늘리면 제목·경로·상태 라벨이 받는 폭이 그만큼 줄고, 라벨이
    "어디서 잘리는가"를 고정해 둔 회귀 테스트들(`test_widget.py`,
    `test_content_view.py::TestNoHorizontalOverflow`)이 3-OS 폰트 메트릭
    차이에 걸려 깨진다. 그 테스트들은 잘림 위치를 직접 보기 때문에
    **로컬 한 OS에서는 통과하고 CI 3-OS에서 실패**할 수 있다 — PR #234의
    첫 CI가 정확히 그랬다.

    **#244 재설계로 기준이 바뀌었다(재베이스라인)**: 이전 불변식은
    "위젯 폭 − 20px"(카드 외부 여백 9×2 + 테두리 1×2)이었다. 재설계 후
    카드 외부 좌우 여백은 0(정렬선을 상·하단 바와 공유), 안쪽 여백은
    QSS padding이 아니라 bodyLayout 마진(theme.METRICS["cardPadding"])이
    준다 — 하단 진행바가 카드 가장자리에 딱 붙어야 해서다. 그래서 이제
    "프레임 contentsRect == 위젯 폭 − 2(테두리)"와 "본문 가용 폭 ==
    위젯 폭 − 2 − cardPadding×2"를 함께 고정한다. 상수가 아니라 토큰을
    직접 읽으므로 오너가 cardPadding을 바꿔도 게이트는 옳게 따라간다.

    **#237 추가 — "순수 기하값"이되 요청 폭이 아니라 실제 폭을 써야
    한다.** `resize(...)`가 요청한 폭 그대로 받는다는 보장은 없다 —
    최소폭 클램프가 있어도 "관계"는 폰트와 무관하게 성립한다.
    """

    @staticmethod
    def _expected_inset():
        return 2 + 2 * theme.METRICS["cardPadding"]  # 테두리 1×2 + 안쪽 여백×2

    @pytest.mark.parametrize("width", [460, 600, 900])
    def test_content_width_matches_the_baseline_budget(self, qapp, width):
        widget = _widget(qapp, DownloadState.WAITING)
        widget.resize(width, 130)
        widget.show()
        qapp.processEvents()

        actual_width = widget.width()
        frame_inner = widget.contentFrame.contentsRect().width()
        assert frame_inner == actual_width - 2, (
            "카드 프레임에 테두리 외 가로 여백이 생겼다 — QSS padding이 다시 붙었는지 확인"
        )
        body = widget.bodyLayout.contentsRect().width()
        assert body == actual_width - self._expected_inset(), (
            f"본문 가용 폭이 기대에서 벗어났다(위젯 {actual_width}px, 본문 {body}px) — "
            "라벨 잘림 위치가 밀려 3-OS 폰트 메트릭 회귀 테스트가 깨진다"
        )

    @pytest.mark.parametrize(
        "download_state",
        [DownloadState.WAITING, DownloadState.RUNNING, DownloadState.FINISHED, DownloadState.FAILED],
    )
    def test_no_extra_horizontal_inset_in_any_state(self, qapp, download_state):
        """상태가 달라져도 가로 가용 폭은 같아야 한다 — 폭이 상태에 따라 흔들리면 안 된다."""
        widget = _widget(qapp, download_state)
        widget.resize(600, 130)
        widget.show()
        qapp.processEvents()
        body = widget.bodyLayout.contentsRect().width()
        assert body == widget.width() - self._expected_inset()
