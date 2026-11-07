"""시작 시 기본 저장 경로 결정·보존 검증 (#159 — #157 실측 후속).

#157 실측: macOS .app을 Finder/Dock으로 실행하면 cwd='/'(쓰기 불가)라
기본값 os.getcwd()로는 첫 실행 유저의 첫 다운로드가 반드시 실패했다.
초기값 우선순위(저장된 경로 → 시스템 다운로드 폴더 → cwd)와 실사용 경로의
설정 보존을 실배선(윈도우 생성·버튼 클릭)으로 검증한다.

config 로드·저장은 메모리로 격리한다 — 실유저 config.json 무접촉.
"""

import os

import pytest

import app.views.mainWindow as mw_mod
import app.system.settings as settings_mod
from typing import Never


@pytest.fixture(autouse=True)
def no_network(monkeypatch) -> None:
    """조회·위젯 스레드가 실네트워크에 나가지 않게 차단한다."""

    class _FailingSession:
        def head(self, *args, **kwargs) -> Never:
            raise RuntimeError("network disabled in tests")

        def get(self, *args, **kwargs) -> Never:
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("app.widgets.widget.get_thread_session", lambda: _FailingSession())
    monkeypatch.setattr("app.network._session", _FailingSession())


@pytest.fixture
def config_store(monkeypatch):
    """config 로드·저장을 메모리 딕셔너리로 격리하고 저장 이력을 기록한다."""
    store = {"cookies": {"NID_AUT": "", "NID_SES": ""}}
    saves: list[dict] = []

    monkeypatch.setattr(settings_mod, "load_settings", lambda: dict(store))

    def _save(cfg) -> None:
        saves.append(dict(cfg))
        store.clear()
        store.update(cfg)

    monkeypatch.setattr(settings_mod, "save_settings", _save)
    return store, saves


@pytest.fixture
def window_factory(qapp, monkeypatch, config_store):
    """팝업을 기록으로 대체한 실물 메인 윈도우를 만든다 (offscreen 실배선)."""
    warnings = []
    monkeypatch.setattr(
        mw_mod.QMessageBox,
        "warning",
        lambda parent, title, text, *a, **k: warnings.append(text),
    )
    # 차단된 네트워크의 조회 실패는 critical로 온다 — 실모달이 뜨면
    # offscreen 이벤트 루프가 영원히 대기하므로 기록으로 대체한다
    monkeypatch.setattr(
        mw_mod.QMessageBox,
        "critical",
        lambda parent, title, text, *a, **k: warnings.append(text),
    )
    made = []

    def _make():
        win = mw_mod.VodDownloader()
        made.append(win)
        return win

    yield _make, warnings
    for win in made:
        win.close()
        win.deleteLater()
    qapp.processEvents()


def test_downloadpath_key_registered_in_default_settings() -> None:
    """정규화(normalize_settings)가 키 표 미등재 키를 버리므로 등재가 보존의 전제다."""
    assert "downloadPath" in settings_mod.default_settings()
    assert settings_mod.default_settings()["downloadPath"] == ""  # 빈 값 = 미설정


def test_saved_path_wins(window_factory, config_store, tmp_path) -> None:
    """저장된 경로가 실존하면 초기값 ①로 쓰인다."""
    store, _saves = config_store
    store["downloadPath"] = str(tmp_path)
    make, _ = window_factory

    assert make().saveLocations.current() == str(tmp_path)


def test_missing_saved_falls_back_to_standard_downloads(
    window_factory, config_store, tmp_path, monkeypatch
) -> None:
    """저장된 경로가 사라졌으면(외장 분리 등) 시스템 다운로드 폴더 ②로 간다."""
    store, _saves = config_store
    store["downloadPath"] = str(tmp_path / "분리된 드라이브")
    downloads = tmp_path / "Downloads"
    downloads.mkdir()

    class _StubPaths:
        StandardLocation = mw_mod.QStandardPaths.StandardLocation

        @staticmethod
        def writableLocation(kind):
            return str(downloads)

    monkeypatch.setattr(mw_mod, "QStandardPaths", _StubPaths)
    make, _ = window_factory

    assert make().saveLocations.current() == str(downloads)


def test_standard_missing_falls_back_to_cwd(
    window_factory, config_store, tmp_path, monkeypatch
) -> None:
    """표준 폴더까지 없으면 cwd ③ — 소스 실행(개발) 관례 유지."""

    class _StubPaths:
        StandardLocation = mw_mod.QStandardPaths.StandardLocation

        @staticmethod
        def writableLocation(kind):
            return str(tmp_path / "없는 폴더")

    monkeypatch.setattr(mw_mod, "QStandardPaths", _StubPaths)
    make, _ = window_factory

    assert make().saveLocations.current() == os.getcwd()


def _menu_action(win, object_name: str):
    """저장 위치 메뉴를 실제로 열고(aboutToShow 배선) 그 이름의 명령 항목을 돌려준다."""
    menu = win.saveLocationButton.menuWidget
    menu.popup(win.saveLocationButton.mapToGlobal(win.saveLocationButton.rect().bottomLeft()))
    action = next(a for a in menu.actions() if a.objectName() == object_name)
    return menu, action


def _find_folder(win, monkeypatch, folder) -> None:
    """메뉴 「폴더 찾기…」로 그 폴더를 고른다 — 폴더 선택 창만 바꿔 끼운다."""
    from PySide6.QtWidgets import QFileDialog

    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(folder)))
    menu, action = _menu_action(win, "findFolderAction")
    action.trigger()
    menu.hide()


def test_found_folder_goes_to_recent_and_leaves_the_default_alone(
    window_factory, config_store, tmp_path, monkeypatch
) -> None:
    """「폴더 찾기…」로 고른 폴더는 지금 위치 · 최근 맨 앞이 되고 downloadPath는 그대로여야 한다 (#495)."""
    store, _saves = config_store
    default = tmp_path / "기본"
    default.mkdir()
    store["downloadPath"] = str(default)
    make, _warnings = window_factory
    win = make()
    chosen = tmp_path / "고른 폴더"
    chosen.mkdir()

    _find_folder(win, monkeypatch, chosen)

    assert win.saveLocations.current() == str(chosen)
    assert store["recentPaths"][0] == str(chosen)
    assert store["downloadPath"] == str(default)


def test_fetch_uses_the_chosen_location_and_leaves_the_default_alone(
    window_factory, config_store, tmp_path, monkeypatch, qapp
) -> None:
    """고른 위치로 등록하면 그 카드의 저장 위치는 고른 위치이고 downloadPath는 그대로여야 한다 (#495)."""
    store, _saves = config_store
    default = tmp_path / "기본"
    default.mkdir()
    store["downloadPath"] = str(default)
    make, _warnings = window_factory
    win = make()
    chosen = tmp_path / "space dir"
    chosen.mkdir()
    _find_folder(win, monkeypatch, chosen)
    win.urlInput.setText("https://chzzk.naver.com/video/1")

    win.fetchButton.click()

    item = win.contentManager.model.itemAt(0)
    assert item is not None and item.download_path == str(chosen)
    win.contentManager.threadpool.waitForDone(8000)
    qapp.processEvents()
    assert store["downloadPath"] == str(default)


def test_a_vanished_location_is_rejected_at_fetch_and_nothing_is_saved(
    window_factory, config_store, tmp_path, monkeypatch, qapp
) -> None:
    """고른 뒤 사라진 폴더로 등록하면 등록 오류 줄에 안내만 뜨고 카드 · downloadPath가 그대로여야 한다(#169 조회 관문)."""
    store, _saves = config_store
    make, warnings = window_factory
    win = make()
    gone = tmp_path / "분리된 드라이브"
    gone.mkdir()
    _find_folder(win, monkeypatch, gone)
    gone.rmdir()
    win.urlInput.setText("https://chzzk.naver.com/video/1")

    win.fetchButton.click()

    assert win.registerErrorLabel.text() == "Path does not exist."  # #495 — 대화상자 대신 오류 줄
    assert warnings == []
    assert win.contentManager.model.rowCount() == 0
    assert store.get("downloadPath", "") == ""


def test_closing_does_not_change_the_default_location(
    window_factory, config_store, tmp_path, monkeypatch
) -> None:
    """다른 위치를 고른 채 창을 닫아도 downloadPath는 그대로여야 한다 (#495 — #165의 닫기 보존을 걷었다)."""
    store, _saves = config_store
    default = tmp_path / "기본"
    default.mkdir()
    store["downloadPath"] = str(default)
    make, _warnings = window_factory
    win = make()
    chosen = tmp_path / "닫기 전 폴더"
    chosen.mkdir()
    _find_folder(win, monkeypatch, chosen)

    win.close()

    assert store["downloadPath"] == str(default)


def test_a_restart_starts_at_the_default_and_keeps_the_recent_list(
    window_factory, config_store, tmp_path, monkeypatch
) -> None:
    """고른 위치로 바꾼 뒤 창을 닫고 다시 띄우면 지금 위치는 기본 위치, 메뉴 「최근」에는 고른 위치가 있어야 한다."""
    store, _saves = config_store
    default = tmp_path / "기본"
    default.mkdir()
    store["downloadPath"] = str(default)
    make, _warnings = window_factory
    first = make()
    chosen = tmp_path / "바꾼 폴더"
    chosen.mkdir()
    _find_folder(first, monkeypatch, chosen)
    first.close()

    second = make()
    menu, _action = _menu_action(second, "findFolderAction")
    recent = [a.data()[1] for a in menu.choices() if a.data()[0] == "recent"]
    menu.hide()

    assert second.saveLocations.current() == str(default)
    assert recent == [str(chosen)]
