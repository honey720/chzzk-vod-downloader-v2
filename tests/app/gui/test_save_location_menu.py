"""헤더바 저장 위치 버튼 · 메뉴 게이트 (#495 — 셸 S1, 설계 「저장 위치 버튼」).

메뉴는 실제로 열어(`popup` → `aboutToShow`) 채우고, 항목은 `trigger()`로 누른다. 폴더 선택 창 ·
별칭 입력 창만 바꿔 끼운다. 설정은 `tests/conftest.py`가 임시 폴더로 돌린 DB에 직접 써서 시작한다.
"""

from collections.abc import Callable, Iterator

import pytest
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog

import app.system.settings as settings
import app.viewmodels.content_viewmodel as content_viewmodel_module
from app.views.mainWindow import VodDownloader
from app.widgets.header_controls import ChoiceMenu
from app.widgets.widget import abbreviate_path


class _IdleFetchJob:
    """아무것도 하지 않는 조회 작업 — 카드는 조회 중으로 남는다(네트워크 없음)."""

    def __init__(self, *args) -> None:
        from PySide6.QtCore import QObject, Signal

        class _Signals(QObject):
            finished = Signal(object, str)
            error = Signal(str)

        self._signals = _Signals()
        self.finished = self._signals.finished
        self.error = self._signals.error

    def run(self) -> None:
        pass


@pytest.fixture
def folders(tmp_path) -> Callable[[str], str]:
    """이름 붙인 실존 폴더를 만든다 — `folders("가")` → 경로 문자열."""

    def make(name: str) -> str:
        path = tmp_path / name
        path.mkdir(exist_ok=True)
        return str(path)

    return make


def _seed(**values) -> None:
    """창을 띄우기 전 설정 DB에 값을 쓴다."""
    settings.update_settings(values)


@pytest.fixture
def make_window(qapp) -> Iterator[Callable[[], VodDownloader]]:
    made = []

    def make() -> VodDownloader:
        win = VodDownloader()
        made.append(win)
        return win

    yield make
    for win in made:
        win.saveLocationButton.menuWidget.hide()
        win.hide()
        win.deleteLater()
    QApplication.processEvents()


def _open(win) -> ChoiceMenu:
    """저장 위치 메뉴를 실제로 연다 — 열 때 채운다(aboutToShow)."""
    button = win.saveLocationButton
    menu = button.menuWidget
    menu.popup(button.mapToGlobal(button.rect().bottomLeft()))
    QApplication.processEvents()
    return menu


def _rows(menu) -> list[tuple]:
    """메뉴의 줄 — (종류, 글 또는 키). 머리는 ("header", 글), 구분선은 ("-",), 명령은 ("command", objectName)."""
    rows = []
    for action in menu.actions():
        if action.isSeparator():
            rows.append(("-",))
        elif action.property("menuHeader"):
            rows.append(("header", action.text()))
        elif action.data() is not None:
            rows.append(tuple(action.data()))
        else:
            rows.append(("command", action.objectName()))
    return rows


def _choice(menu, kind: str, path: str) -> QAction:
    return next(a for a in menu.choices() if tuple(a.data()) == (kind, path))


def _command(menu, name: str) -> QAction:
    return next(a for a in menu.actions() if a.objectName() == name)


class TestMenuRows:
    def test_rows_are_current_favorites_recent_then_commands(self, make_window, folders) -> None:
        """즐겨찾기 둘 · 최근 둘이 있으면 메뉴는 지금 위치 · 즐겨찾기 머리와 둘 · 최근 머리와 둘 · 구분선 · 명령 셋 차례여야 한다."""
        a, b, r1, r2 = folders("즐1"), folders("즐2"), folders("최1"), folders("최2")
        _seed(
            favoritePaths=[{"name": "하나", "path": a}, {"name": "둘", "path": b}],
            recentPaths=[r1, r2],
        )
        win = make_window()
        current = win.saveLocations.current()

        rows = _rows(_open(win))

        assert rows == [
            ("current", current),
            ("header", "Favorites"),
            ("favorite", a),
            ("favorite", b),
            ("header", "Recent"),
            ("recent", r1),
            ("recent", r2),
            ("-",),
            ("command", "addFavoriteAction"),
            ("command", "findFolderAction"),
            ("command", "defaultLocationAction"),
        ]

    def test_without_favorites_or_recent_there_are_no_headers(self, make_window) -> None:
        """즐겨찾기 · 최근이 없으면 메뉴는 지금 위치 · 구분선 · 명령 셋이어야 한다(빈 머리 없음)."""
        win = make_window()
        rows = _rows(_open(win))
        assert [r[0] for r in rows] == ["current", "-", "command", "command", "command"]

    def test_only_the_current_location_is_checked(self, make_window, folders) -> None:
        """즐겨찾기 · 최근이 있어도 ✓는 지금 위치 줄 하나에만 있어야 한다."""
        a, r1 = folders("즐1"), folders("최1")
        _seed(favoritePaths=[{"name": "하나", "path": a}], recentPaths=[r1])
        win = make_window()
        menu = _open(win)
        assert menu.markedKeys() == [("current", win.saveLocations.current())]

    def test_a_favorite_row_shows_its_name_and_its_path_after_a_tab(
        self, make_window, folders
    ) -> None:
        """즐겨찾기 줄의 글은 「★ 별칭」 다음 탭 뒤에 카드 표기 경로(abbreviate_path)여야 한다(탭 뒤는 흐리게 그린다)."""
        a = folders("즐1")
        _seed(favoritePaths=[{"name": "녹화", "path": a}])
        win = make_window()
        action = _choice(_open(win), "favorite", a)
        assert action.text() == f"★ 녹화\t{abbreviate_path(a)}"


class TestLimitsAndOverlap:
    def test_recent_shows_at_most_three(self, make_window, folders) -> None:
        """최근이 다섯 저장돼 있으면 메뉴 「최근」에는 앞 셋만 저장 차례대로 보여야 한다."""
        recent = [folders(f"최{n}") for n in range(5)]
        _seed(recentPaths=recent)
        win = make_window()
        rows = _rows(_open(win))
        assert [r[1] for r in rows if r[0] == "recent"] == recent[:3]

    def test_recent_leaves_out_the_current_location_and_favorites(
        self, make_window, folders
    ) -> None:
        """최근에 지금 위치 · 즐겨찾기 경로가 섞여 있으면 메뉴 「최근」에서 빠지고 그 뒤 것이 채워져야 한다."""
        fav, r1, r2, r3 = folders("즐"), folders("최1"), folders("최2"), folders("최3")
        _seed(favoritePaths=[{"name": "즐", "path": fav}], recentPaths=[fav, r1, r2, r3])
        win = make_window()
        assert win.saveLocations.choose(r1)  # 지금 위치 = r1(최근 맨 앞으로 올라간다)

        rows = _rows(_open(win))

        assert [r[1] for r in rows if r[0] == "recent"] == [r2, r3]

    def test_adding_a_favorite_is_off_when_the_list_is_full(self, make_window, folders) -> None:
        """즐겨찾기가 10개이면 「지금 위치를 즐겨찾기에」가 꺼져 있어야 한다."""
        favorites = [
            {"name": f"즐{n}", "path": folders(f"즐{n}")} for n in range(settings.FAVORITES_MAX)
        ]
        _seed(favoritePaths=favorites)
        win = make_window()
        menu = _open(win)
        assert (
            len([a for a in menu.choices() if a.data()[0] == "favorite"]) == settings.FAVORITES_MAX
        )
        assert not _command(menu, "addFavoriteAction").isEnabled()

    def test_adding_a_favorite_is_off_when_the_current_location_is_one(
        self, make_window, folders
    ) -> None:
        """지금 위치가 이미 즐겨찾기이면 「지금 위치를 즐겨찾기에」가 꺼져 있어야 한다."""
        fav = folders("즐")
        _seed(favoritePaths=[{"name": "즐", "path": fav}])
        win = make_window()
        assert win.saveLocations.choose(fav)
        assert not _command(_open(win), "addFavoriteAction").isEnabled()


class TestMissingFolders:
    @pytest.fixture
    def menu_with_missing(
        self, make_window, folders, tmp_path
    ) -> tuple[VodDownloader, ChoiceMenu, str, str]:
        fav, recent = folders("사라질 즐겨찾기"), folders("사라질 최근")
        _seed(favoritePaths=[{"name": "외장", "path": fav}], recentPaths=[recent])
        win = make_window()
        (tmp_path / "사라질 즐겨찾기").rmdir()
        (tmp_path / "사라질 최근").rmdir()
        return win, _open(win), fav, recent

    def test_missing_rows_are_marked_and_disabled(self, menu_with_missing) -> None:
        """폴더가 없어진 즐겨찾기 · 최근 줄은 「(missing)」이 붙고 꺼져 있어야 한다."""
        _win, menu, fav, recent = menu_with_missing
        for kind, path in (("favorite", fav), ("recent", recent)):
            action = _choice(menu, kind, path)
            assert "(missing)" in action.text()
            assert action.property("missing") is True
            assert not action.isEnabled()

    def test_a_missing_row_cannot_be_chosen(self, menu_with_missing) -> None:
        """꺼진 줄을 눌러도 지금 위치가 바뀌지 않아야 한다."""
        win, menu, fav, _recent = menu_with_missing
        before = win.saveLocations.current()
        _choice(menu, "favorite", fav).trigger()
        assert win.saveLocations.current() == before


class TestChoosing:
    def test_choosing_a_recent_row_makes_it_current(self, make_window, folders) -> None:
        """메뉴 「최근」 줄을 누르면 그 경로가 지금 위치 · 버튼 툴팁이 되고 버튼 글이 폴더 이름이어야 한다."""
        r1 = folders("최근 폴더")
        _seed(recentPaths=[r1])
        win = make_window()
        win.resize(win.minimumWidth() + 900, win.height())  # 저장 위치 버튼이 글 모양인 폭(#529)
        win.show()
        QApplication.processEvents()
        assert not win.saveLocationButton.isCompact(), "전제: 글 모양"
        _choice(_open(win), "recent", r1).trigger()
        assert win.saveLocations.current() == r1
        assert win.saveLocationButton.toolTip() == r1
        assert win.saveLocationButton.fullText() == "최근 폴더"

    def test_a_favorite_location_shows_its_name_on_the_button(self, make_window, folders) -> None:
        """즐겨찾기 줄을 누르면 버튼 글이 폴더 이름이 아니라 별칭이어야 한다."""
        fav = folders("D_녹화")
        _seed(favoritePaths=[{"name": "녹화함", "path": fav}])
        win = make_window()
        _choice(_open(win), "favorite", fav).trigger()
        assert win.saveLocationButton.fullText() == "녹화함"

    def test_default_location_goes_back_to_the_start(self, make_window, folders) -> None:
        """다른 위치를 고른 뒤 「기본 위치로」를 누르면 지금 위치가 시작 위치로 돌아와야 한다."""
        win = make_window()
        start = win.saveLocations.current()
        assert win.saveLocations.choose(folders("다른"))
        _command(_open(win), "defaultLocationAction").trigger()
        assert win.saveLocations.current() == start

    def test_adding_the_current_location_to_favorites_saves_it(
        self, make_window, folders, monkeypatch
    ) -> None:
        """「지금 위치를 즐겨찾기에」에서 별칭을 넣으면 favoritePaths에 저장되고 최근에서 빠져야 한다."""
        chosen = folders("자주 쓰는 폴더")
        win = make_window()
        assert win.saveLocations.choose(chosen)
        asked = []

        def fake_get_text(parent, title, label, text="") -> tuple[str, bool]:
            asked.append(text)
            return "자주", True

        monkeypatch.setattr(QInputDialog, "getText", staticmethod(fake_get_text))
        _command(_open(win), "addFavoriteAction").trigger()

        assert asked == ["자주 쓰는 폴더"]  # 기본값 = 폴더 이름
        saved = settings.load_settings()
        assert saved["favoritePaths"] == [{"name": "자주", "path": chosen}]
        assert chosen not in saved["recentPaths"]
        assert win.saveLocationButton.fullText() == "자주"

    def test_a_duplicate_favorite_name_is_refused_with_a_reason(
        self, make_window, folders, monkeypatch
    ) -> None:
        """이미 있는 별칭으로 더하면 저장하지 않고 까닭을 알려야 한다."""
        fav, other = folders("즐"), folders("다른")
        _seed(favoritePaths=[{"name": "같은 이름", "path": fav}])
        win = make_window()
        errors = []
        win.saveLocationButton.errorOccurred.disconnect()
        win.saveLocationButton.errorOccurred.connect(errors.append)
        assert win.saveLocations.choose(other)
        monkeypatch.setattr(
            QInputDialog, "getText", staticmethod(lambda *a, **k: ("같은 이름", True))
        )

        _command(_open(win), "addFavoriteAction").trigger()

        assert errors == ["A favorite with this name already exists."]
        assert settings.load_settings()["favoritePaths"] == [{"name": "같은 이름", "path": fav}]


class TestChosenLocationAppliesToLaterCards:
    def test_cards_registered_before_keep_their_location(
        self, make_window, folders, monkeypatch
    ) -> None:
        """위치 A에서 카드 하나를 등록하고 B를 고른 뒤 또 등록하면 앞 카드는 A, 뒤 카드는 B여야 한다."""
        monkeypatch.setattr(content_viewmodel_module, "FetchJob", _IdleFetchJob)
        first, second = folders("첫 위치"), folders("둘째 위치")
        win = make_window()
        assert win.saveLocations.choose(first)
        win.urlInput.setText("https://chzzk.naver.com/video/1")
        win.fetchButton.click()

        monkeypatch.setattr(
            QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: second)
        )
        _command(_open(win), "findFolderAction").trigger()
        win.urlInput.setText("https://chzzk.naver.com/video/2")
        win.fetchButton.click()

        model = win.contentManager.model
        assert [model.itemAt(n).download_path for n in range(model.rowCount())] == [first, second]


class TestRowsAndLimits:
    """inbox 71 — 줄 글 · 툴팁 · 메뉴 폭 상한 · 끄는 조건(#495)."""

    def test_the_current_row_is_its_folder_name_and_a_muted_path(
        self, make_window, folders
    ) -> None:
        """지금 위치 줄은 폴더 이름 다음 탭 뒤에 카드 표기 경로여야 하고 툴팁은 전체 경로여야 한다."""
        chosen = folders("지금 폴더")
        win = make_window()
        assert win.saveLocations.choose(chosen)
        action = _choice(_open(win), "current", chosen)
        assert action.text() == f"지금 폴더\t{abbreviate_path(chosen)}"
        assert action.toolTip() == chosen

    def test_a_current_favorite_row_shows_its_name(self, make_window, folders) -> None:
        """지금 위치가 즐겨찾기면 지금 위치 줄의 이름은 폴더 이름이 아니라 별칭이어야 한다."""
        fav = folders("D_녹화")
        _seed(favoritePaths=[{"name": "녹화함", "path": fav}])
        win = make_window()
        assert win.saveLocations.choose(fav)
        assert _choice(_open(win), "current", fav).text().startswith("녹화함\t")

    def test_paths_use_the_card_notation_with_a_home_tilde(
        self, make_window, folders, tmp_path, monkeypatch
    ) -> None:
        """홈 아래 경로의 흐린 글은 ~로 시작하는 카드 표기여야 하고 툴팁은 전체 경로여야 한다.

        홈(HOME · USERPROFILE) = tmp_path, 최근 = tmp_path/최근
        -> 흐린 글 "~/…", 툴팁 = 전체 경로
        """
        import os

        monkeypatch.setenv("HOME", str(tmp_path))  # 임시 폴더는 OS마다 홈 밖일 수 있어 홈을 정한다
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        recent = folders("최근")
        assert os.path.normcase(recent).startswith(os.path.normcase(os.path.expanduser("~"))), (
            "전제: 시험 폴더가 홈 아래"
        )
        _seed(recentPaths=[recent])
        win = make_window()
        action = _choice(_open(win), "recent", recent)
        assert action.text().split("\t")[1].startswith("~/")
        assert action.toolTip() == recent

    def test_a_long_path_keeps_the_menu_within_the_window(self, make_window, tmp_path) -> None:
        """마지막 폴더 이름이 창보다 긴 경로를 고르면 메뉴 폭이 창 폭 − 24 이하여야 한다."""
        # 카드 표기로 줄여도 마지막 폴더는 남아 창보다 넓은 이름 — 글자당 폭이 넓은 「W」(1바이트)로
        # 리눅스 · macOS 이름 한도(255바이트)와 Windows 경로 한도(260자) 안에서 길게 짓는다
        deep = tmp_path / ("W" * min(150, 250 - len(str(tmp_path))))
        deep.mkdir()
        win = make_window()
        win.resize(win.minimumWidth(), win.height())
        win.show()
        QApplication.processEvents()
        assert win.saveLocations.choose(str(deep))
        menu = _open(win)
        metrics = menu.fontMetrics()
        assert metrics.horizontalAdvance(abbreviate_path(str(deep))) > win.width(), (
            "전제: 상한이 없으면 창을 넘는 글"
        )
        assert menu.isVisible()
        assert menu.width() <= win.width() - 24
        # 창이 offscreen 화면(800px)만큼 넓은 폰트에서는 Qt가 메뉴를 화면 폭으로도 자른다 — 상한이 없어도
        # 위 단언이 통과하므로, 메뉴에 걸린 최대폭도 잰다
        assert menu.maximumWidth() <= win.width() - 24

    def test_muted_paths_stay_inside_a_capped_menu(self, make_window, folders, tmp_path) -> None:
        """메뉴 폭이 상한에 걸려도 다른 줄(최근)의 흐린 경로가 보이는 메뉴 오른쪽 안쪽에 그려져야 한다."""
        from PySide6.QtCore import QRect

        deep = tmp_path / (
            "W" * min(150, 250 - len(str(tmp_path)))
        )  # 창보다 넓은 이름 — 위 테스트와 같은 한도
        deep.mkdir()
        recent = folders("짧은 최근")
        _seed(recentPaths=[recent])
        win = make_window()
        win.resize(win.minimumWidth(), win.height())
        win.show()
        QApplication.processEvents()
        assert win.saveLocations.choose(str(deep))
        menu = _open(win)
        row = _choice(menu, "recent", recent)
        image = menu.grab().toImage()
        ratio = image.devicePixelRatio()
        g = menu.actionGeometry(row)
        zone = QRect(menu.width() * 2 // 3, g.top() + 2, menu.width() // 3 - 12, g.height() - 4)
        face = image.pixelColor(int((zone.left() - 20) * ratio), int(g.center().y() * ratio))
        inked = sum(
            1
            for y in range(int(zone.top() * ratio), int(zone.bottom() * ratio))
            for x in range(int(zone.left() * ratio), int(zone.right() * ratio))
            if sum(
                abs(a - b) for a, b in zip(image.pixelColor(x, y).getRgb()[:3], face.getRgb()[:3])
            )
            > 60
        )
        assert menu.width() <= win.width() - 24, "전제: 메뉴가 상한에 걸려 있다"
        assert inked > 10, "흐린 경로가 보이는 메뉴 안에 그려지지 않았다"

    def test_a_long_path_is_shortened_like_the_card(self, make_window, tmp_path) -> None:
        """폭이 모자라면 경로는 뿌리/…/마지막 폴더로 접히고 그래도 모자라면 마지막 폴더 가운데가 줄어야 한다."""
        import math
        import os

        from PySide6.QtGui import QFontMetricsF

        from app.widgets.header_controls import fit_path

        path = str(tmp_path / "중간" / ("마지막 폴더 이름이 아주 김 " * 6).strip())
        win = make_window()
        metrics = win.saveLocationButton.menuWidget.fontMetrics()
        last = os.path.basename(path)  # 마지막 폴더 — 구분자는 OS마다 다르다
        # 칸 = 접은 글의 소수 폭 올림 — 정수 폭은 소수 폭 글꼴 엔진에서 글보다 좁을 수 있다(#527)
        room = math.ceil(QFontMetricsF(metrics).horizontalAdvance("~/…/" + last))
        folded = fit_path(metrics, path, room)
        squeezed = fit_path(metrics, path, 120)
        assert folded.endswith(last) and "/…/" in folded
        assert (
            "…" in squeezed[len(squeezed.split("/")[0]) + 3 :]
            and metrics.horizontalAdvance(squeezed) <= 120
        )

    def test_a_path_that_fits_keeps_its_last_folder_when_the_text_width_is_fractional(
        self, qapp: QApplication
    ) -> None:
        """글 폭이 소수인 글꼴에서 칸이 경로의 정수 글 폭이면 fit_path가 마지막 폴더 이름을 온전히 남겨야 한다.

        정수 글 폭 칸에서는 경로가 말줄임되는 소수 글자 간격, 경로 = 뿌리/중간 폴더/긴 마지막 폴더,
        칸 = 정수 글 폭 -> 끝이 「/마지막 폴더」 (#527)
        """
        import os

        from PySide6.QtGui import QFontMetrics
        from PySide6.QtWidgets import QMenu

        from app.widgets.header_controls import fit_path
        from app.widgets.widget import path_display_parts
        from tests.app.gui.test_header_layout import _font_whose_rounded_width_elides

        last = (
            "마지막 폴더 이름이 꽤 긴 다시보기 보관함"  # 가운데 말줄임이 이 이름 안에 떨어지는 길이
        )
        path = os.path.join(os.path.abspath(os.sep), "중간 폴더 이름", last)  # 뿌리 아래 두 단계
        full = path_display_parts(path)[0]
        assert full.endswith("/" + last), "전제: ①단계 글이 중간 폴더를 접지 않는다"
        font = _font_whose_rounded_width_elides(QMenu().font(), full)
        metrics = QFontMetrics(font)
        shown = fit_path(metrics, path, metrics.horizontalAdvance(full))
        assert shown.endswith("/" + last)

    def test_default_location_row_is_off_at_the_default_and_on_elsewhere(
        self, make_window, folders
    ) -> None:
        """지금 위치가 기본 위치면 「기본 위치로」가 꺼지고, 다른 위치면 켜지며 줄에 기본 위치 흐린 경로가 있어야 한다."""
        win = make_window()
        menu = _open(win)
        reset = _command(menu, "defaultLocationAction")
        assert not reset.isEnabled()
        assert reset.text() == f"Default location\t{abbreviate_path(win.saveLocations.default())}"
        menu.hide()
        assert win.saveLocations.choose(folders("다른"))
        assert _command(_open(win), "defaultLocationAction").isEnabled()

    def test_the_default_with_a_trailing_separator_counts_as_the_default(
        self, make_window, monkeypatch
    ) -> None:
        """「폴더 찾기…」로 끝 구분자만 붙은 기본 위치를 고르면 같은 위치로 보아 「기본 위치로」가 꺼져 있어야 한다."""
        import os

        win = make_window()
        default = win.saveLocations.default()
        monkeypatch.setattr(
            QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: default + os.sep)
        )
        _command(_open(win), "findFolderAction").trigger()
        assert not _command(_open(win), "defaultLocationAction").isEnabled()
