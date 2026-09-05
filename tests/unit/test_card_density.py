"""카드 목록 밀도 회귀 게이트 (#239 실측 근거, #244 카드 압축의 완료 조건).

오너가 확정한 수치 목표: 창 높이 1000px에서 카드 ≥6개, 750px에서 ≥4개가
보여야 한다. 실제 `application.mainWindow.VodDownloader`(헤더·목록·하단
바를 갖춘 실제 창)를 생성해 실제 리사이즈·실제 레이아웃 결과로 잰다 —
카드 sizeHint()나 QSS 상수로 계산한 값이 아니라, 목록 뷰포트에 실제로
들어간 위젯 지오메트리를 직접 본다.

⚠️ 이 수치는 지금 함께 떠 있는 헤더(`headerFrame`)·하단바(`infoFrame`)
크기를 전제로 한다 — 이번 PR은 카드까지만이고 헤더·목록 셸 자체의
재설계는 다음 PR이다. 셸이 바뀌면 이 게이트를 다시 재야 한다(그때도
"실제 창 실측"이라는 방법론은 그대로 유지할 것 — 계산값으로 대체하지 말 것).
"""

import pytest
from PySide6.QtWidgets import QApplication

import main as main_module
import app.theme as theme
from app.views.mainWindow import VodDownloader
from app.viewmodels.data import ContentItem
from core.models.download_state import DownloadState
from tests.unit.card_helpers import hold_style


@pytest.fixture(autouse=True)
def _apply_production_qss(qapp):
    """실제 전역 QSS를 태운 상태에서 잰다 — 안 태우면 카드 높이가 QSS
    토큰(cardPaddingV 등)과 무관하게 항상 같아져 이 게이트가 카드 압축
    회귀를 못 잡는다(#242에서 겪은 "게이트 눈멀기"와 같은 함정).

    ⚠️ `scope="function"`(기본값) 유지 — session/module로 넓히면 macOS
    프로세스 종료 시점 크래시가 재현된다(`test_widget_theme.py`의
    `_apply_dark_card_qss` 문서 참고, 같은 `theme.build_style()` 수명 문제).
    """
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))  # 참조 보관 — 이중 해제 우회 (#243, card_helpers.hold_style)
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    class _FailingSession:
        def head(self, *a, **k):
            raise RuntimeError("network disabled in tests")

        def get(self, *a, **k):
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("app.widgets.widget.get_thread_session", lambda: _FailingSession())


def _make_item(i: int) -> ContentItem:
    return ContentItem(
        f"https://chzzk.naver.com/video/{i}",
        {"title": f"제목 {i}", "category": "", "channelName": f"채널{i}", "createdDate": "", "duration": 3600},
        [], None, "", "C:/dl", "video", None,
    )


def _visible_card_count(window: VodDownloader, window_height: int, require_fully_visible: bool) -> int:
    window.resize(900, window_height)
    QApplication.processEvents()

    viewport = window.listView.viewport()
    viewport_height = viewport.height()
    count = 0
    for widget in window.listView._widgets.values():
        top = widget.mapTo(viewport, widget.rect().topLeft()).y()
        bottom = widget.mapTo(viewport, widget.rect().bottomLeft()).y()
        if require_fully_visible:
            visible = top >= 0 and bottom <= viewport_height
        else:
            shown = max(0, min(bottom, viewport_height) - max(top, 0))
            visible = shown / max(1, widget.height()) >= 0.5
        if visible:
            count += 1
    return count


@pytest.fixture
def window_with_cards(qapp):
    window = VodDownloader()
    for i in range(30):  # 두 밀도 목표(6·4)를 넉넉히 넘는 카드 수
        item = _make_item(i)
        item.downloadState = DownloadState.WAITING
        window.contentManager.model.addItem(item)
    QApplication.processEvents()
    yield window
    window.deleteLater()
    QApplication.processEvents()


@pytest.mark.parametrize(
    "window_height,minimum_cards",
    [(1000, 6), (750, 4)],
)
def test_card_density_meets_the_owner_confirmed_minimum(window_with_cards, window_height, minimum_cards):
    fully_visible = _visible_card_count(window_with_cards, window_height, require_fully_visible=True)
    assert fully_visible >= minimum_cards, (
        f"창 높이 {window_height}px에서 완전히 보이는 카드가 {fully_visible}개뿐이다 "
        f"(최소 {minimum_cards}개 필요) — 카드 압축(#244)이 목표를 못 채운 회귀다"
    )
