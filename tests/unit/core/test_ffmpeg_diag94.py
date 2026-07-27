"""임시 진단 2차 (#94 CI 전용, 머지 전 삭제) — 빌드 결함 vs 환경 상호작용 판정.

1차 결과: jvs 정적 빌드 4.2.2(2019)·7.0.2·git master 전부 최소 명령
(`-i in.ts -c copy out.mp4`)에서 rc=-11. 러너에 시스템 ffmpeg 없음.
5년치 빌드가 전부 같은 지점에서 죽는 것은 단일 빌드 버그로 보기 어렵다.

2차:
B'. distro ffmpeg를 apt로 설치해 같은 명령 실행 — 같은 환경·다른 빌드 계열
F. BtbN 정적 빌드(다른 툴체인) — 정적 빌드 일반의 문제인지 jvs 특유인지
G. 동봉본 -v trace로 크래시 직전 로그 확보 — 업스트림 보고용
"""

import os
import shutil
import stat
import subprocess
import sys
import tarfile

import pytest
import requests

from core.utils.ffmpeg import get_ffmpeg_exe

GEN = [
    "-f", "lavfi", "-i", "testsrc2=duration=1:size=64x64:rate=10",
    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
    "-f", "mpegts",
]


def _run(exe, args, timeout=180):
    try:
        r = subprocess.run([exe, "-hide_banner", "-loglevel", "error", "-y", *args],
                           capture_output=True, text=True, timeout=timeout)
        return f"rc={r.returncode} {r.stderr.strip()[-80:]!r}"
    except Exception as e:  # noqa: BLE001 — 진단 보고용
        return f"실행실패: {e}"


def _version(exe):
    try:
        out = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=30)
        return out.stdout.splitlines()[0][:90]
    except Exception as e:  # noqa: BLE001
        return f"버전확인실패: {e}"


def _fetch_btbn(tmp):
    """BtbN 최신 릴리즈 정적 빌드(linux64-gpl)를 받아 ffmpeg를 꺼낸다."""
    url = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
           "ffmpeg-master-latest-linux64-gpl.tar.xz")
    r = requests.get(url, timeout=600)
    r.raise_for_status()
    xz = tmp / "btbn.tar.xz"
    xz.write_bytes(r.content)
    with tarfile.open(xz, mode="r:xz") as tar:
        member = next(m for m in tar.getmembers() if m.name.endswith("bin/ffmpeg"))
        tar.extract(member, tmp)
        exe = tmp / member.name
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return str(exe)


def test_diagnose_ts_sigsegv_round2(tmp_path):
    if not sys.platform.startswith("linux"):
        pytest.skip("리눅스 CI 전용 진단")

    lines = []
    bundled = get_ffmpeg_exe()
    ts = str(tmp_path / "in.ts")
    lines.append(f"[A] 동봉본: {_version(bundled)}")
    lines.append(f"[A] gen ts: {_run(bundled, [*GEN, ts])}")
    lines.append(f"[A] 재현: {_run(bundled, ['-i', ts, '-c', 'copy', str(tmp_path / 'o.mp4')])}")

    # G. 크래시 직전 trace 로그 (업스트림 보고용)
    try:
        r = subprocess.run(
            [bundled, "-hide_banner", "-v", "trace", "-i", ts, "-c", "copy",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=120,
        )
        tail = [ln for ln in r.stderr.splitlines() if ln.strip()][-12:]
        lines.append(f"[G] trace rc={r.returncode}, 마지막 로그:")
        lines.extend(f"[G]   {ln[:110]}" for ln in tail)
    except Exception as e:  # noqa: BLE001
        lines.append(f"[G] trace 실패: {e}")

    # B'. distro ffmpeg (러너는 passwordless sudo)
    try:
        subprocess.run(["sudo", "apt-get", "install", "-y", "-qq", "ffmpeg"],
                       capture_output=True, timeout=600, check=True)
        distro = shutil.which("ffmpeg")
        lines.append(f"[B'] distro: {_version(distro)}")
        lines.append(f"[B'] distro 재현: {_run(distro, ['-i', ts, '-c', 'copy', str(tmp_path / 'o_d.mp4')])}")
        ts2 = str(tmp_path / "in_d.ts")
        lines.append(f"[C] distro가 만든 ts: {_run(distro, [*GEN, ts2])}")
        lines.append(f"[C] 동봉본이 그 ts 읽기: {_run(bundled, ['-i', ts2, '-c', 'copy', str(tmp_path / 'o_x.mp4')])}")
    except Exception as e:  # noqa: BLE001
        lines.append(f"[B'] distro 설치 실패: {e}")

    # F. BtbN 정적 빌드 (다른 툴체인)
    try:
        btbn = _fetch_btbn(tmp_path)
        lines.append(f"[F] BtbN: {_version(btbn)}")
        lines.append(f"[F] BtbN 재현: {_run(btbn, ['-i', ts, '-c', 'copy', str(tmp_path / 'o_b.mp4')])}")
    except Exception as e:  # noqa: BLE001
        lines.append(f"[F] BtbN 확보 실패: {e}")

    try:
        glibc = subprocess.run(["ldd", "--version"], capture_output=True, text=True, timeout=30)
        lines.append(f"[env] glibc: {glibc.stdout.splitlines()[0][:90]}")
    except Exception as e:  # noqa: BLE001
        lines.append(f"[env] glibc 확인 실패: {e}")
    lines.append(f"[env] uname: {os.uname().release} {os.uname().machine}")

    raise AssertionError("DIAG94-2 RESULT\n" + "\n".join(lines))
