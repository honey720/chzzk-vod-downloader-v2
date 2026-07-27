"""임시 진단 2차 (#92 CI 전용, 머지 전 삭제) — TS→MP4 SIGSEGV 조건 좁히기.

1차 결과: linux 7.0.2-static에서 (lavfi h264 영상 단독) TS→MP4 -c copy가
파일/파이프·faststart 유무 불문 전부 rc=-11. fMP4→MP4는 정상.
2차: 실제 치지직 TS와 유사한 h264+AAC 먹싱 TS, 입력 포맷 명시, 출력 변형을 시험한다.
"""

import subprocess

from core.utils.ffmpeg import get_ffmpeg_exe

GEN_BASE = [
    "-f", "lavfi", "-i", "testsrc2=duration=1:size=64x64:rate=10",
    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
]


def test_diagnose_ts_sigsegv_round2(tmp_path):
    exe = get_ffmpeg_exe()
    lines = [subprocess.run([exe, "-version"], capture_output=True, text=True).stdout.splitlines()[0]]

    def run(tag, args):
        r = subprocess.run([exe, "-hide_banner", "-loglevel", "error", "-y", *args],
                           capture_output=True, text=True, timeout=60)
        lines.append(f"{tag}: rc={r.returncode} {r.stderr[-100:]!r}")
        return r.returncode

    av_ts = str(tmp_path / "av.ts")
    v_ts = str(tmp_path / "v.ts")
    run("gen h264+aac ts", [*GEN_BASE, "-c:v", "libx264", "-preset", "ultrafast",
                            "-c:a", "aac", "-shortest", "-f", "mpegts", av_ts])
    run("gen h264-only ts", [*GEN_BASE[:4], "-c:v", "libx264", "-preset", "ultrafast",
                             "-f", "mpegts", v_ts])

    run("av_ts -> mp4 (file)", ["-i", av_ts, "-c", "copy", "-movflags", "+faststart",
                                "-f", "mp4", str(tmp_path / "o1.mp4")])
    run("av_ts -> mp4 (explicit -f mpegts in)", ["-f", "mpegts", "-i", av_ts, "-c", "copy",
                                                 "-f", "mp4", str(tmp_path / "o2.mp4")])
    run("v_ts -> mp4 (explicit -f mpegts in)", ["-f", "mpegts", "-i", v_ts, "-c", "copy",
                                                "-f", "mp4", str(tmp_path / "o3.mp4")])
    run("v_ts -> mkv (file)", ["-i", v_ts, "-c", "copy", str(tmp_path / "o4.mkv")])
    run("v_ts -> null demux only", ["-i", v_ts, "-c", "copy", "-f", "null", "-"])
    run("av_ts -> mp4 (pipe)", ["-i", "pipe:0", "-c", "copy", "-movflags", "+faststart",
                                "-f", "mp4", str(tmp_path / "o5.mp4")]) if False else None
    # 파이프 케이스는 별도 (stdin 공급)
    p = subprocess.Popen([exe, "-hide_banner", "-loglevel", "error", "-y", "-i", "pipe:0",
                          "-c", "copy", "-movflags", "+faststart", "-f", "mp4",
                          str(tmp_path / "o5.mp4")],
                         stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    _, err = p.communicate(open(av_ts, "rb").read(), timeout=60)
    lines.append(f"av_ts -> mp4 (pipe): rc={p.returncode} {err[-100:]!r}")

    raise AssertionError("DIAG2 RESULT\n" + "\n".join(lines))
