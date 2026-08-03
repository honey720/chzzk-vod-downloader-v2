"""시작 시 기본 저장 경로 결정·보존 검증 (#159 — #157 실측 후속).

#157 실측: macOS .app을 Finder/Dock으로 실행하면 cwd='/'(쓰기 불가)라
기본값 os.getcwd()로는 첫 실행 유저의 첫 다운로드가 반드시 실패했다.
초기값 우선순위(저장된 경로 → 시스템 다운로드 폴더 → cwd)와 실사용 경로의
설정 보존을 실배선(윈도우 생성·버튼 클릭)으로 검증한다.

config 로드·저장은 메모리로 격리한다 — 실유저 config.json 무접촉.
"""

import os

import pytest

import application.mainWindow as mw_mod
import config.config as config_mod


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """조회·위젯 스레드가 실네트워크에 나가지 않게 차단한다."""

    class _FailingSession:
        def head(self, *args, **kwargs):
            raise RuntimeError("network disabled in tests")

        def get(self, *args, **kwargs):
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("content.widget.get_thread_session", lambda: _FailingSession())
    monkeypatch.setattr("content.network.get_thread_session", lambda: _FailingSession())
    monkeypatch.setattr("content.network._session", _FailingSession())
    monkeypatch.setattr("core.api.session._session", _FailingSession())


@pytest.fixture
def config_store(monkeypatch):
    """config 로드·저장을 메모리 딕셔너리로 격리하고 저장 이력을 기록한다."""
    store = {"cookies": {"NID_AUT": "", "NID_SES": ""}}
    saves: list[dict] = []

    monkeypatch.setattr(config_mod, "load_config", lambda: dict(store))

    def _save(cfg):
        saves.append(dict(cfg))
        store.clear()
        store.update(cfg)

    monkeypatch.setattr(config_mod, "save_config", _save)
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


def test_downloadpath_key_registered_in_default_config():
    """reorder_config가 DEFAULT_CONFIG 미등재 키를 삭제하므로 등재가 보존의 전제다."""
    assert "downloadPath" in config_mod.DEFAULT_CONFIG
    assert config_mod.DEFAULT_CONFIG["downloadPath"] == ""  # 빈 값 = 미설정


def test_saved_path_wins(window_factory, config_store, tmp_path):
    """저장된 경로가 실존하면 초기값 ①로 쓰인다."""
    store, _saves = config_store
    store["downloadPath"] = str(tmp_path)
    make, _ = window_factory

    assert make().downloadPathInput.text() == str(tmp_path)


def test_missing_saved_falls_back_to_standard_downloads(
    window_factory, config_store, tmp_path, monkeypatch
):
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

    assert make().downloadPathInput.text() == str(downloads)


def test_standard_missing_falls_back_to_cwd(window_factory, config_store, tmp_path, monkeypatch):
    """표준 폴더까지 없으면 cwd ③ — 소스 실행(개발) 관례 유지."""

    class _StubPaths:
        StandardLocation = mw_mod.QStandardPaths.StandardLocation

        @staticmethod
        def writableLocation(kind):
            return str(tmp_path / "없는 폴더")

    monkeypatch.setattr(mw_mod, "QStandardPaths", _StubPaths)
    make, _ = window_factory

    assert make().downloadPathInput.text() == os.getcwd()


def test_used_path_is_persisted_on_fetch(window_factory, config_store, tmp_path, qapp):
    """조회 관문을 통과해 실사용된 경로가 설정에 저장된다 — 버튼 클릭 실배선."""
    _store, saves = config_store
    make, _warnings = window_factory
    win = make()
    used = tmp_path / "space dir"
    used.mkdir()
    win.downloadPathInput.setText(str(used))
    win.urlInput.setText("https://chzzk.naver.com/video/1")

    win.fetchButton.click()
    win.contentManager.threadpool.waitForDone(8000)
    qapp.processEvents()

    assert saves and saves[-1]["downloadPath"] == str(used)


def test_rejected_path_is_not_persisted(window_factory, config_store, tmp_path, qapp):
    """관문에서 거부된 경로는 저장되지 않는다."""
    _store, saves = config_store
    make, warnings = window_factory
    win = make()
    win.downloadPathInput.setText(str(tmp_path / "없는 폴더"))
    win.urlInput.setText("https://chzzk.naver.com/video/1")

    win.fetchButton.click()
    qapp.processEvents()

    assert "Path does not exist." in warnings
    assert all("downloadPath" not in s or s["downloadPath"] == "" for s in saves)
