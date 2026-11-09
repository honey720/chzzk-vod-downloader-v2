"""저장 경로 행위 로깅 검증 (#148 — #146 감사 ③).

시작 전 관문에서 막힌 경로 실패는 지금까지 로그가 전혀 없어 제보를 로그로
진단할 수 없었다. 경로는
repr로 남긴다 — U+00A0(NBSP) 같은 공백 유사 문자는 그냥 찍으면 U+0020과
육안 구분되지 않는다(#144 실측). repr 검증도 NBSP 경로로 한다: 로그 문자열에
이스케이프("\\xa0")가 실제로 나타나야 한다.

배선은 실제 시그널 연결을 태운다 (#125 교훈) — 조회 관문은 fetchButton.click()으로
진입한다. 카드에서 저장 위치를 고르는 길은 없어졌다(#550 — 카드 편집 창이 한다).
"""

from collections.abc import Iterator

import pytest

import app.viewmodels.content_viewmodel as cvm_mod
from app.viewmodels.data import ContentItem
from app.viewmodels.content_viewmodel import ContentViewModel
from app.widgets.view import ContentListView
from core.models.download_state import DownloadState
from typing import TYPE_CHECKING, Never

if TYPE_CHECKING:
    from app.views.mainWindow import VodDownloader

NBSP_SUFFIX = "없는\u00a0폴더"  # 존재하지 않는 + 공백 유사 문자 포함


@pytest.fixture(autouse=True)
def no_network(monkeypatch) -> None:
    """위젯의 썸네일·파일 크기 조회 스레드가 실네트워크에 나가지 않게 차단한다."""

    class _FailingSession:
        def head(self, *args, **kwargs) -> Never:
            raise RuntimeError("network disabled in tests")

        def get(self, *args, **kwargs) -> Never:
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("app.widgets.widget.get_thread_session", lambda: _FailingSession())


def _metadata(title: str) -> dict:
    return {
        "title": title,
        "category": "게임",
        "channelName": "채널",
        "createdDate": "2026-08-03",
        "duration": 60,
    }


def _make_item(download_path: str, title: str = "경로 로깅 검증") -> ContentItem:
    return ContentItem(
        "https://chzzk.naver.com/video/1",
        _metadata(title),
        [["1080", "http://example.invalid/1080"]],
        "1080",
        "http://example.invalid/1080",
        download_path,
        "video",
        None,
    )


@pytest.fixture
def manager(qapp) -> Iterator[tuple[ContentViewModel, ContentListView]]:
    view = ContentListView()
    m = ContentViewModel()
    view.bind(m)
    yield m, view
    view.deleteLater()
    qapp.processEvents()


# ================================================================ 다운로드 관문 로깅


class TestDownloadGateLogging:
    def test_missing_path_is_logged_with_repr(self, manager, qapp, tmp_path, caplog) -> None:
        """미존재 경로 거부가 WARNING으로 남고, 경로는 repr(이스케이프)로 적힌다."""
        m, _view = manager
        item = _make_item(str(tmp_path / NBSP_SUFFIX))
        m.model.addItem(item)
        qapp.processEvents()

        with caplog.at_level("WARNING", logger="app.viewmodels.content_viewmodel"):
            m.downloadItem()

        assert item.downloadState is DownloadState.FAILED  # 기존 동작 회귀 없음
        assert "다운로드 시작 거부" in caplog.text
        assert "\\xa0" in caplog.text  # repr 이스케이프 — U+00A0이 육안 구분된다

    def test_probe_denied_warning_uses_repr(
        self, manager, qapp, tmp_path, caplog, monkeypatch
    ) -> None:
        """쓰기 프로브 실패 경고도 경로를 repr로 남긴다 (기존 %s → %r 전환)."""
        m, _view = manager
        monkeypatch.setattr(cvm_mod, "probe_writable", lambda d: (False, "denied"))
        item = _make_item(str(tmp_path / NBSP_SUFFIX))
        m.model.addItem(item)
        qapp.processEvents()

        with caplog.at_level("WARNING", logger="app.viewmodels.content_viewmodel"):
            m.downloadItem()

        assert "쓰기 프로브 실패(denied)" in caplog.text
        assert "\\xa0" in caplog.text


# ================================================================ 조회 관문(메인 윈도우)


class TestFetchGateLogging:
    @pytest.fixture
    def window(self, qapp, monkeypatch) -> "Iterator[tuple[VodDownloader, list[str]]]":
        import app.views.mainWindow as mw_mod

        # 팝업은 기록으로 대체한다 — offscreen에서 모달은 테스트를 멈춘다
        warnings = []
        monkeypatch.setattr(
            mw_mod.QMessageBox,
            "warning",
            lambda parent, title, text, *a, **k: warnings.append(text),
        )
        win = mw_mod.VodDownloader()
        yield win, warnings
        win.close()
        win.deleteLater()
        qapp.processEvents()

    @staticmethod
    def _find_folder(win, monkeypatch, folder) -> None:
        """메뉴 「폴더 찾기…」로 그 폴더를 고른다 — 메뉴를 실제로 열고 폴더 선택 창만 바꿔 끼운다."""
        from PySide6.QtWidgets import QFileDialog

        monkeypatch.setattr(
            QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(folder))
        )
        button = win.saveLocationButton
        button.menuWidget.popup(button.mapToGlobal(button.rect().bottomLeft()))
        action = next(
            a for a in button.menuWidget.actions() if a.objectName() == "findFolderAction"
        )
        action.trigger()
        button.menuWidget.hide()

    def test_fetch_reject_logs_input_with_repr(
        self, window, qapp, tmp_path, caplog, monkeypatch
    ) -> None:
        """조회 관문 거부가 경로 repr과 함께 WARNING으로 남는다 — 고른 뒤 사라진 폴더 → 버튼 클릭 실배선."""
        win, warnings = window
        gone = tmp_path / NBSP_SUFFIX
        gone.mkdir()
        self._find_folder(win, monkeypatch, gone)
        gone.rmdir()
        win.urlInput.setText("https://chzzk.naver.com/video/1")

        with caplog.at_level("WARNING", logger="app.views.mainWindow"):
            win.fetchButton.click()

        assert (
            win.registerError.text() == "Path does not exist."
        )  # 안내 글은 그대로, 자리는 오류 줄(#495)
        assert "조회 거부" in caplog.text
        assert "\\xa0" in caplog.text

    def test_chosen_location_is_logged(self, window, qapp, tmp_path, caplog, monkeypatch) -> None:
        """저장 위치 메뉴 「폴더 찾기…」로 고른 위치가 INFO로 남는다 — 메뉴 항목 실배선(창만 대체)."""
        win, _warnings = window
        chosen = tmp_path / "선택한 폴더"
        chosen.mkdir()

        with caplog.at_level("INFO", logger="app.views.mainWindow"):
            self._find_folder(win, monkeypatch, chosen)

        assert win.saveLocations.current() == str(chosen)
        assert "저장 위치 고름" in caplog.text
