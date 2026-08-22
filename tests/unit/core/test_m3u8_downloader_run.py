"""M3U8Downloader.run 통합 검증 — 가짜 세션으로 전체 파이프라인 실행 (#74).

실제 네트워크 없이 m3u8 플레이리스트·초기화 세그먼트·세그먼트 스트림을
흉내 내는 세션으로 다음을 검증한다:
- 완주: 결과 파일이 초기화 세그먼트 + 세그먼트들을 **인덱스 순서대로** 이어붙인
  것과 바이트 단위로 동일(병합 순서 보장), 병합 시작 콜백·완료 콜백 각 1회,
  임시 폴더 삭제
- 일시정지 → 재개 → 완료: PAUSED 동안 진행 정지, 재개 후 완주,
  허용되지 않는 상태 전이(warning) 0건
- 중단(stop): 결과 파일·임시 폴더 삭제, 완료 콜백 없음
"""

import os
import threading
import time

import core.downloaders.base as base_module
import core.downloaders.m3u8_downloader as m3u8_module
from core.downloaders.m3u8_downloader import M3U8Downloader
from core.models.download_state import DownloadState
from core.models.download_data import DownloadData

CHUNK = 8192
SEGMENT_COUNT = 6
BASE_URL = "https://example.invalid/hls/video.m3u8"

INIT_CONTENT = b"\xf0" * CHUNK


def _segment_content(index: int, chunks_per_segment: int) -> bytes:
    """세그먼트별로 구별되는 결정적 본문 — 병합 순서를 바이트로 검증하기 위함."""
    return bytes([index + 1]) * (CHUNK * chunks_per_segment)


def _playlist_text() -> str:
    """EXT-X-MAP 초기화 세그먼트와 세그먼트 목록을 가진 최소 플레이리스트."""
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", '#EXT-X-MAP:URI="init.m4s"']
    for i in range(SEGMENT_COUNT):
        lines.append("#EXTINF:2.000,")
        lines.append(f"seg_{i}.m4v")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines)


class StreamResponse:
    """세그먼트 본문을 청크로 흘리는 가짜 응답. throttle 초/청크로 속도를 조절한다."""

    def __init__(self, body: bytes, throttle: float):
        self._body = body
        self._throttle = throttle

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=CHUNK):
        for i in range(0, len(self._body), chunk_size):
            if self._throttle:
                time.sleep(self._throttle)
            yield self._body[i : i + chunk_size]


class TextResponse:
    """플레이리스트(.text)·초기화 세그먼트(.content) 겸용 가짜 응답."""

    def __init__(self, text: str = "", content: bytes = b""):
        self.text = text
        self.content = content

    def raise_for_status(self):
        pass


class M3U8Session:
    """m3u8 경로의 세 요청(플레이리스트/초기화/세그먼트)을 URL로 분기하는 가짜 세션."""

    def __init__(self, chunks_per_segment: int, throttle: float = 0.0):
        self._chunks_per_segment = chunks_per_segment
        self._throttle = throttle

    def get(self, url, **kwargs):
        if url.endswith(".m3u8"):
            return TextResponse(text=_playlist_text())
        if url.endswith("init.m4s"):
            return TextResponse(content=INIT_CONTENT)
        index = int(url.rsplit("seg_", 1)[1].removesuffix(".m4v"))
        return StreamResponse(_segment_content(index, self._chunks_per_segment), self._throttle)


def _expected_output(chunks_per_segment: int) -> bytes:
    """초기화 세그먼트 + 세그먼트들을 인덱스 순서로 이어붙인 기대 결과."""
    return INIT_CONTENT + b"".join(
        _segment_content(i, chunks_per_segment) for i in range(SEGMENT_COUNT)
    )


def _passthrough_remux_stream(chunks, dst_path):
    """받은 스트림을 그대로 파일에 쓰는 remux_stream 스텁 (공급 순서·바이트 검증용)."""
    with open(dst_path, "wb") as f:
        for chunk in chunks:
            f.write(chunk)


class RunLogger:
    """run() 경로 전체가 쓰는 로거 인터페이스를 기록만 하며 흉내 낸다."""

    def __init__(self):
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.completed_times: list[float] = []
        self.closed = 0
        # 단계 경계 로그 (#110)
        self.transfer_completes: list[tuple] = []
        self.postprocess_starts: list[str] = []
        self.postprocess_completes: list[tuple] = []
        self.breakdowns: list[tuple] = []

    def log_download_start(self, total_size, part_size, segments, initial_threads):
        pass

    def log_m3u8_thread_start(self, thread_id, segment_url):
        pass

    def log_transfer_complete(self, elapsed, downloaded_bytes, retries, peak_threads):
        self.transfer_completes.append((elapsed, downloaded_bytes, retries, peak_threads))

    def log_postprocess_start(self, kind):
        self.postprocess_starts.append(kind)

    def log_postprocess_complete(self, elapsed, output_size):
        self.postprocess_completes.append((elapsed, output_size))

    def log_total_breakdown(self, transfer_elapsed, postprocess_elapsed):
        self.breakdowns.append((transfer_elapsed, postprocess_elapsed))

    def log_thread_complete(self, thread_id, downloaded_size):
        pass

    def log_thread_adjust(self, active_threads, avg_speed):
        pass

    def log_thread_debug(self, active_threads, download_speed, avg_speed):
        pass

    def log_download_complete(self, total_time):
        self.completed_times.append(total_time)

    def log_error(self, message, exception=None):
        self.errors.append(message)

    def log_exception(self, message, exception=None):
        self.errors.append(message)

    def warning(self, message):
        self.warnings.append(message)

    def save_and_close(self):
        self.closed += 1


def _make_engine(tmp_path, monkeypatch, chunks_per_segment: int = 3, throttle: float = 0.0):
    """가짜 세션이 연결된 RUNNING 이전 상태의 엔진을 준비한다."""
    output = tmp_path / "out.mp4"
    data = DownloadData(
        base_url=BASE_URL,
        vod_url="https://chzzk.naver.com/video/1",
        output_path=str(output),
        resolution=1080,
        content_type="m3u8",
    )
    logger = RunLogger()
    session = M3U8Session(chunks_per_segment, throttle)
    monkeypatch.setattr(m3u8_module, "get_thread_session", lambda: session)
    # remux(#88·#92)는 성공 시 스트림 복사 재포장이다 — 이 파일의 검증 대상은
    # 공급 순서·바이트이므로 받은 스트림을 그대로 쓰는 스텁으로 대체해 가짜
    # 세션과 같은 수준으로 히메틱하게 유지한다. 실제 ffmpeg 실행은
    # test_ffmpeg_utils.py가 검증한다
    monkeypatch.setattr(base_module, "remux_stream", _passthrough_remux_stream)

    finished = threading.Event()
    failures: list[BaseException] = []
    merge_starts: list[bool] = []

    def on_finished():
        # 실제 어댑터 경로(manager.finish → task.finish)를 흉내 내 상태를 종결한다
        data.model.finish()
        finished.set()

    engine = M3U8Downloader(
        data,
        logger,
        on_finished=on_finished,
        on_failed=failures.append,
        on_merge_start=lambda: merge_starts.append(True),
    )
    return engine, data, logger, output, finished, failures, merge_starts


def _run_in_thread(engine) -> threading.Thread:
    """엔진 run()을 별도 스레드에서 실행한다 (호출 규약: start() 후 run())."""
    thread = threading.Thread(target=engine.run, daemon=True)
    thread.start()
    return thread


def test_run_merges_segments_in_index_order_byte_exact(tmp_path, monkeypatch):
    """전체 파이프라인이 초기화+세그먼트 순서 그대로의 파일을 만들고 콜백을 1회씩 호출한다."""
    engine, data, logger, output, finished, failures, merge_starts = _make_engine(
        tmp_path, monkeypatch
    )

    data.model.start()
    thread = _run_in_thread(engine)
    assert finished.wait(timeout=30), "완료 콜백이 호출되지 않았다"
    thread.join(timeout=10)

    assert output.read_bytes() == _expected_output(chunks_per_segment=3)
    assert merge_starts == [True]  # 병합 시작 통지 1회
    assert failures == []
    assert logger.errors == []
    assert logger.warnings == []  # 느린 세그먼트 오탐·전이 warning 없음
    assert logger.completed_times and logger.closed == 1
    assert data.completed_threads == SEGMENT_COUNT  # 세그먼트 전부 정상 완료
    assert data.merged_segments == SEGMENT_COUNT + 1  # 초기화 세그먼트 포함 병합
    assert not (tmp_path / "CVDv2_temp_out").exists()  # 임시 폴더 삭제 (#105 산출물 파생 이름)


def test_remux_failure_fails_explicitly_and_preserves_segments(tmp_path, monkeypatch):
    """remux 실패는 폴백 없이 명시적 실패다 — 산출물 없음·세그먼트 보존 (#92).

    바이트 연결본은 #88이 고치려던 결함품이므로 대신 내주지 않는다.
    세그먼트는 재다운로드를 강요하지 않기 위해 보존한다.
    """
    engine, data, logger, output, finished, failures, merge_starts = _make_engine(
        tmp_path, monkeypatch
    )

    def broken_remux_stream(chunks, dst_path):
        # 실제 remux_stream처럼 불완전 산출물을 지우고 실패를 알린다
        raise base_module.FFmpegError("가짜 remux 실패")

    monkeypatch.setattr(base_module, "remux_stream", broken_remux_stream)

    data.model.start()
    thread = _run_in_thread(engine)
    thread.join(timeout=30)
    assert not thread.is_alive()

    assert not finished.is_set()  # 완료가 아니다
    assert not output.exists()  # 결함품 산출물을 내주지 않는다
    assert any(isinstance(e, base_module.PostprocessError) for e in failures)  # 명시적 실패
    assert logger.errors  # 원인 로그 존재
    # 세그먼트(임시 폴더)는 보존된다 — 후처리 실패로 재다운로드를 강요하지 않는다
    temp_dir = tmp_path / "CVDv2_temp_out"
    assert temp_dir.exists()
    assert len(list(temp_dir.iterdir())) == SEGMENT_COUNT + 1  # 초기화 세그먼트 포함
    assert merge_starts == [True]


def test_stop_during_postprocess_cleans_up_without_failure(tmp_path, monkeypatch):
    """후처리 공급 중 중단하면 실패 없이 중단 정리(산출물·임시 폴더 삭제)로 끝난다 (#92)."""
    engine, data, logger, output, finished, failures, merge_starts = _make_engine(
        tmp_path, monkeypatch
    )

    fed_once = threading.Event()

    def slow_remux_stream(chunks, dst_path):
        # 천천히 소비한다 — 그 사이 stop이 들어오면 공급측(feed)이 중단
        # 신호를 던지고, 그 예외는 실제 remux_stream처럼 그대로 전파된다
        for _ in chunks:
            fed_once.set()
            time.sleep(0.05)

    monkeypatch.setattr(base_module, "remux_stream", slow_remux_stream)

    data.model.start()
    thread = _run_in_thread(engine)
    assert fed_once.wait(timeout=30), "후처리 공급이 시작되지 않았다"
    data.model.stop()  # RUNNING → WAITING (취소)
    thread.join(timeout=30)
    assert not thread.is_alive()

    assert not finished.is_set()
    assert failures == []  # 중단은 실패가 아니다
    assert not output.exists()
    assert not (tmp_path / "CVDv2_temp_out").exists()  # 중단은 현행대로 전체 정리


def test_pause_resume_then_complete(tmp_path, monkeypatch):
    """일시정지 동안 진행이 멈추고, 재개 후 완주한다. 전이 warning 0건."""
    # 청크당 5ms 스로틀 × 세그먼트당 120청크 → 일시정지할 여유를 만들고,
    # 일시정지 시점까지 누적 바이트를 충분히 키워 재개 직후 평균 속도가
    # 저속 판정(<100 KB/s)에 걸리는 오탐을 피한다 (판정식은 누적/경과 기반)
    engine, data, logger, output, finished, failures, merge_starts = _make_engine(
        tmp_path, monkeypatch, chunks_per_segment=120, throttle=0.005
    )
    # 저속 재큐 판정 비활성 (#160) — 느린 CI 러너에서는 위 오탐 회피
    # (누적 바이트 키우기)로도 부족해 정상적인 저속 재큐 warning이 남아
    # "전이 warning 0건" 단언이 깨졌다(macOS 실측). 검증 대상은 일시정지·
    # 재개이지 저속 규칙이 아니다 — 저속 규칙은 rules 테스트가 박제한다
    engine._slow_speed_threshold_kb_s = 0

    data.model.start()
    thread = _run_in_thread(engine)

    time.sleep(0.25)  # 다운로드가 진행되는 중간 지점
    assert data.model.pause() is True  # RUNNING → PAUSED (유효 전이)
    assert data.model.state is DownloadState.PAUSED

    # 워커들이 pause_event 대기에 도달할 시간을 준 뒤 진행량이 고정되는지 확인
    time.sleep(0.3)
    snapshot = sum(data.threads_progress) + data.completed_progress
    time.sleep(0.5)
    assert sum(data.threads_progress) + data.completed_progress == snapshot

    assert data.model.resume() is True  # PAUSED → RUNNING (유효 전이)
    assert finished.wait(timeout=60), "재개 후 완료 콜백이 호출되지 않았다"
    thread.join(timeout=10)

    assert output.read_bytes() == _expected_output(chunks_per_segment=120)
    assert failures == []
    assert logger.warnings == []  # 상태 전이 warning 0건 (완료 조건)
    assert data.model.state is DownloadState.FINISHED


def test_phase_boundary_logs_for_m3u8_path(tmp_path, monkeypatch):
    """m3u8 경로의 단계 경계 로그 (#110) — 전송 요약 → remux 시작·종료 → 전체 구분."""
    engine, data, logger, output, finished, failures, merge_starts = _make_engine(
        tmp_path, monkeypatch
    )

    data.model.start()
    thread = _run_in_thread(engine)
    assert finished.wait(timeout=30), "완료 콜백이 호출되지 않았다"
    thread.join(timeout=10)

    assert len(logger.transfer_completes) == 1
    _elapsed, downloaded, retries, peak = logger.transfer_completes[0]
    assert downloaded > 0 and retries == 0 and peak >= 1
    assert logger.postprocess_starts == ["remux"]  # 무엇을 하는지
    assert len(logger.postprocess_completes) == 1
    _pp_elapsed, output_size = logger.postprocess_completes[0]
    assert output_size == output.stat().st_size  # 최종 산출물 크기
    assert len(logger.breakdowns) == 1
    _transfer, postprocess = logger.breakdowns[0]
    assert postprocess is not None  # 전체 = 전송 + 후처리 구분
    assert logger.completed_times  # 기존 완료 줄(형식 불변)도 그대로


def test_monitor_thread_is_stopped_before_postprocess(tmp_path, monkeypatch):
    """후처리 중에는 관측 스레드가 살아 있지 않다 — 병합 꼬리 로그의 원천 제거 (#89).

    관측 정지가 remux 시작보다 먼저임을 remux 스텁 안에서 직접 확인한다.
    """
    engine, data, logger, output, finished, failures, merge_starts = _make_engine(
        tmp_path, monkeypatch
    )

    monitor_alive_during_remux = []

    def checking_remux_stream(chunks, dst_path):
        monitor_alive_during_remux.append(
            any(t.name == "DownloadMonitor" and t.is_alive() for t in threading.enumerate())
        )
        _passthrough_remux_stream(chunks, dst_path)

    monkeypatch.setattr(base_module, "remux_stream", checking_remux_stream)

    data.model.start()
    thread = _run_in_thread(engine)
    assert finished.wait(timeout=30), "완료 콜백이 호출되지 않았다"
    thread.join(timeout=10)

    assert monitor_alive_during_remux == [False]
    assert output.read_bytes() == _expected_output(chunks_per_segment=3)  # 완주 무영향


def test_postprocess_progress_is_emitted_by_feed_loop(tmp_path, monkeypatch):
    """후처리 진행 통지는 공급 루프가 담당한다 (#89) — 병합 % 변화마다 1회.

    병합 구간 이벤트는 구 관측 스레드의 병합 관측과 동일하게 속도 0·활성
    스레드 0이어야 한다 (어댑터 변환식 무변경 → UI 표시 결과 무변경).
    """
    engine, data, logger, output, finished, failures, merge_starts = _make_engine(
        tmp_path, monkeypatch
    )

    events = []  # (이벤트, 통지 시점의 병합 세그먼트 수)
    engine.set_on_progress(lambda event: events.append((event, data.merged_segments)))

    data.model.start()
    thread = _run_in_thread(engine)
    assert finished.wait(timeout=30), "완료 콜백이 호출되지 않았다"
    thread.join(timeout=10)

    merge_events = [(e, merged) for e, merged in events if merged > 0]
    # 초기화 세그먼트 포함 7개 병합 → int(k/7*100)이 매 세그먼트마다 변해 7회 통지
    assert len(merge_events) == SEGMENT_COUNT + 1
    assert merge_events[-1][1] == SEGMENT_COUNT + 1  # 마지막 통지가 100% 시점
    for event, _merged in merge_events:
        assert event.speed == 0.0
        assert event.active_threads == 0


def test_postprocess_progress_throttled_to_percent_changes(tmp_path, monkeypatch):
    """세그먼트가 %당 여러 개면 % 변화 시에만 통지한다 — 최대 ~101회로 묶인다 (#89)."""
    engine, data, logger, output, finished, failures, merge_starts = _make_engine(
        tmp_path, monkeypatch
    )

    seg_dir = tmp_path / "segs"
    seg_dir.mkdir()
    paths = []
    for i in range(300):
        p = seg_dir / f"{i:04d}.m4s"
        p.write_bytes(b"x")
        paths.append(str(p))

    events = []
    engine.set_on_progress(lambda event: events.append(event))
    data.model.start()

    engine._remux_streamed(paths)

    assert data.merged_segments == 300
    # int(k/300*100), k=1..300은 0~100의 정수를 모두 한 번씩 지난다 → 정확히 101회
    assert len(events) == 101


def test_stop_removes_partial_file_and_temp_dir(tmp_path, monkeypatch):
    """중단(stop)하면 임시 폴더·부분 파일을 삭제하고 완료 콜백을 호출하지 않는다."""
    engine, data, logger, output, finished, failures, merge_starts = _make_engine(
        tmp_path, monkeypatch, chunks_per_segment=40, throttle=0.005
    )

    data.model.start()
    thread = _run_in_thread(engine)

    time.sleep(0.25)
    data.model.stop()  # RUNNING → WAITING (취소)
    thread.join(timeout=30)
    assert not thread.is_alive()

    assert not output.exists()  # 부분 파일 없음
    assert not (tmp_path / "CVDv2_temp_out").exists()  # 임시 폴더 삭제
    assert not finished.is_set()
    assert failures == []


def test_forced_slow_requeue_does_not_corrupt_output(tmp_path, monkeypatch):
    """저속 재큐가 여러 번 발동해도 최종 산출물은 손상되지 않는다 (#180 조사).

    #180 초기 진단은 macOS 실기 로그의 "Retries: 14" + remux 실패(exit 183,
    Invalid data)를 근거로 "재큐된 세그먼트가 기존 파일에 이어쓰기돼 컨테이너가
    깨진다"는 가설을 세웠다. 코드 확인 결과 세그먼트 파일은 매 시도(재큐 포함)
    마다 ``open(temp_file, "wb")``로 새로 열린다 — "wb"는 매번 트렁케이트라
    이어쓰기가 될 수 없다. 이 테스트는 그 반증을 실측으로 고정한다: 저속
    임계를 극단적으로 높여 강제로 재큐를 유발한 뒤, 최종 산출물이 정상
    세그먼트를 이어붙인 것과 바이트 단위로 같은지 확인한다.
    """
    engine, data, logger, output, finished, failures, merge_starts = _make_engine(
        tmp_path, monkeypatch, chunks_per_segment=20
    )
    # 극단적으로 높은 임계로 모든 청크를 "저속"으로 오판시켜 강제 재큐를 유발한다
    engine._slow_speed_threshold_kb_s = 1e15

    def _disable_after_first_requeue():
        while data.restart_threads == 0:
            time.sleep(0.01)
        # 재큐가 최소 1회 발동한 뒤에는 정상 임계로 되돌려 완주시킨다
        engine._slow_speed_threshold_kb_s = 0

    watcher = threading.Thread(target=_disable_after_first_requeue, daemon=True)
    watcher.start()

    data.model.start()
    thread = _run_in_thread(engine)
    assert finished.wait(timeout=30), "완료 콜백이 호출되지 않았다"
    thread.join(timeout=10)
    watcher.join(timeout=5)

    assert data.restart_threads > 0, "저속 재큐가 실제로 발동해야 이 테스트가 유효하다"
    assert output.read_bytes() == _expected_output(chunks_per_segment=20)
    assert failures == []


def test_stray_dotfile_in_temp_dir_is_excluded_from_merge(tmp_path, monkeypatch):
    """세그먼트가 아닌 파일이 임시 폴더에 있어도 병합에서 제외되고 경고가 남는다 (#180).

    exFAT 등 xattr 미지원 파일시스템에 macOS가 쓰는 AppleDouble 사이드카
    (``._세그먼트명``)나 Finder의 ``.DS_Store``처럼 '.'으로 시작하는 이름은
    사전순 정렬에서 항상 맨 앞에 온다 — 오너 실기(exFAT 외장 SD 카드)에서
    세그먼트 수만큼 실물로 확인된 오염이다. ``_list_segment_files``가
    화이트리스트(숫자+확장자)로 걸러 이 오염을 막는지 검증한다.
    """
    engine, data, logger, output, finished, failures, merge_starts = _make_engine(
        tmp_path, monkeypatch
    )

    def _pollute_then_start_merge():
        # AppleDouble류 사이드카뿐 아니라 확장자 없는 잡파일까지 함께 심는다
        for name in ("._0000000.m4s", ".DS_Store", "notes.txt"):
            with open(os.path.join(engine.temp_dir, name), "wb") as f:
                f.write(b"not-a-segment")
        merge_starts.append(True)

    engine._on_merge_start = _pollute_then_start_merge

    data.model.start()
    thread = _run_in_thread(engine)
    assert finished.wait(timeout=30), "완료 콜백이 호출되지 않았다"
    thread.join(timeout=10)

    # 오염 파일이 있었는데도 산출물은 오염 없이 정상 그대로다
    assert output.read_bytes() == _expected_output(chunks_per_segment=3)
    assert failures == []
    # 조용히 건너뛰지 않는다 — 무엇을 걸렀는지 경고로 남긴다
    assert len(logger.warnings) == 1
    assert "._0000000.m4s" in logger.warnings[0]
    assert ".DS_Store" in logger.warnings[0]
    assert "notes.txt" in logger.warnings[0]


def test_downstream_stop_after_postprocess_failure_preserves_segments(tmp_path, monkeypatch):
    """실패 콜백이 비동기로 상태를 WAITING으로 돌려도 세그먼트는 보존된다 (#185).

    base.py의 PostprocessError 분기는 _cleanup_partial()을 부르지 않아 세그먼트를
    보존하도록 설계됐다(#92 — 재다운로드 강요 방지). 그런데 실제 프로덕션 배선
    (download/qt_bridge.py)에서는 on_failed 콜백(_relay_failed)이 Qt 큐드 시그널을
    거쳐 메인 스레드의 _onEngineFailed에서 task.stop()을 호출한다 — 상태를
    WAITING으로 되돌린다. 고장난 버전에서는 run()의 try/except/finally 전체
    바깥에 있던 "if WAITING: cleanup_partial()"이 이 비동기 전이를 "유저가
    중단한 경우"로 오인해 방금 보존하려던 세그먼트를 도로 지웠다(오너 실기
    관찰과 일치 — #180 조사 중 이 테스트로 고장난 커밋에서 먼저 실패를
    재현한 뒤 수정함).

    수정: 그 체크를 try 블록 안(예외 없이 끝난 경로에서만 닿는 지점)으로
    옮겨, except 분기가 이미 내린 정리 결정을 이후의 비동기 상태 변화가
    다시 뒤집지 못하게 한다 — 엔진 종료 신호(WAITING)와 실패 처리를 같은
    체크 하나로 뭉뚱그리지 않는다(#135와 같은 자리).

    on_failed 안에서 model.stop()을 호출해 프로덕션의 결과를 직접 재현한다.
    """
    engine, data, logger, output, finished, failures, merge_starts = _make_engine(
        tmp_path, monkeypatch
    )

    def broken_remux_stream(chunks, dst_path):
        raise base_module.FFmpegError("가짜 remux 실패")

    monkeypatch.setattr(base_module, "remux_stream", broken_remux_stream)

    # 실제 qt_bridge._onEngineFailed가 (큐드 커넥션을 거쳐) 하는 일 — task.stop()
    original_on_failed = engine._on_failed

    def on_failed_then_stop(exc):
        original_on_failed(exc)
        data.model.stop()

    engine._on_failed = on_failed_then_stop

    data.model.start()
    thread = _run_in_thread(engine)
    thread.join(timeout=30)
    assert not thread.is_alive()

    assert any(isinstance(e, base_module.PostprocessError) for e in failures)
    temp_dir = tmp_path / "CVDv2_temp_out"
    # #92 정책: 후처리 실패는 세그먼트를 보존해야 한다 — 실패 콜백이 상태를
    # WAITING으로 돌리더라도(비동기 stop()) 이 정책은 지켜져야 한다.
    assert temp_dir.exists(), "후처리 실패 시 세그먼트가 보존돼야 하는데 지워졌다"
    assert len(list(temp_dir.iterdir())) == SEGMENT_COUNT + 1  # 초기화 세그먼트 포함
