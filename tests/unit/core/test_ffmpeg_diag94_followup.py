"""임시 진단 3차 (#94 후속 확인 전용 — 이 브랜치는 머지하지 않고 닫는다).

오너 요청 두 가지:
1. ubuntu-22.04 재현 여부 — 러너 OS는 워크플로우(금지 구역) 수정 없이는 못
   바꾸므로 두 대체 실험으로 판정한다:
   - docker ubuntu:22.04/20.04 컨테이너에서 동봉본 실행 (userland 가설 판정.
     단 컨테이너는 호스트 커널을 공유한다 — 정적 바이너리라 userland보다
     커널·ASLR 상호작용이 유력 변수)
   - ASLR 엔트로피 실험: ubuntu-24.04 계열 커널은 vm.mmap_rnd_bits=32가
     기본이고 22.04는 28이다. 값을 28로 낮춰 재실행이 살아나면
     "22.04에서는 정상"과 등가의 판정 + 근본 원인 특정이 된다.
     (setarch -R로 ASLR 완전 비활성 실험도 병행)
2. SDT 방아쇠 확인 — PID 0x11(SDT) 패킷만 제거한 TS로 같은 명령 실행.
"""

import os
import subprocess
import sys

import pytest

from core.utils.ffmpeg import get_ffmpeg_exe

GEN = [
    "-f", "lavfi", "-i", "testsrc2=duration=1:size=64x64:rate=10",
    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
    "-f", "mpegts",
]

TS_PACKET = 188
SDT_PID = 0x11


def _run(cmd, timeout=300, **kw):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
        return r.returncode, (r.stderr or "").strip()[-100:]
    except Exception as e:  # noqa: BLE001 — 진단 보고용
        return None, f"실행실패: {e}"


def _ff(exe, args, timeout=300):
    rc, err = _run([exe, "-hide_banner", "-loglevel", "error", "-y", *args], timeout=timeout)
    return f"rc={rc} {err!r}"


def _strip_pid(src_path, dst_path, pid):
    """TS에서 특정 PID 패킷만 제거한다. (제거 전/후 패킷 수 반환)"""
    data = open(src_path, "rb").read()
    kept = bytearray()
    total = removed = 0
    for i in range(0, len(data) - TS_PACKET + 1, TS_PACKET):
        pkt = data[i : i + TS_PACKET]
        if pkt[0] != 0x47:
            kept.extend(pkt)
            continue
        total += 1
        if ((pkt[1] & 0x1F) << 8) | pkt[2] == pid:
            removed += 1
            continue
        kept.extend(pkt)
    open(dst_path, "wb").write(bytes(kept))
    return total, removed


def test_diag94_followup(tmp_path):
    if not sys.platform.startswith("linux"):
        pytest.skip("리눅스 CI 전용 진단")

    lines = []
    exe = get_ffmpeg_exe()
    rc, _ = _run([exe, "-version"])
    ver = subprocess.run([exe, "-version"], capture_output=True, text=True).stdout.splitlines()[0]
    lines.append(f"[A] 동봉본: {ver[:90]}")

    ts = str(tmp_path / "in.ts")
    lines.append(f"[A] gen: {_ff(exe, [*GEN, ts])}")
    lines.append(f"[A] 대조 재현: {_ff(exe, ['-i', ts, '-c', 'copy', str(tmp_path / 'a.mp4')])}")

    # 2. SDT 제거 TS
    nosdt = str(tmp_path / "nosdt.ts")
    total, removed = _strip_pid(ts, nosdt, SDT_PID)
    lines.append(f"[SDT] 패킷 {total}개 중 SDT(PID 0x11) {removed}개 제거")
    lines.append(f"[SDT] SDT 없는 TS 재현: {_ff(exe, ['-i', nosdt, '-c', 'copy', str(tmp_path / 's.mp4')])}")

    # 1-a. ASLR 실험
    rc, out = _run(["sysctl", "-n", "vm.mmap_rnd_bits"])
    lines.append(f"[ASLR] 현재 vm.mmap_rnd_bits={out if rc == 0 else '확인불가'}")
    rc, err = _run(["setarch", os.uname().machine, "-R", exe, "-hide_banner", "-loglevel",
                    "error", "-y", "-i", ts, "-c", "copy", str(tmp_path / "r.mp4")])
    lines.append(f"[ASLR] setarch -R(ASLR off) 재현: rc={rc} {err!r}")
    rc28, _ = _run(["sudo", "sysctl", "-w", "vm.mmap_rnd_bits=28"])
    if rc28 == 0:
        lines.append(f"[ASLR] rnd_bits=28(22.04 기본)로 변경 후: {_ff(exe, ['-i', ts, '-c', 'copy', str(tmp_path / 'b28.mp4')])}")
        _run(["sudo", "sysctl", "-w", "vm.mmap_rnd_bits=32"])
        lines.append(f"[ASLR] rnd_bits=32 복원 후: {_ff(exe, ['-i', ts, '-c', 'copy', str(tmp_path / 'b32.mp4')])}")
    else:
        lines.append("[ASLR] sysctl 변경 불가")

    # 1-b. docker ubuntu:22.04 / 20.04 (userland 판정 — 호스트 커널 공유 주의)
    exe_dir = os.path.dirname(exe)
    exe_name = os.path.basename(exe)
    for image in ("ubuntu:22.04", "ubuntu:20.04"):
        rc, err = _run(
            ["docker", "run", "--rm",
             "-v", f"{exe_dir}:/ff:ro", "-v", f"{tmp_path}:/work",
             image, f"/ff/{exe_name}", "-hide_banner", "-loglevel", "error", "-y",
             "-i", "/work/in.ts", "-c", "copy", f"/work/out_{image.replace(':', '')}.mp4"],
            timeout=600,
        )
        lines.append(f"[22.04?] docker {image}: rc={rc} {err!r}")

    raise AssertionError("DIAG94-3 RESULT\n" + "\n".join(lines))
