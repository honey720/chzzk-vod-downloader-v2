"""저장 경로 행위 로깅·카드 편집 피드백 검증 (#148 — #146 감사 ③).

시작 전 관문에서 막힌 경로 실패는 지금까지 로그가 전혀 없어 제보를 로그로
진단할 수 없었고, 카드 경로 편집의 거부는 유저 피드백도 없었다. 경로는
repr로 남긴다 — U+00A0(NBSP) 같은 공백 유사 문자는 그냥 찍으면 U+0020과
육안 구분되지 않는다(#144 실측). repr 검증도 NBSP 경로로 한다: 로그 문자열에
이스케이프("\\xa0")가 실제로 나타나야 한다.

배선은 실제 시그널 연결을 태운다 (#125 교훈) — 카드 편집은 editingFinished
시그널로, 조회 관문은 fetchButton.click()으로 진입한다.
"""

import pytest

import content.manager as manager_mod
import content.widget as widget_mod
from content.data import ContentItem
from content.manager import ContentManager
from content.view import ContentListView
from download.state import DownloadState

NBSP_SUFFIX = "없는\u00a0폴더"  # 존재하지 않는 + 공백 유사 문자 포함


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """위젯의 썸네일·파일 크기 조회 스레드가 실네트워크에 나가지 않게 차단한다."""

    class _FailingSession:
        def head(self, *args, **kwargs):
            raise RuntimeError("network disabled in tests")

        def get(self, *args, **kwargs):
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("content.widget.get_thread_session", lambda: _FailingSession())
    monkeypatch.setattr("content.network.get_thread_session", lambda: _FailingSession())


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
def manager(qapp):
    view = ContentListView()
    m = ContentManager(view)
    yield m, view
    view.deleteLater()
    qapp.processEvents()


# ================================================================ 다운로드 관문 로깅


class TestDownloadGateLogging:
    def test_missing_path_is_logged_with_repr(self, manager, qapp, tmp_path, caplog):
        """미존재 경로 거부가 WARNING으로 남고, 경로는 repr(이스케이프)로 적힌다."""
        m, _view = manager
        item = _make_item(str(tmp_path / NBSP_SUFFIX))
        m.model.addItem(item)
        qapp.processEvents()

        with caplog.at_level("WARNING", logger="content.manager"):
            m.downloadItem()

        assert item.downloadState is DownloadState.FAILED  # 기존 동작 회귀 없음
        assert "다운로드 시작 거부" in caplog.text
        assert "\\xa0" in caplog.text  # repr 이스케이프 — U+00A0이 육안 구분된다

    def test_probe_denied_warning_uses_repr(self, manager, qapp, tmp_path, caplog, monkeypatch):
        """쓰기 프로브 실패 경고도 경로를 repr로 남긴다 (기존 %s → %r 전환)."""
        m, _view = manager
        monkeypatch.setattr(manager_mod, "probe_writable", lambda d: (False, "denied"))
        item = _make_item(str(tmp_path / NBSP_SUFFIX))
        m.model.addItem(item)
        qapp.processEvents()

        with caplog.at_level("WARNING", logger="content.manager"):
            m.downloadItem()

        assert "쓰기 프로브 실패(denied)" in caplog.text
        assert "\\xa0" in caplog.text


# ================================================================ 카드 경로 편집


class TestCardPathEditFeedback:
    def _widget(self, manager, qapp, path):
        m, view = manager
        item = _make_item(path)
        m.model.addItem(item)
        qapp.processEvents()
        return item, view.indexWidget(m.model.index(0, 0))

    def test_reject_notifies_and_logs(self, manager, qapp, tmp_path, caplog, monkeypatch):
        """존재하지 않는 경로는 반영하지 않고, 이제 안내 팝업과 WARNING 로그를 남긴다.

        무피드백 조용한 거부(#146 감사)의 해소 — 배선은 editingFinished
        시그널 실연결을 태운다.
        """
        item, widget = self._widget(manager, qapp, str(tmp_path))
        warnings = []
        monkeypatch.setattr(
            widget_mod.QMessageBox,
            "warning",
            lambda parent, title, text, *a, **k: warnings.append(text),
        )
        widget.startPathEditing(None)  # 실제 편집 진입(WAITING 상태 가드 포함)
        widget.directoryEdit.setText(str(tmp_path / NBSP_SUFFIX))

        with caplog.at_level("WARNING", logger="content.widget"):
            widget.directoryEdit.editingFinished.emit()

        assert item.download_path == str(tmp_path)  # 기존 경로 유지 (동작 불변)
        assert warnings == ["Path does not exist."]  # 안내 1회 (중복 없음)
        assert "카드 저장 경로 거부" in caplog.text
        assert "\\xa0" in caplog.text

    def test_accept_updates_and_logs(self, manager, qapp, tmp_path, caplog):
        """유효한 경로(공백 포함)는 기존대로 반영되고 INFO로 남는다."""
        item, widget = self._widget(manager, qapp, str(tmp_path))
        new_dir = tmp_path / "space dir"
        new_dir.mkdir()
        widget.startPathEditing(None)
        widget.directoryEdit.setText(str(new_dir))

        with caplog.at_level("INFO", logger="content.widget"):
            widget.directoryEdit.editingFinished.emit()

        assert item.download_path == str(new_dir)
        assert "카드 저장 경로 변경" in caplog.text

    def test_editing_finished_twice_notifies_once(self, manager, qapp, tmp_path, monkeypatch):
        """returnPressed+포커스 이탈로 시그널이 연달아 와도 안내는 한 번이다."""
        item, widget = self._widget(manager, qapp, str(tmp_path))
        warnings = []
        monkeypatch.setattr(
            widget_mod.QMessageBox,
            "warning",
            lambda parent, title, text, *a, **k: warnings.append(text),
        )
        widget.startPathEditing(None)
        widget.directoryEdit.setText(str(tmp_path / "없는 폴더"))

        widget.directoryEdit.editingFinished.emit()
        widget.directoryEdit.editingFinished.emit()  # 두 번째는 isEditing 가드가 무시

        assert warnings == ["Path does not exist."]
        assert item.download_path == str(tmp_path)


# ================================================================ 조회 관문(메인 윈도우)


class TestFetchGateLogging:
    @pytest.fixture
    def window(self, qapp, monkeypatch):
        import application.mainWindow as mw_mod

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

    def test_fetch_reject_logs_input_with_repr(self, window, qapp, tmp_path, caplog):
        """조회 관문 거부가 WARNING으로 남는다 — 입력창 → 버튼 클릭 실배선."""
        win, warnings = window
        win.downloadPathInput.setText(str(tmp_path / NBSP_SUFFIX))
        win.urlInput.setText("https://chzzk.naver.com/video/1")

        with caplog.at_level("WARNING", logger="application.mainWindow"):
            win.fetchButton.click()

        assert "Path does not exist." in warnings  # 기존 안내 유지
        assert "조회 거부" in caplog.text
        assert "\\xa0" in caplog.text

    def test_find_path_selection_is_logged(self, window, qapp, tmp_path, caplog, monkeypatch):
        """'경로 찾기' 선택값이 INFO로 남는다 — 버튼 클릭 실배선(다이얼로그만 대체)."""
        import application.mainWindow as mw_mod

        win, _warnings = window
        chosen = tmp_path / "선택한 폴더"
        chosen.mkdir()
        monkeypatch.setattr(
            mw_mod.QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(chosen))
        )

        with caplog.at_level("INFO", logger="application.mainWindow"):
            win.downloadPathButton.click()

        assert win.downloadPathInput.text() == str(chosen)
        assert "저장 경로 선택(경로 찾기)" in caplog.text
