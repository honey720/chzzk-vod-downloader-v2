"""임시 진단 (#92 CI 전용, 머지 전 삭제) — 리눅스 동봉 ffmpeg의 exit -11 원인 분리.

파일/파이프 입력 × TS/fMP4 컨테이너 × faststart 유무 매트릭스를 실행해
어떤 조합이 죽는지 한 번에 보고한다. 항상 실패로 끝나 결과를 CI 로그에 남긴다.
"""

import subprocess

from core.utils.ffmpeg import get_ffmpeg_exe


def _gen(exe, path, container_args):
    return subprocess.run(
        [exe, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=duration=1:size=64x64:rate=10",
         "-c:v", "libx264", "-preset", "ultrafast", *container_args, str(path)],
        capture_output=True, text=True,
    )


def test_diagnose_linux_sigsegv(tmp_path):
    exe = get_ffmpeg_exe()
    lines = [subprocess.run([exe, "-version"], capture_output=True, text=True).stdout.splitlines()[0]]

    ts = tmp_path / "in.ts"
    fmp4 = tmp_path / "in.mp4"
    g1 = _gen(exe, ts, ["-f", "mpegts"])
    g2 = _gen(exe, fmp4, ["-movflags", "frag_keyframe+empty_moov", "-f", "mp4"])
    lines.append(f"gen ts rc={g1.returncode} {g1.stderr[-120:]!r} / gen fmp4 rc={g2.returncode} {g2.stderr[-120:]!r}")

    cases = []
    for src, name in [(ts, "ts"), (fmp4, "fmp4")]:
        for fast in (True, False):
            flags = ["-movflags", "+faststart"] if fast else []
            # 파일 입력
            out = tmp_path / f"f_{name}_{fast}.mp4"
            r = subprocess.run(
                [exe, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                 "-c", "copy", *flags, "-f", "mp4", str(out)],
                capture_output=True, text=True,
            )
            cases.append(f"file  {name:<5} fast={fast}: rc={r.returncode} {r.stderr[-100:]!r}")
            # 파이프 입력
            out2 = tmp_path / f"p_{name}_{fast}.mp4"
            p = subprocess.Popen(
                [exe, "-hide_banner", "-loglevel", "error", "-y", "-i", "pipe:0",
                 "-c", "copy", *flags, "-f", "mp4", str(out2)],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            _, err = p.communicate(src.read_bytes(), timeout=60)
            cases.append(f"pipe  {name:<5} fast={fast}: rc={p.returncode} {err[-100:]!r}")

    lines.extend(cases)
    raise AssertionError("DIAG RESULT\n" + "\n".join(lines))
