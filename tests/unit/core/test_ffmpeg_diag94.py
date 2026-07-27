"""임시 진단 (#94 CI 전용, 머지 전 삭제) — TS demux SIGSEGV의 원인 판정.

리눅스 CI에서만 의미가 있다. 파이프·faststart·자체 코드를 전부 배제한
최소 명령(`ffmpeg -i in.ts -c copy out.mp4`)을 여러 빌드로 교차 실행한다:

A. 동봉본(imageio-ffmpeg 0.6.0, jvs 7.0.2-static) — 최소 재현
B. 러너 시스템 ffmpeg — 같은 환경·같은 입력·다른 빌드 (환경 vs 빌드 판정)
C. 시스템 ffmpeg가 만든 TS를 동봉본이 읽기 — 입력 특성 배제
D. imageio-ffmpeg 0.5.1의 리눅스 동봉본 — 구버전 빌드 재현 여부
E. johnvansickle 최신 release/git 정적 빌드 — 업스트림 수정 여부

항상 실패로 끝나 결과 표를 CI 로그에 남긴다.
"""

import lzma
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import zipfile

import pytest
import requests

from core.utils.ffmpeg import get_ffmpeg_exe

GEN = [
    "-f", "lavfi", "-i", "testsrc2=duration=1:size=64x64:rate=10",
    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
    "-f", "mpegts",
]


def _run(exe, args, timeout=120):
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


def _fetch_wheel_ffmpeg(tmp, version):
    """PyPI에서 imageio-ffmpeg 휠(manylinux x86_64)을 받아 동봉 바이너리를 꺼낸다."""
    meta = requests.get(f"https://pypi.org/pypi/imageio-ffmpeg/{version}/json", timeout=60).json()
    url = next(u["url"] for u in meta["urls"] if "manylinux" in u["filename"] and "x86_64" in u["filename"])
    whl = tmp / f"iio-{version}.whl"
    whl.write_bytes(requests.get(url, timeout=300).content)
    with zipfile.ZipFile(whl) as z:
        name = next(n for n in z.namelist() if "/binaries/ffmpeg-linux" in n)
        z.extract(name, tmp)
    exe = tmp / name
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return str(exe)


def _fetch_jvs(tmp, flavor):
    """johnvansickle 정적 빌드(release|git)를 받아 ffmpeg 바이너리를 꺼낸다."""
    url = f"https://johnvansickle.com/ffmpeg/builds/ffmpeg-{flavor}-amd64-static.tar.xz"
    r = requests.get(url, timeout=600)
    r.raise_for_status()
    xz = tmp / f"jvs-{flavor}.tar.xz"
    xz.write_bytes(r.content)
    with lzma.open(xz) as f, tarfile.open(fileobj=f) as tar:
        member = next(m for m in tar.getmembers() if m.name.endswith("/ffmpeg"))
        tar.extract(member, tmp)
        exe = tmp / member.name
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return str(exe)


def test_diagnose_ts_sigsegv_builds(tmp_path):
    if not sys.platform.startswith("linux"):
        pytest.skip("리눅스 CI 전용 진단")

    lines = []
    bundled = get_ffmpeg_exe()
    lines.append(f"[A] 동봉본: {_version(bundled)}")

    ts = str(tmp_path / "in.ts")
    lines.append(f"[A] gen ts: {_run(bundled, [*GEN, ts])}")
    out = str(tmp_path / "out.mp4")
    lines.append(f"[A] 최소재현 bundled -i in.ts -c copy out.mp4: {_run(bundled, ['-i', ts, '-c', 'copy', out])}")

    system = shutil.which("ffmpeg")
    if system and os.path.realpath(system) != os.path.realpath(bundled):
        lines.append(f"[B] 시스템: {_version(system)}")
        lines.append(f"[B] system  -i in.ts -c copy: {_run(system, ['-i', ts, '-c', 'copy', str(tmp_path / 'o_sys.mp4')])}")
        ts2 = str(tmp_path / "in_sys.ts")
        lines.append(f"[C] system이 만든 ts: {_run(system, [*GEN, ts2])}")
        lines.append(f"[C] bundled가 그 ts 읽기: {_run(bundled, ['-i', ts2, '-c', 'copy', str(tmp_path / 'o_cross.mp4')])}")
    else:
        lines.append(f"[B/C] 시스템 ffmpeg 없음 (which={system})")

    try:
        old = _fetch_wheel_ffmpeg(tmp_path, "0.5.1")
        lines.append(f"[D] iio 0.5.1 동봉본: {_version(old)}")
        lines.append(f"[D] 0.5.1  -i in.ts -c copy: {_run(old, ['-i', ts, '-c', 'copy', str(tmp_path / 'o_051.mp4')])}")
    except Exception as e:  # noqa: BLE001
        lines.append(f"[D] 0.5.1 확보 실패: {e}")

    for flavor in ("release", "git"):
        try:
            jvs = _fetch_jvs(tmp_path, flavor)
            lines.append(f"[E] jvs {flavor}: {_version(jvs)}")
            lines.append(f"[E] jvs {flavor} -i in.ts -c copy: {_run(jvs, ['-i', ts, '-c', 'copy', str(tmp_path / f'o_{flavor}.mp4')])}")
        except Exception as e:  # noqa: BLE001
            lines.append(f"[E] jvs {flavor} 확보 실패: {e}")

    raise AssertionError("DIAG94 RESULT\n" + "\n".join(lines))
