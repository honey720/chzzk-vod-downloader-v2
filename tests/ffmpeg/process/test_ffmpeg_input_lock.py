"""ffmpeg 입력 잠금(core/utils/ffmpeg.py ``input_lock``) 테스트 (#600).

실제 ffmpeg를 실행한다. 입력은 lavfi로 테스트 안에서 만든다 — 저장소에 영상 파일을 두지 않는다.

핵심 계약:
- 잠근 형식 밖의 컨테이너를 pipe remux에 넣으면 실패한다
- 잠근 코덱 밖의 영상을 경계 다시 인코딩에 넣으면 실패한다
- concat 목록 안의 허용 밖 형식은 열리지 않는다
- 잠금을 붙여도 산출물의 패킷(framecrc)은 잠금 없는 ffmpeg의 산출물과 같다
"""

import subprocess
from pathlib import Path

import pytest

import core.sections.hybrid as hybrid_module
import core.utils.ffmpeg as ffmpeg_module
from core.media.mp4 import read_mp4_index
from core.sections.cut import CutFrames, CutPiece
from core.sections.hybrid import CutError, cut_frames_from_mp4
from core.utils.ffmpeg import (
    RemuxError,
    get_ffmpeg_exe,
    input_lock,
    read_in_chunks,
    remux_stream,
    run_ffmpeg,
)

# 테스트 입력의 lavfi 소스 — 320x240 · 30fps · 2초. 영상 + 오디오
_SOURCE = [
    "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=2",
    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
    "-pix_fmt", "yuv420p", "-c:a", "aac",
]  # fmt: skip
# h264 인코딩 옵션 — 키프레임은 0초 · 1초에만
_H264 = [
    "-c:v", "libx264", "-preset", "veryfast", "-bf", "2",
    "-x264-params", "keyint=30:min-keyint=30:scenecut=0",
]  # fmt: skip


def _ffmpeg(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """잠금 없는 ffmpeg를 실행한다 — 입력을 만들고 산출물을 읽는 쪽이다.

    리눅스 동봉본의 GCONV_PATH 가드(#97)는 제품과 같이 붙인다.
    """
    exe = get_ffmpeg_exe()
    return subprocess.run(
        [exe, "-hide_banner", "-v", "error", "-y", *args],
        cwd=cwd,
        env=ffmpeg_module._subprocess_env(exe),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _make(path: Path, *args: str) -> Path:
    done = _ffmpeg(*_SOURCE, *args, str(path))
    assert done.returncode == 0, done.stderr
    return path


def _has_encoder(name: str) -> bool:
    done = _ffmpeg("-encoders")
    return f" {name} " in done.stdout


def _framecrc(path: Path) -> list[str]:
    """파일의 모든 패킷을 디코드 없이 읽은 framecrc 줄 — 잠금 없는 ffmpeg로 읽는다."""
    done = _ffmpeg("-i", str(path), "-map", "0", "-c", "copy", "-f", "framecrc", "-")
    assert done.returncode == 0, done.stderr
    return [line for line in done.stdout.splitlines() if not line.startswith("#")]


def _mp4_frames(path: Path) -> CutFrames:
    def read(offset: int, size: int) -> bytes:
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read(size)

    return cut_frames_from_mp4(read_mp4_index(read))


@pytest.fixture(scope="module")
def sources(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """같은 lavfi 소스를 형식 · 코덱만 바꿔 쓴 입력들."""
    folder = tmp_path_factory.mktemp("input_lock")
    made = {
        "fmp4": _make(
            folder / "source_fmp4.mp4", *_H264, "-movflags", "frag_keyframe+empty_moov", "-f", "mp4"
        ),
        "ts": _make(folder / "source.ts", *_H264, "-f", "mpegts"),
        "mp4": _make(folder / "source.mp4", *_H264, "-f", "mp4"),
        "mkv": _make(folder / "source.mkv", *_H264, "-f", "matroska"),
        "mjpeg": _make(folder / "source_mjpeg.mp4", "-c:v", "mjpeg", "-f", "mp4"),
    }
    if _has_encoder("libx265"):
        made["hevc"] = _make(
            folder / "source_hevc.mp4", "-c:v", "libx265", "-preset", "ultrafast", "-f", "mp4"
        )
    return made


# ================================================================ 형식 잠금 — pipe remux


@pytest.mark.parametrize(
    ("given", "input_format"),
    [("mkv", "mov"), ("mkv", "mpegts"), ("ts", "mov"), ("fmp4", "mpegts")],
)
def test_remux_stream_fails_when_the_container_is_not_the_locked_format(
    sources: dict[str, Path], tmp_path: Path, given: str, input_format: str
) -> None:
    """remux_stream에 잠근 형식이 아닌 컨테이너를 넣으면 RemuxError이고 산출물이 없어야 한다.

    lavfi로 만든 mkv · TS · fMP4(h264 + aac)를 다른 형식으로 잠가 넣음
    -> RemuxError, 산출물 없음
    """
    out = tmp_path / "out.mp4"

    with pytest.raises(RemuxError):
        remux_stream(read_in_chunks(str(sources[given])), str(out), input_format=input_format)

    assert not out.exists()


# ================================================================ 코덱 잠금 — 경계 다시 인코딩


@pytest.mark.parametrize("codec", ["hevc", "mjpeg"])
def test_boundary_reencode_fails_when_the_video_codec_is_not_locked(
    sources: dict[str, Path], tmp_path: Path, codec: str
) -> None:
    """경계 다시 인코딩에 h264 · aac가 아닌 영상이 든 mp4를 넣으면 CutError여야 한다.

    lavfi로 만든 HEVC(libx265) · MJPEG 영상 mp4. 프레임 정보 · SPS는 같은 소스의 h264 mp4에서 읽음
    -> CutError. 같은 명령에 h264 mp4를 넣으면 조각이 생긴다
    """
    if codec not in sources:
        pytest.skip(f"이 ffmpeg에는 {codec} 테스트 입력을 만드는 인코더가 없다")
    h264 = sources["mp4"]
    frames = _mp4_frames(h264)
    video = hybrid_module._read_params(str(h264), 1)
    piece = CutPiece("head", 5, 30)

    with pytest.raises(CutError):
        hybrid_module._run(
            hybrid_module._encode_command(str(sources[codec]), frames, piece, video, "locked.mp4"),
            hybrid_module._ENCODE_TIMEOUT,
            str(tmp_path),
        )
    hybrid_module._run(
        hybrid_module._encode_command(str(h264), frames, piece, video, "h264.mp4"),
        hybrid_module._ENCODE_TIMEOUT,
        str(tmp_path),
    )

    assert (tmp_path / "h264.mp4").stat().st_size > 0


# ================================================================ concat 목록 안의 형식


def test_concat_join_fails_when_the_listed_file_is_not_the_locked_format(
    sources: dict[str, Path], tmp_path: Path
) -> None:
    """concat 목록의 첫 파일이 잠근 형식(mov) 밖이면 잇기가 0이 아닌 코드로 끝나야 한다.

    목록: mkv(h264 + aac) → mp4(h264 + aac). 구간 잇기와 같은 입력 옵션
    (-protocol_whitelist file,pipe,fd + input_lock("concat") + -safe 0)
    -> 종료 코드 != 0, stderr에 "not on whitelist". 목록을 mp4 → mp4로 바꾸면 종료 코드 0
    """
    for name in ("mkv", "mp4"):
        (tmp_path / f"piece.{name}").write_bytes(sources[name].read_bytes())
    (tmp_path / "bad.txt").write_text(
        "ffconcat version 1.0\nfile piece.mkv\nfile piece.mp4\n", encoding="utf-8", newline="\n"
    )
    (tmp_path / "good.txt").write_text(
        "ffconcat version 1.0\nfile piece.mp4\nfile piece.mp4\n", encoding="utf-8", newline="\n"
    )

    def join(listed: str) -> subprocess.CompletedProcess[str]:
        return run_ffmpeg(
            [
                "-v", "warning", "-y",
                "-protocol_whitelist", "file,pipe,fd", *input_lock("concat"), "-safe", "0",
                "-i", listed, "-map", "0:v:0", "-c", "copy", "-f", "mp4", "out.mp4",
            ],
            timeout=60,
            cwd=str(tmp_path),
        )  # fmt: skip

    bad, good = join("bad.txt"), join("good.txt")

    assert bad.returncode != 0
    assert "not on whitelist" in bad.stderr
    assert good.returncode == 0, good.stderr


# ================================================================ 잠금 전후 산출물


@pytest.mark.parametrize(("given", "input_format"), [("fmp4", "mov"), ("ts", "mpegts")])
def test_locked_remux_writes_the_same_packets_as_an_unlocked_ffmpeg(
    sources: dict[str, Path], tmp_path: Path, given: str, input_format: str
) -> None:
    """잠근 remux_stream의 산출물은 잠금 없는 ffmpeg로 같은 바이트를 remux한 산출물과 패킷이 같아야 한다.

    lavfi로 만든 fMP4 · TS(h264 + aac). 잠금 전: ffmpeg -i pipe:0 -c copy -f mp4(테스트가 직접 실행)
    -> 두 산출물의 framecrc 줄(영상 · 오디오의 모든 패킷)이 같다
    """
    locked = tmp_path / "locked.mp4"
    unlocked = tmp_path / "unlocked.mp4"
    remux_stream(read_in_chunks(str(sources[given])), str(locked), input_format=input_format)
    exe = get_ffmpeg_exe()
    done = subprocess.run(
        [exe, "-hide_banner", "-v", "error", "-y", "-i", "pipe:0",
         "-c", "copy", "-f", "mp4", str(unlocked)],
        input=sources[given].read_bytes(),
        capture_output=True,
        env=ffmpeg_module._subprocess_env(exe),
    )  # fmt: skip
    assert done.returncode == 0, done.stderr

    locked_lines, unlocked_lines = _framecrc(locked), _framecrc(unlocked)

    assert len(locked_lines) > 60  # 영상 60프레임 + 오디오 패킷
    assert locked_lines == unlocked_lines


def test_input_lock_rejects_an_unknown_format() -> None:
    """input_lock에 잠금 표에 없는 형식을 넘기면 ValueError여야 한다.

    input_lock("matroska")
    -> ValueError
    """
    with pytest.raises(ValueError):
        input_lock("matroska")
