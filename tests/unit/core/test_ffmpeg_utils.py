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
import inspect
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
import weakref
from types import SimpleNamespace

import pytest

import core.downloaders.base as base_module
import core.utils.ffmpeg as ffmpeg_module
import core.utils.fmp4_cut as fmp4_cut_module
from core.downloaders.base import BaseDownloader, PostprocessError
from core.models.download_state import DownloadState
from core.utils.ffmpeg import (
    FFmpegNotFoundError,
    RemuxError,
    get_ffmpeg_exe,
    read_in_chunks,
    remux_stream,
)
from tests.unit.core.undeletable import Undeletable


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


# ================================================================ 못 찾은 결과를 기억하지 않는다 (#423)


@pytest.fixture
def bundled_slot(tmp_path, monkeypatch):
    """동봉본이 놓일 자리를 빈 폴더로 바꾸고, 실행 확인(-version)을 그 자리의 파일만 통과시킨다.

    시스템 · conda의 ffmpeg는 확인을 통과하지 못한다. 확인한 경로를 ``checked``에 적는다.
    imageio-ffmpeg의 탐색 캐시를 앞뒤로 비운다 — 다른 테스트가 실제 경로를 그대로 쓴다.
    """
    import imageio_ffmpeg
    from imageio_ffmpeg._definitions import FNAME_PER_PLATFORM, get_platform

    utils = imageio_ffmpeg._utils
    checked: list[str] = []

    def is_valid(exe: str) -> bool:
        checked.append(exe)
        return os.path.dirname(exe) == str(tmp_path) and os.path.isfile(exe)

    monkeypatch.delenv("IMAGEIO_FFMPEG_EXE", raising=False)
    monkeypatch.setattr(ffmpeg_module, "_IS_LINUX", False)
    monkeypatch.setattr(utils, "_get_bin_dir", lambda: str(tmp_path))
    monkeypatch.setattr(utils, "_is_valid_exe", is_valid)
    utils._get_ffmpeg_exe.cache_clear()
    yield SimpleNamespace(path=str(tmp_path / FNAME_PER_PLATFORM[get_platform()]), checked=checked)
    utils._get_ffmpeg_exe.cache_clear()


def test_ffmpeg_that_appears_after_a_failed_lookup_is_found_on_the_next_lookup(bundled_slot):
    """처음 조회에서 ffmpeg를 못 찾았어도 그 뒤 ffmpeg가 생기면 다음 조회에서 찾아야 한다.

    동봉본 자리가 빈 상태로 get_ffmpeg_exe → 그 자리에 파일을 둠 → 다시 get_ffmpeg_exe
    -> 처음은 FFmpegNotFoundError, 다음은 그 파일의 경로
    """
    with pytest.raises(FFmpegNotFoundError):
        get_ffmpeg_exe()
    with open(bundled_slot.path, "wb") as f:
        f.write(b"ffmpeg")

    assert get_ffmpeg_exe() == bundled_slot.path


def test_found_ffmpeg_is_remembered_and_not_checked_again(bundled_slot):
    """ffmpeg를 찾은 뒤의 조회는 실행 확인을 다시 하지 않고 같은 경로를 돌려줘야 한다.

    동봉본 자리에 파일을 두고 get_ffmpeg_exe 세 번
    -> 세 번 모두 그 경로, 실행 확인은 첫 조회의 한 번뿐
    """
    with open(bundled_slot.path, "wb") as f:
        f.write(b"ffmpeg")

    found = [get_ffmpeg_exe() for _ in range(3)]

    assert found == [bundled_slot.path] * 3
    assert bundled_slot.checked == [bundled_slot.path]


def test_imageio_lookup_cache_can_be_cleared():
    """imageio-ffmpeg의 자동 탐색 함수(_utils._get_ffmpeg_exe)는 cache_clear를 가져야 한다.

    get_ffmpeg_exe가 못 찾은 결과를 지울 때 이 비공개 이름을 쓴다 (#423)
    -> imageio_ffmpeg._utils._get_ffmpeg_exe.cache_clear가 있다
    """
    import imageio_ffmpeg

    assert callable(imageio_ffmpeg._utils._get_ffmpeg_exe.cache_clear)


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

    def broken_remux_stream(chunks, dst_path, **_wait):
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

    def passthrough(chunks, dst_path, **_wait):
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

    def watching(chunks, dst_path, **_wait):
        for _chunk in chunks:
            seen.append([os.path.exists(s) for s in segs])

    monkeypatch.setattr(base_module, "remux_stream", watching)
    BaseDownloader._remux_streamed(fake, segs)

    assert seen == [[True, True, True], [False, True, True], [False, False, True]]


def test_streamed_feed_removes_a_segment_only_after_its_last_chunk(tmp_path, monkeypatch):
    """조각이 여러 개인 세그먼트는 마지막 조각까지 넘긴 뒤에만 지워야 한다 — 그 전에는 지우려 하지도 않는다.

    세그먼트 3개(10바이트씩)를 4바이트 조각으로 읽게 함 — 세그먼트마다 조각 3개.
    지우는 함수가 불릴 때마다 (세그먼트 이름, 그때까지 ffmpeg 쪽이 받은 조각 수)를 적음
    -> [("0000.ts", 3), ("0001.ts", 6), ("0002.ts", 9)],
       둘째 · 셋째 조각을 받는 순간마다 그 세그먼트 파일이 아직 있다
    """
    fake = _FakeEngine(tmp_path)
    segs = []
    for i in range(3):
        p = tmp_path / f"{i:04d}.ts"
        p.write_bytes(bytes([i + 1]) * 10)
        segs.append(str(p))
    real_read, real_remove = base_module.read_in_chunks, base_module._remove_fed_segment
    received = []  # 조각을 받는 순간, 그 조각이 든 세그먼트 파일이 있는가
    removed = []

    def small_chunks(path):
        return real_read(path, 4)

    def recording_remove(path):
        removed.append((os.path.basename(path), len(received)))
        real_remove(path)

    def watching(chunks, dst_path, **_wait):
        for chunk in chunks:
            received.append(os.path.exists(segs[chunk[0] - 1]))

    monkeypatch.setattr(base_module, "read_in_chunks", small_chunks)
    monkeypatch.setattr(base_module, "_remove_fed_segment", recording_remove)
    monkeypatch.setattr(base_module, "remux_stream", watching)
    BaseDownloader._remux_streamed(fake, segs)

    assert len(received) == 9, "전제: 세그먼트마다 조각이 셋이다"
    assert all(received)
    assert removed == [("0000.ts", 3), ("0001.ts", 6), ("0002.ts", 9)]
    assert not any(os.path.exists(s) for s in segs)


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

    def fails_after_two(chunks, dst_path, **_wait):
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

    def passthrough(chunks, dst_path, **_wait):
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


_NOT_MEDIA = b"not media " * 6554  # 미디어가 아닌 바이트 약 64KB — ffmpeg가 읽다가 실패하고 끝난다


def _remux_in_one_step(chunks, out, work) -> None:
    remux_stream(chunks, str(out))


def _remux_in_two_steps(chunks, out, work) -> None:
    ffmpeg_module.remux_through(chunks, _CONVERT, _join_args(work, out), str(out), cwd=str(work))


_REMUXERS = pytest.mark.parametrize(
    "remux", [_remux_in_one_step, _remux_in_two_steps], ids=["remux_stream", "remux_through"]
)


@_REMUXERS
def test_feed_left_midway_because_ffmpeg_ended_is_closed_with_the_file_it_opened(tmp_path, remux):
    """ffmpeg가 먼저 끝나 공급이 중간에 멈추면, 실패가 올라간 뒤 공급 제너레이터와 그것이 연 파일이 닫혀 있어야 한다 (#389).

    공급: 미디어가 아닌 파일 하나를 연 채 그 내용을 끝없이 내는 제너레이터
    -> RemuxError, 그 예외의 트레이스백이 살아 있는 동안 제너레이터 상태 == GEN_CLOSED, 연 파일이 닫혀 있다
    """
    source = tmp_path / "not_media.bin"
    source.write_bytes(_NOT_MEDIA)
    opened = []

    def endless():
        with open(source, "rb") as f:
            opened.append(f)
            while True:
                f.seek(0)
                yield f.read()

    feed = endless()

    with pytest.raises(RemuxError) as raised:
        remux(feed, tmp_path / "out.mp4", tmp_path)

    assert raised.value.__traceback__ is not None  # 공급을 부른 프레임이 아직 살아 있다
    assert len(opened) == 1  # 끝없는 공급이 중간에 멈췄다 — 끝까지 돌아 닫힌 것이 아니다
    assert inspect.getgeneratorstate(feed) == inspect.GEN_CLOSED
    assert opened[0].closed


def test_segment_feed_left_midway_closes_the_reader_of_every_segment_it_opened(
    tmp_path, monkeypatch
):
    """구간 컷의 공급(fmp4_cut._feed)이 중간에 멈추면 세그먼트마다 연 읽기 제너레이터가 모두 닫혀 있어야 한다 (#389).

    공급: 미디어가 아닌 약 64KB 파일 200개를 _feed로 잇는다. 변환 ffmpeg는 그것을 읽지 못하고 끝난다
    -> RemuxError, 트레이스백이 살아 있는 동안 연 읽기 제너레이터가 1개 이상 200개 미만이고 모두
       닫혔거나 사라졌다
    """
    source = tmp_path / "not_media.bin"
    source.write_bytes(_NOT_MEDIA)
    # 약한 참조로 적는다 — 강한 참조를 들면 이 목록이 제너레이터를 살려 둔다
    readers: list[weakref.ref] = []
    real = fmp4_cut_module.read_in_chunks

    def recording(path, chunk_size):
        reader = real(path, chunk_size)
        readers.append(weakref.ref(reader))
        return reader

    monkeypatch.setattr(fmp4_cut_module, "read_in_chunks", recording)
    out = tmp_path / "out.mp4"

    with pytest.raises(RemuxError) as raised:
        ffmpeg_module.remux_through(
            fmp4_cut_module._feed(b"", [str(source)] * 200),
            _CONVERT,
            _join_args(tmp_path, out),
            str(out),
            cwd=str(tmp_path),
        )

    assert raised.value.__traceback__ is not None
    assert 1 <= len(readers) < 200  # 공급이 중간에 멈췄다
    left_open = [
        ref
        for ref in readers
        if ref() is not None and inspect.getgeneratorstate(ref()) != inspect.GEN_CLOSED
    ]
    assert left_open == []


@_REMUXERS
def test_output_that_cannot_be_removed_does_not_replace_the_remux_failure(
    tmp_path, monkeypatch, remux
):
    """실패한 산출물을 지울 수 없어도 재포장의 실패(RemuxError)가 그대로 올라가야 한다 (#389).

    출력 경로에 파일이 있고 그 os.remove가 PermissionError. 공급: 미디어가 아닌 바이트
    -> RemuxError, 지우기를 거절한 경로 == [출력 경로], 출력 파일이 남아 있다
    """
    out = tmp_path / "out.mp4"
    out.write_bytes(b"left over")
    blocker = Undeletable(monkeypatch)
    blocker.block(out)

    with pytest.raises(RemuxError):
        remux(iter([_NOT_MEDIA]), out, tmp_path)

    assert blocker.refused == [str(out)]
    assert out.exists()


def _fake_pair(monkeypatch, dst, convert_code: int, join_code: int) -> None:
    """remux_through가 띄우는 두 프로세스를 파이썬 대역으로 바꾼다 — 종료 코드를 정할 수 있게.

    변환 대역은 표준 입력을 표준 출력으로 그대로 옮긴 뒤 convert_code로 끝난다. 잇기 대역은
    산출물을 만들고(만들었다는 표식 파일도 함께) 표준 입력을 끝까지 읽은 뒤 join_code로 끝난다.
    잇는 쪽은 명령에 든 -nostdin으로 가린다.
    """
    real = subprocess.Popen
    convert = (
        "import shutil, sys\n"
        "shutil.copyfileobj(sys.stdin.buffer, sys.stdout.buffer)\n"
        "sys.stdout.buffer.flush()\n"
        "sys.exit(int(sys.argv[1]))\n"
    )
    join = (
        "import sys\n"
        "open(sys.argv[1], 'wb').write(b'partial')\n"
        "open(sys.argv[1] + '.made', 'wb').close()\n"
        "sys.stdin.buffer.read()\n"
        "sys.exit(int(sys.argv[2]))\n"
    )

    def fake(command, **kwargs):
        if "-nostdin" in command:
            return real([sys.executable, "-c", join, str(dst), str(join_code)], **kwargs)
        return real([sys.executable, "-c", convert, str(convert_code)], **kwargs)

    monkeypatch.setattr(ffmpeg_module.subprocess, "Popen", fake)


def test_remux_through_with_both_sides_succeeding_keeps_the_output(tmp_path, monkeypatch):
    """두 프로세스가 모두 0으로 끝나면 예외 없이 산출물이 남아야 한다 — 아래 두 테스트의 대조군이다.

    변환 대역 0 · 잇기 대역 0 -> 예외 없음, 산출물이 있다
    """
    out = tmp_path / "out.mp4"
    _fake_pair(monkeypatch, out, convert_code=0, join_code=0)

    ffmpeg_module.remux_through(iter([b"abc" * 1000]), [], [], str(out))

    assert out.read_bytes() == b"partial"


def test_remux_through_removes_the_output_the_join_side_made_when_the_convert_side_fails(
    tmp_path, monkeypatch
):
    """잇는 쪽이 산출물을 만든 뒤에 변환 쪽이 실패하면 RemuxError로 끝나고 그 산출물이 지워져야 한다.

    변환 대역 1 · 잇기 대역 0(산출물을 만들고 입력을 끝까지 읽는다)
    -> RemuxError, 잇는 쪽이 산출물을 만들었다는 표식은 있다, 산출물은 없다
    """
    out = tmp_path / "out.mp4"
    _fake_pair(monkeypatch, out, convert_code=1, join_code=0)

    with pytest.raises(RemuxError):
        ffmpeg_module.remux_through(iter([b"abc" * 1000]), [], [], str(out))

    assert (tmp_path / "out.mp4.made").exists(), "전제: 잇는 쪽이 산출물을 만들었다"
    assert not out.exists()


def test_remux_through_fails_when_the_join_side_fails_after_the_convert_side_ended_well(
    tmp_path, monkeypatch
):
    """변환 쪽이 다 넘기고 0으로 끝났어도 잇는 쪽이 실패하면 RemuxError로 끝나고 산출물이 지워져야 한다.

    잇는 쪽만 실패하는 경우가 있다 — 입력을 다 받은 뒤 파일의 끝부분을 쓰다 디스크가 모자란 때.
    변환 대역 0 · 잇기 대역 1(입력을 끝까지 읽은 뒤 끝난다)
    -> RemuxError, 잇는 쪽이 산출물을 만들었다는 표식은 있다, 산출물은 없다
    """
    out = tmp_path / "out.mp4"
    _fake_pair(monkeypatch, out, convert_code=0, join_code=1)

    with pytest.raises(RemuxError):
        ffmpeg_module.remux_through(iter([b"abc" * 1000]), [], [], str(out))

    assert (tmp_path / "out.mp4.made").exists(), "전제: 잇는 쪽이 산출물을 만들었다"
    assert not out.exists()


def test_remux_through_returns_the_warnings_of_the_join_side_and_asks_it_for_them(
    tmp_path, monkeypatch
):
    """remux_through는 잇는 ffmpeg를 경고까지 내게 띄우고, 그것이 stderr에 쓴 글을 돌려줘야 한다.

    변환 대역 0 · 잇기 대역 0, 잇기 대역이 stderr에 "DTS mark"를 쓴다
    -> 돌려준 글에 "DTS mark"가 있다, 잇는 쪽 명령의 -loglevel 값 == "warning"
    """
    out = tmp_path / "out.mp4"
    real = subprocess.Popen
    commands = []
    join = (
        "import sys\n"
        "open(sys.argv[1], 'wb').write(b'made')\n"
        "sys.stdin.buffer.read()\n"
        "sys.stderr.write('DTS mark')\n"
    )
    convert = "import shutil, sys\nshutil.copyfileobj(sys.stdin.buffer, sys.stdout.buffer)\n"

    def fake(command, **kwargs):
        commands.append(list(command))
        if "-nostdin" in command:
            return real([sys.executable, "-c", join, str(out)], **kwargs)
        return real([sys.executable, "-c", convert], **kwargs)

    monkeypatch.setattr(ffmpeg_module.subprocess, "Popen", fake)

    warnings = ffmpeg_module.remux_through(iter([b"abc" * 1000]), [], [], str(out))

    assert "DTS mark" in warnings
    joined = next(command for command in commands if "-nostdin" in command)
    assert joined[joined.index("-loglevel") + 1] == "warning"


def _lingering_pair(monkeypatch, dst, linger: float) -> dict:
    """remux_through의 두 프로세스를 대역으로 바꾼다 — 잇기 대역이 입력을 다 읽은 뒤 linger초 더 돈다.

    잇기 대역은 산출물을 만들고, 입력을 끝까지 읽고, linger초 뒤 "<산출물>.done"을 만들고 0으로
    끝난다. 띄운 프로세스를 구실("join" · "convert")로 적는 사전을 돌려준다.
    """
    # ffmpeg를 찾는 일이 프로세스를 띄울 수 있다 — 대역으로 바꾸기 전에 끝내 둔다(그 실행이 변환
    # 대역으로 바뀌면 표준 입력을 읽으며 서 있는다)
    ffmpeg_module.get_ffmpeg_exe()
    real = subprocess.Popen
    # 가상 환경의 파이썬은 Windows에서 진짜 인터프리터를 자식으로 띄우는 실행기다 — 실행기를 멈춰도
    # 자식은 돈다. 멈춤을 재려면 진짜 인터프리터를 바로 띄운다
    python = getattr(sys, "_base_executable", None) or sys.executable
    started = {}
    convert = "import shutil, sys\nshutil.copyfileobj(sys.stdin.buffer, sys.stdout.buffer)\n"
    join = (
        "import sys, time\n"
        "open(sys.argv[1], 'wb').write(b'partial')\n"
        "sys.stdin.buffer.read()\n"
        "time.sleep(float(sys.argv[2]))\n"
        "open(sys.argv[1] + '.done', 'wb').close()\n"
    )

    def fake(command, **kwargs):
        if "-nostdin" in command:
            process = started["join"] = real([python, "-c", join, str(dst), str(linger)], **kwargs)
        else:
            assert "convert" not in started, f"두 프로세스 말고 더 띄웠다: {command}"
            process = started["convert"] = real([python, "-c", convert], **kwargs)
        return process

    monkeypatch.setattr(ffmpeg_module.subprocess, "Popen", fake)
    return started


def test_remux_through_stops_waiting_when_asked_to_stop_after_the_input_ended(
    tmp_path, monkeypatch
):
    """입력을 다 넣은 뒤 두 ffmpeg를 기다리는 동안 멈추라고 하면 둘을 끝내고 산출물을 지워야 한다.

    잇기 대역이 입력을 다 읽은 뒤 8초 더 돈다. should_stop이 늘 참
    -> FFmpegCancelledError, 두 프로세스가 끝나 있다, 산출물 없음, 잇기 대역이 끝까지 가지 않았다
    """
    out = tmp_path / "out.mp4"
    started = _lingering_pair(monkeypatch, out, linger=8)

    with pytest.raises(ffmpeg_module.FFmpegCancelledError):
        ffmpeg_module.remux_through(
            iter([b"abc" * 1000]), [], [], str(out), should_stop=lambda: True
        )

    assert sorted(started) == ["convert", "join"]
    assert all(process.poll() is not None for process in started.values())
    assert not out.exists() and not (tmp_path / "out.mp4.done").exists()


def test_remux_through_gives_up_when_the_processes_outlive_the_timeout_after_the_input_ended(
    tmp_path, monkeypatch
):
    """입력을 다 넣은 뒤 두 ffmpeg가 제한 시간 안에 끝나지 않으면 둘을 끝내고 산출물을 지워야 한다.

    잇기 대역이 입력을 다 읽은 뒤 8초 더 돈다. timeout 0.5초
    -> FFmpegTimeoutError, 두 프로세스가 끝나 있다, 산출물 없음
    """
    out = tmp_path / "out.mp4"
    started = _lingering_pair(monkeypatch, out, linger=8)

    with pytest.raises(ffmpeg_module.FFmpegTimeoutError):
        ffmpeg_module.remux_through(iter([b"abc" * 1000]), [], [], str(out), timeout=0.5)

    assert sorted(started) == ["convert", "join"]
    assert all(process.poll() is not None for process in started.values())
    assert not out.exists()


def test_remux_through_holds_the_processes_while_paused_and_does_not_count_that_time(
    tmp_path, monkeypatch
):
    """기다리는 동안 일시정지면 두 ffmpeg가 서 있어야 하고, 서 있던 시간은 제한 시간에 세지 않아야 한다.

    잇기 대역이 입력을 다 읽은 뒤 1초 더 돈다. 변환 대역이 끝난 것을 본 때부터 2초 동안
    should_pause가 참(그때 도는 것은 잇기 대역뿐이다), timeout 1.5초
    -> 예외 없이 끝난다(산출물과 끝 표식이 있다), 걸린 시간 >= 2초(서 있는 동안 끝나지 못했다)
    """
    out = tmp_path / "out.mp4"
    started = _lingering_pair(monkeypatch, out, linger=1.0)
    paused_at = []

    def should_pause() -> bool:
        if not paused_at and started["convert"].poll() is not None:
            paused_at.append(time.monotonic())
        return bool(paused_at) and time.monotonic() - paused_at[0] < 2.0

    began = time.monotonic()
    ffmpeg_module.remux_through(
        iter([b"abc" * 1000]), [], [], str(out), timeout=1.5, should_pause=should_pause
    )

    assert time.monotonic() - began >= 2.0
    assert out.exists() and (tmp_path / "out.mp4.done").exists()


def _lingering_one(monkeypatch, linger: float) -> list:
    """remux_stream이 띄우는 ffmpeg를 대역으로 바꾼다 — 입력을 다 읽은 뒤 linger초 더 돈다.

    대역은 산출물(명령의 마지막 인자)을 만들고, 입력을 끝까지 읽고, linger초 뒤 "<산출물>.done"을
    만들고 0으로 끝난다. 띄운 프로세스를 적는 목록을 돌려준다.
    """
    ffmpeg_module.get_ffmpeg_exe()  # ffmpeg를 찾는 실행이 대역으로 바뀌지 않게 먼저 끝내 둔다
    real = subprocess.Popen
    python = getattr(sys, "_base_executable", None) or sys.executable
    started = []
    script = (
        "import sys, time\n"
        "open(sys.argv[1], 'wb').write(b'partial')\n"
        "sys.stdin.buffer.read()\n"
        "time.sleep(float(sys.argv[2]))\n"
        "open(sys.argv[1] + '.done', 'wb').close()\n"
    )

    def fake(command, **kwargs):
        assert not started, f"프로세스를 하나 더 띄웠다: {command}"
        started.append(real([python, "-c", script, command[-1], str(linger)], **kwargs))
        return started[0]

    monkeypatch.setattr(ffmpeg_module.subprocess, "Popen", fake)
    return started


def test_remux_stream_stops_waiting_when_asked_to_stop_after_the_input_ended(tmp_path, monkeypatch):
    """입력을 다 넣은 뒤 ffmpeg를 기다리는 동안 멈추라고 하면 끝내고 산출물을 지워야 한다.

    대역이 입력을 다 읽은 뒤 8초 더 돈다. should_stop이 늘 참
    -> FFmpegCancelledError, 프로세스가 끝나 있다, 산출물 없음, 대역이 끝까지 가지 않았다
    """
    out = tmp_path / "out.mp4"
    started = _lingering_one(monkeypatch, linger=8)

    with pytest.raises(ffmpeg_module.FFmpegCancelledError):
        ffmpeg_module.remux_stream(iter([b"abc" * 1000]), str(out), should_stop=lambda: True)

    assert len(started) == 1 and started[0].poll() is not None
    assert not out.exists() and not (tmp_path / "out.mp4.done").exists()


def test_remux_stream_gives_up_when_ffmpeg_outlives_the_timeout_after_the_input_ended(
    tmp_path, monkeypatch
):
    """입력을 다 넣은 뒤 ffmpeg가 제한 시간 안에 끝나지 않으면 끝내고 산출물을 지워야 한다.

    대역이 입력을 다 읽은 뒤 8초 더 돈다. timeout 0.5초
    -> FFmpegTimeoutError, 프로세스가 끝나 있다, 산출물 없음
    """
    out = tmp_path / "out.mp4"
    started = _lingering_one(monkeypatch, linger=8)

    with pytest.raises(ffmpeg_module.FFmpegTimeoutError):
        ffmpeg_module.remux_stream(iter([b"abc" * 1000]), str(out), timeout=0.5)

    assert len(started) == 1 and started[0].poll() is not None
    assert not out.exists()


def test_remux_stream_holds_ffmpeg_while_paused_and_does_not_count_that_time(tmp_path, monkeypatch):
    """기다리는 동안 일시정지면 ffmpeg가 서 있어야 하고, 서 있던 시간은 제한 시간에 세지 않아야 한다.

    대역이 입력을 다 읽은 뒤 1초 더 돈다. should_pause가 처음 불린 뒤 2초 동안 참, timeout 1.5초
    -> 예외 없이 끝난다(산출물과 끝 표식이 있다), 걸린 시간 >= 2초(서 있는 동안 끝나지 못했다)
    """
    out = tmp_path / "out.mp4"
    _lingering_one(monkeypatch, linger=1.0)
    asked = []

    def should_pause() -> bool:
        asked.append(time.monotonic())
        return asked[-1] - asked[0] < 2.0

    began = time.monotonic()
    ffmpeg_module.remux_stream(
        iter([b"abc" * 1000]), str(out), timeout=1.5, should_pause=should_pause
    )

    assert time.monotonic() - began >= 2.0
    assert out.exists() and (tmp_path / "out.mp4.done").exists()


def test_streamed_remux_waits_under_the_stop_pause_and_time_limit_of_the_download(
    tmp_path, monkeypatch
):
    """구간 없는 후처리는 remux에 한 시간의 제한 시간과 다운로드의 중단 · 일시정지 확인을 넘겨야 한다.

    세그먼트 하나. 대역 remux가 받은 인자로 상태를 RUNNING · PAUSED · WAITING으로 바꿔 가며 물어봄
    -> timeout == 3600, (should_stop, should_pause) == RUNNING (거짓, 거짓) · PAUSED (거짓, 참) ·
       WAITING (참, 거짓)
    """
    fake = _FakeEngine(tmp_path)
    segment = tmp_path / "0000.ts"
    segment.write_bytes(b"seg-bytes")
    seen = {}

    def asking(chunks, dst_path, **wait):
        for _chunk in chunks:
            pass
        seen["timeout"] = wait["timeout"]
        for state in (DownloadState.RUNNING, DownloadState.PAUSED, DownloadState.WAITING):
            fake.state = state
            seen[state] = (wait["should_stop"](), wait["should_pause"]())
        fake.state = DownloadState.RUNNING

    monkeypatch.setattr(base_module, "remux_stream", asking)

    BaseDownloader._remux_streamed(fake, [str(segment)])

    assert seen == {
        "timeout": 3600,
        DownloadState.RUNNING: (False, False),
        DownloadState.PAUSED: (False, True),
        DownloadState.WAITING: (True, False),
    }


def test_streamed_remux_stopped_while_waiting_ends_without_a_failure(tmp_path, monkeypatch):
    """remux가 기다리다 중단 요청으로 끝났으면 구간 없는 후처리는 실패가 아니라 중단으로 끝나야 한다.

    세그먼트 하나. 대역 remux가 FFmpegCancelledError를 냄 -> 예외 없이 돌아온다, 오류 로그 없음
    """
    fake = _FakeEngine(tmp_path)
    segment = tmp_path / "0000.ts"
    segment.write_bytes(b"seg-bytes")

    def cancelled(chunks, dst_path, **_wait):
        raise ffmpeg_module.FFmpegCancelledError("stopped")

    monkeypatch.setattr(base_module, "remux_stream", cancelled)

    BaseDownloader._remux_streamed(fake, [str(segment)])

    assert fake.errors == []


# ============ 오류 글의 꼬리 (#397)


def test_error_tail_that_would_start_mid_line_starts_at_the_head_of_that_line():
    """끝 limit 글자가 줄 중간에서 시작하면 꼬리는 그 줄의 머리에서 시작하고 글자 수 꼬리를 다 담아야 한다 (#397).

    "first line\\nsecond line\\nthird", limit=10 (글자 수 꼬리 "line\\nthird")
    -> "second line\\nthird"
    """
    text = "first line\nsecond line\nthird"

    tail = ffmpeg_module.error_tail(text, 10)

    assert tail == "second line\nthird"
    assert tail.endswith(text[-10:])


def test_error_tail_that_starts_at_a_line_head_is_the_character_tail():
    """끝 limit 글자가 줄 머리에서 시작하면 꼬리는 글자 수 꼬리 그대로여야 한다 (#397).

    "first line\\nthird", limit=5
    -> "third"
    """
    assert ffmpeg_module.error_tail("first line\nthird", 5) == "third"


def test_error_tail_treats_a_carriage_return_as_a_line_break():
    """줄 바꿈이 \\r뿐인 글(ffmpeg의 진행 줄)에서도 줄 머리에서 시작해야 한다 (#397).

    "progress 1\\rframe 22 end", limit=6 (글자 수 꼬리 "22 end", \\r 다음 줄 머리는 6글자 앞)
    -> "frame 22 end" (낱말 머리 "22 end"가 아니라 줄 머리)
    """
    assert ffmpeg_module.error_tail("progress 1\rframe 22 end", 6) == "frame 22 end"


def test_error_tail_of_a_short_text_is_the_whole_stripped_text():
    """글이 limit보다 짧으면 앞뒤 공백을 걷은 글 전체여야 한다 (#397).

    "  short\\n", limit=500
    -> "short"
    """
    assert ffmpeg_module.error_tail("  short\n", 500) == "short"


def test_error_tail_of_one_word_longer_than_limit_from_its_line_head_is_the_character_tail():
    """줄 머리가 limit보다 멀고 그 줄이 끝까지 한 낱말이면 글자 수 꼬리여야 한다 (#397).

    "head\\n" + "a" * 30 + "end", limit=10 (줄 머리가 꼬리 시작에서 23글자 앞, 공백 없음)
    -> 끝 10글자
    """
    text = "head\n" + "a" * 30 + "end"

    assert ffmpeg_module.error_tail(text, 10) == text[-10:]


def test_error_tail_in_a_long_line_starts_at_the_head_of_the_word_it_would_cut():
    """줄 머리가 limit보다 멀면 자른 자리가 든 낱말의 머리(앞 공백 다음)에서 시작해야 한다 (#397).

    "w " * 20 + "C:/aa/bbbb/c.mp4 end", limit=12 (글자 수 꼬리 "bb/c.mp4 end", 낱말 머리는 8글자 앞)
    -> "C:/aa/bbbb/c.mp4 end"
    """
    assert (
        ffmpeg_module.error_tail("w " * 20 + "C:/aa/bbbb/c.mp4 end", 12) == "C:/aa/bbbb/c.mp4 end"
    )


def test_error_tail_in_a_long_line_drops_the_rest_of_a_word_whose_head_is_too_far():
    """줄 머리도 낱말 머리도 limit보다 멀면 자른 자리가 든 낱말의 남은 부분을 버려야 한다 (#397).

    "w " * 20 + "C:/" + "a" * 30 + "/b.mp4 end tail", limit=12 (낱말 머리가 꼬리 시작에서 36글자 앞)
    -> "end tail"
    """
    text = "w " * 20 + "C:/" + "a" * 30 + "/b.mp4 end tail"

    assert ffmpeg_module.error_tail(text, 12) == "end tail"


# ============ 오류 글의 머리 (#397)


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        ("open C:/aa/bbbb/c failed", 13, "open C:/aa/bbbb"),  # "bb|bb" — 다음 구분자까지
        ("open C:\\aa\\bbbb failed", 13, "open C:\\aa\\bbbb"),  # \\ 구분자 · 다음 공백까지
        ("open C:/aa/bbbb", 13, "open C:/aa/bbbb"),  # 조각이 글 끝까지 — 글 끝까지
    ],
)
def test_error_head_that_would_end_inside_a_piece_ends_at_the_end_of_that_piece(
    text, limit, expected
):
    """앞 limit 글자가 경로 조각 안에서 끝나면 그 조각의 끝(공백 · 경로 구분자 · 글 끝)까지 늘려야 한다 (#397).

    글, limit -> expected
    """
    assert ffmpeg_module.error_head(text, limit) == expected


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        ("open C:/aa/bbbb", 11, "open C:/aa/"),  # 자른 자리 앞이 구분자
        ("open C:/aa/bbbb", 10, "open C:/aa"),  # 자른 자리 뒤가 구분자
        ("short", 50, "short"),  # limit보다 짧다
    ],
)
def test_error_head_on_a_piece_boundary_is_the_character_head(text, limit, expected):
    """자른 자리가 조각 경계이거나 글이 limit보다 짧으면 앞 limit 글자 그대로여야 한다 (#397).

    글, limit -> expected
    """
    assert ffmpeg_module.error_head(text, limit) == expected


def test_error_head_whose_piece_end_is_farther_than_limit_ends_before_that_piece():
    """조각 끝이 limit보다 멀면 그 조각 앞에서 잘라야 한다 (#397).

    "open " + "a" * 30, limit=10 (조각 끝이 자른 자리에서 25글자 뒤)
    -> "open"
    """
    assert ffmpeg_module.error_head("open " + "a" * 30, 10) == "open"


def test_error_head_of_one_long_piece_is_the_character_head():
    """글 전체가 limit보다 긴 한 조각이면 앞 limit 글자여야 한다 (#397).

    "a" * 30, limit=10 -> "a" * 10
    """
    assert ffmpeg_module.error_head("a" * 30, 10) == "a" * 10
