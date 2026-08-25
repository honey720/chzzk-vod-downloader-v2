"""app/platform_adapter.py 검증 (#211) — QStandardPaths·QDesktopServices 대체.

회귀 캡처: `content/widget.py`의 `requestOpenDir`와 `config/dialog.py`의
`openLogsFolder`를 읽어 확인한 기존 3-OS 분기(Windows explorer.exe /select,
· macOS open -R · Linux nautilus, 그리고 폴더는 QDesktopServices.openUrl)와
정확히 같은 명령이 나가는지를 각 OS 분기별로 고정한다. 실제 OS 프로세스는
띄우지 않는다 — subprocess.Popen/os.startfile을 스파이로 바꿔 인자만 본다.
"""

import pytest

import app.platform_adapter as adapter


class FakePopenCall:
    """subprocess.Popen 대역 — 호출 인자만 기록하고 실제 프로세스는 안 띄운다."""

    def __init__(self, raise_oserror: bool = False):
        self.calls: list[list[str]] = []
        self._raise = raise_oserror

    def __call__(self, args, *a, **kw):
        self.calls.append(args)
        if self._raise:
            raise OSError("실행 파일을 찾을 수 없음")
        return object()  # Popen 인스턴스 대역 — 이후 아무것도 안 씀


@pytest.fixture
def fake_popen(monkeypatch):
    fake = FakePopenCall()
    monkeypatch.setattr(adapter.subprocess, "Popen", fake)
    return fake


class TestGetDefaultDownloadDir:
    def test_delegates_to_platformdirs(self, monkeypatch):
        monkeypatch.setattr(adapter.platformdirs, "user_downloads_dir", lambda: "/fake/Downloads")
        assert adapter.get_default_download_dir() == "/fake/Downloads"


class TestOpenFolder:
    """QDesktopServices.openUrl(QUrl.fromLocalFile(path)) 대체 — config/dialog.py의
    openLogsFolder가 오늘 이 패턴으로 로그 폴더를 여는 것과 동등해야 한다."""

    def test_windows_uses_os_startfile(self, monkeypatch):
        calls = []
        monkeypatch.setattr(adapter.platform, "system", lambda: "Windows")
        # os.startfile은 Windows에만 존재하는 속성이다 — CI의 Linux/macOS 러너에서는
        # 애초에 os 모듈에 이 이름이 없어 monkeypatch.setattr이 기본값(raising=True)으로
        # AttributeError를 던진다 (#181과 같은 함정이 이번엔 테스트 코드 쪽에서 재현됨).
        # raising=False로 "없는 속성도 만들어서 패치"하도록 허용해야 3-OS 전부에서 돈다.
        monkeypatch.setattr(adapter.os, "startfile", lambda path: calls.append(path), raising=False)

        assert adapter.open_folder("C:/logs") is True
        assert calls == ["C:/logs"]

    def test_windows_startfile_failure_returns_false_not_raises(self, monkeypatch):
        """#181: os.startfile은 Windows 전용이라 다른 OS에서 AttributeError로 죽었었다 —
        여기서는 반대로 Windows에서 startfile 자체가 실패하는 경우(OSError)를 검증한다."""
        monkeypatch.setattr(adapter.platform, "system", lambda: "Windows")

        def boom(path):
            raise OSError("연결된 프로그램 없음")

        monkeypatch.setattr(adapter.os, "startfile", boom, raising=False)

        assert adapter.open_folder("C:/missing") is False

    def test_macos_uses_open(self, monkeypatch, fake_popen):
        monkeypatch.setattr(adapter.platform, "system", lambda: "Darwin")

        assert adapter.open_folder("/Users/x/logs") is True
        assert fake_popen.calls == [["open", "/Users/x/logs"]]

    def test_linux_uses_xdg_open(self, monkeypatch, fake_popen):
        monkeypatch.setattr(adapter.platform, "system", lambda: "Linux")

        assert adapter.open_folder("/home/x/logs") is True
        assert fake_popen.calls == [["xdg-open", "/home/x/logs"]]

    def test_popen_oserror_returns_false(self, monkeypatch):
        monkeypatch.setattr(adapter.platform, "system", lambda: "Linux")
        monkeypatch.setattr(adapter.subprocess, "Popen", FakePopenCall(raise_oserror=True))

        assert adapter.open_folder("/no/such/xdg-open") is False

    def test_unknown_platform_returns_false(self, monkeypatch):
        monkeypatch.setattr(adapter.platform, "system", lambda: "SomeOtherOS")

        assert adapter.open_folder("/anywhere") is False


class TestRevealInFileManager:
    """content/widget.py의 requestOpenDir 3-way 분기 그대로 이식."""

    def test_folder_path_delegates_to_open_folder(self, monkeypatch, tmp_path, fake_popen):
        """경로가 폴더면 requestOpenDir의 else 분기(QDesktopServices.openUrl)와 동일하다."""
        monkeypatch.setattr(adapter.platform, "system", lambda: "Linux")
        folder = tmp_path  # 실제 존재하는 디렉토리 — os.path.isfile이 False가 되게

        result = adapter.reveal_in_file_manager(str(folder))

        assert result is True
        assert fake_popen.calls == [["xdg-open", str(folder)]]

    def test_windows_file_path_uses_explorer_select(self, monkeypatch, tmp_path, fake_popen):
        monkeypatch.setattr(adapter.platform, "system", lambda: "Windows")
        file_path = tmp_path / "video.mp4"
        file_path.write_text("x")

        result = adapter.reveal_in_file_manager(str(file_path))

        assert result is True
        [call] = fake_popen.calls
        assert call[0] == "explorer.exe"
        assert call[1] == "/select,"
        # QDir.toNativeSeparators 대응 — os.path.normpath로 네이티브 구분자화
        import os as _os

        assert call[2] == _os.path.normpath(str(file_path))

    def test_macos_file_path_uses_open_dash_r(self, monkeypatch, tmp_path, fake_popen):
        monkeypatch.setattr(adapter.platform, "system", lambda: "Darwin")
        file_path = tmp_path / "video.mp4"
        file_path.write_text("x")

        result = adapter.reveal_in_file_manager(str(file_path))

        assert result is True
        [call] = fake_popen.calls
        assert call[0] == "open"
        assert call[1] == "-R"

    def test_linux_file_path_uses_nautilus(self, monkeypatch, tmp_path, fake_popen):
        """Linux는 nautilus 고정 — GNOME 전제(#193 감사가 이미 지적, 이 PR에서 안 바꿈)."""
        monkeypatch.setattr(adapter.platform, "system", lambda: "Linux")
        file_path = tmp_path / "video.mp4"
        file_path.write_text("x")

        result = adapter.reveal_in_file_manager(str(file_path))

        assert result is True
        [call] = fake_popen.calls
        assert call[0] == "nautilus"

    def test_file_manager_launch_failure_returns_false(self, monkeypatch, tmp_path):
        monkeypatch.setattr(adapter.platform, "system", lambda: "Linux")
        monkeypatch.setattr(adapter.subprocess, "Popen", FakePopenCall(raise_oserror=True))
        file_path = tmp_path / "video.mp4"
        file_path.write_text("x")

        assert adapter.reveal_in_file_manager(str(file_path)) is False

    def test_unknown_platform_file_path_returns_false(self, monkeypatch, tmp_path):
        monkeypatch.setattr(adapter.platform, "system", lambda: "SomeOtherOS")
        file_path = tmp_path / "video.mp4"
        file_path.write_text("x")

        assert adapter.reveal_in_file_manager(str(file_path)) is False


class TestStartDetachedIsNonBlocking:
    """QProcess.startDetached처럼 프로세스 종료를 기다리지 않는다 (#136/#137/PR#135 원칙).

    subprocess.run이 아니라 subprocess.Popen을 쓴다는 것 자체를 계약으로 고정한다 —
    누군가 나중에 손쉬운 리팩터링으로 run()으로 되돌리면 파일탐색기 기동이
    느린 환경에서 호출 스레드가 붙잡힌다.
    """

    def test_uses_popen_not_run(self, monkeypatch):
        run_calls = []
        monkeypatch.setattr(adapter.subprocess, "run", lambda *a, **kw: run_calls.append(a))
        monkeypatch.setattr(adapter.subprocess, "Popen", lambda *a, **kw: object())
        monkeypatch.setattr(adapter.platform, "system", lambda: "Linux")

        adapter.open_folder("/some/path")

        assert run_calls == []  # subprocess.run은 한 번도 안 불렸다
