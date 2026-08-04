"""시작 전 쓰기 프로브 검증 (#137 — #136 제안 ②).

프로브는 제물 스레드 + join(timeout)으로 감싼다 — 존재 검사조차 무응답
마운트에서는 메인 스레드를 매달 수 있기 때문이다 (#136 조사). 무응답
마운트 자체는 실드라이브 없이 재현할 수 없으므로, 프로브 내부의 I/O
호출(tempfile.mkstemp)을 반환하지 않는 함수로 바꿔 흉내 낸다 — 프로브가
검사하는 것이 바로 그 호출이므로 대체가 재현으로 유효하다.

downloadItem 연동은 실제 모델·뷰·시그널 배선으로 검증한다 (#125 교훈).
"""

import threading

import pytest

import content.manager as manager_mod
from content.data import ContentItem
from content.manager import ContentManager, probe_writable
from content.view import ContentListView
from core.models.download_state import DownloadState


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


class TestProbeWritable:
    def test_writable_directory_passes_and_leaves_no_residue(self, tmp_path):
        assert probe_writable(str(tmp_path)) == (True, "")
        assert list(tmp_path.iterdir()) == []  # 프로브 파일 잔재 없음

    def test_missing_directory(self, tmp_path):
        assert probe_writable(str(tmp_path / "없는폴더")) == (False, "missing")

    def test_permission_error_is_denied(self, tmp_path, monkeypatch):
        """권한 오류가 정상적으로 오류로 반환되는 경우 (예: 로컬 읽기 전용)."""

        def deny(*args, **kwargs):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(manager_mod.tempfile, "mkstemp", deny)
        assert probe_writable(str(tmp_path)) == (False, "denied")

    def test_hanging_io_times_out(self, tmp_path, monkeypatch):
        """무응답 마운트 흉내 — 파일 생성 호출이 반환하지 않으면 상한에서 포기한다.

        RaiDrive가 SFTP 권한 오류(ZFS 풀 루트)를 Windows 오류로 전파하지
        못하고 매달리는 실측 증상(#136)의 재현이다. 갇힌 제물 스레드는
        테스트 종료 전에 release로 깨워 잔류를 막는다.
        """
        release = threading.Event()

        def hang(*args, **kwargs):
            release.wait(5)  # 반환하지 않는 I/O 흉내
            raise PermissionError(13, "belated")

        monkeypatch.setattr(manager_mod.tempfile, "mkstemp", hang)
        writable, reason = probe_writable(str(tmp_path), timeout_s=0.2)
        release.set()

        assert (writable, reason) == (False, "timeout")


# ================================================================ downloadItem 연동


def _metadata(title: str) -> dict:
    return {
        "title": title,
        "category": "게임",
        "channelName": "채널",
        "createdDate": "2026-07-30",
        "duration": 60,
    }


def _make_item(download_path: str, title: str) -> ContentItem:
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


class TestDownloadItemWriteGate:
    def test_unwritable_path_fails_before_start(self, manager, qapp, tmp_path, monkeypatch):
        """완료 조건: 쓰기 불가 경로는 다운로드 시작 전에 카드 실패로 끝난다."""
        m, view = manager
        # 프로브 판정 자체는 위 단위 테스트가 고정한다 — 여기서는 배선만 본다
        monkeypatch.setattr(manager_mod, "probe_writable", lambda d: (False, "denied"))

        item = _make_item(str(tmp_path), "쓰기 불가 항목")
        m.model.addItem(item)
        qapp.processEvents()
        requested, finished_all = [], []
        m.downloadRequested.connect(requested.append)
        m.finishedAllRequested.connect(lambda: finished_all.append(True))

        m.downloadItem()

        assert requested == []  # 다운로드가 시작되지 않는다
        assert item.downloadState is DownloadState.FAILED
        assert item.stateMessage == "Failed to save file"  # 번역기 미설치 — 키 원문
        assert finished_all == [True]  # 배치는 종료 신호까지 간다
        widget = view.indexWidget(m.model.index(0, 0))
        assert widget.statusLabel.text().startswith("Download failed")  # 카드에 명확히 표시

    def test_timeout_probe_fails_the_same_way(self, manager, qapp, tmp_path, monkeypatch):
        """무응답(timeout)도 같은 안내다 — 유저에게는 '저장할 수 없는 경로'라는 같은 사실."""
        m, _view = manager
        monkeypatch.setattr(manager_mod, "probe_writable", lambda d: (False, "timeout"))
        item = _make_item(str(tmp_path), "무응답 항목")
        m.model.addItem(item)
        qapp.processEvents()

        m.downloadItem()

        assert item.downloadState is DownloadState.FAILED
        assert item.stateMessage == "Failed to save file"

    def test_missing_path_keeps_invalid_path_message(self, manager, qapp, tmp_path):
        """존재하지 않는 경로는 기존 안내(Invalid file path)를 유지한다 — 실프로브 경유."""
        m, _view = manager
        item = _make_item(str(tmp_path / "없는폴더"), "경로 없음 항목")
        m.model.addItem(item)
        qapp.processEvents()

        m.downloadItem()

        assert item.downloadState is DownloadState.FAILED
        assert item.stateMessage == "Invalid file path"

    def test_writable_path_proceeds_to_download(self, manager, qapp, tmp_path):
        """완료 조건: 정상 경로는 회귀 없음 — 실프로브를 지나 다운로드 요청이 나간다."""
        m, _view = manager
        item = _make_item(str(tmp_path), "정상 항목")
        m.model.addItem(item)
        qapp.processEvents()
        requested = []
        m.downloadRequested.connect(requested.append)

        m.downloadItem()

        assert requested == [item]
        assert item.downloadState is DownloadState.WAITING  # 시작 전이 — 상태 전이는 브리지 몫
