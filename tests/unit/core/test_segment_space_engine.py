"""세그먼트 다운로드의 여유 공간 검사가 엔진에서 도는지 — m3u8 · 암호화 VOD (#309).

가짜 세션으로 엔진을 끝까지 돌린다. 여유 공간은 segment_space.free_bytes를 바꿔 정한다.

- 시작 전: 선언된 비트레이트로 어림한 크기가 들어가지 않으면 세그먼트를 하나도 청하지 않고 실패
- 전송 중: 20개를 받은 뒤 실제 평균으로 다시 어림해, 들어가지 않으면 전송을 멈추고 실패

전송 중 검사는 실행 루프가 0.1초마다 본다. 가짜 세션이 21번째부터의 세그먼트 요청을 붙잡아 두어
"20개를 받았고 받을 것이 남은" 상태를 만든다 — 붙잡지 않으면 검사가 돌기 전에 전부 받아질 수 있다.
"""

import threading

import pytest

import core.downloaders.base as base_module
import core.downloaders.hls_aes_downloader as aes_module
import core.downloaders.m3u8_downloader as m3u8_module
import core.downloaders.segment_space as space_module
from core.downloaders.base import (
    DISK_SPACE_SHORT_WHOLE,
    FILE_TOO_LARGE_FOR_FAT,
    DiskSpaceError,
    FileSizeLimitError,
)
from core.downloaders.m3u8_downloader import M3U8Downloader
from core.models.content import StreamKey
from core.models.download_data import DownloadData
from tests.unit.core import test_hls_aes_downloader as aes_tests
from tests.unit.core.test_m3u8_downloader_run import (
    BASE_URL,
    CHUNK,
    INIT_CONTENT,
    RunLogger,
    StreamResponse,
    TextResponse,
    _passthrough_remux_stream,
)
from tests.unit.fmp4_samples import media_segment

MIB = 1024 * 1024
COUNT = 24  # 세그먼트 수 — 다시 어림하는 기준(20개)을 넘고 받을 것이 4개 남는다
SECONDS = 2.0  # 세그먼트 하나의 길이(#EXTINF)
SEGMENT_BYTES = CHUNK * 3  # 세그먼트 하나의 크기 — 24,576바이트
HOLD_FROM = 20  # 이 인덱스부터의 세그먼트 요청을 붙잡는다 — 0~19번 20개만 받아진다
BANDWIDTH = 8_000_000  # 선언된 비트레이트(bit/s) — 1,000,000바이트/초
# 시작 전 어림: 1,000,000바이트/초 x 48초
START_ESTIMATE = 48_000_000
START_NEEDED = 52_800_000 + 256 * MIB  # x 1.1 + 256MiB
# 전송 중 어림: 받은 20개 + 남은 4개(실제 평균으로) = 24 x 24,576
TRANSFER_ESTIMATE = COUNT * SEGMENT_BYTES
# 더 쓸 양은 남은 4개 = 98,304바이트 -> x 1.1 = 108,134(내림) + 256MiB
TRANSFER_NEEDED = 108_134 + 256 * MIB


def _playlist_text() -> str:
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", '#EXT-X-MAP:URI="init.m4s"']
    for index in range(COUNT):
        lines += [f"#EXTINF:{SECONDS:.3f},", f"seg_{index}.m4v"]
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines)


class GatedSession:
    """m3u8 경로의 가짜 세션 — hold_from번부터의 세그먼트 요청은 release가 설 때까지 붙잡는다."""

    def __init__(self, hold_from: int | None = None):
        self.hold_from = hold_from
        self.release = threading.Event()
        self.segments: list[int] = []  # 청한 세그먼트의 인덱스

    def get(self, url, **kwargs):
        if url.endswith(".m3u8"):
            return TextResponse(text=_playlist_text())
        if url.endswith("init.m4s"):
            return TextResponse(content=INIT_CONTENT)
        index = int(url.rsplit("seg_", 1)[1].removesuffix(".m4v"))
        self.segments.append(index)
        if self.hold_from is not None and index >= self.hold_from:
            self.release.wait(timeout=30)
        return StreamResponse(media_segment(SEGMENT_BYTES, bytes([index + 1])), 0.0)


class SpaceLogger(RunLogger):
    """여유 공간 줄을 적어 두는 로거 — 전송 중 검사가 적히면 붙잡아 둔 요청을 푼다."""

    def __init__(self, release: threading.Event):
        super().__init__()
        self.space: list[tuple] = []
        self._release = release

    def log_space_check(self, stage, segments, output, checks):
        self.space.append((stage, segments, output, checks))
        if stage == "transfer":
            self._release.set()


class _Run:
    """m3u8 엔진 한 번의 실행."""

    def __init__(self, tmp_path, monkeypatch, free: int, bandwidth: int | None, hold: bool):
        self.output = tmp_path / "out.mp4"
        self.data = DownloadData(
            base_url=BASE_URL,
            vod_url="https://chzzk.naver.com/video/1",
            output_path=str(self.output),
            resolution=1080,
            content_type="m3u8",
        )
        if bandwidth is not None:
            self.data.content.stream = StreamKey(1920, 1080, 60.0, bandwidth)
        self.session = GatedSession(HOLD_FROM if hold else None)
        self.logger = SpaceLogger(self.session.release)
        monkeypatch.setattr(m3u8_module, "get_thread_session", lambda: self.session)
        monkeypatch.setattr(base_module, "remux_stream", _passthrough_remux_stream)
        monkeypatch.setattr(space_module, "same_volume", lambda a, b: True)
        monkeypatch.setattr(space_module, "free_bytes", lambda folder: free)
        self.finished = 0
        self.failures: list[BaseException] = []
        self.engine = M3U8Downloader(
            self.data, self.logger, on_finished=self._on_finished, on_failed=self._on_failed
        )
        self.engine._slow_speed_threshold_kb_s = 0  # 러너 속도와 무관하게 — 저속 재큐를 끈다

    def _on_finished(self) -> None:
        self.finished += 1

    def _on_failed(self, exc: BaseException) -> None:
        self.failures.append(exc)
        self.session.release.set()  # 붙잡아 둔 요청을 풀어 받는 스레드가 끝나게 한다

    def start(self) -> "_Run":
        self.data.model.start()
        thread = threading.Thread(target=self.engine.run, daemon=True)
        thread.start()
        thread.join(timeout=60)
        assert not thread.is_alive(), "엔진이 끝나지 않았다"
        return self


def test_start_check_fails_before_any_segment_is_requested(tmp_path, monkeypatch):
    """시작 전 어림이 여유 공간에 1바이트라도 안 들어가면 세그먼트를 청하지 않고 실패해야 한다.

    선언 8,000,000 bit/s, 2초 세그먼트 24개 -> 어림 48,000,000, 필요 52,800,000 + 256MiB
    여유 = 필요 − 1
    -> 실패 1건(DiskSpaceError, 키 "Not enough disk space for the download" — 구간 없는
       다운로드의 키, needed · free 그대로),
       청한 세그먼트 0개, 완료 0회, 산출물 · 임시 폴더 없음
    """
    run = _Run(tmp_path, monkeypatch, free=START_NEEDED - 1, bandwidth=BANDWIDTH, hold=False)

    run.start()

    assert [type(e) for e in run.failures] == [DiskSpaceError]
    error = run.failures[0]
    assert (error.message_key, error.needed, error.free) == (
        DISK_SPACE_SHORT_WHOLE,
        START_NEEDED,
        START_NEEDED - 1,
    )
    assert (run.session.segments, run.finished) == ([], 0)
    assert not run.output.exists() and not (tmp_path / "CVDv2_temp_out").exists()


def test_start_check_passes_when_the_free_space_equals_the_needed_bytes(tmp_path, monkeypatch):
    """여유 공간이 필요한 양과 같으면 받기를 시작해 끝까지 가야 한다.

    선언 8,000,000 bit/s, 2초 세그먼트 24개, 여유 = 52,800,000 + 256MiB
    -> 완료 1회, 실패 0건, 시작 줄: ("start", 48,000,000, 48,000,000, (("both", 필요, 여유),))
    """
    run = _Run(tmp_path, monkeypatch, free=START_NEEDED, bandwidth=BANDWIDTH, hold=False)

    run.start()

    assert (run.finished, run.failures) == (1, [])
    assert run.logger.space[0] == (
        "start",
        START_ESTIMATE,
        START_ESTIMATE,
        (("both", START_NEEDED, START_NEEDED),),
    )


def test_start_check_is_skipped_without_a_declared_bitrate(tmp_path, monkeypatch):
    """비트레이트를 모르면 여유 공간이 0이어도 받기를 시작해야 한다 — 시작 줄이 없어야 한다.

    변형의 정체 없음, 여유 0, 21번째부터의 요청을 붙잡음
    -> 세그먼트를 20개 넘게 청했고(전송이 돌았다), 여유 공간 줄에 "start"가 없음
    """
    run = _Run(tmp_path, monkeypatch, free=0, bandwidth=None, hold=True)

    run.start()

    assert len(run.session.segments) >= HOLD_FROM
    assert "start" not in [line[0] for line in run.logger.space]


def test_transfer_check_stops_the_transfer_when_the_real_sizes_do_not_fit(tmp_path, monkeypatch):
    """20개를 받은 뒤 실제 평균으로 본 남은 양이 안 들어가면 전송을 멈추고 실패해야 한다.

    변형의 정체 없음(시작 전 검사 없음), 세그먼트 24개 x 24,576바이트, 20개 받은 뒤 여유 5바이트
    -> 실패 1건(DiskSpaceError, needed = 4 x 24,576 x 1.1(내림) + 256MiB, free = 5),
       완료 0회, 산출물 · 임시 폴더 없음, 전송 줄의 세그먼트 어림 = 24 x 24,576
    """
    run = _Run(tmp_path, monkeypatch, free=5, bandwidth=None, hold=True)

    run.start()

    assert [type(e) for e in run.failures] == [DiskSpaceError]
    error = run.failures[0]
    assert (error.message_key, error.needed, error.free) == (
        DISK_SPACE_SHORT_WHOLE,
        TRANSFER_NEEDED,
        5,
    )
    assert run.finished == 0
    assert not run.output.exists() and not (tmp_path / "CVDv2_temp_out").exists()
    assert run.logger.space == [
        ("transfer", TRANSFER_ESTIMATE, TRANSFER_ESTIMATE, (("both", TRANSFER_NEEDED, 5),))
    ]


def test_transfer_check_uses_the_real_average_not_the_declared_bitrate(tmp_path, monkeypatch):
    """전송 중 어림은 선언된 비트레이트가 아니라 받은 세그먼트의 실제 크기로 해야 한다.

    선언 8,000,000 bit/s(시작 어림 48,000,000), 실제 세그먼트 24,576바이트 x 24, 여유 넉넉
    -> 완료 1회, 줄 2개: start 48,000,000 · transfer 589,824(= 전송 줄의 받은 바이트)
    """
    run = _Run(tmp_path, monkeypatch, free=10**12, bandwidth=BANDWIDTH, hold=True)

    run.start()

    assert (run.finished, run.failures) == (1, [])
    assert [(line[0], line[1]) for line in run.logger.space] == [
        ("start", START_ESTIMATE),
        ("transfer", TRANSFER_ESTIMATE),
    ]
    assert run.logger.transfer_completes[0][1] == TRANSFER_ESTIMATE


def test_volumes_apart_are_checked_one_by_one(tmp_path, monkeypatch):
    """임시 폴더와 저장 폴더가 다른 볼륨이면 볼륨마다 따로 견줘야 한다.

    선언 8,000,000 bit/s, 다른 볼륨, 임시 폴더 쪽 여유는 넉넉하고 저장 폴더 쪽 여유 = 필요 − 1
    -> 실패 1건(DiskSpaceError, needed = 52,800,000 + 256MiB, free = 그 값 − 1)
    """
    run = _Run(tmp_path, monkeypatch, free=10**12, bandwidth=BANDWIDTH, hold=False)
    temp_dir, output_dir = run.engine._space_folders()
    free = {temp_dir: 10**12, output_dir: START_NEEDED - 1}
    monkeypatch.setattr(space_module, "same_volume", lambda a, b: False)
    monkeypatch.setattr(space_module, "free_bytes", lambda folder: free[folder])

    run.start()

    assert [(type(e), e.needed, e.free) for e in run.failures] == [
        (DiskSpaceError, START_NEEDED, START_NEEDED - 1)
    ]
    assert run.logger.space == [
        (
            "start",
            START_ESTIMATE,
            START_ESTIMATE,
            (("temp", START_NEEDED, 10**12), ("output", START_NEEDED, START_NEEDED - 1)),
        )
    ]


# ================================================================ FAT 계열의 파일 크기 한계


def _on_fat(monkeypatch, name: str, limit: int) -> list[tuple]:
    """저장 폴더의 파일 시스템 이름과 파일 하나의 한계를 정해 둔다 — 파일 크기 줄을 적을 목록을 돌려준다."""
    monkeypatch.setattr(base_module, "filesystem_name", lambda folder: name)
    monkeypatch.setattr(base_module, "FAT_MAX_FILE_BYTES", limit)
    return []


@pytest.mark.parametrize(
    ("name", "over", "refused"),
    [("FAT32", 1, True), ("FAT32", 0, False), ("exFAT", 1, False), (None, 1, False)],
    ids=["fat-over", "fat-at-limit", "exfat-over", "unknown-over"],
)
def test_start_check_refuses_an_output_over_the_fat_limit(
    tmp_path, monkeypatch, name, over, refused
):
    """구간 없는 m3u8은 저장 폴더가 FAT 계열이고 어림한 산출물이 한계를 넘을 때만 받지 않고 실패해야 한다.

    선언 8,000,000 bit/s, 2초 세그먼트 24개 -> 어림 48,000,000. 한계를 어림 − 1(넘는다) 또는
    어림(꼭 맞는다)으로 둠. 파일 시스템은 표의 것, 여유 공간은 넉넉함
    -> 거부면: 실패 1건(FileSizeLimitError, 키 "File too large for a FAT32 drive", largest 48,000,000),
       청한 세그먼트 0개, 완료 0회 / 아니면: 완료 1회, 실패 0건
    """
    run = _Run(tmp_path, monkeypatch, free=10**13, bandwidth=BANDWIDTH, hold=False)
    _on_fat(monkeypatch, name, START_ESTIMATE - over)

    run.start()

    if refused:
        assert [(type(e), e.message_key, e.largest) for e in run.failures] == [
            (FileSizeLimitError, FILE_TOO_LARGE_FOR_FAT, START_ESTIMATE)
        ]
        assert (run.session.segments, run.finished) == ([], 0)
    else:
        assert (run.finished, run.failures) == (1, [])


def test_transfer_check_stops_the_transfer_when_the_output_is_over_the_fat_limit(
    tmp_path, monkeypatch
):
    """받기 전 어림이 없는 다운로드는 20개를 받은 뒤의 어림이 FAT의 한계를 넘으면 전송을 멈추고 실패해야 한다.

    변형의 정체 없음(시작 전 검사 없음), 세그먼트 24개 x 24,576바이트(어림 589,824), 파일 시스템
    "FAT32", 한계 589,823, 여유 공간 넉넉함
    -> 실패 1건(FileSizeLimitError, largest 589,824), 완료 0회, 산출물 · 임시 폴더 없음
    """
    run = _Run(tmp_path, monkeypatch, free=10**13, bandwidth=None, hold=True)
    _on_fat(monkeypatch, "FAT32", TRANSFER_ESTIMATE - 1)

    run.start()

    assert [(type(e), e.largest) for e in run.failures] == [(FileSizeLimitError, TRANSFER_ESTIMATE)]
    assert run.finished == 0
    assert not run.output.exists() and not (tmp_path / "CVDv2_temp_out").exists()


# ================================================================ 암호화 VOD (TS)


class _GatedAesSession(aes_tests.FakeSession):
    """암호화 VOD의 가짜 세션 — hold_from번부터의 세그먼트 요청을 붙잡는다."""

    def __init__(self, playlist: str, hold_from: int):
        super().__init__(playlist)
        self.hold_from = hold_from
        self.release = threading.Event()

    def get(self, url, **kwargs):
        if not url.endswith("media.m3u8"):
            index = int(url.rsplit("segment-", 1)[1].removesuffix(".ts"))
            if index >= self.hold_from:
                self.release.wait(timeout=30)
        return super().get(url, **kwargs)


def _aes_playlist() -> str:
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-MEDIA-SEQUENCE:0"]
    lines.append(f'#EXT-X-KEY:METHOD=AES-128,URI="{aes_tests.KEY_URI}"')
    for index in range(COUNT):
        lines += ["#EXTINF:3.840,", f"segment-{index:06d}.ts"]
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines)


@pytest.mark.parametrize("free", [5, 10**12], ids=["short", "enough"])
def test_encrypted_vod_transfer_check_counts_the_decrypted_segments(tmp_path, monkeypatch, free):
    """암호화 VOD도 20개를 받은 뒤 디스크에 쓴(복호화한) 크기로 다시 어림해 여유 공간을 봐야 한다.

    세그먼트 24개, 복호화한 크기 376바이트(TS 패킷 2개), 3.84초. 20개 받은 뒤 검사
    -> 더 쓸 양 4 x 376 = 1,504 -> 필요 1,654 + 256MiB
       여유 5: 실패 1건(DiskSpaceError), 완료 안 함 / 여유 넉넉: 완료, 실패 0건
    """
    engine, data, _logger, finished, failures, _merges = aes_tests._make_engine(
        tmp_path, monkeypatch, playlist=_aes_playlist(), key_resolver=lambda c, u: aes_tests.KEY
    )
    session = _GatedAesSession(_aes_playlist(), HOLD_FROM)
    monkeypatch.setattr(aes_module, "get_thread_session", lambda: session)
    monkeypatch.setattr(space_module, "same_volume", lambda a, b: True)
    monkeypatch.setattr(space_module, "free_bytes", lambda folder: free)
    engine._slow_speed_threshold_kb_s = 0
    checked: list[tuple] = []

    def log_space_check(stage, segments, output, checks):
        checked.append((stage, segments, checks))
        session.release.set()

    engine.logger.log_space_check = log_space_check
    engine._on_failed = lambda exc: (failures.append(exc), session.release.set())

    data.model.start()
    thread = threading.Thread(target=engine.run, daemon=True)
    thread.start()
    thread.join(timeout=60)

    assert not thread.is_alive(), "엔진이 끝나지 않았다"
    needed = 1_654 + 256 * MIB
    assert checked == [("transfer", COUNT * 376, (("both", needed, free),))]
    if free == 5:
        assert [(type(e), e.needed, e.free) for e in failures] == [(DiskSpaceError, needed, 5)]
        assert not finished.is_set()
    else:
        assert failures == [] and finished.is_set()


def test_encrypted_vod_transfer_check_stops_when_the_output_is_over_the_fat_limit(
    tmp_path, monkeypatch
):
    """암호화 VOD(받기 전 어림 없음)도 20개를 받은 뒤의 어림이 FAT의 한계를 넘으면 전송을 멈추고 실패해야 한다.

    세그먼트 24개, 복호화한 크기 376바이트(어림 9,024), 파일 시스템 "FAT32", 한계 9,023
    -> 실패 1건(FileSizeLimitError, largest 9,024), 완료 안 함
    """
    engine, data, _logger, finished, failures, _merges = aes_tests._make_engine(
        tmp_path, monkeypatch, playlist=_aes_playlist(), key_resolver=lambda c, u: aes_tests.KEY
    )
    session = _GatedAesSession(_aes_playlist(), HOLD_FROM)
    monkeypatch.setattr(aes_module, "get_thread_session", lambda: session)
    monkeypatch.setattr(space_module, "same_volume", lambda a, b: True)
    monkeypatch.setattr(space_module, "free_bytes", lambda folder: 10**13)
    _on_fat(monkeypatch, "FAT32", COUNT * 376 - 1)
    engine._slow_speed_threshold_kb_s = 0
    engine._on_failed = lambda exc: (failures.append(exc), session.release.set())

    data.model.start()
    thread = threading.Thread(target=engine.run, daemon=True)
    thread.start()
    thread.join(timeout=60)

    assert not thread.is_alive(), "엔진이 끝나지 않았다"
    assert [(type(e), e.largest) for e in failures] == [(FileSizeLimitError, COUNT * 376)]
    assert not finished.is_set()


def test_file_size_check_compares_the_size_it_reports_when_the_estimate_is_a_fraction(
    tmp_path, monkeypatch
):
    """어림이 소수여도 파일 크기 검사는 예외 · 로그에 적는 그 값을 한계와 견줘야 한다.

    파일 시스템 "FAT32", 어림 1000.5바이트. 한계 1000으로 검사한 뒤, 한계를 예외에 적힌 크기로
    두고 다시 검사함
    -> 한계 1000: FileSizeLimitError(largest == 1001 — 바이트로 올림),
       한계 1001: 통과, 파일 크기 줄이 두 번 다 ("FAT32", 1001)
    """
    run = _Run(tmp_path, monkeypatch, free=10**13, bandwidth=BANDWIDTH, hold=False)
    _on_fat(monkeypatch, "FAT32", 1000)
    sizes = []
    run.logger.log_file_size_check = lambda name, largest: sizes.append((name, largest))

    with pytest.raises(FileSizeLimitError) as refused:
        run.engine._require_file_size_fits(1000.5)
    monkeypatch.setattr(base_module, "FAT_MAX_FILE_BYTES", refused.value.largest)
    run.engine._require_file_size_fits(1000.5)

    assert refused.value.largest == 1001
    assert sizes == [("FAT32", 1001), ("FAT32", 1001)]
