"""셸 S0 뼈대 게이트 (#492) — 네 칸 차례 · 창 제목 · 메뉴바 「설정」 · 창 최소 크기 · 등록 배선.

- 네 칸은 위에서 아래로 메뉴바 · 헤더바 · 목록 · 하단바이고 서로 겹치지 않는다
- 창 제목은 표시 이름 + 버전이다(정식 · rc는 그대로, 개발 버전은 `+dev`까지). 번역하지 않는다
- 메뉴바 「설정」은 설정 창을 띄우고 고른 메뉴는 「VOD 다운」에 남는다
- 창 최소 크기에서 모든 버튼이 창 안에 다 보이고 목록에 카드 한 장이 다 보인다.
  최소 크기는 카드를 넣고 빼도 같다
- URL 칸 Enter · 목록에 끌어다 놓기가 등록(조회)까지 실제 배선으로 간다

폭 · 높이는 절대 px로 박지 않는다 — 창의 실제 최소 크기에서 유도한다.
"""

import pytest
from PySide6.QtCore import QMimeData, QObject, QPoint, QRect, Qt, QTranslator, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractButton, QApplication

import CVDv2 as main_module
import app.theme as theme
import app.viewmodels.content_viewmodel as content_viewmodel_module
import app.views.mainWindow as main_window_module
import config.config as config
from app.views.mainWindow import VodDownloader, window_title
from app.viewmodels.data import ContentItem
from core.models.download_state import DownloadState
from tests.app.gui.card_helpers import drop_new_top_levels, hold_style, snapshot_top_levels


@pytest.fixture(autouse=True)
def _apply_production_qss(qapp):
    """실제 전역 QSS · 스타일을 태운다(폭 · 높이는 QSS 여백 · 글꼴에 좌우된다). ⚠️ function scope 유지."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))  # 참조 보관 — 이중 해제 우회 (#243)
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))


@pytest.fixture(autouse=True)
def _destroy_windows():
    """테스트가 만든 최상위 창은 close()가 아니라 파괴한다 — close()는 창 크기를 기억한다(#253)."""
    before = snapshot_top_levels()
    yield
    drop_new_top_levels(before)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """카드의 썸네일 · 크기 조회가 실호출로 새지 않게 세션을 막는다 — 호출되면 즉시 실패한다."""

    class _FailingSession:
        def head(self, *a, **k):
            """HEAD 요청은 금지 — 호출 자체가 테스트 실패다."""
            raise RuntimeError("network disabled in tests")

        def get(self, *a, **k):
            """GET 요청은 금지 — 호출 자체가 테스트 실패다."""
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("app.widgets.widget.get_thread_session", lambda: _FailingSession())


def _make_item(i: int) -> ContentItem:
    """대기 카드용 아이템 — 제목 · 채널만 다르고 해상도 목록은 비어 있다."""
    return ContentItem(
        f"https://chzzk.naver.com/video/{i}",
        {"title": f"제목 {i}", "category": "", "channelName": f"채널{i}", "createdDate": "", "duration": 3600},
        [],
        None,
        "",
        "C:/dl",
        "video",
        None,
    )


def _add_cards(win: VodDownloader, start: int, count: int) -> list[ContentItem]:
    """대기 카드 `count`장을 모델 경로로 넣는다(실배선). 넣은 아이템을 돌려준다."""
    items = []
    for i in range(start, start + count):
        item = _make_item(i)
        item.downloadState = DownloadState.WAITING
        win.contentManager.model.addItem(item)
        items.append(item)
    QApplication.processEvents()
    return items


class _IdleFetchJob(QObject):
    """아무것도 하지 않는 조회 작업 대역 — 결과 · 오류를 내지 않아 카드가 조회 중으로 남는다."""

    finished = Signal(object, str)
    error = Signal(str)

    def __init__(self, *args) -> None:
        super().__init__()

    def run(self) -> None:
        """조회하지 않는다(네트워크 없음)."""


def _window() -> VodDownloader:
    win = VodDownloader()
    QApplication.processEvents()
    return win


def _in_window(win, w) -> QRect:
    """위젯의 창 좌표계 사각형."""
    return QRect(w.mapTo(win, QPoint(0, 0)), w.size())


def _at_size(win: VodDownloader, width: int, height: int) -> None:
    """창을 그 크기로 놓고 실제 크기가 요청과 같은지 단언한다 — resize()는 요청이지 결과가 아니다."""
    win.resize(width, height)
    win.show()
    QApplication.processEvents()
    QApplication.processEvents()
    assert (win.width(), win.height()) == (width, height), (
        f"요청 {width}×{height}인데 실제 {win.width()}×{win.height()} — 최소 크기"
        f"({win.minimumWidth()}×{win.minimumHeight()})에 클램프됐다"
    )


def _zones(win) -> list:
    return [win.appMenuBar, win.headerFrame, win.listView, win.infoFrame]


class TestFourZones:
    @pytest.mark.parametrize("extra", (0, 400))
    def test_menu_header_list_footer_run_top_to_bottom_without_overlap(self, extra):
        """창 최소 크기와 그보다 넓고 높은 크기에서 네 칸은 보이고 메뉴바 · 헤더바 · 목록 · 하단바 차례로 겹침 없이 놓여야 한다."""
        win = _window()
        _at_size(win, win.minimumWidth() + extra, win.minimumHeight() + extra)
        rects = [_in_window(win, z) for z in _zones(win)]
        assert all(z.isVisible() for z in _zones(win))
        for upper, lower in zip(rects, rects[1:]):
            assert upper.bottom() < lower.top(), f"칸이 겹치거나 차례가 다르다: {upper} / {lower}"

    def test_the_menu_holds_vod_download_then_settings(self):
        """메뉴바에는 왼쪽부터 「VOD 다운」 · 「설정」이 있고 「VOD 다운」이 고른 상태여야 한다."""
        win = _window()
        win.show()
        QApplication.processEvents()
        download, settings_button = win.menuDownloadButton, win.menuSettingsButton
        assert (download.text(), settings_button.text()) == ("VOD Download", "Settings")
        assert download.x() < settings_button.x()
        assert download.isChecked()

    def test_the_footer_ends_with_download_then_stop(self):
        """하단바의 마지막 두 위젯은 다운로드 · 중지 차례여야 한다(오른쪽 끝)."""
        win = _window()
        layout = win.infoLayout
        last_two = [layout.itemAt(i).widget() for i in range(layout.count() - 2, layout.count())]
        assert last_two == [win.downloadButton, win.stopButton]


class TestWindowTitle:
    @pytest.mark.parametrize(
        ("version", "expected"),
        (
            ("2.12.0", "Chzzk VOD Downloader 2.12.0"),
            ("2.12.0-rc1", "Chzzk VOD Downloader 2.12.0-rc1"),
            ("2.12.0+dev.4-g1a2b3c4-dirty", "Chzzk VOD Downloader 2.12.0+dev"),
            ("2.12.0+dev", "Chzzk VOD Downloader 2.12.0+dev"),
        ),
    )
    def test_title_is_the_name_and_the_version_without_the_commit(self, version, expected):
        """버전 문자열이 정식 · rc · 개발(커밋 있음 · 없음)이면 창 제목은 표시 이름 + 버전(커밋 뺌)이어야 한다."""
        assert window_title(version) == expected

    @pytest.mark.parametrize("version", ("2.12.0", "2.12.0-rc1", "2.12.0+dev.7-gabcdef0"))
    def test_window_shows_the_title_from_get_app_version(self, monkeypatch, version):
        """`config.get_app_version()`이 그 버전을 내면 창 제목은 window_title(그 버전)이어야 한다."""
        monkeypatch.setattr(config, "get_app_version", lambda: version)
        win = _window()
        assert win.windowTitle() == window_title(version)

    def test_title_is_not_translated(self, qapp, monkeypatch):
        """한국어 번역을 올려도 창 제목은 영어 표시 이름 그대로여야 한다."""
        monkeypatch.setattr(config, "get_app_version", lambda: "2.12.0")
        translator = QTranslator()
        assert translator.load(main_module.resource_path("translations/ko_KR.qm"))
        qapp.installTranslator(translator)
        try:
            win = _window()
            assert win.menuSettingsButton.text() == "설정", "전제: 한국어 번역이 적용돼야 한다"
            assert win.windowTitle() == "Chzzk VOD Downloader 2.12.0"
        finally:
            qapp.removeTranslator(translator)


class TestMenuSettings:
    def test_settings_menu_opens_the_settings_dialog_and_keeps_vod_download_chosen(self, monkeypatch):
        """메뉴 「설정」을 누르면 설정 창이 한 번 열리고 고른 메뉴는 「VOD 다운」 그대로여야 한다."""
        opened = []
        monkeypatch.setattr(
            main_window_module.SettingDialog, "exec_", lambda self: opened.append(self) or 0
        )
        win = _window()
        win.menuSettingsButton.click()
        QApplication.processEvents()
        assert len(opened) == 1 and isinstance(opened[0], main_window_module.SettingDialog)
        assert win.menuDownloadButton.isChecked()
        assert not win.menuSettingsButton.isCheckable()

    def test_pressing_vod_download_again_keeps_it_chosen(self):
        """고른 「VOD 다운」을 다시 눌러도 고른 상태가 풀리지 않아야 한다."""
        win = _window()
        win.menuDownloadButton.click()
        QApplication.processEvents()
        assert win.menuDownloadButton.isChecked()


class TestMinimumSize:
    def test_at_the_minimum_every_button_is_whole_inside_the_window(self):
        """창 최소 크기에서 보이는 모든 버튼은 크기가 0이 아니고 창 사각형 안에 다 들어가야 한다."""
        win = _window()
        _at_size(win, win.minimumWidth(), win.minimumHeight())
        window_rect = QRect(QPoint(0, 0), win.size())
        buttons = [b for b in win.findChildren(QAbstractButton) if b.isVisible() and b.window() is win]
        names = {b.objectName() for b in buttons}
        assert {"menuDownloadButton", "menuSettingsButton", "fetchButton", "downloadPathButton",
                "clearFinishedButton", "downloadButton", "stopButton"} <= names
        cut = [(b.objectName(), _in_window(win, b)) for b in buttons
               if b.width() <= 0 or b.height() <= 0 or not window_rect.contains(_in_window(win, b))]
        assert not cut, f"창 최소 크기 {win.size().toTuple()}에서 잘리거나 밖으로 나간 버튼: {cut}"

    def test_at_the_minimum_one_card_is_fully_visible_in_the_list(self):
        """창 최소 크기에서 카드 한 장이 목록 뷰포트 안에 위아래까지 다 보여야 한다."""
        win = _window()
        (item,) = _add_cards(win, 0, 1)
        _at_size(win, win.minimumWidth(), win.minimumHeight())
        card = win.listView.widgetFor(item)
        viewport = win.listView.viewport()
        rect = QRect(card.mapTo(viewport, QPoint(0, 0)), card.size())
        assert card.isVisible()
        assert viewport.rect().contains(rect), f"카드 {rect}가 목록 뷰포트 {viewport.rect()}에 다 들어가지 않는다"

    def test_at_the_minimum_the_tallest_card_fits_without_the_bottom_padding(self):
        """창 최소 크기에서 해상도 pill이 있는 대기 카드가 목록 아래 안쪽 여백을 뺀 자리 안에 다 보여야 한다.

        pill이 있는 대기 카드가 평소 카드 가운데 가장 높다(상태 글 최소 높이 = pill 높이, #245 — 다른
        상태도 같은 높이). 아래 안쪽 여백을 빼고 재므로 견본 카드가 그만큼 낮게 재어져 여백 덕에
        들어가는 경우를 잡는다(#493 리뷰).
        """
        win = _window()
        item = ContentItem(
            "https://chzzk.naver.com/video/0",
            {"title": "제목 0", "category": "", "channelName": "채널0", "createdDate": "", "duration": 3600},
            [[r, f"https://v.invalid/{r}.mp4"] for r in (1080, 720, 480)],  # 해상도 셋 — pill 줄
            None,
            "",
            "C:/dl",
            "video",
            None,
        )
        item.downloadState = DownloadState.WAITING
        win.contentManager.model.addItem(item)
        QApplication.processEvents()
        _at_size(win, win.minimumWidth(), win.minimumHeight())
        card = win.listView.widgetFor(item)
        assert card.buttons and any(b.isVisible() for b in card.buttons), "전제: 해상도 pill이 보여야 한다"
        viewport = win.listView.viewport()
        bottom_padding = win.listView.widget().layout().contentsMargins().bottom()
        room = QRect(0, 0, viewport.width(), viewport.height() - bottom_padding)
        rect = QRect(card.mapTo(viewport, QPoint(0, 0)), card.size())
        assert room.contains(rect), (
            f"카드 {rect}가 아래 안쪽 여백({bottom_padding}px)을 뺀 목록 자리 {room}에 다 들어가지 않는다"
        )

    def test_minimum_size_is_the_same_with_0_1_and_50_cards(self, monkeypatch, tmp_path):
        """카드 0장 · 1장 · 50장일 때 창에 걸린 최소 크기와 최소 크기 계산값이 모두 처음과 같아야 한다.

        1장째는 실제 등록 길(URL 칸 Enter → 조회 중 카드)로 넣어 등록 때 창이 받는 신호까지 타고,
        나머지 49장은 모델에 바로 넣는다. 조회 작업은 아무것도 하지 않는 대역이라 카드는 조회 중으로 남는다.
        창에 걸린 값은 시작 때 한 번 정해지므로, 계산(`_contentMinimumWidth` · `_contentMinimumHeight`)이
        카드를 읽게 바뀌어도 드러나도록 카드 수마다 계산을 다시 불러 비교한다(#493 리뷰).
        """
        monkeypatch.setattr(content_viewmodel_module, "FetchJob", _IdleFetchJob)
        win = _window()
        win.downloadPathInput.setText(str(tmp_path))
        win.show()
        QApplication.processEvents()

        def measured() -> tuple:
            """(창에 걸린 최소 폭 · 높이, 다시 계산한 최소 폭 · 높이)."""
            return win.minimumSize().toTuple(), (win._contentMinimumWidth(), win._contentMinimumHeight())

        sizes = {0: measured()}
        win.urlInput.setText("https://chzzk.naver.com/video/1")
        QTest.keyClick(win.urlInput, Qt.Key.Key_Return)
        QApplication.processEvents()
        assert win.contentManager.model.rowCount() == 1, "전제: 등록 길로 카드가 한 장 생겨야 한다"
        sizes[1] = measured()
        _add_cards(win, 1, 49)
        sizes[50] = measured()
        assert len(set(sizes.values())) == 1, f"카드 수에 따라 최소 크기가 바뀐다: {sizes}"
        assert sizes[0][0] == sizes[0][1], f"창에 걸린 최소 크기가 계산값과 다르다: {sizes[0]}"


class TestRegistrationWiring:
    @pytest.fixture
    def fetches(self, monkeypatch, tmp_path):
        """조회 요청을 기록한다 — 창의 콘텐츠 뷰모델 `fetchContent`를 대역으로 바꾼다(네트워크 없음)."""
        calls = []
        win = _window()
        win.downloadPathInput.setText(str(tmp_path))
        monkeypatch.setattr(
            win.contentManager, "fetchContent", lambda url, cookies, path: calls.append((url, path))
        )
        return win, calls, str(tmp_path)

    def test_enter_in_the_url_field_registers_the_url(self, fetches):
        """URL 칸에 주소를 넣고 Enter를 누르면 그 주소로 조회가 한 번 요청돼야 한다."""
        win, calls, path = fetches
        url = "https://chzzk.naver.com/video/1"
        win.urlInput.setText(url)
        QTest.keyClick(win.urlInput, Qt.Key.Key_Return)
        QApplication.processEvents()
        assert calls == [(url, path)]

    def test_dropping_a_url_on_the_list_registers_it(self, fetches):
        """목록 뷰포트에 주소 글을 끌어다 놓으면 그 주소로 조회가 한 번 요청돼야 한다."""
        win, calls, path = fetches
        url = "https://chzzk.naver.com/video/2"
        mime = QMimeData()
        mime.setText(url)
        viewport = win.listView.viewport()
        at = QPoint(viewport.width() // 2, viewport.height() // 2)
        # 실제 끌기처럼 들어오기부터 — Qt는 들어오기를 받아들인 위젯에만 놓기를 넘긴다
        enter = QDragEnterEvent(
            at, Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
        )
        QApplication.sendEvent(viewport, enter)
        event = QDropEvent(
            at.toPointF(),
            Qt.DropAction.CopyAction,
            mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(viewport, event)
        QApplication.processEvents()
        assert calls == [(url, path)]
