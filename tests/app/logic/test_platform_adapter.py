"""app/platform_adapter.py 검증 (#211) — QStandardPaths·QDesktopServices 대체.

회귀 캡처: `content/widget.py`의 `requestOpenDir`와 `config/dialog.py`의
`openLogsFolder`를 읽어 확인한 기존 3-OS 분기(Windows explorer.exe /select,
· macOS open -R · Linux nautilus, 그리고 폴더는 QDesktopServices.openUrl)와
정확히 같은 명령이 나가는지를 각 OS 분기별로 고정한다. 실제 OS 프로세스는
띄우지 않는다 — subprocess.Popen/os.startfile을 스파이로 바꿔 인자만 본다.
"""

import logging
import sys
from pathlib import Path

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


# ================================================================ GIL 넘김 · 타이머 (#309)


@pytest.fixture
def timing(monkeypatch):
    """타이머 쪽의 모듈 상태를 깨끗이 하고, 타이머를 청하고 푸는 자리를 적는 대역으로 바꾼다.

    끝나면 넘김 간격과 모듈 상태를 되돌린다 — 다른 테스트가 바뀐 넘김 간격으로 돌지 않는다.
    """
    interval = sys.getswitchinterval()
    monkeypatch.setattr(adapter, "_timing_enabled", False)
    monkeypatch.setattr(adapter, "_work_count", 0)
    monkeypatch.setattr(adapter, "_timer_requested", False)
    calls: list[tuple[str, int]] = []
    granted = {"value": True}

    def request(period_ms: int) -> bool:
        calls.append(("청함", period_ms))
        return granted["value"]

    monkeypatch.setattr(adapter, "_request_timer_resolution", request)
    monkeypatch.setattr(
        adapter, "_release_timer_resolution", lambda period_ms: calls.append(("풂", period_ms))
    )
    yield calls, granted
    sys.setswitchinterval(interval)


class TestResponsiveTiming:
    def test_enabling_on_windows_sets_the_switch_interval_to_one_millisecond(
        self, monkeypatch, timing
    ):
        """Windows에서 켜면 GIL 넘김 간격이 1ms가 돼야 한다.

        platform.system() == "Windows", 넘김 간격 5ms에서 켬 -> 넘김 간격 == 0.001초
        """
        monkeypatch.setattr(adapter.platform, "system", lambda: "Windows")
        sys.setswitchinterval(0.005)

        adapter.enable_responsive_timing()

        assert sys.getswitchinterval() == pytest.approx(0.001)

    @pytest.mark.parametrize("system", ["Darwin", "Linux"])
    def test_other_systems_change_nothing(self, monkeypatch, timing, system):
        """Windows가 아니면 켜도 넘김 간격이 그대로이고, 일이 돌아도 타이머를 청하지 않아야 한다.

        platform.system() == system, 넘김 간격 5ms에서 켜고 일을 하나 돌림
        -> 넘김 간격 == 0.005초, 타이머를 청하거나 푼 적 없음
        """
        calls, _granted = timing
        monkeypatch.setattr(adapter.platform, "system", lambda: system)
        sys.setswitchinterval(0.005)

        adapter.enable_responsive_timing()
        with adapter.background_work():
            pass

        assert sys.getswitchinterval() == pytest.approx(0.005)
        assert calls == []

    def test_nothing_is_requested_before_it_is_enabled(self, monkeypatch, timing):
        """켜지 않았으면(헤드리스) Windows에서도 일이 돌 때 타이머를 청하지 않아야 한다.

        platform.system() == "Windows", 켜지 않고 일을 하나 돌림 -> 타이머를 청하거나 푼 적 없음
        """
        calls, _granted = timing
        monkeypatch.setattr(adapter.platform, "system", lambda: "Windows")

        with adapter.background_work():
            pass

        assert calls == []

    def test_timer_is_requested_for_the_first_work_and_released_after_the_last(
        self, monkeypatch, timing
    ):
        """겹치는 일은 수를 세어, 첫 일에서 타이머를 청하고 마지막 일이 끝날 때 한 번만 풀어야 한다.

        Windows에서 켬. 일 A 시작 → 일 B 시작 → A 끝 → B 끝
        -> B가 시작한 뒤: [청함 1], A가 끝난 뒤: [청함 1], B가 끝난 뒤: [청함 1, 풂 1]
        """
        calls, _granted = timing
        monkeypatch.setattr(adapter.platform, "system", lambda: "Windows")
        adapter.enable_responsive_timing()

        adapter.begin_background_work()
        adapter.begin_background_work()
        both_running = list(calls)
        adapter.end_background_work()
        one_left = list(calls)
        adapter.end_background_work()

        assert both_running == [("청함", 1)]
        assert one_left == [("청함", 1)]
        assert calls == [("청함", 1), ("풂", 1)]

    def test_timer_is_requested_again_for_work_that_starts_after_the_release(
        self, monkeypatch, timing
    ):
        """마지막 일이 끝나 타이머를 푼 뒤 새 일이 시작하면 다시 청해야 한다.

        Windows에서 켬. 일 하나를 돌리고, 끝난 뒤 하나를 더 돌림 -> [청함 1, 풂 1, 청함 1, 풂 1]
        """
        calls, _granted = timing
        monkeypatch.setattr(adapter.platform, "system", lambda: "Windows")
        adapter.enable_responsive_timing()

        with adapter.background_work():
            pass
        with adapter.background_work():
            pass

        assert calls == [("청함", 1), ("풂", 1), ("청함", 1), ("풂", 1)]

    def test_enabling_on_windows_records_the_switch_interval(self, monkeypatch, timing, caplog):
        """Windows에서 켜면 그때의 넘김 간격을 기록 한 줄로 남겨야 한다.

        platform.system() == "Windows", 넘김 간격 5ms에서 켬
        -> app.platform_adapter 로거의 INFO 한 줄 == "화면 반응 설정: GIL 넘김 간격 1ms"
        """
        monkeypatch.setattr(adapter.platform, "system", lambda: "Windows")
        sys.setswitchinterval(0.005)

        with caplog.at_level(logging.DEBUG):
            adapter.enable_responsive_timing()

        records = [r for r in caplog.records if r.name == "app.platform_adapter"]
        assert [(r.levelno, r.getMessage()) for r in records] == [
            (logging.INFO, "화면 반응 설정: GIL 넘김 간격 1ms")
        ]

    @pytest.mark.parametrize(
        ("granted", "expected"),
        [
            (True, ["타이머 단위 1ms 청함: 받아들여짐", "타이머 단위 1ms 청을 풂"]),
            (False, ["타이머 단위 1ms 청함: 받아들여지지 않음"]),
        ],
        ids=["받아들여짐", "거절됨"],
    )
    def test_timer_request_and_release_are_recorded(
        self, monkeypatch, timing, caplog, granted, expected
    ):
        """타이머를 청한 결과와 푼 것을 기록으로 남겨야 한다 — 거절된 청은 풀지 않으므로 푼 줄이 없다.

        Windows에서 켠 뒤 일을 하나 돌림. 청이 받아들여짐 / 거절됨
        -> 켠 뒤의 app.platform_adapter 로거의 DEBUG 줄 == expected
        """
        _calls, answer = timing
        answer["value"] = granted
        monkeypatch.setattr(adapter.platform, "system", lambda: "Windows")
        adapter.enable_responsive_timing()

        with caplog.at_level(logging.DEBUG):
            with adapter.background_work():
                pass

        records = [r for r in caplog.records if r.name == "app.platform_adapter"]
        assert [(r.levelno, r.getMessage()) for r in records] == [
            (logging.DEBUG, message) for message in expected
        ]

    def test_a_refused_request_is_not_released(self, monkeypatch, timing):
        """타이머의 청이 받아들여지지 않았으면 풀지 않아야 한다.

        Windows에서 켬. 청이 거절되게 하고 일을 하나 돌림 -> [청함 1]
        """
        calls, granted = timing
        granted["value"] = False
        monkeypatch.setattr(adapter.platform, "system", lambda: "Windows")
        adapter.enable_responsive_timing()

        with adapter.background_work():
            pass

        assert calls == [("청함", 1)]

    def test_work_that_raises_still_releases_the_timer(self, monkeypatch, timing):
        """일이 예외로 끝나도 타이머를 풀어야 한다.

        Windows에서 켬. 일 안에서 예외를 던짐 -> 예외가 그대로 나온다, [청함 1, 풂 1]
        """
        calls, _granted = timing
        monkeypatch.setattr(adapter.platform, "system", lambda: "Windows")
        adapter.enable_responsive_timing()

        with pytest.raises(RuntimeError):
            with adapter.background_work():
                raise RuntimeError("일이 실패했다(대역)")

        assert calls == [("청함", 1), ("풂", 1)]

    def test_an_end_without_a_begin_is_ignored(self, monkeypatch, timing):
        """짝이 없는 끝은 무시해야 한다 — 그 뒤의 일이 정상으로 세어진다.

        Windows에서 켬. 끝을 먼저 한 번 부르고 일을 하나 돌림 -> [청함 1, 풂 1]
        """
        calls, _granted = timing
        monkeypatch.setattr(adapter.platform, "system", lambda: "Windows")
        adapter.enable_responsive_timing()

        adapter.end_background_work()
        with adapter.background_work():
            pass

        assert calls == [("청함", 1), ("풂", 1)]

    def test_timer_library_is_absent_where_the_loader_does_not_exist(self, monkeypatch):
        """winmm을 여는 길이 없는 OS에서는 타이머를 청해도 거짓을 돌려주고 예외가 없어야 한다.

        ctypes에 WinDLL이 없는 것으로 만들고 청함 · 풂을 부름 -> 청함 == False, 풂은 조용히 돌아온다
        """
        import ctypes

        monkeypatch.setattr(adapter, "_winmm", None)
        monkeypatch.delattr(ctypes, "WinDLL", raising=False)

        assert adapter._request_timer_resolution(1) is False
        adapter._release_timer_resolution(1)


def _source(relative: str) -> str:
    return (Path(__file__).resolve().parents[3] / relative).read_text(encoding="utf-8")


def test_gui_entry_enables_responsive_timing_and_headless_does_not():
    """GUI 진입점은 타이머 청을 켜고, 헤드리스 스크립트는 켜지 않아야 한다.

    CVDv2.py · scripts/headless_download.py의 소스 -> 앞의 것에만 enable_responsive_timing() 호출이 있다
    """
    assert "enable_responsive_timing()" in _source("CVDv2.py")
    assert "enable_responsive_timing" not in _source("scripts/headless_download.py")
