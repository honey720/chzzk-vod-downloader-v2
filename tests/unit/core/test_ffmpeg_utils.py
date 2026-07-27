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


def test_remux_stream_produces_mp4_with_leading_moov(tmp_path):
    """파이프 공급 산출물은 mp4이고 전역 인덱스(moov)가 mdat보다 앞에 있어야 한다."""
    src = tmp_path / "src.mp4"
    dst = tmp_path / "dst.mp4"
    _make_tiny_fmp4(src)

    remux_stream(read_in_chunks(str(src)), str(dst))

    boxes = _top_level_boxes(dst)
    assert "moov" in boxes and "mdat" in boxes
    # -movflags +faststart의 목적 그 자체 — moov가 mdat보다 앞이다
    assert boxes.index("moov") < boxes.index("mdat")


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
    assert boxes.index("moov") < boxes.index("mdat")


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
    assert boxes.index("moov") < boxes.index("mdat")  # 재개 후 정상 완주
    assert fake.s.merged_segments == len(paths)


def test_stop_while_paused_kills_ffmpeg_and_leaves_no_output(tmp_path):
    """stdin 대기 상태에서 중단하면 산출물 없이 조용히 끝나고 세그먼트는 남는다 (#92).

    중단 시 세그먼트·임시 폴더 삭제는 엔진(run)의 중단 정리 몫이므로
    _remux_streamed 자신은 아무것도 지우지 않아야 한다.
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
    assert boxes.index("moov") < boxes.index("mdat")


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
