"""플랫폼 어댑터 — QStandardPaths·QDesktopServices 대체를 한 모듈로 (#211, Phase A3).

PySide6는 렌더링 외에도 이 둘을 제공했다(#208 ⑦ 조사):
- `QStandardPaths.writableLocation(DownloadLocation)` → `get_default_download_dir()`
- `QDesktopServices.openUrl` → `open_folder()` (폴더를 그냥 연다, `config/dialog.py`의
  `openLogsFolder`가 오늘 이렇게 쓰고 있다)

**`QDesktopServices`가 이미 절반만 문제를 풀고 있었다**(#211 조사): "폴더 열기"는
`QDesktopServices`로 충분했지만 "파일을 파일탐색기에서 선택된 채로 보여주기"는
Qt에도 대응 API가 없어서, `content/widget.py`의 `requestOpenDir`가 이미 3-way
수동 OS 분기(Windows `explorer.exe /select,` / macOS `open -R` / Linux
`nautilus`)를 쓰고 있었다. `reveal_in_file_manager()`는 그 분기를 그대로
옮긴 것이다 — Linux `nautilus` 고정(GNOME 전제, KDE·XFCE에서 실패할 수
있음 — `#193` 감사가 이미 지적)을 유지할지 낮출지는 **이 모듈에서 결정하지
않는다**. 기존 동작 보존이 기본값이다.

**상대 경로 절대화(`#146` ⓑ-4)는 이 모듈에 없다** — `app/viewmodels/path_gates.py`가
이미 "저장 경로 관문 판정의 단일 지점"으로 존재하고 이건 OS 상호작용이 아니라
경로 판정/정규화 문제라, Phase B에서 그 모듈에 자연스럽게 편입하는 쪽으로
판단했다(#211에서 결정, 여기서 구현하지 않음).
"""

import os
import platform
import subprocess

import platformdirs


def get_default_download_dir() -> str:
    """기본 다운로드 폴더 — `QStandardPaths.writableLocation(DownloadLocation)` 대체."""
    return platformdirs.user_downloads_dir()


def _start_detached(args: list[str]) -> bool:
    """`QProcess.startDetached`와 같은 의미로 프로세스를 띄운다 — 시작 성공만 본다.

    원본(Qt)도 프로세스가 끝나기를 기다리지 않는다(detached) — 여기서
    `subprocess.run`으로 기다리면 파일탐색기 프로세스 기동이 느린 환경에서
    호출자(장차 JS→Python 호출 스레드)를 붙잡아 둘 수 있다. `#136`/`#137`/
    PR #135 이후 이 프로젝트가 계속 지켜온 "외부 I/O를 기다리다 UI를 얼리지
    않는다" 원칙을 그대로 따른다. 성공 판정도 원본과 동일하게 "프로세스
    시작 자체가 됐는가"이지 "그 프로세스가 0으로 끝났는가"가 아니다.
    """
    try:
        subprocess.Popen(args)  # noqa: S603 -- 인자는 전부 이 모듈이 하드코딩한 실행파일명 + 경로
        return True
    except OSError:
        return False


def open_folder(path: str) -> bool:
    """폴더를 연다 — `QDesktopServices.openUrl(QUrl.fromLocalFile(path))` 대체.

    `config/dialog.py`의 `openLogsFolder`가 오늘 이 패턴으로 로그 폴더를 연다.
    `os.startfile`은 Windows에만 있는 속성이라(다른 OS에서는 AttributeError,
    `#181`이 실제로 이걸로 죽었었다) 반드시 분기해야 한다.

    Returns
    -------
    성공 여부. 실패해도 예외를 던지지 않는다(호출자가 안내 문구를 결정한다 —
    `#181`/`config/dialog.py`와 동일한 "실패는 False로 알린다" 계약).
    """
    system = platform.system()
    if system == "Windows":
        try:
            os.startfile(path)  # noqa: S606 -- Windows 전용 API, 분기로 보호됨
            return True
        except OSError:
            return False
    if system == "Darwin":
        return _start_detached(["open", path])
    if system == "Linux":
        return _start_detached(["xdg-open", path])
    return False


def reveal_in_file_manager(path: str) -> bool:
    """파일탐색기에서 경로를 보여준다 — `content/widget.py`의 `requestOpenDir` 그대로 이식.

    경로가 파일이면 파일탐색기에서 그 파일이 선택된 채로 열린다(3-OS 수동 분기,
    Qt에도 대응 API가 없어 원래부터 이 방식이었다). 폴더면 `open_folder()`와
    동일하게 그냥 연다.

    Returns
    -------
    성공 여부. 원본과 동일하게 실패해도 예외를 던지지 않는다.
    """
    if not os.path.isfile(path):
        return open_folder(path)

    system = platform.system()
    native_path = os.path.normpath(path)  # QDir.toNativeSeparators(path) 대응
    if system == "Windows":
        return _start_detached(["explorer.exe", "/select,", native_path])
    if system == "Darwin":
        return _start_detached(["open", "-R", native_path])
    if system == "Linux":
        return _start_detached(["nautilus", native_path])
    return False
