"""core/utils/ffmpeg.py 검증 (#88) — 경로 탐색·remux 실행.

remux 성공 검증은 실제 ffmpeg 바이너리(imageio-ffmpeg 동봉)를 실행한다 —
입력은 ffmpeg 내장 lavfi 소스로 즉석 생성한 초소형 파일이라 네트워크·외부
픽스처가 없다.
"""

import os
import struct
import subprocess

import pytest

from core.utils.ffmpeg import RemuxError, get_ffmpeg_exe, remux


def _top_level_boxes(path) -> list[str]:
    """MP4 최상위 박스 이름 목록 — moov(전역 인덱스) 위치 검증용."""
    boxes = []
    with open(path, "rb") as f:
        while True:
            header = f.read(8)
            if len(header) < 8:
                break
            size, box_type = struct.unpack(">I4s", header)
            if size < 8:
                break
            boxes.append(box_type.decode("latin1"))
            f.seek(size - 8, os.SEEK_CUR)
    return boxes


def _make_tiny_mp4(path) -> None:
    """ffmpeg 내장 lavfi·네이티브 mpeg4 인코더로 0.2초짜리 검증용 입력을 만든다.

    외부 코덱 라이브러리(libx264 등) 유무에 좌우되지 않도록 네이티브
    인코더만 쓴다 — 이 테스트의 대상은 인코딩이 아니라 remux다.
    """
    subprocess.run(
        [
            get_ffmpeg_exe(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=duration=0.2:size=64x64:rate=10",
            "-c:v",
            "mpeg4",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


# ================================================================ 경로 탐색


def test_get_ffmpeg_exe_returns_existing_executable():
    """탐색 결과는 실재하는 실행 파일 경로여야 한다 (배포 방식 검증의 로컬 관문)."""
    exe = get_ffmpeg_exe()
    assert os.path.isfile(exe)
    assert os.access(exe, os.X_OK)


# ================================================================ remux 실행


def test_remux_produces_mp4_with_leading_moov(tmp_path):
    """remux 산출물은 mp4이고 전역 인덱스(moov)가 mdat보다 앞(선두)에 있어야 한다."""
    src = tmp_path / "src.mp4"
    dst = tmp_path / "dst.mp4"
    _make_tiny_mp4(src)

    remux(str(src), str(dst))

    boxes = _top_level_boxes(dst)
    assert "moov" in boxes and "mdat" in boxes
    # -movflags +faststart의 목적 그 자체 — moov가 mdat보다 앞이다
    assert boxes.index("moov") < boxes.index("mdat")


def test_remux_invalid_input_raises_and_leaves_no_output(tmp_path):
    """미디어가 아닌 입력이면 RemuxError를 던지고 부분 산출물을 남기지 않는다."""
    src = tmp_path / "garbage.bin"
    dst = tmp_path / "dst.mp4"
    src.write_bytes(b"this is not media data" * 100)

    with pytest.raises(RemuxError):
        remux(str(src), str(dst))
    assert not dst.exists()  # 무음 실패 금지·쓰레기 산출물 금지
