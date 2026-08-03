"""macOS .app 번들 실행 시 cwd 계측 (#157 — 머지하지 않는 draft PR 전용).

기본 저장 경로가 os.getcwd()인 앱에서, Finder/Dock 실행(.app → Launch
Services 경유)의 cwd가 실제로 무엇이 되는지를 CI의 macOS 러너에서
실측한다. 초소형 .app 번들을 만들어 `open -W`로 실행하고 그 프로세스의
pwd를 파일로 회수한다. 대조군으로 같은 실행 파일의 직접 exec cwd,
참고로 QStandardPaths의 표준 폴더 값도 수집한다.

결과는 경고(warnings)로 내보낸다 — pytest -q에서도 경고 요약에 값이
남아 CI 로그로 회수할 수 있다.
"""

import os
import plistlib
import subprocess
import sys
import time
import warnings

import pytest


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Launch Services 계측 전용")
def test_probe_app_bundle_cwd(tmp_path):
    """Launch Services로 실행된 .app의 cwd와 대조군·표준 폴더를 계측한다."""
    app = tmp_path / "CwdProbe.app"
    macos_dir = app / "Contents" / "MacOS"
    macos_dir.mkdir(parents=True)
    result = tmp_path / "cwd_via_open.txt"

    exe = macos_dir / "CwdProbe"
    exe.write_text(f'#!/bin/sh\npwd > "{result}"\n')
    exe.chmod(0o755)
    with open(app / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump(
            {
                "CFBundleExecutable": "CwdProbe",
                "CFBundleIdentifier": "dev.cvdv2.cwdprobe",
                "CFBundleName": "CwdProbe",
                "CFBundlePackageType": "APPL",
            },
            f,
        )

    # 대조군: 셸에서 직접 exec — 부모 cwd를 물려받는 경로
    direct = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=30, cwd=str(tmp_path)
    )
    direct_cwd = result.read_text().strip() if result.exists() else "(미기록)"
    result.unlink(missing_ok=True)

    # 본 계측: Launch Services 경유 (Finder/Dock 실행과 같은 스폰 경로)
    launched = subprocess.run(
        ["open", "-W", str(app)], capture_output=True, text=True, timeout=60
    )
    deadline = time.time() + 15
    while not result.exists() and time.time() < deadline:
        time.sleep(0.2)
    open_cwd = result.read_text().strip() if result.exists() else "(미기록)"

    # 참고: Qt가 보는 표준 폴더 (개선 방향 후보 판단용)
    from PySide6.QtCore import QStandardPaths

    downloads = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
    movies = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.MoviesLocation)

    warnings.warn(
        "MACOS_CWD_PROBE | "
        f"open(LaunchServices) cwd={open_cwd!r} (rc={launched.returncode}, "
        f"stderr={launched.stderr.strip()!r}) | "
        f"direct exec cwd={direct_cwd!r} (rc={direct.returncode}) | "
        f"os.access(open_cwd, W_OK)={os.access(open_cwd, os.W_OK) if open_cwd.startswith('/') else 'n/a'} | "
        f"QStandardPaths Download={downloads!r} Movies={movies!r}",
        stacklevel=1,
    )
    assert open_cwd != "(미기록)", f"open 경유 실행이 기록을 남기지 못함: {launched}"
