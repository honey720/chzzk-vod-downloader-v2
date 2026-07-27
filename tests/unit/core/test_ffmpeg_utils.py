"""core/utils/ffmpeg.py·후처리 정책 검증 (#88·#92) — 경로 탐색·스트림 remux·실패 정책.

remux 검증은 실제 ffmpeg 바이너리(imageio-ffmpeg 동봉)를 실행한다 — 입력은
ffmpeg 내장 lavfi 소스로 즉석 생성한 초소형 MPEG-TS(스트리밍 컨테이너)라
네트워크·외부 픽스처가 없다. 일시정지·중단은 ffmpeg가 stdin을 기다리며 멈춘
실제 상태에서 검증한다(#92 완료 조건). 다운로더의 실패 정책(_remux_streamed:
폴백 없음·세그먼트 보존)은 remux_stream을 스텁으로 바꿔 단위 수준에서 검증한다.
"""

import os
import struct
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

import core.downloaders.base as base_module
from core.downloaders.base import BaseDownloader, PostprocessError
from core.models.download_state import DownloadState
from core.utils.ffmpeg import RemuxError, get_ffmpeg_exe, read_in_chunks, remux_stream


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


def _make_tiny_ts(path) -> None:
    """ffmpeg 내장 lavfi·네이티브 mpeg4 인코더로 1초짜리 MPEG-TS 입력을 만든다.

    TS는 스트리밍 컨테이너라 파이프 공급 검증에 적합하다(실제 hls_aes 경로의
    입력과 동일 형식). 외부 코덱 라이브러리(libx264 등) 유무에 좌우되지
    않도록 네이티브 인코더만 쓴다 — 검증 대상은 인코딩이 아니라 remux다.
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
            "testsrc2=duration=1:size=64x64:rate=10",
            "-c:v",
            "mpeg4",
            "-f",
            "mpegts",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _split_segments(src, outdir, parts: int) -> list[str]:
    """파일을 임의 바이트 경계로 잘라 세그먼트 파일 목록을 만든다.

    fMP4·TS는 바이트 연결이 곧 유효한 스트림이므로, 임의 분할 조각을
    순서대로 공급하면 원본과 같은 스트림이 된다.
    """
    data = src.read_bytes()
    step = len(data) // parts + 1
    paths = []
    for i in range(parts):
        p = outdir / f"{i:04d}.ts"
        p.write_bytes(data[i * step : (i + 1) * step])
        paths.append(str(p))
    return [p for p in paths if os.path.getsize(p)]


# ================================================================ 경로 탐색


def test_get_ffmpeg_exe_returns_existing_executable():
    """탐색 결과는 실재하는 실행 파일 경로여야 한다 (배포 방식 검증의 로컬 관문)."""
    exe = get_ffmpeg_exe()
    assert os.path.isfile(exe)
    assert os.access(exe, os.X_OK)


# ================================================================ 스트림 remux


def test_remux_stream_produces_mp4_with_leading_moov(tmp_path):
    """파이프 공급 산출물은 mp4이고 전역 인덱스(moov)가 mdat보다 앞에 있어야 한다."""
    src = tmp_path / "src.ts"
    dst = tmp_path / "dst.mp4"
    _make_tiny_ts(src)

    remux_stream(read_in_chunks(str(src)), str(dst))

    boxes = _top_level_boxes(dst)
    assert "moov" in boxes and "mdat" in boxes
    # -movflags +faststart의 목적 그 자체 — moov가 mdat보다 앞이다
    assert boxes.index("moov") < boxes.index("mdat")


def test_remux_stream_writes_mp4_regardless_of_extension(tmp_path):
    """출력 컨테이너는 -f mp4로 명시된다 — 확장자가 .mp4가 아니어도 mp4다 (#92)."""
    src = tmp_path / "src.ts"
    dst = tmp_path / "이름을 바꾼 산출물.mkv"
    _make_tiny_ts(src)

    remux_stream(read_in_chunks(str(src)), str(dst))

    assert _top_level_boxes(dst)[0] == "ftyp"  # mp4 계열 시그니처


def test_remux_stream_invalid_input_raises_and_leaves_no_output(tmp_path):
    """미디어가 아닌 스트림이면 RemuxError를 던지고 부분 산출물을 남기지 않는다."""
    dst = tmp_path / "dst.mp4"

    with pytest.raises(RemuxError):
        remux_stream(iter([b"this is not media data" * 100]), str(dst))
    assert not dst.exists()  # 무음 실패 금지·쓰레기 산출물 금지


def test_remux_stream_feed_exception_propagates_and_cleans_output(tmp_path):
    """공급측 예외(중단 신호 등)는 그대로 전파되고 산출물은 남지 않는다."""
    src = tmp_path / "src.ts"
    dst = tmp_path / "dst.mp4"
    _make_tiny_ts(src)

    class FeedAborted(Exception):
        pass

    def feed():
        yield src.read_bytes()[: 64 * 1024]
        raise FeedAborted

    with pytest.raises(FeedAborted):
        remux_stream(feed(), str(dst))
    assert not dst.exists()  # 중단 시 불완전 산출물 미잔류


# ================================================================ 다운로더 실패 정책


class _FakeEngine:
    """_remux_streamed가 참조하는 최소 속성만 가진 가짜 다운로더."""

    def __init__(self, tmp_path):
        self.state = DownloadState.RUNNING
        self.s = SimpleNamespace(
            output_path=str(tmp_path / "out.mp4"),
            merged_segments=0,
            _pause_event=threading.Event(),
        )
        self.s._pause_event.set()
        self.errors: list[str] = []
        self.logger = SimpleNamespace(
            log_error=lambda message, exc=None: self.errors.append(message)
        )


def test_streamed_failure_raises_postprocess_error_and_preserves_segments(tmp_path, monkeypatch):
    """remux 실패는 PostprocessError로 명확히 실패하고 세그먼트를 지우지 않는다 (#92)."""
    fake = _FakeEngine(tmp_path)
    segs = [tmp_path / "0000.ts", tmp_path / "0001.ts"]
    for s in segs:
        s.write_bytes(b"seg-bytes")

    def broken_remux_stream(chunks, dst_path):
        raise RemuxError("가짜 실패")

    monkeypatch.setattr(base_module, "remux_stream", broken_remux_stream)

    with pytest.raises(PostprocessError):
        BaseDownloader._remux_streamed(fake, [str(s) for s in segs])

    assert all(s.exists() for s in segs)  # 세그먼트 보존 — 재다운로드 강요 금지
    assert fake.errors  # 원인 로그 존재 (무음 실패 금지)


def test_streamed_success_feeds_in_order_and_counts_segments(tmp_path, monkeypatch):
    """공급은 목록 순서 그대로이고 세그먼트 단위로 진행 카운트가 올라간다."""
    fake = _FakeEngine(tmp_path)
    segs = []
    for i in range(3):
        p = tmp_path / f"{i:04d}.ts"
        p.write_bytes(bytes([i + 1]) * 10)
        segs.append(str(p))

    def passthrough(chunks, dst_path):
        with open(dst_path, "wb") as f:
            for chunk in chunks:
                f.write(chunk)

    monkeypatch.setattr(base_module, "remux_stream", passthrough)
    BaseDownloader._remux_streamed(fake, segs)

    assert (tmp_path / "out.mp4").read_bytes() == b"\x01" * 10 + b"\x02" * 10 + b"\x03" * 10
    assert fake.s.merged_segments == 3
    assert all(os.path.exists(s) for s in segs)  # 성공 후 정리는 엔진(run)의 몫


# ================================================================ 일시정지·중단 (실제 ffmpeg)


def _run_streamed_in_thread(fake, paths):
    """_remux_streamed를 스레드로 실행하고 (스레드, 예외 수집 리스트)를 반환한다."""
    raised: list[BaseException] = []

    def target():
        try:
            BaseDownloader._remux_streamed(fake, paths)
        except BaseException as e:  # noqa: BLE001 — 검증용 수집
            raised.append(e)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, raised


def test_pause_blocks_feed_then_resume_completes(tmp_path):
    """일시정지면 ffmpeg가 stdin을 기다리며 멈추고, 재개하면 완주한다 (#92)."""
    src = tmp_path / "src.ts"
    _make_tiny_ts(src)
    segdir = tmp_path / "segs"
    segdir.mkdir()
    paths = _split_segments(src, segdir, parts=3)

    fake = _FakeEngine(tmp_path)
    fake.state = DownloadState.PAUSED
    fake.s._pause_event.clear()

    thread, raised = _run_streamed_in_thread(fake, paths)

    time.sleep(0.5)  # ffmpeg 기동 + 공급 루프가 pause 대기에 도달할 시간
    assert thread.is_alive()  # stdin 공급이 멈춘 채 대기 중
    assert fake.s.merged_segments == 0  # 일시정지 동안 진행 없음

    fake.state = DownloadState.RUNNING
    fake.s._pause_event.set()  # 재개
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert raised == []

    out = tmp_path / "out.mp4"
    boxes = _top_level_boxes(out)
    assert boxes.index("moov") < boxes.index("mdat")  # 재개 후 정상 완주
    assert fake.s.merged_segments == len(paths)


def test_stop_while_paused_kills_ffmpeg_and_leaves_no_output(tmp_path):
    """stdin 대기 상태에서 중단하면 산출물 없이 조용히 끝나고 세그먼트는 남는다 (#92).

    중단 시 세그먼트·임시 폴더 삭제는 엔진(run)의 중단 정리 몫이므로
    _remux_streamed 자신은 아무것도 지우지 않아야 한다.
    """
    src = tmp_path / "src.ts"
    _make_tiny_ts(src)
    segdir = tmp_path / "segs"
    segdir.mkdir()
    paths = _split_segments(src, segdir, parts=3)

    fake = _FakeEngine(tmp_path)
    fake.state = DownloadState.PAUSED
    fake.s._pause_event.clear()

    thread, raised = _run_streamed_in_thread(fake, paths)
    time.sleep(0.5)  # ffmpeg가 stdin을 기다리는 상태 도달

    fake.state = DownloadState.WAITING  # 중단
    fake.s._pause_event.set()  # 대기 해제 (모델 stop과 동일한 효과)
    thread.join(timeout=30)
    assert not thread.is_alive()

    assert raised == []  # 중단은 예외·실패가 아니다
    assert not (tmp_path / "out.mp4").exists()  # 불완전 산출물 미잔류
    assert all(os.path.exists(p) for p in paths)  # 세그먼트는 건드리지 않는다
