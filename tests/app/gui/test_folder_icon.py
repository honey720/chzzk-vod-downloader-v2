"""폴더 아이콘 게이트 — 그리는 곳 다섯이 한 함수를 지나는지, 윤곽에 뾰족한 꼭짓점이 없는지, 배율에서 잘리지 않는지 (#650).

그리는 곳은 실제 위젯을 `grab()`으로 그려 확인한다. 픽스맵 캐시는 테스트마다 비운다 — 앞 테스트가 그린
픽스맵이 남아 있으면 그리는 함수를 부르지 않고 지나간다.
"""

import math
from collections.abc import Callable, Iterator

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QPainterPath
from PySide6.QtWidgets import QApplication, QWidget

import app.system.settings as settings
import app.widgets.widget as card_module
from app.viewmodels.data import ContentItem
from app.views.mainWindow import VodDownloader
from app.widgets import icons
from app.widgets.widget import ContentItemWidget
from core.models.download_state import DownloadState

GRIDS = (16, 20)  # 폴더가 그려지는 그림 칸 — 메뉴 줄 · 글 옆 · 카드 16, 아이콘만인 헤더 버튼 20(px)
CORNER = 0.10  # 모서리 반경 / 칸 — 이슈 #650 값(중지 · 선택 상자 0.12와 같은 무게)
CORNER_MIN = 0.05  # 짧은 변에서 줄어든 반경의 하한 / 칸 — 16px에서 0.8px 넘게 둥글다
OPEN_CORNERS = 8  # 열린 폴더 꼭짓점 수 — 선 끝 둘을 뺀 점
CLOSED_CORNERS = 6  # 닫힌 폴더(탭 달린 상자) 꼭짓점 수


@pytest.fixture
def folder_calls(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """`_folder_path`를 부를 때마다 `opened` 값을 적는 목록 — 픽스맵 캐시는 빈 것으로 바꾼다."""
    calls: list[bool] = []
    original = icons._folder_path

    def spy(s: float, opened: bool) -> QPainterPath:
        calls.append(opened)
        return original(s, opened)

    monkeypatch.setattr(icons, "_folder_path", spy)
    monkeypatch.setattr(icons, "_CACHE", {})
    return calls


@pytest.fixture
def window(qapp: QApplication, tmp_path) -> Iterator[VodDownloader]:
    """저장 위치가 실존 폴더인 메인 창 — 정리 때 메뉴 · 창을 숨기고 파괴한다(닫지 않는다)."""
    folder = tmp_path / "받기"
    folder.mkdir()
    settings.update_settings({"downloadPath": str(folder)})
    win = VodDownloader()
    win.resize(1100, 640)
    win.show()
    QApplication.processEvents()
    yield win
    win.saveLocationButton.menuWidget.hide()
    win.hide()
    win.deleteLater()
    QApplication.processEvents()


def _card(state: DownloadState, path: str) -> ContentItemWidget:
    """상태 · 저장 위치를 정한 카드 하나를 띄운다."""
    item = ContentItem(
        "https://chzzk.naver.com/video/1",
        {"title": "제목", "category": "", "channelName": "채널", "createdDate": "", "duration": 60},
        [["1080", "u1"]],
        None,
        "",
        path,
        "video",
        None,
    )
    item.downloadState = DownloadState.WAITING
    widget = ContentItemWidget(item, 0)
    widget.loadRepresentations()
    item.total_size = "1 MB"
    item.downloadState = state
    item.download_size = 1_000_000
    item.download_time = "00:00:10"
    widget.setData(item, 0)
    widget.resize(760, widget.sizeHint().height())
    widget.show()
    QApplication.processEvents()
    return widget


def _header_text(win: VodDownloader) -> tuple[QWidget, QWidget | None]:
    return win.saveLocationButton, None


def _header_icon(win: VodDownloader) -> tuple[QWidget, QWidget | None]:
    win.saveLocationButton.setCompact(True)
    QApplication.processEvents()
    return win.saveLocationButton, None


def _menu(win: VodDownloader) -> tuple[QWidget, QWidget | None]:
    button = win.saveLocationButton
    button.menuWidget.popup(button.mapToGlobal(button.rect().bottomLeft()))
    QApplication.processEvents()
    return button.menuWidget, None


def _card_path_mark(win: VodDownloader) -> tuple[QWidget, QWidget | None]:
    card_module.set_global_download_path(win.saveLocations.current())
    card = _card(DownloadState.WAITING, "C:/다른 곳")
    assert card.pathIcon.isVisible(), "전제: 다른 저장 위치 표시가 보인다"
    return card.pathIcon, card


def _card_open_folder(win: VodDownloader) -> tuple[QWidget, QWidget | None]:
    card = _card(DownloadState.FINISHED, win.saveLocations.current())
    card.setHovered(True)
    QApplication.processEvents()
    assert card.openDirectoryButton.isVisible(), "전제: 폴더 열기 버튼이 보인다"
    return card.openDirectoryButton, card


#: 그리는 곳 → (위젯과 그 위젯을 든 카드를 꺼내는 함수, 열린 폴더인지)
SITES: dict[str, tuple[Callable[[VodDownloader], tuple[QWidget, QWidget | None]], bool]] = {
    "header-text": (_header_text, True),  # 헤더바 저장 위치 버튼 — 글 옆 폴더
    "header-icon": (_header_icon, True),  # 좁은 폭 — 아이콘만
    "menu-find": (_menu, False),  # 저장 위치 메뉴 「폴더 찾기…」 줄
    "card-path": (_card_path_mark, False),  # 카드 3줄 다른 저장 위치 표시
    "card-open": (_card_open_folder, True),  # 완료 카드 폴더 열기 버튼
}


@pytest.mark.parametrize("site", SITES)
def test_every_folder_site_draws_through_one_folder_path(
    window: VodDownloader, folder_calls: list[bool], site: str
) -> None:
    """폴더를 그리는 곳은 실제 위젯을 그리면 모두 `_folder_path`를 지나야 한다.

    그리는 곳마다, 캐시를 비운 뒤 그 위젯 grab() -> `_folder_path` 호출이 하나 이상이고 opened 값이 모두
    그곳의 폴더(열린 · 닫힌)와 같다
    """
    pick, opened = SITES[site]
    widget, card = pick(window)
    folder_calls.clear()
    icons._CACHE.clear()  # 띄우면서 그린 픽스맵이 남으면 grab이 그리는 함수를 부르지 않는다

    widget.grab()
    if card is not None:
        card.hide()
        card.deleteLater()

    assert folder_calls, f"{site}: 폴더를 그리면서 _folder_path를 부르지 않았다"
    assert set(folder_calls) == {opened}, f"{site}: {folder_calls}"


def _segments(path: QPainterPath) -> list[tuple[QPointF, QPointF, QPointF, QPointF]]:
    """경로를 (시작, 시작 방향, 끝 방향, 끝) 조각 목록으로 — 길이 0인 조각은 뺀다. 곡선은 3차 곡선이다."""
    pieces = []
    index = 0
    current = QPointF()
    while index < path.elementCount():
        element = path.elementAt(index)
        point = QPointF(element.x, element.y)
        if element.isMoveTo():
            current = point
            index += 1
            continue
        if element.isLineTo():
            if point != current:
                pieces.append((current, point - current, point - current, point))
            current = point
            index += 1
            continue
        c1 = point
        c2 = QPointF(path.elementAt(index + 1).x, path.elementAt(index + 1).y)
        end = QPointF(path.elementAt(index + 2).x, path.elementAt(index + 2).y)
        if end != current:
            start_dir = c1 - current if c1 != current else c2 - current
            end_dir = end - c2 if end != c2 else end - c1
            pieces.append((current, start_dir, end_dir, end))
        current = end
        index += 3
    return pieces


def _angle(a: QPointF, b: QPointF) -> float:
    """두 방향 사이 각(도)."""
    dot = a.x() * b.x() + a.y() * b.y()
    norm = math.hypot(a.x(), a.y()) * math.hypot(b.x(), b.y())
    return math.degrees(math.acos(max(-1.0, min(1.0, dot / norm))))


@pytest.mark.parametrize("side", GRIDS)
@pytest.mark.parametrize("opened", (True, False))
def test_folder_outline_has_no_sharp_corner(qapp: QApplication, opened: bool, side: int) -> None:
    """폴더 윤곽은 이어지는 두 조각의 방향이 같아야 한다 — 뾰족하게 꺾이는 꼭짓점이 없다.

    열린 · 닫힌 폴더, 칸 16 · 20 -> 앞 조각의 끝 방향과 다음 조각의 시작 방향 사이 각 < 1°
    (닫힌 폴더는 끝 조각 → 첫 조각 이음도 잰다)
    """
    pieces = _segments(icons._folder_path(float(side), opened))
    pairs = list(zip(pieces, pieces[1:]))
    if not opened:
        pairs.append((pieces[-1], pieces[0]))

    for before, after in pairs:
        assert _angle(before[2], after[1]) < 1.0, (
            f"opened={opened} 칸 {side}: {before[3].x():.2f},{before[3].y():.2f}에서 꺾인다"
        )


@pytest.mark.parametrize("side", GRIDS)
@pytest.mark.parametrize(("opened", "corners"), ((True, OPEN_CORNERS), (False, CLOSED_CORNERS)))
def test_folder_corners_are_rounded_by_the_corner_radius(
    qapp: QApplication, opened: bool, corners: int, side: int
) -> None:
    """폴더 꼭짓점마다 곡선이 하나 있고, 곡선 반경이 칸의 0.05 ~ 0.10이며 가장 큰 것이 0.10이어야 한다.

    열린 폴더 꼭짓점 8 · 닫힌 폴더 6, 칸 16 · 20 -> 곡선 수 == 꼭짓점 수, 곡선 시작점 ↔ 꼭짓점 거리 / 칸이
    [0.05, 0.10], 최댓값 == 0.10
    """
    path = icons._folder_path(float(side), opened)
    legs = []
    for index in range(path.elementCount()):
        element = path.elementAt(index)
        if element.isCurveTo():
            start = path.elementAt(index - 1)
            # 2차 곡선을 3차로 바꾼 첫 조절점 c1 = 시작 + 2/3(꼭짓점 - 시작) → 꼭짓점 거리 = 1.5 |c1 - 시작|
            legs.append(1.5 * math.hypot(element.x - start.x, element.y - start.y) / side)

    assert len(legs) == corners
    assert min(legs) >= CORNER_MIN - 1e-6, legs
    assert max(legs) == pytest.approx(CORNER), legs


@pytest.mark.parametrize("opened", (True, False))
def test_only_the_open_folder_has_line_ends(qapp: QApplication, opened: bool) -> None:
    """열린 폴더는 시작점과 끝 점이 다른 열린 선이고 닫힌 폴더는 닫힌 선이어야 한다.

    칸 16 -> 열린 폴더: 첫 점 != 끝 점, 닫힌 폴더: 첫 점 == 끝 점
    """
    path = icons._folder_path(16.0, opened)
    first = path.elementAt(0)
    last = path.elementAt(path.elementCount() - 1)

    assert ((first.x, first.y) != (last.x, last.y)) is opened


@pytest.mark.parametrize("dpr", (1.0, 1.5))
@pytest.mark.parametrize("side", GRIDS)
@pytest.mark.parametrize("name", ("folder", "folder_outline"))
def test_folder_icon_is_not_clipped_at_100_and_150_percent(
    qapp: QApplication, name: str, side: int, dpr: float
) -> None:
    """폴더 픽스맵은 배율 100% · 150%에서 바깥 테두리 한 줄 화소가 모두 비어 있어야 한다 — 잘리지 않는다.

    열린 · 닫힌 폴더, 칸 16 · 20, 배율 1.0 · 1.5 -> 그림 둘레 1px 화소의 불투명도 == 0
    """
    image = icons.action_pixmap(name, "#ffffff", side, dpr).toImage()
    width, height = image.width(), image.height()
    edge = [(x, 0) for x in range(width)] + [(x, height - 1) for x in range(width)]
    edge += [(0, y) for y in range(height)] + [(width - 1, y) for y in range(height)]

    assert [(x, y) for x, y in edge if image.pixelColor(x, y).alpha() > 0] == []
