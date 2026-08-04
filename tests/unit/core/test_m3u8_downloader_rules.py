"""m3u8 스레드 스케일링·느린 세그먼트 판정·속도 계산 규칙 박제 (#74 착수 조건 1).

이 테스트는 m3u8 다운로드 엔진 이주(QThread → threading) 전후 모두 통과해야 한다.
규칙 자체를 고정하는 것이 목적이므로, 시나리오와 단언은 이주 후에도 바꾸지 않는다.
이주 시에는 아래 팩토리(_make_scaler/_make_engine)의 대상 클래스만 교체한다.

박제하는 규칙 (현행 동작 그대로 — 파일 경로(#73)와의 차이는 ★ 표시):
- ★ 기준 속도는 해상도별 테이블을 따른다: 144→0.2, 360·480→0.5, 720→1.2, 그 외→3 (MB/s)
- 스레드 증가: 활성 스레드당 평균 속도 > 기준 속도 틱마다 adjust_count += 1,
  adjust_count > 1이면 adjust_threads를 +4 (max_threads 상한), 카운터 리셋
- 스레드 감소: 평균 속도 < 기준 속도/2 틱마다 adjust_count -= 1,
  adjust_count < -4이면 adjust_threads를 절반으로 (하한 1), 카운터 리셋
- 중간 대역(기준/2 ~ 기준)에서는 adjust_count가 0을 향해 1씩 감쇠
- 속도 계산: 직전 틱 대비 바이트 증가량을 MB/s로 환산, prev_size 갱신
- 느린 세그먼트: 청크 속도 < 100 KB/s가 연속 6회(slow_count > 5) 누적되면
  해당 세그먼트를 중단하고 (index, segment)를 재큐잉(restart_threads += 1)
- 세그먼트 실패: 요청 예외 시 (index, segment) 재큐잉(failed_threads += 1)
- ★ 세그먼트 임시 파일명은 (index+1)을 width 자리로 0채움한 .m4v — 병합 순서의 전제
"""

import pytest
import requests

from core.models.download_data import DownloadData

MB = 1024 * 1024


class RecordingLogger:
    """DownloadLogger 호환 가짜 로거 — 실제 파일을 만들지 않고 호출만 기록한다."""

    def __init__(self):
        self.adjust_calls: list[tuple] = []
        self.debug_calls: list[tuple] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.thread_completes: list[tuple] = []

    def log_thread_adjust(self, active_threads, avg_speed):
        self.adjust_calls.append((active_threads, avg_speed))

    def log_thread_debug(self, active_threads, download_speed, avg_speed):
        self.debug_calls.append((active_threads, download_speed, avg_speed))

    def log_m3u8_thread_start(self, thread_id, segment_url):
        pass

    def log_thread_complete(self, thread_id, downloaded_size):
        self.thread_completes.append((thread_id, downloaded_size))

    def log_error(self, message, exception=None):
        self.errors.append(message)

    def warning(self, message):
        self.warnings.append(message)


def _make_data(resolution: int = 1080, output_path: str = "unused.mp4") -> DownloadData:
    """테스트용 DownloadData를 만든다 (네트워크 정보는 더미)."""
    return DownloadData(
        base_url="https://example.invalid/hls/video.m3u8",
        vod_url="https://chzzk.naver.com/video/1",
        output_path=output_path,
        resolution=resolution,
        content_type="m3u8",
    )


def _make_scaler(data: DownloadData, logger: RecordingLogger):
    """스케일링 규칙(_adjust_threads/measure_speed) 보유 객체를 만든다.

    이주 전: download.monitor_m3u8.MonitorM3U8Thread / 이주 후: core M3U8Downloader.
    반환 객체는 adjust_count 속성과 세 메서드를 노출해야 한다.
    """
    from core.downloaders.m3u8_downloader import M3U8Downloader

    return M3U8Downloader(data, logger)


def _make_engine(data: DownloadData, logger: RecordingLogger):
    """세그먼트 다운로드 로직(_download_segment) 보유 객체를 만든다.

    이주 전: download.download_m3u8.DownloadM3U8Thread / 이주 후: core M3U8Downloader.
    """
    from core.downloaders.m3u8_downloader import M3U8Downloader

    return M3U8Downloader(data, logger)


def _engine_module():
    """_download_segment가 사는 모듈 — 시간·세션 몽키패치 대상."""
    import core.downloaders.m3u8_downloader as mod

    return mod


# ================================================================ 스레드 조정 (#112 — 총 처리량 등반)
#
# 구 규칙(스레드당 평균 속도 vs 해상도별 기준 테이블)의 박제는 #112로 폐기했다.
# 근거: (1) 치지직은 연결당 처리량을 제한해(144p 연결당 ~0.95 MB/s, 총량은
# 스레드 수에 선형) 스레드당 속도는 줄일 신호가 아니고, (2) 평균의 분모였던
# 순간 활성 수는 세그먼트가 잘게 쪼개진 저해상도에서 빈 슬롯을 '저속'으로
# 오판해 4→2→1 붕괴를 일으켰다. 새 신호는 총 처리량의 실제 증감이다.


def _tick(scaler, data, speed: float) -> None:
    """1초 관측 틱을 흉내 낸다 — 측정된 총 처리량 반영 후 조정 1회."""
    data.speed_mb = speed
    scaler._adjust_threads()


def test_warmup_without_measurement_does_nothing():
    """첫 유효 측정 전(속도 0)에는 조정하지 않는다 — 시작 직후 오판 방지."""
    data, logger = _make_data(), RecordingLogger()
    scaler = _make_scaler(data, logger)

    _tick(scaler, data, 0.0)

    assert data.adjust_threads == 4
    assert logger.adjust_calls == []


def test_first_valid_tick_probes_up_by_4():
    """첫 유효 측정에서 +4 탐침을 시작한다 (구 규칙의 +4 보폭 유지)."""
    data, logger = _make_data(), RecordingLogger()
    data.max_threads = 6996
    scaler = _make_scaler(data, logger)

    _tick(scaler, data, 3.9)

    assert data.adjust_threads == 8
    assert logger.adjust_calls == [(8, 3.9)]


def test_settle_ticks_after_change_skip_decision():
    """조정 직후 2틱은 판단을 쉰다 — 새 연결 램프업이 섞인 측정으로 판단하지 않는다."""
    data, logger = _make_data(), RecordingLogger()
    data.max_threads = 6996
    scaler = _make_scaler(data, logger)

    _tick(scaler, data, 3.9)  # 4→8
    _tick(scaler, data, 100.0)  # settle 1 — 무시된다
    _tick(scaler, data, 100.0)  # settle 2 — 무시된다

    assert data.adjust_threads == 8
    assert logger.adjust_calls == [(8, 3.9)]


def test_climbs_to_cap_while_throughput_scales_linearly():
    """총 처리량이 선형으로 느는 동안 +4 계단으로 상한(48)까지 등반한다.

    #112 실측 재현: 144p 연결당 ~0.95 MB/s 선형 구간. 계단 모양(+4)은
    구 규칙의 1080p 정상 궤적(4→8→…→48)과 동일해야 한다 (회귀 금지 조건).
    """
    data, logger = _make_data(), RecordingLogger()
    data.max_threads = 6996
    scaler = _make_scaler(data, logger)

    for _ in range(40):  # settle 틱 포함 여유 있는 반복
        _tick(scaler, data, data.adjust_threads * 0.95)

    assert data.adjust_threads == 48
    targets = [call[0] for call in logger.adjust_calls]
    assert targets == [8, 12, 16, 20, 24, 28, 32, 36, 40, 44, 48]  # +4 계단


def test_cap_respects_max_threads():
    """상한은 min(max_threads, 48)이다 — 세그먼트가 적으면 그 수가 상한."""
    data, logger = _make_data(), RecordingLogger()
    data.max_threads = 10
    scaler = _make_scaler(data, logger)

    for _ in range(10):
        _tick(scaler, data, data.adjust_threads * 0.95)

    assert data.adjust_threads == 10  # 8+4=12가 아니라 상한 10


def test_reverts_and_holds_when_raise_does_not_increase_throughput():
    """인상해도 총 처리량이 늘지 않으면 되물리고 그 지점을 상한으로 정체한다.

    서버가 총량 기준으로 제한하는 구간에서는 스레드를 늘려도 이득이 없다 —
    이때 연결 수를 아끼는 것이 (d) 규명 결과에 맞는 동작이다.
    """
    data, logger = _make_data(), RecordingLogger()
    data.max_threads = 6996
    scaler = _make_scaler(data, logger)

    _tick(scaler, data, 5.0)  # 4→8 (기준 5.0)
    _tick(scaler, data, 5.0)  # settle 1
    _tick(scaler, data, 5.0)  # settle 2
    _tick(scaler, data, 5.2)  # 요구치(5.0×1.125=5.625) 미달 → 8→4 되물림 + 정체

    assert data.adjust_threads == 4
    assert logger.adjust_calls == [(8, 5.0), (4, 5.2)]

    for _ in range(5):  # 정체 중 같은 속도로는 더 조정하지 않는다
        _tick(scaler, data, 5.0)
    assert data.adjust_threads == 4


def test_growth_requirement_scales_with_target():
    """유효 판정 요구 증가폭은 인상 전 목표에 비례한다.

    고정 비율(+10% 등)은 목표가 커질수록 선형 이득(+4/n)보다 커져 정상
    등반을 막는다 — 목표 40→44 인상의 선형 이득(+9.5%, 38.0→41.8)은
    고정 +10%로는 기각되지만 비례 요구치(38×1.0125=38.475)로는 유효다.
    등반 중간 상태는 시나리오로 만들기 길어 직접 세팅한다.
    """
    data, logger = _make_data(), RecordingLogger()
    data.max_threads = 6996
    data.adjust_threads = 44
    scaler = _make_scaler(data, logger)
    scaler._reference_speed = 38.0  # 목표 40이 내던 총 처리량 (40×0.95)
    scaler._reference_target = 40

    _tick(scaler, data, 41.8)  # 44×0.95 — 선형 이득. 요구치 38.475 초과

    assert data.adjust_threads == 48  # 인상 유효 → 계속 등반


def test_sustained_collapse_halves_after_five_ticks():
    """정체 기준의 절반 미만이 5틱 지속되면 절반으로 줄인다 (관성은 구 규칙과 동일)."""
    data, logger = _make_data(), RecordingLogger()
    data.max_threads = 6996
    scaler = _make_scaler(data, logger)

    _tick(scaler, data, 5.0)  # 4→8
    _tick(scaler, data, 9.0)  # settle 1
    _tick(scaler, data, 9.0)  # settle 2
    _tick(scaler, data, 9.0)  # 요구치(5.0×1.125) 초과 → 8→12 (기준 9.0)
    _tick(scaler, data, 9.2)  # settle 1
    _tick(scaler, data, 9.2)  # settle 2
    _tick(scaler, data, 9.2)  # 요구치(9.0×1.0417=9.375) 미달 → 12→8 되물림 + 정체 (기준 9.2)
    assert data.adjust_threads == 8

    for _ in range(4):  # 기준의 절반(4.6) 미만 4틱 — 아직 불변
        _tick(scaler, data, 4.0)
    assert data.adjust_threads == 8

    _tick(scaler, data, 4.0)  # 5틱째 — 절반으로
    assert data.adjust_threads == 4
    assert logger.adjust_calls[-1] == (4, 4.0)


def test_halving_floor_is_one_thread():
    """처리량 하락이 이어지면 절반씩 1까지 내려가고, 그 밑으로는 내려가지 않는다.

    절반 감소 후에는 그 시점 처리량이 새 기준이 된다 — 추가 감소는 기준
    대비 또다시 절반 미만으로 떨어졌을 때만 일어난다(연쇄 자동 붕괴 방지).
    """
    data, logger = _make_data(), RecordingLogger()
    data.max_threads = 6996
    scaler = _make_scaler(data, logger)

    _tick(scaler, data, 5.0)  # 4→8
    _tick(scaler, data, 5.0)  # settle 1
    _tick(scaler, data, 5.0)  # settle 2
    _tick(scaler, data, 5.0)  # 요구치 미달 → 되물림 → 4 정체 (기준 5.0)
    assert data.adjust_threads == 4

    for _ in range(7):  # 기준(5.0)의 절반 미만 지속 → 4→2 (새 기준 2.0)
        _tick(scaler, data, 2.0)
    assert data.adjust_threads == 2

    for _ in range(7):  # 또 절반 미만으로 하락 → 2→1
        _tick(scaler, data, 0.9)
    assert data.adjust_threads == 1

    for _ in range(10):  # 더 하락해도 1 밑으로는 내려가지 않는다
        _tick(scaler, data, 0.1)
    assert data.adjust_threads == 1


def test_collapse_counter_decays_on_stable_tick():
    """붕괴 카운터는 안정 틱에서 0으로 1씩 되돌아간다 (히스테리시스 유지)."""
    data, logger = _make_data(), RecordingLogger()
    data.max_threads = 6996
    scaler = _make_scaler(data, logger)

    _tick(scaler, data, 5.0)  # 4→8
    _tick(scaler, data, 5.0)  # settle 1
    _tick(scaler, data, 5.0)  # settle 2
    _tick(scaler, data, 5.0)  # 되물림 → 4 정체 (기준 5.0)

    for _ in range(3):  # 붕괴 방향 3틱
        _tick(scaler, data, 2.0)
    assert scaler.adjust_count == -3

    _tick(scaler, data, 5.0)  # 안정 틱 — 감쇠
    assert scaler.adjust_count == -2
    assert data.adjust_threads == 4  # 임계 미달로 불변


def test_hold_resumes_climbing_on_recovery():
    """정체 기준보다 +30% 넘게 빨라지면 등반을 재개한다 (경합 해소 등 상황 변화).

    임계 30%는 링크 포화 구간의 처리량 노이즈(±15% 실측)로 인한 정체↔등반
    진동을 막기 위한 값이다 — 노이즈 범위 안의 출렁임으로는 재개하지 않는다.
    """
    data, logger = _make_data(), RecordingLogger()
    data.max_threads = 6996
    scaler = _make_scaler(data, logger)

    _tick(scaler, data, 5.0)  # 4→8
    _tick(scaler, data, 5.0)  # settle 1
    _tick(scaler, data, 5.0)  # settle 2
    _tick(scaler, data, 5.0)  # 되물림 → 4 정체 (기준 5.0)
    assert data.adjust_threads == 4

    _tick(scaler, data, 5.6)  # +12% — 노이즈 범위, 재개하지 않는다
    assert data.adjust_threads == 4

    _tick(scaler, data, 7.0)  # 기준×1.3(6.5+) 초과 — 재개 신호
    _tick(scaler, data, 7.0)  # 등반 재개: +4 탐침

    assert data.adjust_threads == 8


def test_reprobe_after_fifteen_stall_ticks():
    """정체가 15틱 이어지면 +4 재탐침한다 — 서버 상황 변화를 놓치지 않기 위함."""
    data, logger = _make_data(), RecordingLogger()
    data.max_threads = 6996
    scaler = _make_scaler(data, logger)

    _tick(scaler, data, 5.0)  # 4→8
    _tick(scaler, data, 5.0)  # settle 1
    _tick(scaler, data, 5.0)  # settle 2
    _tick(scaler, data, 5.0)  # 되물림 → 4 정체
    assert data.adjust_threads == 4

    for _ in range(15):  # 안정 정체 15틱
        _tick(scaler, data, 5.0)
    _tick(scaler, data, 5.0)  # 재탐침 틱

    assert data.adjust_threads == 8


def test_zero_active_sample_is_not_a_slow_signal():
    """관측 순간 활성 0이어도 총 처리량이 신호다 — 구 규칙 붕괴 원인의 반전 박제 (#112).

    구 규칙은 활성 0 표본을 평균 0(저속)으로 간주해, 세그먼트가 잘게 쪼개진
    저해상도에서 실제 4.7 MB/s로 받는 중에도 4→2→1로 붕괴시켰다.
    """
    data, logger = _make_data(), RecordingLogger()
    data.max_threads = 6996
    scaler = _make_scaler(data, logger)

    data.future_count = 0  # 빈 슬롯 순간에 관측됐다
    _tick(scaler, data, 3.9)

    assert data.adjust_threads == 8  # 감소가 아니라 정상 탐침


# ================================================================ 속도 계산


def test_measure_speed_uses_delta_from_previous_tick():
    """속도 = (현재 누적 - 직전 누적) 바이트를 MB/s로 환산, prev_size 갱신."""
    data, logger = _make_data(), RecordingLogger()
    scaler = _make_scaler(data, logger)

    data.total_downloaded_size = 5 * MB
    data.prev_size = 2 * MB
    data.future_count = 3

    scaler.measure_speed()

    assert data.speed_mb == pytest.approx(3.0)
    assert data.prev_size == 5 * MB
    assert logger.debug_calls == [(3, pytest.approx(3.0), pytest.approx(1.0))]


def test_measure_speed_with_no_active_threads_reports_zero_average():
    """활성 스레드 0이면 평균 속도는 0으로 기록한다 (0 나눗셈 금지)."""
    data, logger = _make_data(), RecordingLogger()
    scaler = _make_scaler(data, logger)

    data.total_downloaded_size = 1 * MB
    data.prev_size = 0
    data.future_count = 0

    scaler.measure_speed()

    assert logger.debug_calls == [(0, pytest.approx(1.0), 0)]


# ================================================================ 느린 세그먼트·실패 처리


class FakeResponse:
    """스트리밍 응답 흉내 — 지정한 청크 목록을 그대로 흘린다."""

    def __init__(self, chunks):
        self._chunks = chunks

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=8192):
        yield from self._chunks


class FakeSession:
    """get_thread_session() 대체 — 준비된 응답을 반환하거나 예외를 던진다."""

    def __init__(self, response=None, exception=None):
        self._response = response
        self._exception = exception

    def get(self, url, **kwargs):
        if self._exception is not None:
            raise self._exception
        return self._response


class TickingClock:
    """time.time 대체 — 호출마다 지정 간격으로 흐르는 가짜 시계."""

    def __init__(self, step: float):
        self.now = 1_000_000.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def _prepare_running_engine(tmp_path, monkeypatch, chunks=None, exception=None, clock_step=1.0):
    """RUNNING 상태의 엔진과 부속(데이터·로거)을 준비한다.

    clock_step=1.0이면 청크당 1초가 흘러 항상 저속(<100 KB/s) 판정,
    아주 작은 값이면 항상 고속 판정이 난다.
    """
    data = _make_data(output_path=str(tmp_path / "out.mp4"))
    logger = RecordingLogger()
    engine = _make_engine(data, logger)

    data.model.start()  # RUNNING 상태로 진입
    data.threads_progress = [0] * 4
    data.remaining_ranges = []
    # run()이 채우는 실행 컨텍스트를 직접 주입한다 (세그먼트 로직만 단위 검증)
    engine.temp_dir = str(tmp_path)
    engine.width = 4

    mod = _engine_module()
    monkeypatch.setattr(
        mod, "get_thread_session", lambda: FakeSession(FakeResponse(chunks or []), exception)
    )
    monkeypatch.setattr(mod.tm, "time", TickingClock(clock_step))
    return engine, data, logger


def test_slow_segment_restarts_after_six_slow_chunks(tmp_path, monkeypatch):
    """청크 속도 < 100 KB/s 연속 6회(slow_count > 5)면 세그먼트를 중단·재큐잉한다."""
    chunks = [b"x" * 8192] * 10  # 1초/청크 → 8 KB/s로 항상 저속
    engine, data, logger = _prepare_running_engine(
        tmp_path, monkeypatch, chunks=chunks, clock_step=1.0
    )

    returned = engine._download_segment(
        index=7, segment="segment_007.m4v", part_num=0, total_ranges=4
    )

    assert returned == 0
    assert data.restart_threads == 1
    assert data.remaining_ranges == [(7, "segment_007.m4v")]  # 같은 세그먼트 재큐잉
    assert data.threads_progress[0] == 0  # 진행 바이트 롤백
    assert data.completed_threads == 0
    assert any("slow" in w for w in logger.warnings)


def test_fast_segment_completes_and_accumulates_progress(tmp_path, monkeypatch):
    """정상 속도면 세그먼트를 완주하고 완료 카운터·누적 진행에 반영한다."""
    segment_size = 3 * 8192
    chunks = [b"x" * 8192] * 3
    engine, data, logger = _prepare_running_engine(
        tmp_path, monkeypatch, chunks=chunks, clock_step=1e-6
    )

    returned = engine._download_segment(
        index=7, segment="segment_007.m4v", part_num=0, total_ranges=4
    )

    assert returned == 0
    assert data.completed_threads == 1
    assert data.completed_progress == segment_size
    assert data.restart_threads == 0
    assert data.remaining_ranges == []
    assert logger.thread_completes == [(0, segment_size)]
    # 임시 파일명은 (index+1)을 width 자리로 0채움 — sorted() 병합 순서의 전제
    assert (tmp_path / "0008.m4v").read_bytes() == b"x" * segment_size


def test_request_exception_requeues_segment_as_failed(tmp_path, monkeypatch):
    """요청 예외 시 실패 카운터를 올리고 같은 세그먼트를 재큐잉한다."""
    engine, data, logger = _prepare_running_engine(
        tmp_path, monkeypatch, exception=requests.ConnectionError("boom")
    )

    returned = engine._download_segment(
        index=3, segment="segment_003.m4v", part_num=0, total_ranges=4
    )

    assert returned == 0
    assert data.failed_threads == 1
    assert data.remaining_ranges == [(3, "segment_003.m4v")]
    assert data.threads_progress[0] == 0
    assert len(logger.errors) == 1
