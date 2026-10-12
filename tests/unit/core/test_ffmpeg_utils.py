"""core/utils/ffmpeg.py·후처리 정책 검증 (#88·#92) — 경로 탐색·스트림 remux·실패 정책.

remux 검증은 실제 ffmpeg 바이너리(imageio-ffmpeg 동봉)를 실행한다 — 입력은
ffmpeg 내장 lavfi 소스로 즉석 생성한 초소형 파일이라 네트워크·외부 픽스처가
없다. 기본 입력은 fMP4(m3u8 경로와 동일 형식)다 — 리눅스 동봉 빌드
(7.0.2-static)는 mpegts 입력 demux 전반이 SIGSEGV라(#94) TS 입력 검증은
동봉 빌드 헬스체크를 통과하는 플랫폼에서만 수행한다.

일시정지·중단은 ffmpeg가 stdin을 기다리며 멈춘 실제 상태에서 검증한다
(#92 완료 조건). 다운로더의 실패 정책(_remux_streamed: 폴백 없음·세그먼트
보존)은 remux_stream을 스텁으로 바꿔 단위 수준에서 검증한다.
"""

import functools
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

import pytest

import core.downloaders.base as base_module
import core.utils.ffmpeg as ffmpeg_module
from core.downloaders.base import BaseDownloader, PostprocessError
from core.models.download_state import DownloadState
from core.utils.ffmpeg import (
    FFmpegNotFoundError,
    RemuxError,
    get_ffmpeg_exe,
    read_in_chunks,
    remux_stream,
)


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


def _generate(path, *container_args) -> None:
    """lavfi 테스트 소스를 libx264로 인코딩해 초소형 검증 입력을 만든다.

    libx264는 imageio-ffmpeg 동봉 GPL 빌드 3-OS 모두에 포함되어 있고,
    실제 치지직 스트림과 같은 코덱이다 — 검증 대상은 인코딩이 아니라 remux다.
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
            "libx264",
            "-preset",
            "ultrafast",
            *container_args,
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _make_tiny_fmp4(path) -> None:
    """1초짜리 h264 fMP4(조각 mp4) 입력 — m3u8 경로의 스트림과 동일 형식."""
    _generate(path, "-movflags", "frag_keyframe+empty_moov", "-f", "mp4")


def _make_tiny_ts(path) -> None:
    """1초짜리 h264 MPEG-TS 입력 — hls_aes 경로의 스트림과 동일 형식."""
    _generate(path, "-f", "mpegts")


@functools.lru_cache(maxsize=1)
def _resolved_ffmpeg_demuxes_ts() -> bool:
    """선택된 ffmpeg가 mpegts 입력을 읽을 수 있는지 1회 검사한다.

    프로덕션(remux_stream)과 같은 조건으로 검사한다 — 리눅스 동봉본에는
    GCONV_PATH 가드(#97)가 적용되므로 여기서도 적용한다. 가드로도 못 읽는
    환경이 있으면 TS 입력 검증을 스킵하고, 건강해지면 자동 복귀한다.
    """
    with tempfile.TemporaryDirectory() as d:
        ts = os.path.join(d, "probe.ts")
        _make_tiny_ts(ts)
        exe = get_ffmpeg_exe()
        proc = subprocess.run(
            [exe, "-v", "error", "-i", ts, "-c", "copy", "-f", "null", "-"],
            capture_output=True,
            env=ffmpeg_module._subprocess_env(exe),
        )
        return proc.returncode == 0


# ================================================================ 경로 탐색


def test_get_ffmpeg_exe_returns_existing_executable():
    """탐색 결과는 실재하는 실행 파일 경로여야 한다 (배포 방식 검증의 로컬 관문)."""
    exe = get_ffmpeg_exe()
    assert os.path.isfile(exe)
    assert os.access(exe, os.X_OK)


# ================================================================ 탐색 순서 (#94)


def test_explicit_env_var_wins_over_everything(monkeypatch):
    """IMAGEIO_FFMPEG_EXE 명시 지정은 플랫폼·시스템 탐색보다 항상 우선한다."""
    monkeypatch.setenv("IMAGEIO_FFMPEG_EXE", "/explicit/ffmpeg")
    monkeypatch.setattr(ffmpeg_module, "_IS_LINUX", True)
    monkeypatch.setattr(ffmpeg_module.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    assert get_ffmpeg_exe() == "/explicit/ffmpeg"


def test_linux_prefers_system_ffmpeg(monkeypatch):
    """리눅스는 시스템 ffmpeg를 동봉본보다 우선한다 (#94 — 동봉본 TS demux SIGSEGV 회피)."""
    monkeypatch.delenv("IMAGEIO_FFMPEG_EXE", raising=False)
    monkeypatch.setattr(ffmpeg_module, "_IS_LINUX", True)
    monkeypatch.setattr(ffmpeg_module.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    assert get_ffmpeg_exe() == "/usr/bin/ffmpeg"


def test_linux_without_system_falls_back_to_bundled(monkeypatch):
    """리눅스에 시스템 ffmpeg가 없으면 동봉본으로 폴백한다 (m3u8 경로는 동봉본도 정상)."""
    import imageio_ffmpeg

    monkeypatch.delenv("IMAGEIO_FFMPEG_EXE", raising=False)
    monkeypatch.setattr(ffmpeg_module, "_IS_LINUX", True)
    monkeypatch.setattr(ffmpeg_module.shutil, "which", lambda name: None)

    assert get_ffmpeg_exe() == imageio_ffmpeg.get_ffmpeg_exe()


def test_non_linux_does_not_probe_system(monkeypatch):
    """리눅스가 아니면 시스템 탐색을 하지 않는다 — 동봉본이 기본이다."""
    monkeypatch.delenv("IMAGEIO_FFMPEG_EXE", raising=False)
    monkeypatch.setattr(ffmpeg_module, "_IS_LINUX", False)

    def must_not_be_called(name):
        raise AssertionError("리눅스가 아니면 shutil.which를 호출하지 않아야 한다")

    monkeypatch.setattr(ffmpeg_module.shutil, "which", must_not_be_called)
    assert os.path.isfile(get_ffmpeg_exe())


def test_not_found_error_guides_installation(monkeypatch):
    """어느 경로로도 못 찾으면 설치 유도 안내를 담아 명확히 실패한다 (#94)."""
    import imageio_ffmpeg

    monkeypatch.delenv("IMAGEIO_FFMPEG_EXE", raising=False)
    monkeypatch.setattr(ffmpeg_module, "_IS_LINUX", True)
    monkeypatch.setattr(ffmpeg_module.shutil, "which", lambda name: None)

    def broken():
        raise RuntimeError("no exe")

    monkeypatch.setattr(imageio_ffmpeg, "get_ffmpeg_exe", broken)

    with pytest.raises(FFmpegNotFoundError) as exc_info:
        get_ffmpeg_exe()
    message = str(exc_info.value)
    assert "설치" in message and "apt" in message and "IMAGEIO_FFMPEG_EXE" in message


# ================================================================ 스트림 remux


def _assert_fully_decodes(path) -> None:
    """산출물이 오류 없이 끝까지 디코드되는지 확인한다 (#108 — moov 후행 유효성)."""
    result = subprocess.run(
        [get_ffmpeg_exe(), "-hide_banner", "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True,
    )
    assert result.returncode == 0 and not result.stderr.strip()


def test_remux_stream_produces_mp4_with_moov_and_no_second_pass(tmp_path):
    """산출물은 전역 인덱스(moov)를 가진 mp4이고, moov는 파일 끝이다 (#108).

    편집 프로그램 인식에 필요한 것은 moov의 **존재**(#88)이지 위치가 아니다.
    moov가 mdat 뒤(ffmpeg 기본)임을 단언해 +faststart의 전체 재기록 2차
    패스가 되살아나지 않게 박제한다 — 선두 이동이 다시 필요해지면 이
    단언을 근거와 함께 뒤집어야 한다. 후행 moov 산출물의 유효성은 전체
    디코드로 확인한다.
    """
    src = tmp_path / "src.mp4"
    dst = tmp_path / "dst.mp4"
    _make_tiny_fmp4(src)

    remux_stream(read_in_chunks(str(src)), str(dst))

    boxes = _top_level_boxes(dst)
    assert "moov" in boxes and "mdat" in boxes  # 전역 인덱스 존재 (#88의 본질)
    assert boxes.index("mdat") < boxes.index("moov")  # 2차 패스 없음 (#108)
    _assert_fully_decodes(dst)


def test_remux_stream_ts_input_produces_mp4(tmp_path):
    """TS 스트림(hls_aes 경로 형식)도 파이프 공급으로 mp4가 된다.

    리눅스 동봉 빌드는 mpegts demux가 SIGSEGV라 스킵된다(#94) — 실제 치지직
    TS의 remux 정합성은 #92 실측(AES 완주, #91 산출물과 해시 일치)이 담보한다.
    """
    if not _resolved_ffmpeg_demuxes_ts():
        pytest.skip("선택된 ffmpeg 빌드가 mpegts 입력에서 SIGSEGV (#94)")
    src = tmp_path / "src.ts"
    dst = tmp_path / "dst.mp4"
    _make_tiny_ts(src)

    remux_stream(read_in_chunks(str(src)), str(dst))

    boxes = _top_level_boxes(dst)
    assert "moov" in boxes and "mdat" in boxes
    assert boxes.index("mdat") < boxes.index("moov")  # 2차 패스 없음 (#108)
    _assert_fully_decodes(dst)


def test_remux_stream_writes_mp4_regardless_of_extension(tmp_path):
    """출력 컨테이너는 -f mp4로 명시된다 — 확장자가 .mp4가 아니어도 mp4다 (#92)."""
    src = tmp_path / "src.mp4"
    dst = tmp_path / "이름을 바꾼 산출물.mkv"
    _make_tiny_fmp4(src)

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
    src = tmp_path / "src.mp4"
    dst = tmp_path / "dst.mp4"
    _make_tiny_fmp4(src)

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
            total_downloaded_size=0,
            _pause_event=threading.Event(),
        )
        self.s._pause_event.set()
        self.errors: list[str] = []
        self.logger = SimpleNamespace(
            log_error=lambda message, exc=None: self.errors.append(message)
        )
        # 후처리 진행 통지 경로 (#89) — 이 파일의 검증 대상이 아니므로 무시한다
        self.progress_events: list = []
        self._on_progress = self.progress_events.append
        self._progress_total_size = lambda: None


def test_streamed_failure_raises_postprocess_error_and_preserves_segments(tmp_path, monkeypatch):
    """remux가 세그먼트를 하나도 받기 전에 실패하면 PostprocessError로 끝나고 세그먼트가 모두 남아야 한다.

    #92의 규칙은 "실패해도 세그먼트를 모두 남긴다"였다. #309에서 넘긴 세그먼트는 곧바로
    지우게 바꿨다(오너 결정 2026-10-12: 넘기자마자 지우고, 후처리가 실패하면 그 범위는
    다시 받는다). 넘기지 않은 세그먼트는 지금도 건드리지 않는다 — 이 테스트가 재는 것이다.
    """
    fake = _FakeEngine(tmp_path)
    segs = [tmp_path / "0000.ts", tmp_path / "0001.ts"]
    for s in segs:
        s.write_bytes(b"seg-bytes")

    def broken_remux_stream(chunks, dst_path):
        raise RemuxError("가짜 실패")

    monkeypatch.setattr(base_module, "remux_stream", broken_remux_stream)

    with pytest.raises(PostprocessError):
        BaseDownloader._remux_streamed(fake, [str(s) for s in segs])

    assert all(s.exists() for s in segs)  # 넘기지 않은 세그먼트는 건드리지 않는다
    assert fake.errors  # 원인 로그 존재 (무음 실패 금지)


def test_streamed_success_feeds_in_order_and_counts_segments(tmp_path, monkeypatch):
    """공급은 목록 순서 그대로이고 세그먼트 단위로 진행 카운트가 오르며, 넘긴 세그먼트는 지워져야 한다.

    세그먼트 3개(10바이트씩)를 그대로 받아 쓰는 대역 remux
    -> 산출물 == 세 세그먼트를 이은 것, merged_segments == 3, 세그먼트 파일이 모두 없다.
    예전에는 성공해도 세그먼트를 남기고 엔진(run)이 지웠다 — #309에서 넘기자마자 지우게 바꿨다
    (오너 결정 2026-10-12)
    """
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
    assert not any(os.path.exists(s) for s in segs)  # 넘긴 세그먼트는 곧바로 지운다


def test_streamed_feed_removes_each_segment_once_the_next_one_is_asked_for(tmp_path, monkeypatch):
    """세그먼트는 그 마지막 조각이 넘어간 뒤에 지워져야 한다 — 다음 세그먼트를 청하기 전에는 남아 있다.

    세그먼트 3개. 대역 remux가 조각을 하나 받을 때마다 그때 남아 있는 세그먼트를 적음
    -> 첫 조각을 받을 때: 3개 모두 있다 / 둘째: 첫 세그먼트만 없다 / 셋째: 셋째만 있다
    """
    fake = _FakeEngine(tmp_path)
    segs = []
    for i in range(3):
        p = tmp_path / f"{i:04d}.ts"
        p.write_bytes(bytes([i + 1]) * 10)
        segs.append(str(p))
    seen = []

    def watching(chunks, dst_path):
        for _chunk in chunks:
            seen.append([os.path.exists(s) for s in segs])

    monkeypatch.setattr(base_module, "remux_stream", watching)
    BaseDownloader._remux_streamed(fake, segs)

    assert seen == [[True, True, True], [False, True, True], [False, False, True]]


def test_streamed_failure_midway_leaves_only_the_segments_not_yet_fed(tmp_path, monkeypatch):
    """remux가 도중에 실패하면 이미 넘긴 세그먼트는 없고 넘기지 않은 세그먼트만 남아야 한다 (#309).

    세그먼트 3개. 대역 remux가 조각 둘을 받은 뒤 RemuxError
    -> PostprocessError, 첫 세그먼트는 없다(다 넘겼다), 둘째 · 셋째는 남아 있다,
       원인 로그가 있다. 예전(#92)에는 셋 다 남았다 — 오너 결정(2026-10-12)으로 바꿨다:
       넘기자마자 지우고, 후처리가 실패하면 그 범위는 다시 받는다
    """
    fake = _FakeEngine(tmp_path)
    segs = []
    for i in range(3):
        p = tmp_path / f"{i:04d}.ts"
        p.write_bytes(bytes([i + 1]) * 10)
        segs.append(str(p))

    def fails_after_two(chunks, dst_path):
        feed = iter(chunks)
        next(feed)
        next(feed)
        raise RemuxError("가짜 실패")

    monkeypatch.setattr(base_module, "remux_stream", fails_after_two)

    with pytest.raises(PostprocessError):
        BaseDownloader._remux_streamed(fake, segs)

    assert [os.path.exists(s) for s in segs] == [False, True, True]
    assert fake.errors


def test_streamed_feed_goes_on_when_a_fed_segment_cannot_be_removed(tmp_path, monkeypatch):
    """넘긴 세그먼트를 지우지 못해도 후처리는 끝까지 가야 한다 — 산출물은 그대로다.

    세그먼트 3개, os.remove가 늘 OSError -> 산출물 == 세 세그먼트를 이은 것, merged_segments == 3
    """
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

    def locked(path):
        raise OSError("시험")

    monkeypatch.setattr(base_module, "remux_stream", passthrough)
    monkeypatch.setattr(base_module.os, "remove", locked)
    BaseDownloader._remux_streamed(fake, segs)

    assert (tmp_path / "out.mp4").read_bytes() == b"\x01" * 10 + b"\x02" * 10 + b"\x03" * 10
    assert fake.s.merged_segments == 3


# ================================================================ 일시정지·중단 (실제 ffmpeg)


def _split_segments(src, outdir, parts: int) -> list[str]:
    """파일을 임의 바이트 경계로 잘라 세그먼트 파일 목록을 만든다.

    fMP4·TS는 바이트 연결이 곧 유효한 스트림이므로, 임의 분할 조각을
    순서대로 공급하면 원본과 같은 스트림이 된다.
    """
    data = src.read_bytes()
    step = len(data) // parts + 1
    paths = []
    for i in range(parts):
        p = outdir / f"{i:04d}.m4v"
        p.write_bytes(data[i * step : (i + 1) * step])
        paths.append(str(p))
    return [p for p in paths if os.path.getsize(p)]


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
    src = tmp_path / "src.mp4"
    _make_tiny_fmp4(src)
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
    assert "moov" in boxes and "mdat" in boxes  # 재개 후 정상 완주 (moov 위치는 #108)
    assert fake.s.merged_segments == len(paths)


def test_stop_while_paused_kills_ffmpeg_and_leaves_no_output(tmp_path):
    """아무것도 넘기기 전의 stdin 대기 상태에서 중단하면 산출물 없이 조용히 끝나고 세그먼트는 남는다 (#92).

    중단 시 세그먼트·임시 폴더 삭제는 엔진(run)의 중단 정리 몫이다. _remux_streamed는
    넘긴 세그먼트만 지운다(#309) — 여기서는 넘긴 것이 없어 아무것도 지우지 않는다.
    """
    src = tmp_path / "src.mp4"
    _make_tiny_fmp4(src)
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


# ================================================================ GCONV_PATH 가드 (#97)


def _explicit_bundled_exe() -> str:
    """imageio-ffmpeg 동봉 바이너리 경로를 탐색 순서를 거치지 않고 직접 얻는다.

    CI에 시스템 ffmpeg가 있으면 정상 탐색(get_ffmpeg_exe)은 동봉본을 타지
    않으므로(#95), 가드 검증은 동봉본을 명시 지정해야 한다 (#97 요건).
    """
    import imageio_ffmpeg

    binaries = os.path.join(os.path.dirname(imageio_ffmpeg.__file__), "binaries")
    for name in os.listdir(binaries):
        if name.startswith("ffmpeg-"):
            return os.path.join(binaries, name)
    raise AssertionError("동봉 바이너리를 찾지 못했다")


def _strip_sdt(src_path, dst_path) -> None:
    """TS에서 SDT(PID 0x11) 패킷만 제거한다 — 가드 유무 양쪽이 성공하는 입력용."""
    data = open(src_path, "rb").read()
    kept = bytearray()
    for i in range(0, len(data) - 187, 188):
        pkt = data[i : i + 188]
        if pkt[0] == 0x47 and ((pkt[1] & 0x1F) << 8) | pkt[2] == 0x11:
            continue
        kept.extend(pkt)
    open(dst_path, "wb").write(bytes(kept))


def test_gconv_guard_applied_for_bundled_on_linux(monkeypatch):
    """리눅스+동봉본 조합에서만 GCONV_PATH 가드가 적용된다 — 빈 디렉토리·전역 불변."""
    monkeypatch.setattr(ffmpeg_module, "_IS_LINUX", True)
    env = ffmpeg_module._subprocess_env(_explicit_bundled_exe())

    assert env is not None and "GCONV_PATH" in env
    assert os.path.isdir(env["GCONV_PATH"])
    assert os.listdir(env["GCONV_PATH"]) == []  # 빈 디렉토리 — 모듈 로드 차단
    assert "GCONV_PATH" not in os.environ  # 프로세스 전역은 건드리지 않는다


def test_gconv_guard_not_applied_for_system_exe(monkeypatch):
    """시스템 ffmpeg(동적 glibc — 자기 배포판과 호환)에는 가드를 적용하지 않는다."""
    monkeypatch.setattr(ffmpeg_module, "_IS_LINUX", True)
    assert ffmpeg_module._subprocess_env("/usr/bin/ffmpeg") is None


def test_gconv_guard_not_applied_off_linux(monkeypatch):
    """리눅스가 아니면 동봉본이어도 가드를 적용하지 않는다 (Windows·macOS 무영향)."""
    monkeypatch.setattr(ffmpeg_module, "_IS_LINUX", False)
    assert ffmpeg_module._subprocess_env(_explicit_bundled_exe()) is None


def test_remux_stream_passes_guard_env_to_subprocess(tmp_path, monkeypatch):
    """remux_stream은 _subprocess_env가 준 환경을 그대로 서브프로세스에 전달한다."""
    import io

    sentinel_env = dict(os.environ, CVDV2_GUARD_MARKER="1")
    captured = {}

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            captured.update(kwargs)
            self.stdin = io.BytesIO()
            self.stderr = io.BytesIO(b"")

        def wait(self):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(ffmpeg_module, "_subprocess_env", lambda exe: sentinel_env)
    monkeypatch.setattr(ffmpeg_module.subprocess, "Popen", _FakePopen)

    remux_stream(iter([b"x"]), str(tmp_path / "out.mp4"))

    assert captured["env"] is sentinel_env


# ================ CI(리눅스) 전용 — 동봉본 명시 지정 실검증 (#97 완료 조건)


def test_bundled_ts_remux_succeeds_with_guard(tmp_path, monkeypatch):
    """최신 배포판(CI)에서 동봉본만으로 TS remux가 성공한다 — #97의 목적 그 자체.

    가드 도입 전에는 이 remux가 SIGSEGV였다(#94). 동봉본을 명시 지정해
    탐색 순서(시스템 우선)에 좌우되지 않게 한다.
    """
    if not sys.platform.startswith("linux"):
        pytest.skip("리눅스 동봉본 전용 검증")
    bundled = _explicit_bundled_exe()
    monkeypatch.setattr(ffmpeg_module, "get_ffmpeg_exe", lambda: bundled)
    monkeypatch.setitem(globals(), "get_ffmpeg_exe", lambda: bundled)

    src = tmp_path / "src.ts"
    dst = tmp_path / "dst.mp4"
    _make_tiny_ts(src)  # SDT 포함 — 가드 없이는 크래시하는 입력

    remux_stream(read_in_chunks(str(src)), str(dst))

    boxes = _top_level_boxes(dst)
    assert "moov" in boxes and "mdat" in boxes  # 정상 remux 완주 (moov 위치는 #108)


def test_guard_output_is_byte_identical(tmp_path, monkeypatch):
    """가드 적용 전후 산출물이 바이트 단위로 동일하다 — A/V 무영향 입증 (#97).

    양쪽 다 성공하는 입력(fMP4, SDT 제거 TS)으로 비교한다 — SDT 포함 TS는
    가드 없이는 크래시라 '전' 산출물 자체가 존재하지 않는다.
    """
    if not sys.platform.startswith("linux"):
        pytest.skip("리눅스 동봉본 전용 검증")
    bundled = _explicit_bundled_exe()
    monkeypatch.setattr(ffmpeg_module, "get_ffmpeg_exe", lambda: bundled)
    monkeypatch.setitem(globals(), "get_ffmpeg_exe", lambda: bundled)

    fmp4 = tmp_path / "src.mp4"
    _make_tiny_fmp4(fmp4)
    ts_full = tmp_path / "full.ts"
    _make_tiny_ts(ts_full)
    ts_nosdt = tmp_path / "nosdt.ts"
    _strip_sdt(ts_full, ts_nosdt)

    for src in (fmp4, ts_nosdt):
        guarded = tmp_path / f"{src.stem}_guarded.mp4"
        remux_stream(read_in_chunks(str(src)), str(guarded))  # 가드 자동 적용

        unguarded = tmp_path / f"{src.stem}_unguarded.mp4"
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(ffmpeg_module, "_subprocess_env", lambda exe: None)
            remux_stream(read_in_chunks(str(src)), str(unguarded))

        assert guarded.read_bytes() == unguarded.read_bytes(), f"{src.name} 산출물 상이"


# ================================================================ 변환 · 잇기 두 ffmpeg (#309)


def _video_stamps(path) -> list[str]:
    """영상 패킷마다의 (DTS, PTS) 글자 — 파일에 놓인 순서."""
    done = ffmpeg_module.run_ffmpeg(
        ["-v", "error", "-i", str(path), "-map", "0:v:0", "-c", "copy", "-f", "framecrc", "-"],
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    rows = [line for line in done.stdout.splitlines() if line and not line.startswith("#")]
    return [",".join(part.strip() for part in row.split(",")[1:3]) for row in rows]


_CONVERT = [
    "-i", "pipe:0", "-map", "0:v:0", "-c", "copy",
    "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1",
]  # fmt: skip


def _join_args(work, dst) -> list[str]:
    """concat 목록(파이프 하나)을 쓰고, 그것을 dst로 잇는 ffmpeg의 인자를 돌려준다."""
    with open(os.path.join(work, "list.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write("ffconcat version 1.0\nfile pipe:0\n")
    return [
        "-protocol_whitelist", "file,pipe,fd", "-f", "concat", "-safe", "0", "-i", "list.txt",
        "-map", "0:v:0", "-c", "copy", "-f", "mp4", str(dst),
    ]  # fmt: skip


def test_remux_through_makes_the_same_video_as_remuxing_directly(tmp_path):
    """변환 ffmpeg를 거쳐 이은 mp4의 영상 패킷은 바로 재포장한 mp4의 것과 같아야 한다.

    1초짜리 fMP4를 세 조각으로 나눠 공급. 변환: 영상만 조각난 mp4로 / 잇기: concat의 pipe:0
    -> 산출물의 영상 패킷마다 (DTS, PTS) == remux_stream으로 만든 mp4의 것, 패킷이 하나 이상
    """
    src = tmp_path / "src.mp4"
    _make_tiny_fmp4(src)
    data = src.read_bytes()
    step = len(data) // 3 + 1
    chunks = [data[i : i + step] for i in range(0, len(data), step)]
    direct, through = tmp_path / "direct.mp4", tmp_path / "through.mp4"
    remux_stream(iter(chunks), str(direct))

    ffmpeg_module.remux_through(
        iter(chunks), _CONVERT, _join_args(tmp_path, through), str(through), cwd=str(tmp_path)
    )

    stamps = _video_stamps(through)
    assert stamps and stamps == _video_stamps(direct)
    _assert_fully_decodes(through)


def test_remux_through_fails_and_leaves_no_output_when_the_input_is_not_media(tmp_path):
    """변환 ffmpeg가 읽지 못하는 입력이면 RemuxError로 끝나고 산출물이 남지 않아야 한다.

    공급: 미디어가 아닌 바이트 4096개 -> RemuxError, 산출물 없음
    """
    out = tmp_path / "out.mp4"

    with pytest.raises(RemuxError):
        ffmpeg_module.remux_through(
            iter([b"not media " * 400]),
            _CONVERT,
            _join_args(tmp_path, out),
            str(out),
            cwd=str(tmp_path),
        )

    assert not out.exists()


def test_remux_through_ends_both_processes_and_cleans_up_when_the_feed_raises(
    tmp_path, monkeypatch
):
    """공급이 예외를 던지면 두 ffmpeg를 모두 끝내고 산출물을 지운 뒤 그 예외를 그대로 올려야 한다.

    공급: 첫 조각을 낸 뒤 RuntimeError -> 같은 RuntimeError, 띄운 프로세스 2개가 모두 끝났다, 산출물 없음
    """
    src = tmp_path / "src.mp4"
    _make_tiny_fmp4(src)
    data = src.read_bytes()
    out = tmp_path / "out.mp4"
    launched = []
    real = subprocess.Popen

    def recording(*args, **kwargs):
        process = real(*args, **kwargs)
        launched.append(process)
        return process

    monkeypatch.setattr(ffmpeg_module.subprocess, "Popen", recording)

    def feed():
        yield data[:1000]
        raise RuntimeError("공급 중단")

    with pytest.raises(RuntimeError, match="공급 중단"):
        ffmpeg_module.remux_through(
            feed(), _CONVERT, _join_args(tmp_path, out), str(out), cwd=str(tmp_path)
        )

    assert len(launched) == 2
    assert all(process.poll() is not None for process in launched)
    assert not out.exists()


def test_remux_through_fails_when_only_the_join_side_fails(tmp_path):
    """잇는 ffmpeg만 실패해도(없는 목록 파일) RemuxError로 끝나야 한다.

    잇기의 입력 목록이 없는 파일 -> RemuxError, 산출물 없음
    """
    src = tmp_path / "src.mp4"
    _make_tiny_fmp4(src)
    out = tmp_path / "out.mp4"
    join = [
        "-f", "concat", "-safe", "0", "-i", "missing.txt", "-c", "copy", "-f", "mp4", str(out),
    ]  # fmt: skip

    with pytest.raises(RemuxError):
        ffmpeg_module.remux_through(
            iter([src.read_bytes()]), _CONVERT, join, str(out), cwd=str(tmp_path)
        )

    assert not out.exists()
