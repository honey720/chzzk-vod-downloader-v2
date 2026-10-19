"""FileDownloader의 구간 다운로드 — 받기부터 구간 파일까지 (#309).

실제 ffmpeg(imageio-ffmpeg 동봉)로 테스트 안에서 짧은 mp4를 만들고, 범위 요청에 답하는
호스트(tests/unit/core/range_host.py)로 내준다. FileDownloader를 구간과 함께 실제로
돌려 구간 파일이 나오는지, 그 파일이 정합 판정(check_cut)을 통과하는지 본다.

호스트는 소켓을 열지 않는다 — 테스트 네트워크 가드가 loopback도 막는다. 엔진은 진짜
requests 세션으로 요청하고 진짜 응답 객체를 받으며, 전송 어댑터만 메모리에서 답한다.

입력:
- 기본: 320x240 · 30fps · 6초(180프레임) · B프레임 · 키프레임 0·30·42·72·90·120·150 ·
  moov가 파일 앞
- 프레임이 빠진 것: 위와 같되 프레임 20~22가 없다(뒤 프레임의 번호가 3씩 당겨진다)
- moov가 파일 뒤에 있는 것

핵심 계약:
- 받는 것은 moov(한 번)와 구간 범위뿐이고, 겹치는 범위는 한 번만 받는다
- 받은 바이트는 구간 파일(다 쓰기 전에는 본문 파일)의 제자리에 바로 쓴다. 다시 인코딩할 경계
  GOP의 원본 샘플만 경계 원본에 따로 모은다 — 성공하면 지우고 후처리가 실패하면 남긴다
- 구간 파일의 영상 패킷 · PTS · DTS · 디코드한 프레임은 온전한 파일에서 하이브리드 컷으로 자른
  것과 같다
"""

import errno
import os
import re
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

import core.api.mp4 as mp4_module
import core.downloaders.base as base_module
import core.downloaders.file_downloader as fd_module
import core.utils.hybrid_cut as cut_module
import core.utils.interaction as interaction
import core.utils.mp4_partial as partial_module
from core.api.mp4 import (
    MP4_UNSUPPORTED,
    Mp4Error,
    fetch_mp4_head,
    fetch_mp4_raw,
    pending_mp4_head,
    read_mp4_index,
)
from core.downloaders.base import (
    DISK_SPACE_SHORT,
    FILE_TOO_LARGE_FOR_FAT,
    DiskSpaceError,
    FileSizeLimitError,
    PostprocessError,
    TruncatedBodyError,
)
from core.downloaders.file_downloader import FileDownloader
from core.downloaders.ranges import split_span
from core.models.download_data import DownloadData
from core.models.download_state import DownloadState
from core.models.plan import TimeRange
from core.utils.cut_check import check_cut
from core.utils.ffmpeg import get_ffmpeg_exe
from core.utils.mp4_ranges import (
    frames_byte_range,
    selection_byte_ranges,
    sections_download_size,
)
from core.utils.mp4_section_writer import section_pieces
from core.utils.interaction import resting_while
from core.utils.hybrid_cut import CUT_FAILED, CutError, cut_frames_from_mp4
from core.utils.paths import (
    build_section_output_paths,
    partial_source_path_for,
    section_body_path_for,
)
from core.utils.selections import SELECTION_OUT_OF_RANGE, SelectionError
from tests.unit.core.long_ffmpeg import (
    LONG_RUNNING,
    end_all,
    is_long_running,
    record_processes,
)
from tests.unit.core.reference_cut import hybrid_cut
from tests.unit.core.midway_cut_failure import MidwayCutFailure
from core.utils.retry import RETRY_MAX_DELAY
from tests.unit.core.range_host import RangeHost
from tests.unit.core.section_retry import CutCalls, hand_over, snapshot
from tests.unit.core.undeletable import Undeletable

FPS = 30
KEYFRAMES = (0, 30, 42, 72, 90, 120, 150)  # -force_key_frames 0,1,1.4,2.4,3,4,5 (30fps)
# 엔진 한 번의 실행을 기다려 주는 시간(초). 넘으면 중단을 보낸다. 가장 긴 실행이 로컬에서 1.8초다
# (구간 여럿 · 컷 도중 1초 일시정지) — 그 8배다. CI 러너는 로컬의 두세 배 느리고, 러너가 잠깐
# 바쁘면 거기서 또 두 배쯤 늘어난다
_RUN_LIMIT_SECONDS = 15.0


def _seconds(frame: int) -> float:
    return frame / FPS


def _ffmpeg(*args: str) -> None:
    done = subprocess.run(
        [get_ffmpeg_exe(), "-hide_banner", "-v", "error", "-y", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert done.returncode == 0, done.stderr


def _make_source(path: str, *extra: str, faststart: bool = True) -> None:
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=6",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6",
        *extra,
        "-c:a", "aac", "-ac", "2", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast",
        "-bf", "2", "-force_key_frames", "0,1,1.4,2.4,3,4,5",
        "-x264-params", "b-pyramid=none:keyint=300:min-keyint=1:scenecut=0",
        *(["-movflags", "+faststart"] if faststart else []),
        path,
    )  # fmt: skip


def _index(path: str):
    def read(offset: int, size: int) -> bytes:
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read(size)

    return read_mp4_index(read)


@pytest.fixture(scope="module")
def sources(tmp_path_factory) -> dict[str, str]:
    """이름 → 만든 mp4 파일 경로."""
    folder = tmp_path_factory.mktemp("section_sources")
    paths = {name: str(folder / f"{name}.mp4") for name in ("plain", "gappy", "tail_moov")}
    _make_source(paths["plain"])
    _make_source(
        paths["gappy"],
        # 프레임 20~22를 뺀다. 남은 프레임의 시각은 그대로다(30fps 격자, 512틱)
        "-vf", "settb=1/15360,setpts=N*512,select='not(between(n,20,22))'",
        "-fps_mode", "passthrough", "-enc_time_base:v", "1:15360", "-video_track_timescale", "15360",
    )  # fmt: skip
    _make_source(paths["tail_moov"], faststart=False)
    return paths


@pytest.fixture(scope="module")
def server(sources):
    """만든 파일을 내주는 호스트."""
    files = {}
    for name, path in sources.items():
        with open(path, "rb") as f:
            files[name] = f.read()
    return RangeHost(files)


@pytest.fixture(autouse=True)
def _requests_go_to_host(server, monkeypatch):
    """엔진과 moov 읽기가 쓰는 세션을 호스트로 가는 세션으로 바꾼다."""
    monkeypatch.setattr(fd_module, "get_thread_session", server.session)
    monkeypatch.setattr(mp4_module, "get_thread_session", server.session)


class _Logger:
    """엔진이 부르는 로그 메서드를 이름과 인자로 기록한다."""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args))

        return record


class _LeapingClock:
    """엔진의 재요청 시계(`_retry_now`) 대체 — 읽을 때마다 재요청 대기의 상한만큼 흐른다."""

    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        self.now += RETRY_MAX_DELAY
        return self.now


class _Run:
    """엔진 한 번의 실행과 그 결과."""

    def __init__(self, server, name: str, tmp_path, selections, *, content_type: str = "video"):
        self.folder = tmp_path / "out"
        self.folder.mkdir(exist_ok=True)
        self.data = DownloadData(
            base_url=server.url(name),
            vod_url="https://chzzk.naver.com/video/1",
            output_path=str(self.folder / "unused.mp4"),
            resolution=144,  # 파트 1MB
            content_type=content_type,
        )
        self.data.content.selections = tuple(selections)
        self.data.content.selection_paths = build_section_output_paths(
            str(self.folder), "구간 시험", 144, len(selections)
        )
        self.paths = self.data.content.selection_paths
        self.finished = 0
        self.failures: list[BaseException] = []
        self.logger = _Logger()
        self.engine = FileDownloader(
            self.data,
            self.logger,
            on_finished=self._on_finished,
            on_failed=self.failures.append,
        )
        self.engine._inspect_cuts = True
        # 재요청 대기(#329)를 실제 시간으로 기다리지 않는다 — 읽을 때마다 대기의 상한만큼 흐르는 시계
        self.engine._retry_now = _LeapingClock()

    def _on_finished(self) -> None:
        self.finished += 1

    def start(self) -> "_Run":
        self.data.model.start()
        # 실행이 끝나지 않으면 테스트가 중단을 보낸다 — 제품의 기다림(일시정지 등)에는 상한이 없다
        guard = threading.Timer(_RUN_LIMIT_SECONDS, self.data.model.stop)
        guard.daemon = True
        guard.start()
        try:
            self.engine.run()
        finally:
            guard.cancel()
        return self

    @property
    def source_path(self) -> str:
        return partial_source_path_for(self.paths[0])

    def listing(self) -> list[str]:
        return sorted(os.listdir(self.folder))


def _frames(path: str):
    return cut_frames_from_mp4(_index(path))


def _ffmpeg_lines(*args: str) -> list[str]:
    done = subprocess.run(
        [get_ffmpeg_exe(), "-hide_banner", "-v", "error", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert done.returncode == 0 and not done.stderr.strip(), done.stderr
    return [line for line in done.stdout.splitlines() if line and not line.startswith("#")]


def _video_stamps(path: str) -> list[tuple[str, str]]:
    """영상 패킷마다의 (DTS, PTS) — 파일에 놓인 순서."""
    rows = _ffmpeg_lines("-i", path, "-map", "0:v:0", "-c", "copy", "-f", "framecrc", "-")
    return [tuple(part.strip() for part in row.split(",")[1:3]) for row in rows]


def _decoded_frames(path: str) -> list[str]:
    """디코드한 영상 프레임마다의 해시 — 표시 순서."""
    rows = _ffmpeg_lines("-i", path, "-map", "0:v:0", "-f", "framemd5", "-")
    return [row.split(",")[-1].strip() for row in rows]


def _assert_same_video(made: str, reference: str) -> None:
    """두 파일의 영상 패킷 수 · DTS · PTS와 디코드한 프레임이 같다."""
    assert _video_stamps(made) == _video_stamps(reference)
    frames = _decoded_frames(made)
    assert frames and frames == _decoded_frames(reference)


def _first_sample_offset(index) -> int:
    """원본에서 첫 샘플이 놓인 위치 — 임시 원본의 머리 길이."""
    return min(min(index.video.offsets), min(index.audio.offsets))


def _moov_requests(server, name: str, index) -> list[str]:
    """호스트에 온 요청 가운데 moov가 든 범위를 요청한 것의 Range 머리."""
    first, last = index.moov_range
    found = []
    for _method, requested, header in server.requests:
        match = re.fullmatch(r"bytes=(\d+)-(\d+)", header or "")
        if (
            requested == name
            and match
            and int(match.group(1)) <= last
            and int(match.group(2)) >= first
        ):
            found.append(header)
    return found


# ================================================================ 끝까지 경로


def test_section_download_makes_a_file_that_passes_every_check(server, sources, tmp_path):
    """구간 하나를 받으면 `_1` 파일 하나가 생기고 그 파일이 판정 다섯 항목을 통과해야 한다.

    기본 입력, 구간 프레임 35~80 (키프레임 30 · 42 · 72 사이 — 머리 · 가운데 · 꼬리)
    -> 완료 콜백 1회, 실패 0건, "구간 시험 144p_1.mp4", check.ok
    """
    run = _Run(server, "plain", tmp_path, [TimeRange(_seconds(35), _seconds(80))]).start()

    assert (run.finished, run.failures) == (1, [])
    assert run.listing() == ["구간 시험 144p_1.mp4"]
    section = run.engine.sections[0]
    assert (section.first_frame, section.last_frame) == (35, 80)
    check = check_cut(_frames(sources["plain"]), run.engine.cut_results[0])
    assert check.ok, check.notes


def test_section_file_equals_the_cut_from_the_whole_file(server, sources, tmp_path):
    """구간 파일의 영상 패킷 · DTS · PTS와 디코드한 프레임은 온전한 파일에서 하이브리드 컷으로 자른 파일과 같아야 한다.

    기본 입력, 구간 프레임 35~80
    -> 구간 파일과 hybrid_cut(온전한 파일, 35, 80)의 영상 패킷마다 (DTS, PTS)가 같고,
       디코드한 프레임의 해시가 같다
    """
    run = _Run(server, "plain", tmp_path, [TimeRange(_seconds(35), _seconds(80))]).start()
    reference = str(tmp_path / "reference.mp4")
    hybrid_cut(sources["plain"], _frames(sources["plain"]), 35, 80, reference)

    _assert_same_video(run.paths[0], reference)


def test_several_sections_make_one_file_each(server, sources, tmp_path):
    """구간 셋을 받으면 목록 순서대로 `_1` · `_2` · `_3` 파일이 생기고 모두 판정을 통과해야 한다.

    기본 입력, 구간 프레임 35~80 · 60~100(앞 구간과 겹친다) · 120~140(키프레임에서 시작, 한 GOP 안)
    -> 파일 셋, 구간마다 check.ok, 임시 원본 없음
    """
    selections = [
        TimeRange(_seconds(35), _seconds(80)),
        TimeRange(_seconds(60), _seconds(100)),
        TimeRange(_seconds(120), _seconds(140)),
    ]

    run = _Run(server, "plain", tmp_path, selections).start()

    assert (run.finished, run.failures) == (1, [])
    assert run.listing() == [f"구간 시험 144p_{n}.mp4" for n in (1, 2, 3)]
    frames = _frames(sources["plain"])
    assert [(s.first_frame, s.last_frame) for s in run.engine.sections] == [
        (35, 80),
        (60, 100),
        (120, 140),
    ]
    for result in run.engine.cut_results:
        check = check_cut(frames, result)
        assert check.ok, check.notes


def test_section_plan_holds_only_section_ranges(server, sources, tmp_path):
    """구간 다운로드의 계획에는 구간 범위만 들고 같은 바이트가 두 번 들지 않아야 한다 — moov는 항목이 아니다.

    기본 입력, 구간 프레임 100~110 · 105~115 (서로 겹친다)
    -> 계획의 항목은 모두 첫 샘플 위치부터이고, 서로 겹치지 않고, 합이 total_size이며 원본보다 작다
    """
    selections = [TimeRange(_seconds(100), _seconds(110)), TimeRange(_seconds(105), _seconds(115))]
    run = _Run(server, "plain", tmp_path, selections)
    index = _index(sources["plain"])

    plan = run.engine.prepare(run.data.content)

    items = sorted(plan.items)
    assert items[0][0] >= _first_sample_offset(index) > index.moov_range[1]
    assert all(a[1] < b[0] for a, b in zip(items, items[1:]))  # 겹치지 않는다
    assert plan.total_size == sum(last - first + 1 for first, last in items)
    assert plan.total_size < os.path.getsize(sources["plain"])
    assert plan.requires_postprocess is True
    assert plan.selections == tuple(selections)


def test_overlapping_sections_cost_no_more_than_their_union(server, sources, tmp_path):
    """겹치는 두 구간의 받을 바이트는 두 구간을 합친 한 구간의 받을 바이트와 같아야 한다.

    기본 입력, 구간 프레임 100~110 · 105~115 와, 한 구간 100~115
    -> 두 계획의 total_size가 같다
    """
    two = _Run(
        server,
        "plain",
        tmp_path,
        [TimeRange(_seconds(100), _seconds(110)), TimeRange(_seconds(105), _seconds(115))],
    )
    one = _Run(server, "plain", tmp_path, [TimeRange(_seconds(100), _seconds(115))])

    assert (
        two.engine.prepare(two.data.content).total_size
        == one.engine.prepare(one.data.content).total_size
    )


def test_section_times_are_snapped_to_real_frames(server, sources, tmp_path):
    """구간의 시각은 번호를 계산해서가 아니라 실제 프레임의 PTS에 맞춰 프레임으로 바뀌어야 한다.

    프레임 20~22가 빠진 입력, 구간 40/30초 ~ 80/30초 (그 시각의 프레임은 37번째와 77번째다)
    -> sections[0]의 프레임 (37, 77), check.ok
    """
    run = _Run(server, "gappy", tmp_path, [TimeRange(_seconds(40), _seconds(80))]).start()

    assert run.failures == []
    section = run.engine.sections[0]
    assert (section.first_frame, section.last_frame) == (37, 77)
    check = check_cut(_frames(sources["gappy"]), run.engine.cut_results[0])
    assert check.ok, check.notes


# ================================================================ 임시 원본


def test_partial_source_is_removed_after_success(server, tmp_path):
    """구간을 모두 만들면 임시 원본과 컷의 중간 폴더가 남지 않아야 한다.

    기본 입력, 구간 프레임 35~80
    -> 저장 폴더에 구간 파일 하나뿐
    """
    run = _Run(server, "plain", tmp_path, [TimeRange(_seconds(35), _seconds(80))]).start()

    assert not os.path.exists(run.source_path)
    assert run.listing() == ["구간 시험 144p_1.mp4"]


def test_moov_is_requested_once_per_download(server, sources, tmp_path):
    """구간 다운로드 한 번에 moov가 든 범위는 한 번만 요청해야 한다.

    기본 입력, 구간 둘
    -> 완료 1회, moov와 겹치는 범위 요청 1건
    """
    selections = [TimeRange(_seconds(35), _seconds(80)), TimeRange(_seconds(100), _seconds(110))]
    run = _Run(server, "plain", tmp_path, selections)
    server.forget()

    run.start()

    assert (run.finished, run.failures) == (1, [])
    assert len(_moov_requests(server, "plain", _index(sources["plain"]))) == 1


def test_moov_handed_in_is_not_requested_again(server, sources, tmp_path):
    """이미 받은 moov(Content.mp4_head)를 넘기면 엔진은 moov를 요청하지 않고 같은 구간 파일을 만들어야 한다.

    기본 입력, 구간 프레임 35~80, fetch_mp4_head의 결과를 content.mp4_head에 넣음
    -> moov와 겹치는 범위 요청 0건, 구간 파일의 영상 패킷 · 디코드한 프레임이 온전한 파일에서
       하이브리드 컷으로 자른 파일과 같다
    """
    run = _Run(server, "plain", tmp_path, [TimeRange(_seconds(35), _seconds(80))])
    run.data.content.mp4_head = fetch_mp4_head(server.url("plain"))
    server.forget()

    run.start()

    reference = str(tmp_path / "reference.mp4")
    hybrid_cut(sources["plain"], _frames(sources["plain"]), 35, 80, reference)
    assert (run.finished, run.failures) == (1, [])
    assert _moov_requests(server, "plain", _index(sources["plain"])) == []
    _assert_same_video(run.paths[0], reference)


def test_section_is_cut_from_a_source_whose_mdat_runs_to_end_of_file(
    server, sources, tmp_path, monkeypatch
):
    """임시 원본의 mdat 크기를 0("파일 끝까지")으로 적어도 구간 파일은 판정을 통과해야 한다.

    32비트 상한을 100바이트로 줄여 mdat 크기를 0으로 적게 함, 구간 프레임 35~80
    -> 완료 1회, check.ok
    """
    monkeypatch.setattr(partial_module, "_MAX_BOX_SIZE_32", 100)

    run = _Run(server, "plain", tmp_path, [TimeRange(_seconds(35), _seconds(80))]).start()

    assert (run.finished, run.failures) == (1, [])
    check = check_cut(_frames(sources["plain"]), run.engine.cut_results[0])
    assert check.ok, check.notes


def test_partial_source_is_head_plus_the_bytes_received(server, sources, tmp_path, monkeypatch):
    """컷이 실패해 남은 임시 원본의 크기는 머리 + 받은 바이트 수여야 한다 — 원본 크기가 아니다.

    기본 입력, 파일 뒤쪽의 구간 프레임 150~170, 컷이 CutError를 내도록 바꿈
    -> 임시 원본의 크기 == 첫 샘플의 위치 + 계획의 total_size < 원본 크기
    """

    def broken(*args, **kwargs):
        raise CutError(CUT_FAILED, "시험")

    monkeypatch.setattr(fd_module, "finish_section_file", broken)

    run = _Run(server, "plain", tmp_path, [TimeRange(_seconds(150), _seconds(170))]).start()

    head_size = _first_sample_offset(_index(sources["plain"]))
    assert os.path.getsize(run.source_path) == head_size + run.data.total_size
    assert head_size + run.data.total_size < os.path.getsize(sources["plain"])


def test_cut_failure_keeps_partial_source_and_finished_sections(server, tmp_path, monkeypatch):
    """둘째 구간의 컷이 실패하면 다운로드는 실패하고, 임시 원본과 먼저 만든 구간 파일은 남아야 한다.

    기본 입력, 구간 둘, 둘째 컷이 CutError를 내도록 바꿈
    -> 완료 콜백 0회, 실패 1건(PostprocessError, 원인 CutError), `_1` 파일과 경계 원본,
       끝내지 못한 둘째 구간의 본문 파일만 남음
    """
    real = fd_module.finish_section_file
    calls = []

    def second_fails(*args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            raise CutError(CUT_FAILED, "시험")
        return real(*args, **kwargs)

    monkeypatch.setattr(fd_module, "finish_section_file", second_fails)
    selections = [TimeRange(_seconds(35), _seconds(80)), TimeRange(_seconds(100), _seconds(110))]

    run = _Run(server, "plain", tmp_path, selections).start()

    assert run.finished == 0
    assert len(run.failures) == 1
    assert isinstance(run.failures[0], PostprocessError)
    assert isinstance(run.failures[0].__cause__, CutError)
    assert run.listing() == [
        "CVDv2_body_구간 시험 144p_2.mp4",
        "CVDv2_part_구간 시험 144p_1.mp4",
        "구간 시험 144p_1.mp4",
    ]


def test_cut_failure_of_one_section_still_cuts_the_sections_after_it(server, tmp_path, monkeypatch):
    """구간 셋 가운데 둘째의 컷이 실패해도 셋째는 잘라야 하고, 다운로드는 그 뒤에 한 번 실패해야 한다.

    기본 입력, 구간 셋, 둘째 컷이 첫 ffmpeg 실행 자리에서 출력 파일을 반쯤 쓰고 CutError를 냄
    -> 컷 호출 3회, 완료 콜백 0회, 실패 1건(PostprocessError, 원인 CutError),
       저장 폴더에 `_1` · `_3` 파일과 경계 원본, 둘째 구간의 본문 파일뿐(`_2` 파일 · 컷 작업 폴더 없음),
       공유 데이터의 구간 상태 == (전체 3, 완료 2, 실패 1)
    """
    real = fd_module.finish_section_file
    failure = MidwayCutFailure(monkeypatch)
    calls = []

    def second_fails(*args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            failure.arm(source_path=args[0], output_path=args[4])
        return real(*args, **kwargs)

    monkeypatch.setattr(fd_module, "finish_section_file", second_fails)
    selections = [
        TimeRange(_seconds(35), _seconds(80)),
        TimeRange(_seconds(100), _seconds(110)),
        TimeRange(_seconds(120), _seconds(140)),
    ]

    run = _Run(server, "plain", tmp_path, selections).start()

    assert len(calls) == 3
    assert run.finished == 0
    assert len(run.failures) == 1
    assert isinstance(run.failures[0], PostprocessError)
    assert isinstance(run.failures[0].__cause__, CutError)
    # 실패시킨 순간에는 작업 폴더가 있었다 — 아래의 "없다"가 정리의 결과임을 먼저 확인한다
    assert (failure.fired, failure.work_dir_existed) == (1, True)
    assert os.path.basename(failure.output_path) == "구간 시험 144p_2.mp4"
    assert not os.path.exists(failure.output_path)
    assert not os.path.exists(failure.work_dir)
    # 다시 인코딩의 입력은 경계 원본이고 그것은 남는다. 받은 바이트가 든 본문 파일도 남는다
    assert failure.source_path == os.path.abspath(run.source_path)
    assert run.listing() == [
        "CVDv2_body_구간 시험 144p_2.mp4",
        "CVDv2_part_구간 시험 144p_1.mp4",
        "구간 시험 144p_1.mp4",
        "구간 시험 144p_3.mp4",
    ]
    data = run.data
    assert (data.sections_total, data.sections_done, data.sections_failed) == (3, 2, 1)


def _sizes(paths) -> int:
    return sum(os.path.getsize(path) for path in paths)


def test_engine_reports_the_size_of_the_section_files_it_made(server, tmp_path):
    """구간을 모두 만든 실행은 만든 구간 파일 크기의 합을 공유 데이터에 적어야 한다.

    기본 입력, 구간 셋 -> 완료 1회, sections_output_size == 세 파일 크기의 합 > 0,
    로그의 Output size도 같은 값
    """
    run = _Run(server, "plain", tmp_path, _three())
    assert run.data.sections_output_size is None, "전제: 컷을 마치기 전에는 값이 없다"

    run.start()

    assert (run.finished, run.failures) == (1, [])
    expected = _sizes(run.paths)
    assert expected > 0
    assert run.data.sections_output_size == expected
    ((_elapsed, logged),) = _logged(run, "log_postprocess_complete")
    assert logged == expected


def test_finished_section_run_reports_the_saved_size_as_the_sum_of_its_section_files(
    server, tmp_path
):
    """구간을 모두 만든 mp4 실행은 저장된 크기로 구간 파일 크기의 합을 적어야 한다.

    기본 입력, 구간 셋 -> 완료 1회, output_size == 세 파일 크기의 합 == 로그의 Output size
    """
    run = _Run(server, "plain", tmp_path, _three())
    assert run.data.output_size is None

    run.start()

    assert (run.finished, run.failures) == (1, [])
    ((_elapsed, logged),) = _logged(run, "log_postprocess_complete")
    assert run.data.output_size == _sizes(run.paths) == logged > 0


def test_engine_reports_only_the_files_it_made_when_a_section_fails(server, tmp_path, monkeypatch):
    """일부 구간이 실패한 실행은 만든 구간 파일만의 합을 적어야 한다 — 실패한 구간은 들지 않는다.

    기본 입력, 구간 셋, 둘째 컷이 도중에 실패 -> 실패 1건,
    sections_output_size == `_1` · `_3` 파일 크기의 합(`_2` 파일은 없다)
    """
    cuts = CutCalls(monkeypatch, fd_module, "finish_section_file", source_arg=0)
    cuts.fail_on = (2,)

    run = _Run(server, "plain", tmp_path, _three()).start()

    assert len(run.failures) == 1
    assert not os.path.exists(run.paths[1])
    assert run.data.sections_output_size == _sizes([run.paths[0], run.paths[2]]) > 0


def test_retry_reports_the_size_of_every_section_file_not_only_the_ones_it_made(
    server, tmp_path, monkeypatch
):
    """이어받은 실행은 이전 실행이 만든 구간 파일까지 더한 합을 적어야 한다.

    첫 실행: 구간 셋, 둘째 컷이 도중에 실패. 이어받은 실행이 둘째만 만든다
    -> 완료 1회, sections_output_size == 세 파일 크기의 합 > 둘째 파일의 크기
    """
    cuts = CutCalls(monkeypatch, fd_module, "finish_section_file", source_arg=0)
    cuts.fail_on = (2,)
    failed = _Run(server, "plain", tmp_path, _three()).start()
    assert len(failed.failures) == 1
    cuts.restart()

    again = hand_over(failed, _Run(server, "plain", tmp_path, _three())).start()

    assert (again.finished, again.failures) == (1, [])
    assert cuts.outputs == [THREE_FILES[1]]
    assert again.data.sections_output_size == _sizes(again.paths)
    assert again.data.sections_output_size > os.path.getsize(again.paths[1])


def test_transfer_failure_removes_partial_source(server, tmp_path, monkeypatch):
    """받는 도중 실패하면 임시 원본을 남기지 않아야 한다.

    기본 입력, 구간 프레임 35~80, 파트를 받는 함수가 OSError를 내도록 바꿈
    -> 실패 1건 이상, 저장 폴더가 비어 있다
    """
    run = _Run(server, "plain", tmp_path, [TimeRange(_seconds(35), _seconds(80))])

    def broken(item, part_num):
        raise OSError("디스크가 가득 찼다")

    monkeypatch.setattr(run.engine, "_download_item", broken)

    run.start()

    assert run.finished == 0
    assert run.failures
    assert run.data.model.state == DownloadState.WAITING
    assert run.listing() == []


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("msdos", FILE_TOO_LARGE_FOR_FAT),
        (None, FILE_TOO_LARGE_FOR_FAT),  # 파일 시스템 이름을 읽지 못함
        ("NTFS", "Failed to save file"),
    ],
    ids=["fat", "unknown", "not-fat"],
)
def test_section_write_that_hits_the_file_size_limit_carries_a_key_by_the_filesystem(
    server, tmp_path, monkeypatch, name, expected
):
    """받은 바이트를 구간 파일에 쓰다 EFBIG으로 실패하면 저장 폴더의 파일 시스템에 맞는 키가 적혀야 한다.

    기본 입력, 구간 프레임 35~80. 받은 바이트를 파일에 쓰는 곳(_MapWriter.write)이
    OSError(EFBIG, "파일이 너무 큽니다")를 냄
    -> 실패 1건 이상, 첫 실패의 space_key가 "msdos" · 읽지 못함(None)이면
       "File too large for a FAT32 drive", "NTFS"면 "Failed to save file",
       완료 0회, 저장 폴더가 비어 있다
    """
    run = _Run(server, "plain", tmp_path, [TimeRange(_seconds(35), _seconds(80))])
    monkeypatch.setattr(base_module, "filesystem_name", lambda folder: name)

    def too_large(self, chunk):
        # 오류의 글은 OS의 언어를 따른다 — 영어 문구 없이 번호(errno)로 알아봐야 한다
        raise OSError(errno.EFBIG, "파일이 너무 큽니다")

    monkeypatch.setattr(fd_module._MapWriter, "write", too_large)

    run.start()

    assert run.finished == 0
    assert run.failures
    assert getattr(run.failures[0], "space_key", None) == expected
    assert run.listing() == []


@pytest.mark.parametrize("how", ["failure", "stop"])
def test_section_run_that_fails_or_stops_leaves_the_file_at_output_path_alone(
    server, tmp_path, monkeypatch, how
):
    """구간 다운로드가 실패하거나 중단돼도 output_path 자리에 있던 파일은 그대로 남아야 한다.

    기본 입력, 구간 프레임 35~80, output_path 자리에 내용이 b"keep"인 파일을 미리 둠.
    failure: 컷이 OSError를 냄 / stop: 컷 도중 model.stop()
    -> 저장 폴더에 그 파일 하나뿐이고 내용이 b"keep"
    """
    run = _Run(server, "plain", tmp_path, [TimeRange(_seconds(35), _seconds(80))])
    with open(run.data.output_path, "wb") as f:
        f.write(b"keep")
    real = fd_module.finish_section_file

    def interrupted(*args, **kwargs):
        if how == "failure":
            raise OSError("시험")
        run.data.model.stop()
        return real(*args, **kwargs)

    monkeypatch.setattr(fd_module, "finish_section_file", interrupted)

    run.start()

    assert run.finished == 0
    assert len(run.failures) == (1 if how == "failure" else 0)
    assert run.listing() == ["unused.mp4"]
    with open(run.data.output_path, "rb") as f:
        assert f.read() == b"keep"


def test_finished_run_releases_reserved_names(server, tmp_path, monkeypatch):
    """다운로드가 끝나면(실패 포함) 배정받은 구간 파일명의 예약이 풀려야 한다.

    기본 입력, 구간 하나, 컷이 실패하도록 바꿈(구간 파일이 생기지 않는다)
    -> 같은 제목으로 다시 배정하면 같은 `_1` 경로
    """

    def broken(*args, **kwargs):
        raise CutError(CUT_FAILED, "시험")

    monkeypatch.setattr(fd_module, "finish_section_file", broken)
    run = _Run(server, "plain", tmp_path, [TimeRange(_seconds(35), _seconds(80))])
    held = build_section_output_paths(str(run.folder), "구간 시험", 144, 1)  # 예약이 살아 있는 동안
    assert held != run.paths

    run.start()

    assert build_section_output_paths(str(run.folder), "구간 시험", 144, 1) == run.paths


# ================================================================ 거부


def test_section_outside_the_video_fails_with_selection_key(server, tmp_path):
    """구간이 영상 길이를 넘으면 받기 전에 구간 위반 키로 실패해야 한다.

    기본 입력(6초), 구간 5~7초
    -> 실패 1건(SelectionError, message_key == SELECTION_OUT_OF_RANGE), 저장 폴더가 비어 있다
    """
    run = _Run(server, "plain", tmp_path, [TimeRange(5.0, 7.0)]).start()

    assert run.finished == 0
    assert len(run.failures) == 1
    assert isinstance(run.failures[0], SelectionError)
    assert run.failures[0].message_key == SELECTION_OUT_OF_RANGE
    assert run.failures[0].violations == {0: (SELECTION_OUT_OF_RANGE,)}
    assert run.listing() == []


def test_moov_after_samples_fails_with_unsupported_key(server, tmp_path):
    """moov가 샘플보다 뒤에 있는 mp4의 구간 다운로드는 미지원 키로 실패해야 한다.

    moov가 파일 뒤에 있는 입력, 구간 프레임 35~80
    -> 실패 1건(Mp4Error, message_key == MP4_UNSUPPORTED), 저장 폴더가 비어 있다
    """
    run = _Run(server, "tail_moov", tmp_path, [TimeRange(_seconds(35), _seconds(80))]).start()

    assert len(run.failures) == 1
    assert isinstance(run.failures[0], Mp4Error)
    assert run.failures[0].message_key == MP4_UNSUPPORTED
    assert run.listing() == []


def test_clip_rejects_sections(server, tmp_path):
    """clip에 구간을 주면 run()은 미지원 예외를 내고 아무것도 받지 않아야 한다.

    content_type "clip", 구간 하나
    -> NotImplementedError, 서버에 온 요청 0건
    """
    run = _Run(
        server, "plain", tmp_path, [TimeRange(_seconds(35), _seconds(80))], content_type="clip"
    )
    server.forget()

    with pytest.raises(NotImplementedError):
        run.start()

    assert server.requests == []
    assert run.listing() == []


def test_section_count_must_match_path_count(server, tmp_path):
    """구간 수와 산출물 경로 수가 다르면 prepare는 ValueError를 내야 한다.

    구간 둘, 경로 하나
    -> ValueError
    """
    run = _Run(server, "plain", tmp_path, [TimeRange(1.0, 2.0)])
    run.data.content.selections = (TimeRange(1.0, 2.0), TimeRange(3.0, 4.0))

    with pytest.raises(ValueError):
        run.engine.prepare(run.data.content)


# ================================================================ 전체 다운로드 (보존)


def test_download_without_sections_writes_the_whole_file(server, sources, tmp_path):
    """구간이 없으면 지금처럼 파일 전체를 output_path에 받아야 한다.

    기본 입력, selections 빈 튜플
    -> output_path의 bytes == 원본, 후처리 로그 없음
    """
    run = _Run(server, "plain", tmp_path, [])
    run.start()

    with open(run.data.output_path, "rb") as made, open(sources["plain"], "rb") as wanted:
        assert made.read() == wanted.read()
    assert (run.finished, run.failures) == (1, [])
    assert "log_postprocess_start" not in [name for name, _args in run.logger.calls]


def test_section_log_reports_cut_as_the_postprocess_kind(server, tmp_path):
    """구간 다운로드의 후처리 로그는 종류 "cut"으로 시작하고, 끝 로그의 크기는 구간 파일 크기의 합이어야 한다.

    기본 입력, 구간 둘
    -> log_postprocess_start("cut") 1회, log_postprocess_complete의 크기 == 두 파일 크기의 합
    """
    selections = [TimeRange(_seconds(35), _seconds(80)), TimeRange(_seconds(100), _seconds(110))]

    run = _Run(server, "plain", tmp_path, selections).start()

    calls = dict(run.logger.calls)
    assert calls["log_postprocess_start"] == ("cut",)
    assert calls["log_postprocess_complete"][1] == sum(os.path.getsize(p) for p in run.paths)


# ================================================================ 범위 나누기


@pytest.mark.parametrize(
    ("first", "last", "part_size", "expected"),
    [
        (100, 399, 100, [(100, 199), (200, 299), (300, 399)]),  # 나누어떨어진다
        (100, 349, 100, [(100, 199), (200, 299), (300, 349)]),  # 마지막 조각이 짧다
        (100, 149, 100, [(100, 149)]),  # 한 조각보다 작다
        (7, 7, 100, [(7, 7)]),  # 한 바이트
    ],
)
def test_split_span_splits_a_range_in_the_middle_of_the_file(first, last, part_size, expected):
    """split_span은 파일 중간의 범위를 part_size 단위로 나누고 마지막 조각은 범위 끝에서 잘라야 한다.

    주석의 경우마다 (first, last, part_size)
    -> (start, end) 목록 — 양 끝 포함, 빈틈·겹침 없음
    """
    assert split_span(first, last, part_size) == expected


# ================================================================ 범위 요청에 200이 왔을 때 (#309)


def _response(status: int, length: int | None, closed: list):
    headers = {} if length is None else {"Content-Length": str(length)}
    return SimpleNamespace(status_code=status, headers=headers, close=lambda: closed.append(True))


@pytest.mark.parametrize(
    ("status", "length", "file_size", "start", "rejected"),
    [
        (206, 1000, 5000, 1000, False),  # 범위 응답
        (200, 5000, 5000, 0, False),  # 처음부터 시작하는 범위에 파일 전체를 보냈다
        (200, 1000, 5000, 0, True),  # 200인데 요청한 범위만큼만 잘라 보냈다
        (200, None, 5000, 0, False),  # 길이를 말하지 않은 200 — 가릴 수 없다
        (200, 5000, None, 0, True),  # 구간 다운로드 — 받는 범위가 파일 전체일 수 없다
        (206, 1000, None, 0, False),
        (200, 5000, 5000, 1000, True),  # 처음이 아닌 범위에 파일 전체 — 앞부분이 그 자리에 쓰인다
        (200, None, 5000, 1000, True),  # 처음이 아닌 범위의 200은 길이를 몰라도 거부한다
    ],
    ids=[
        "206",
        "200-whole",
        "200-cut",
        "200-unknown-length",
        "200-in-sections",
        "206-in-sections",
        "200-whole-for-a-later-part",
        "200-unknown-length-for-a-later-part",
    ],
)
def test_part_response_of_200_must_be_usable_at_the_requested_position(
    status, length, file_size, start, rejected
):
    """범위 요청에 200이 왔는데 범위가 파일의 처음이 아니거나, 서버가 말한 본문 길이가 파일 전체 크기와 다르면 잘림 오류로 처리해야 한다.

    주석의 경우마다 (상태 코드, Content-Length, 파일 전체 크기 — 구간 다운로드면 None, 범위의 시작 위치)
    -> 거부하는 경우에만 TruncatedBodyError를 내고 응답을 닫는다
    """
    closed: list = []
    response = _response(status, length, closed)

    if rejected:
        with pytest.raises(TruncatedBodyError):
            FileDownloader._require_whole_file_on_200(response, file_size, start)
        assert closed == [True]
    else:
        FileDownloader._require_whole_file_on_200(response, file_size, start)
        assert closed == []


def test_section_download_fails_when_the_server_answers_a_range_with_a_cut_200(
    server, tmp_path, monkeypatch
):
    """구간 다운로드에서 파트의 범위 요청에 200과 잘린 본문이 오면 그 본문으로 파일을 만들지 않고 실패해야 한다.

    기본 입력, 구간 프레임 35~80. moov는 정상으로 받고, 파트 요청에는 200 · Content-Length 100 · 100바이트로 답하게 함
    -> 완료 0회, 실패 1건 이상이고 첫 실패가 TruncatedBodyError, 저장 폴더가 비어 있다
    """
    run = _Run(server, "plain", tmp_path, [TimeRange(_seconds(35), _seconds(80))])
    run.data.content.mp4_head = fetch_mp4_head(server.url("plain"))

    class CutSession:
        """파트 요청에 200과 잘린 본문으로 답하는 세션."""

        def get(self, url, **kwargs):
            return SimpleNamespace(
                status_code=200,
                headers={"Content-Length": "100"},
                raise_for_status=lambda: None,
                iter_content=lambda chunk_size=8192: iter([bytes(100)]),
                close=lambda: None,
            )

    monkeypatch.setattr(fd_module, "get_thread_session", CutSession)

    run.start()

    assert run.finished == 0
    assert run.failures and isinstance(run.failures[0], TruncatedBodyError)
    assert run.listing() == []


@pytest.mark.parametrize(
    ("parts", "finishes"),
    [
        (2, False),  # 둘째 파트는 파일의 처음이 아닌 자리다
        (1, True),  # 범위가 파일의 처음부터 끝까지다
    ],
    ids=["two-parts", "one-part"],
)
def test_download_from_a_server_that_ignores_ranges_finishes_only_with_one_part(
    server, sources, tmp_path, monkeypatch, parts, finishes
):
    """범위 요청마다 200과 파일 전체로 답하는 서버에서는 파트가 하나일 때만 완료하고, 여럿이면 실패해야 한다.

    기본 입력, selections 빈 튜플, 호스트가 범위를 무시하게 함. 주석의 경우마다 파트 수
    -> 파트 하나: 완료 1회, output_path의 bytes == 원본
    -> 파트 둘: 완료 0회, 첫 실패가 TruncatedBodyError
    """
    part_size = -(-os.path.getsize(sources["plain"]) // parts)
    monkeypatch.setattr(server, "ignore_range", True)
    monkeypatch.setattr(fd_module, "decide_part_size", lambda *_args: part_size)
    run = _Run(server, "plain", tmp_path, [])

    run.start()

    if finishes:
        assert (run.finished, run.failures) == (1, [])
        with open(run.data.output_path, "rb") as made, open(sources["plain"], "rb") as wanted:
            assert made.read() == wanted.read()
    else:
        assert run.finished == 0
        assert run.failures and isinstance(run.failures[0], TruncatedBodyError)


# ================================================================ 실패한 구간만 다시 처리 (#309)

THREE_FILES = ["구간 시험 144p_1.mp4", "구간 시험 144p_2.mp4", "구간 시험 144p_3.mp4"]


def _three() -> list[TimeRange]:
    """구간 셋 — 프레임 35~80 · 100~110 · 120~140."""
    return [
        TimeRange(_seconds(35), _seconds(80)),
        TimeRange(_seconds(100), _seconds(110)),
        TimeRange(_seconds(120), _seconds(140)),
    ]


def _spans_after_moov(requests, moov_last: int) -> list[tuple[int, int]]:
    """범위 요청 가운데 moov 뒤에서 시작하는 것의 (시작, 끝) — 구간의 바이트를 받은 요청이다."""
    spans = []
    for _method, _name, header in requests:
        first, last = re.fullmatch(r"bytes=(\d+)-(\d+)", header).groups()
        if int(first) > moov_last:
            spans.append((int(first), int(last)))
    return spans


def test_retry_cuts_only_the_failed_section_from_the_kept_source_without_requests(
    server, sources, tmp_path, monkeypatch
):
    """둘째 구간만 실패한 실행을 이어받은 실행은 둘째만 잘라야 하고 아무것도 요청하지 않아야 한다.

    첫 실행: 구간 셋, 둘째 컷이 도중에 실패. 이어받은 실행: 새 엔진에 첫 실행이 남긴 것과 그때의 경로를 넘김
    -> 컷 호출 1회(`_2`), 완료 1회 · 실패 0건, 이어받은 실행 동안의 요청 0건,
       `_1` · `_3`의 수정 시각 · 내용이 그대로, 경로가 첫 실행과 같다,
       저장 폴더에 `_1` · `_2` · `_3`뿐(임시 원본 없음), `_2`가 판정 통과,
       구간 상태 == (전체 3, 완료 3, 실패 0, 이어받음 2)
    """
    cuts = CutCalls(monkeypatch, fd_module, "finish_section_file", source_arg=0)
    cuts.fail_on = (2,)
    failed = _Run(server, "plain", tmp_path, _three()).start()
    assert len(failed.failures) == 1
    kept = snapshot([failed.paths[0], failed.paths[2]])
    requests_before = len(server.requests)
    cuts.restart()

    again = hand_over(failed, _Run(server, "plain", tmp_path, _three())).start()

    assert (again.finished, again.failures) == (1, [])
    assert cuts.outputs == [THREE_FILES[1]]
    assert server.requests[requests_before:] == []
    assert snapshot(kept) == kept
    assert again.paths == failed.paths
    assert again.listing() == THREE_FILES
    check = check_cut(_frames(sources["plain"]), again.engine.cut_results[0])
    assert check.ok, check.notes
    data = again.data
    assert (
        data.sections_total,
        data.sections_done,
        data.sections_failed,
        data.sections_resumed,
    ) == (3, 3, 0, 2)


@pytest.mark.parametrize("undeletable", [False, True], ids=["지울 수 있음", "지울 수 없음"])
def test_cut_failure_whose_output_cannot_be_removed_still_ends_as_a_partial_failure(
    server, tmp_path, monkeypatch, undeletable
):
    """구간의 컷이 실패하면 그 출력을 지울 수 없어도 일부 실패로 끝나고, 이어받은 실행은 그 구간만 다시 잘라야 한다 (#389).

    구간 셋, 둘째 구간의 컷이 출력 파일을 반쯤 쓰고 CutError. 그 출력의 os.remove가
    PermissionError(대조: 지울 수 있음)
    -> 실패 1건(PostprocessError, 원인 CutError), 구간 상태 == (전체 3, 완료 2, 실패 1), 이어받기 기록이
       있다, `_1` · `_3` 파일과 경계 원본 · 둘째 구간의 본문이 남는다, 지울 수 없을 때만 `_2`가 남고
       지우기 거절이 1번 이상
    -> 남은 `_2`를 치우고 이어받은 실행: 컷 호출 1회(`_2`), 완료 1회 · 실패 0건, 요청 0건
    """
    blocker = Undeletable(monkeypatch)
    cuts = CutCalls(monkeypatch, fd_module, "finish_section_file", source_arg=0)
    cuts.fail_on = (2,)
    failed = _Run(server, "plain", tmp_path, _three())
    if undeletable:
        blocker.block(failed.paths[1])

    failed.start()

    assert len(failed.failures) == 1
    assert isinstance(failed.failures[0], PostprocessError)
    assert isinstance(failed.failures[0].__cause__, CutError)
    data = failed.data
    assert (data.sections_total, data.sections_done, data.sections_failed) == (3, 2, 1)
    assert data.section_resume is not None
    assert os.path.isfile(failed.paths[0]) and os.path.isfile(failed.paths[2])
    assert os.path.isfile(failed.source_path)
    assert os.path.isfile(section_body_path_for(failed.paths[1]))
    assert os.path.exists(failed.paths[1]) == undeletable
    assert bool(blocker.refused) == undeletable

    blocker.release()
    if undeletable:
        # 남은 것이 이름을 차지하면 이어받은 실행은 둘째 구간에 새 이름을 준다 — 이 테스트는 그
        # 경우(이름이 바뀐 mp4 재시도)를 재지 않는다
        os.remove(failed.paths[1])
    requests_before = len(server.requests)
    cuts.restart()
    again = hand_over(failed, _Run(server, "plain", tmp_path, _three())).start()

    assert (again.finished, again.failures) == (1, [])
    assert cuts.outputs == [THREE_FILES[1]]
    assert server.requests[requests_before:] == []


def test_section_file_that_cannot_be_put_in_place_fails_alone(server, tmp_path, monkeypatch):
    """다 쓴 본문을 구간 파일의 이름으로 바꾸지 못하면 그 구간만 실패하고 본문이 남아야 한다 (#389).

    구간 셋, 둘째 구간의 출력 경로로 옮기는 os.replace가 PermissionError
    -> 실패 1건(PostprocessError, 원인 CutError, 그 원인 PermissionError), 구간 상태 == (3, 2, 1), 이어받기
       기록이 있다, `_1` · `_3` 파일과 경계 원본 · 둘째 구간의 본문이 남는다, 옮기기 거절이 1번
    -> 막은 것을 풀고 이어받은 실행: 완료 1회 · 실패 0건, 요청 0건
    """
    blocker = Undeletable(monkeypatch)
    failed = _Run(server, "plain", tmp_path, _three())
    blocker.block_replace_into(failed.paths[1])

    failed.start()

    assert len(failed.failures) == 1
    assert isinstance(failed.failures[0], PostprocessError)
    assert isinstance(failed.failures[0].__cause__, CutError)
    assert isinstance(failed.failures[0].__cause__.__cause__, PermissionError)
    data = failed.data
    assert (data.sections_total, data.sections_done, data.sections_failed) == (3, 2, 1)
    assert data.section_resume is not None
    assert os.path.isfile(failed.paths[0]) and os.path.isfile(failed.paths[2])
    assert os.path.isfile(failed.source_path)
    assert os.path.isfile(section_body_path_for(failed.paths[1]))
    assert blocker.replace_refused == [failed.paths[1]]

    blocker.release()
    requests_before = len(server.requests)
    again = hand_over(failed, _Run(server, "plain", tmp_path, _three())).start()

    assert (again.finished, again.failures) == (1, [])
    assert server.requests[requests_before:] == []


def test_stop_during_retry_removes_only_what_the_retry_made(server, tmp_path, monkeypatch):
    """이어받은 실행을 컷 사이에서 중단하면 그 실행이 만든 구간 파일만 지워야 한다.

    같은 엔진. 첫 실행: 둘째 · 셋째 컷이 도중에 실패(`_1`만 만듦). 이어받은 실행: 둘째 컷이 끝난 뒤 중단
    -> 중단한 순간 `_2`가 있었다, 컷 호출 1회(`_2`), 완료 0회, 실패는 첫 실행의 1건뿐,
       `_1`의 수정 시각 · 내용이 그대로, 저장 폴더에 경계 원본과 `_1`, 손대지 않은 셋째 구간의
       본문 파일뿐(둘째 구간의 본문은 `_2`가 됐다가 함께 지워졌다)
    """
    cuts = CutCalls(monkeypatch, fd_module, "finish_section_file", source_arg=0)
    cuts.fail_on = (2, 3)
    run = _Run(server, "plain", tmp_path, _three()).start()
    assert len(run.failures) == 1
    kept = snapshot([run.paths[0]])
    made_when_stopped = []

    def stop(_number: int) -> None:
        made_when_stopped.append(os.path.exists(run.paths[1]))
        run.data.model.stop()

    cuts.restart()
    cuts.after = stop
    run.data.content.section_resume = run.data.section_resume

    run.engine.run()

    assert made_when_stopped == [True]
    assert cuts.outputs == [THREE_FILES[1]]
    assert (run.finished, len(run.failures)) == (0, 1)
    assert snapshot(kept) == kept
    assert run.listing() == [
        "CVDv2_body_구간 시험 144p_3.mp4",
        "CVDv2_part_구간 시험 144p_1.mp4",
        THREE_FILES[0],
    ]


def test_retry_receives_only_the_failed_sections_ranges_when_the_kept_source_is_gone(
    server, tmp_path, monkeypatch
):
    """남겨 둔 임시 원본이 없으면 이어받은 실행은 끝나지 않은 구간의 범위만 다시 받아야 한다.

    첫 실행: 구간 셋, 둘째 컷이 도중에 실패. 임시 원본을 지움. 이어받은 실행
    -> 완료 1회 · 실패 0건, 이어받은 실행의 요청은 모두 범위 요청이고, moov 뒤에서 시작하는
       요청의 바이트 합 == 이어받은 실행의 받을 크기 < 첫 실행의 받을 크기, 컷 호출 1회(`_2`),
       `_1` · `_3`의 수정 시각 · 내용이 그대로, 저장 폴더에 `_1` · `_2` · `_3`뿐
    """
    cuts = CutCalls(monkeypatch, fd_module, "finish_section_file", source_arg=0)
    cuts.fail_on = (2,)
    failed = _Run(server, "plain", tmp_path, _three()).start()
    assert len(failed.failures) == 1
    kept = snapshot([failed.paths[0], failed.paths[2]])
    os.remove(failed.source_path)
    requests_before = len(server.requests)
    cuts.restart()

    again = hand_over(failed, _Run(server, "plain", tmp_path, _three())).start()

    assert (again.finished, again.failures) == (1, [])
    moov_last = failed.data.section_resume.mp4_head.index.moov_range[1]
    spans = _spans_after_moov(server.requests[requests_before:], moov_last)
    assert spans
    received = sum(last - first + 1 for first, last in spans)
    assert received == again.data.total_size < failed.data.total_size
    assert cuts.outputs == [THREE_FILES[1]]
    assert snapshot(kept) == kept
    assert again.listing() == THREE_FILES


def test_retry_does_not_trust_a_kept_source_of_the_same_size_with_another_head(
    server, tmp_path, monkeypatch
):
    """남겨 둔 임시 원본이 크기는 같아도 머리가 다르면 이어받은 실행은 그것을 쓰지 않고 다시 받아야 한다.

    첫 실행: 구간 셋, 둘째 컷이 도중에 실패. 임시 원본을 같은 크기의 0으로 채운 파일로 바꿈. 이어받은 실행
    -> 완료 1회 · 실패 0건, 이어받은 실행의 요청이 있고 모두 범위 요청, 컷 호출 1회(`_2`),
       `_1` · `_3`의 수정 시각 · 내용이 그대로, 저장 폴더에 `_1` · `_2` · `_3`뿐
    """
    cuts = CutCalls(monkeypatch, fd_module, "finish_section_file", source_arg=0)
    cuts.fail_on = (2,)
    failed = _Run(server, "plain", tmp_path, _three()).start()
    assert len(failed.failures) == 1
    kept = snapshot([failed.paths[0], failed.paths[2]])
    size = os.path.getsize(failed.source_path)
    with open(failed.source_path, "wb") as f:
        f.write(bytes(size))
    assert os.path.getsize(failed.source_path) == failed.data.section_resume.source_size
    requests_before = len(server.requests)
    cuts.restart()

    again = hand_over(failed, _Run(server, "plain", tmp_path, _three())).start()

    assert (again.finished, again.failures) == (1, [])
    requested = server.requests[requests_before:]
    assert requested
    assert all(header is not None for _method, _name, header in requested)
    assert cuts.outputs == [THREE_FILES[1]]
    assert snapshot(kept) == kept
    assert again.listing() == THREE_FILES


def test_retry_without_the_kept_source_plans_the_ranges_from_a_freshly_received_moov(
    server, sources, tmp_path, monkeypatch
):
    """임시 원본을 다시 쓸 수 없으면 이어받은 실행은 moov를 새로 받아 그것으로 끝나지 않은 구간의 범위를 정해야 한다.

    첫 실행: plain, 구간 셋, 둘째 컷이 도중에 실패. 임시 원본을 지우고, 같은 주소가 다른 파일(gappy)을 내주게 바꿈.
    이어받은 실행
    -> 완료 1회 · 실패 0건, 이어받은 실행이 새 파일의 moov가 든 범위를 요청했다,
       moov 뒤에서 시작하는 요청들의 처음 · 끝 == 새 파일의 moov로 정한 둘째 구간의 범위
       (남겨 둔 moov로 정한 범위와 다르다),
       컷 호출 1회(`_2`)이고 `_2`가 새 파일의 프레임으로 판정 통과, `_1` · `_3`의 수정 시각 · 내용이 그대로
    """
    selections = _three()
    cuts = CutCalls(monkeypatch, fd_module, "finish_section_file", source_arg=0)
    cuts.fail_on = (2,)
    failed = _Run(server, "plain", tmp_path, selections).start()
    assert len(failed.failures) == 1
    kept = snapshot([failed.paths[0], failed.paths[2]])
    os.remove(failed.source_path)
    fresh = fetch_mp4_head(server.url("gappy")).index
    stale = failed.data.section_resume.mp4_head.index
    expected = selection_byte_ranges(fresh, selections[1]).ranges
    assert len(expected) == 1  # 이어진 범위 하나
    assert expected != selection_byte_ranges(stale, selections[1]).ranges
    monkeypatch.setitem(server.files, "plain", server.files["gappy"])
    requests_before = len(server.requests)
    cuts.restart()

    again = hand_over(failed, _Run(server, "plain", tmp_path, selections)).start()

    assert (again.finished, again.failures) == (1, [])
    first, last = fresh.moov_range
    moov_requests = [
        header
        for _method, name, header in server.requests[requests_before:]
        if name == "plain"
        and (match := re.fullmatch(r"bytes=(\d+)-(\d+)", header or ""))
        and int(match.group(1)) <= last
        and int(match.group(2)) >= first
    ]
    assert moov_requests
    spans = _spans_after_moov(server.requests[requests_before:], last)
    assert (min(first for first, _ in spans), max(end for _, end in spans)) == expected[0]
    assert cuts.outputs == [THREE_FILES[1]]
    check = check_cut(_frames(sources["gappy"]), again.engine.cut_results[0])
    assert check.ok, check.notes
    assert snapshot(kept) == kept


# ================================================================ 준비 단계의 로그


def _logged(run: "_Run", name: str) -> list[tuple]:
    return [args for called, args in run.logger.calls if called == name]


def test_prepare_log_says_the_moov_was_fetched_when_none_was_handed_in(server, tmp_path):
    """moov를 넘겨받지 않은 구간 다운로드는 준비 로그에 "moov fetched"를 남겨야 한다.

    기본 입력, 구간 프레임 35~80, content.mp4_head 없음
    -> log_prepare_complete 1회, 덧붙인 말 == "moov fetched", 걸린 시간 >= 0
    """
    run = _Run(server, "plain", tmp_path, [TimeRange(_seconds(35), _seconds(80))]).start()

    ((elapsed, note),) = _logged(run, "log_prepare_complete")
    assert note == "moov fetched"
    assert elapsed >= 0


def test_prepare_log_says_the_moov_was_reused_when_one_was_handed_in(server, tmp_path):
    """moov를 넘겨받은 구간 다운로드는 준비 로그에 "moov reused"를 남겨야 한다.

    기본 입력, 구간 프레임 35~80, fetch_mp4_head의 결과를 content.mp4_head에 넣음
    -> log_prepare_complete 1회, 덧붙인 말 == "moov reused"
    """
    run = _Run(server, "plain", tmp_path, [TimeRange(_seconds(35), _seconds(80))])
    run.data.content.mp4_head = fetch_mp4_head(server.url("plain"))

    run.start()

    ((_elapsed, note),) = _logged(run, "log_prepare_complete")
    assert note == "moov reused"


def _slept_when_prepared(run: "_Run", clock: "_RestClock") -> list[int]:
    """준비 로그를 남기는 순간까지 대역이 센 쉰 횟수를 적어 둘 목록을 건다.

    쉬는 것은 준비만이 아니다 — 후처리(구간 파일의 머리 쓰기)도 조각 사이에서 쉰다.
    """
    seen: list[int] = []
    record = run.logger.log_prepare_complete

    def at_prepare(*args, **kwargs) -> None:
        seen.append(clock.slept)
        record(*args, **kwargs)

    run.logger.log_prepare_complete = at_prepare
    return seen


class _RestClock:
    """time 모듈의 대역 — sleep을 부를 때마다 시계가 0.25초 간다. 부른 횟수를 센다."""

    def __init__(self):
        self.now = 10.0
        self.slept = 0

    def perf_counter(self) -> float:
        return self.now

    def sleep(self, _seconds: float) -> None:
        self.slept += 1
        self.now += 0.25


def test_prepare_log_adds_the_rests_taken_while_the_user_was_interacting(
    server, tmp_path, monkeypatch
):
    """조작 중이라 쉬어 가며 준비했으면 준비 로그의 덧붙인 말 끝에 쉰 횟수 · 시간이 붙어야 한다.

    기본 입력, 구간 프레임 35~80. 쉴 때마다 시계가 0.25초 가는 대역. resting_while(늘 참) 안에서 실행
    -> 덧붙인 말 == "moov fetched - rested N times for S s", N == 준비 로그를 남기는 순간까지
       대역이 센 쉰 횟수 > 0, S == N × 0.25
    """
    clock = _RestClock()
    monkeypatch.setattr(interaction, "time", clock)
    run = _Run(server, "plain", tmp_path, [TimeRange(_seconds(35), _seconds(80))])
    slept = _slept_when_prepared(run, clock)

    with resting_while(lambda: True):
        run.start()

    ((_elapsed, note),) = _logged(run, "log_prepare_complete")
    (rested,) = slept
    assert rested > 0
    assert note == f"moov fetched - rested {rested} times for {rested * 0.25:.2f}s"
    assert (run.finished, run.failures) == (1, [])


def test_prepare_log_counts_only_the_rests_of_this_prepare(server, tmp_path, monkeypatch):
    """준비 로그의 쉰 횟수에는 그 준비보다 앞서 같은 스레드가 쉰 것이 들지 않아야 한다.

    기본 입력, 구간 프레임 35~80. resting_while(늘 참) 안에서 먼저 틈을 5번 흉내 낸 뒤 실행
    -> 덧붙인 말의 횟수 == 준비 로그를 남기는 순간까지 대역이 센 횟수에서 앞의 5번을 뺀 값
    """
    clock = _RestClock()
    monkeypatch.setattr(interaction, "time", clock)
    run = _Run(server, "plain", tmp_path, [TimeRange(_seconds(35), _seconds(80))])
    slept = _slept_when_prepared(run, clock)

    with resting_while(lambda: True):
        for _ in range(5):
            interaction.rest_if_user_interacting()
        run.start()

    ((_elapsed, note),) = _logged(run, "log_prepare_complete")
    rested = slept[0] - 5
    assert rested > 0
    assert note == f"moov fetched - rested {rested} times for {rested * 0.25:.2f}s"


def test_prepare_log_says_nothing_of_rests_when_the_user_was_not_interacting(
    server, tmp_path, monkeypatch
):
    """조작 중이 아니어서 쉬지 않았으면 준비 로그의 덧붙인 말은 그대로여야 한다.

    기본 입력, 구간 프레임 35~80. resting_while(늘 거짓) 안에서 실행
    -> 덧붙인 말 == "moov fetched", 대역이 센 쉰 횟수 == 0
    """
    clock = _RestClock()
    monkeypatch.setattr(interaction, "time", clock)
    run = _Run(server, "plain", tmp_path, [TimeRange(_seconds(35), _seconds(80))])

    with resting_while(lambda: False):
        run.start()

    ((_elapsed, note),) = _logged(run, "log_prepare_complete")
    assert (note, clock.slept) == ("moov fetched", 0)


@pytest.mark.parametrize(
    ("note", "before", "after", "expected"),
    [
        ("moov reused", (2, 0.5), (5, 1.75), "moov reused - rested 3 times for 1.25s"),
        ("", (0, 0.0), (1, 0.004), "rested 1 times for 0.00s"),
        ("moov reused", (4, 2.0), (4, 2.0), "moov reused"),
        ("", (0, 0.0), (0, 0.0), ""),
    ],
    ids=["말 뒤에 잇는다", "말이 없으면 쉼만", "쉬지 않았으면 그대로", "둘 다 없음"],
)
def test_with_rests_appends_the_rests_between_two_totals(note, before, after, expected):
    """_with_rests는 앞뒤 값의 차(쉰 횟수 · 시간)를 덧붙인 말 뒤에 이어야 한다.

    (note, 앞의 (횟수, 초), 뒤의 (횟수, 초)) -> 횟수의 차가 0이면 note 그대로, 아니면
    "rested <차> times for <초의 차, 소수 둘째 자리>s"를 " - "로 잇는다
    """
    assert base_module._with_rests(note, before, after) == expected


def test_engine_parses_handed_moov_bytes_without_fetching_again(server, tmp_path, monkeypatch):
    """해석하지 않은 moov 바이트를 넘겨받은 구간 다운로드는 moov를 다시 받지 않고 같은 파일을 내야 한다.

    기본 입력, 구간 프레임 35~80. fetch_mp4_raw의 결과를 pending_mp4_head로 싸 content.mp4_pending에
    넣고 엔진의 moov 받기를 부르면 실패하게 바꿈. 같은 구간을 아무것도 넘기지 않고 받은 것과 견줌
    -> 준비 로그의 덧붙인 말 == "moov reused", 둘 다 완료 1회, 산출물의 프레임 == 넘기지 않고 받은 산출물의 프레임
    """
    sections = [TimeRange(_seconds(35), _seconds(80))]
    (tmp_path / "plain").mkdir()
    (tmp_path / "handed").mkdir()
    plain = _Run(server, "plain", tmp_path / "plain", sections).start()
    raw = fetch_mp4_raw(server.url("plain"))

    def no_fetch(url):
        raise AssertionError("엔진이 moov를 다시 받았다")

    monkeypatch.setattr(fd_module, "fetch_mp4_head", no_fetch)
    run = _Run(server, "plain", tmp_path / "handed", sections)
    run.data.content.mp4_pending = pending_mp4_head(raw)

    run.start()

    ((_elapsed, note),) = _logged(run, "log_prepare_complete")
    assert note == "moov reused"
    assert (plain.finished, run.finished) == (1, 1)
    assert _frames(run.paths[0]) == _frames(plain.paths[0])


def test_engine_uses_the_index_someone_else_already_parsed(server, tmp_path, monkeypatch):
    """넘겨받은 moov를 다른 쪽이 이미 해석했으면 엔진은 다시 해석하지 않고 그 색인으로 받아야 한다.

    기본 입력, 구간 프레임 35~80. content.mp4_pending을 먼저 get()으로 해석해 두고, 해석 함수를
    세는 대역으로 바꾼 뒤 엔진을 돌림
    -> 엔진이 도는 동안 해석 0회, 완료 1회, 준비 로그의 덧붙인 말 == "moov reused"
    """
    pending = pending_mp4_head(fetch_mp4_raw(server.url("plain")))
    pending.get()
    calls = []
    real = mp4_module.index_mp4

    def counting(raw):
        calls.append(raw)
        return real(raw)

    monkeypatch.setattr(mp4_module, "index_mp4", counting)
    run = _Run(server, "plain", tmp_path, [TimeRange(_seconds(35), _seconds(80))])
    run.data.content.mp4_pending = pending

    run.start()

    assert calls == []
    assert run.finished == 1
    ((_elapsed, note),) = _logged(run, "log_prepare_complete")
    assert note == "moov reused"


def test_prepare_log_of_a_whole_download_says_nothing_about_the_moov(server, tmp_path):
    """구간이 없는 다운로드의 준비 로그는 moov에 대해 아무 말도 덧붙이지 않아야 한다.

    기본 입력, selections 빈 튜플
    -> log_prepare_complete 1회, 덧붙인 말 == ""
    """
    run = _Run(server, "plain", tmp_path, []).start()

    ((_elapsed, note),) = _logged(run, "log_prepare_complete")
    assert note == ""


def test_transfer_without_prepare_is_the_transfer_time_minus_the_prepare_time(
    server, tmp_path, monkeypatch
):
    """준비를 뺀 전송 시간은 Transfer 줄의 시간에서 준비 시간을 뺀 값이어야 한다.

    moov 받기가 0.3초 걸리게 함, 구간 프레임 35~80
    -> 준비 >= 0.3초, log_transfer_net의 값 == log_transfer_complete의 시간 − 준비 시간,
       log_transfer_complete의 시간 >= 0.3초(준비를 포함한 채다)
    """
    slow = 0.3  # 초 — 시계 해상도보다 충분히 길다
    real_fetch = fd_module.fetch_mp4_head

    def slow_fetch(url):
        time.sleep(slow)
        return real_fetch(url)

    monkeypatch.setattr(fd_module, "fetch_mp4_head", slow_fetch)

    run = _Run(server, "plain", tmp_path, [TimeRange(_seconds(35), _seconds(80))]).start()

    ((prepare, _note),) = _logged(run, "log_prepare_complete")
    ((transfer, *_rest),) = _logged(run, "log_transfer_complete")
    ((net,),) = _logged(run, "log_transfer_net")
    assert prepare >= slow
    assert transfer >= slow
    assert net == pytest.approx(transfer - prepare, abs=1e-9)


# ================================================================ 컷 단계의 로그


def test_cut_stages_are_logged_once_per_section_with_its_number(server, tmp_path):
    """구간마다 컷의 단계별 시간을 그 구간의 번호와 함께 한 번씩 로그에 남겨야 한다.

    기본 입력, 구간 둘(프레임 35~80은 세 조각, 100~110은 한 GOP 안이라 한 조각)
    -> log_cut_setup 1회, log_cut_stages 2회: (1, 2, …) · (2, 2, …),
       첫 구간의 단계 이름 == [probe, 0_head, 2_tail, write] — 가운데 조각은 받으면서 이미
       옮겼으므로 단계가 없다,
       둘째 구간의 단계 이름 == [probe, 0_whole, write]
    """
    selections = [TimeRange(_seconds(35), _seconds(80)), TimeRange(_seconds(100), _seconds(110))]

    run = _Run(server, "plain", tmp_path, selections).start()

    assert len(_logged(run, "log_cut_setup")) == 1
    first, second = _logged(run, "log_cut_stages")
    assert first[:2] == (1, 2)
    assert second[:2] == (2, 2)
    assert [name for name, _seconds in first[2]] == ["probe", "0_head", "2_tail", "write"]
    assert [name for name, _seconds in second[2]] == ["probe", "0_whole", "write"]


def test_cut_stages_of_a_failed_section_are_logged_up_to_the_failure(server, tmp_path, monkeypatch):
    """컷이 실패한 구간도 실패한 단계까지의 시간을 로그에 남겨야 한다.

    기본 입력, 구간 프레임 35~80, 셋째 ffmpeg 실행(꼬리 재인코딩)이 종료 코드 1로 끝남
    -> 실패 1건, log_cut_stages 1회, 단계 이름 == [probe, 0_head, 2_tail]
    """
    real = cut_module.run_ffmpeg
    calls = []

    def flaky(args, **kwargs):
        calls.append(args)
        if len(calls) == 3:  # 0번은 입력 읽기, 1번은 머리, 2번이 꼬리다
            return subprocess.CompletedProcess(args, 1, "", "boom")
        return real(args, **kwargs)

    run = _Run(server, "plain", tmp_path, [TimeRange(_seconds(35), _seconds(80))])
    run.engine._inspect_cuts = False  # 조각을 따로 읽는 실행이 세는 수에 끼지 않게 한다
    monkeypatch.setattr(cut_module, "run_ffmpeg", flaky)

    run.start()

    assert len(run.failures) == 1
    ((number, total, stages),) = _logged(run, "log_cut_stages")
    assert (number, total) == (1, 1)
    assert [name for name, _seconds in stages] == ["probe", "0_head", "2_tail"]


# ================================================================ 컷의 진행


def _watch_cut_progress(run: "_Run") -> list[tuple[int, float]]:
    """진행 통지가 올 때마다 (끝난 구간 수, 컷 진행)을 적는다 — 컷 진행이 적힌 통지만."""
    seen: list[tuple[int, float]] = []

    def record(event) -> None:
        if run.data.cut_progress is not None:
            seen.append((run.data.sections_done, run.data.cut_progress))

    run.engine.set_on_progress(record)
    return seen


def test_cut_progress_rises_inside_a_section_and_reaches_one_after_the_last(server, tmp_path):
    """컷 진행은 구간 하나를 자르는 동안에도 여러 번 오르고, 줄지 않으며, 마지막 구간이 끝난 뒤에만 1이어야 한다.

    기본 입력, 구간 둘(프레임 35~80 · 100~110)
    -> 끝난 구간이 0개인 동안 서로 다른 컷 진행이 3개 이상, 전체가 오름차순,
       끝난 구간이 2개가 되기 전의 값은 모두 1 미만, 마지막 값 == 1.0
    """
    selections = [TimeRange(_seconds(35), _seconds(80)), TimeRange(_seconds(100), _seconds(110))]
    run = _Run(server, "plain", tmp_path, selections)
    seen = _watch_cut_progress(run)

    run.start()

    values = [value for _done, value in seen]
    assert len({value for done, value in seen if done == 0}) >= 3
    assert values == sorted(values)
    assert all(value < 1.0 for done, value in seen if done < 2)
    assert values[-1] == 1.0


def test_cut_progress_weighs_sections_by_their_length(server, tmp_path):
    """컷 진행에서 구간 하나의 몫은 그 구간의 길이에 비례해야 한다.

    기본 입력, 구간 둘 — 프레임 35~80(46프레임) · 100~110(11프레임)
    -> 첫 구간이 끝난 직후의 컷 진행 == 46 ÷ 57
    """
    selections = [TimeRange(_seconds(35), _seconds(80)), TimeRange(_seconds(100), _seconds(110))]
    run = _Run(server, "plain", tmp_path, selections)
    seen = _watch_cut_progress(run)

    run.start()

    after_first = [value for done, value in seen if done == 1]
    assert after_first[0] == pytest.approx(46 / 57)


def test_failed_section_still_fills_its_share_of_the_cut_progress(server, tmp_path, monkeypatch):
    """자르지 못한 구간도 그 구간의 일이 끝나면 컷 진행에서 제 몫을 다 채워야 한다.

    기본 입력, 구간 하나(프레임 35~80), 컷의 셋째 ffmpeg 실행이 종료 코드 1로 끝남
    -> 실패 1건, 마지막 컷 진행 == 1.0
    """
    real = cut_module.run_ffmpeg
    calls = []

    def flaky(args, **kwargs):
        calls.append(args)
        if len(calls) == 3:
            return subprocess.CompletedProcess(args, 1, "", "boom")
        return real(args, **kwargs)

    run = _Run(server, "plain", tmp_path, [TimeRange(_seconds(35), _seconds(80))])
    run.engine._inspect_cuts = False
    seen = _watch_cut_progress(run)
    monkeypatch.setattr(cut_module, "run_ffmpeg", flaky)

    run.start()

    assert len(run.failures) == 1
    assert seen[-1][1] == 1.0


# ================================================================ 받을 크기 · 준비가 끝난 통지


@pytest.mark.parametrize(
    "frames",
    [
        [(35, 80)],
        [(35, 80), (120, 140)],  # 떨어진 구간 둘
        [(35, 80), (60, 110)],  # 겹치는 구간 — 겹친 자리는 한 번만 받는다
        [(0, 5), (150, 170), (40, 41)],
    ],
)
def test_sections_download_size_equals_the_total_the_engine_plans(
    server, sources, tmp_path, frames
):
    """sections_download_size는 엔진이 그 구간들을 받을 때 정하는 전체 크기와 같아야 한다.

    기본 입력, 프레임 구간 목록(하나 · 떨어진 둘 · 겹치는 둘 · 순서가 섞인 셋)
    -> sections_download_size(색인, 구간들) == 엔진이 공유 데이터에 적은 total_size
       == log_download_start의 첫 인자
    """
    selections = [TimeRange(_seconds(first), _seconds(last)) for first, last in frames]

    run = _Run(server, "plain", tmp_path, selections).start()

    expected = sections_download_size(_index(sources["plain"]), selections)
    assert (run.finished, run.failures) == (1, [])
    assert run.data.total_size == expected
    assert _logged(run, "log_download_start")[0][0] == expected


def test_engine_reports_progress_once_before_it_starts_receiving(server, tmp_path):
    """엔진은 준비가 끝나면 받기 전에 진행을 한 번 알려야 한다 — 받은 크기 0, 전체 크기는 받을 크기.

    기본 입력, 구간 프레임 35~80
    -> 첫 진행 통지 == (받은 0, 전체 == 공유 데이터의 total_size), 그때 컷 진행은 아직 없다(None)
    """
    run = _Run(server, "plain", tmp_path, [TimeRange(_seconds(35), _seconds(80))])
    seen = []
    run.engine.set_on_progress(
        lambda event: seen.append((event.downloaded_size, event.total_size, run.data.cut_progress))
    )

    run.start()

    assert seen[0] == (0, run.data.total_size, None)


# ================================================================ 끝난 엔진이 색인을 놓는지


def test_engine_drops_the_index_when_the_run_ends(server, tmp_path):
    """실행이 끝난 엔진은 moov와 색인, 임시 원본의 머리를 들고 있지 않아야 한다.

    기본 입력, 구간 프레임 35~80, 완료
    -> 엔진의 _mp4_head is None, _index is None, _head == b"", 구간 목록과 컷 결과는 그대로 있다
    """
    run = _Run(server, "plain", tmp_path, [TimeRange(_seconds(35), _seconds(80))]).start()

    assert (run.finished, run.failures) == (1, [])
    assert run.engine._mp4_head is None
    assert run.engine._index is None
    assert run.engine._head == b""
    assert len(run.engine.sections) == 1 and len(run.engine.cut_results) == 1


def test_engine_drops_the_index_after_a_cut_failure_and_the_resume_record_keeps_it(
    server, tmp_path, monkeypatch
):
    """컷이 실패해 끝난 엔진도 색인을 놓아야 하고, 다음 실행이 쓸 moov는 이어받기 기록에 있어야 한다.

    기본 입력, 구간 프레임 35~80, 컷의 셋째 ffmpeg 실행이 종료 코드 1로 끝남
    -> 실패 1건, 엔진의 _mp4_head is None · _index is None,
       공유 데이터의 section_resume.mp4_head is not None
    """
    real = cut_module.run_ffmpeg
    calls = []

    def flaky(args, **kwargs):
        calls.append(args)
        if len(calls) == 3:
            return subprocess.CompletedProcess(args, 1, "", "boom")
        return real(args, **kwargs)

    run = _Run(server, "plain", tmp_path, [TimeRange(_seconds(35), _seconds(80))])
    run.engine._inspect_cuts = False
    monkeypatch.setattr(cut_module, "run_ffmpeg", flaky)

    run.start()

    assert len(run.failures) == 1
    assert run.engine._mp4_head is None
    assert run.engine._index is None
    assert run.data.section_resume.mp4_head is not None


# ================================================================ 컷 도중 중단


def test_stopping_while_a_section_is_being_cut_ends_ffmpeg_and_the_run_at_once(
    server, tmp_path, monkeypatch
):
    """구간을 자르는 도중 중단하면 도는 ffmpeg가 바로 끝나고, 실행도 곧 끝나며 이번 실행의 것이 남지 않아야 한다.

    기본 입력, 구간 둘(프레임 5~25 · 35~80). 둘째 구간의 머리 재인코딩을 10분 도는 명령으로 바꿔
    띄우고, 그 프로세스가 뜬 0.4초 뒤 중단(model.stop()) — 첫 구간은 한 GOP 안이라 머리 조각이 없다
    -> 중단 뒤 1.5초 안에 run()이 돌아온다, 중단할 때 그 ffmpeg는 돌고 있었고 지금은 모두 끝났다
    -> 완료 · 실패 통지 없음, 실패한 구간 0, 폴더에 남은 것 없음(경계 원본 · 본문 파일 · 이번
       실행이 만든 구간 파일 — 끝낸 첫 구간의 파일까지 — 을 지운다: 중단의 지금 규칙이다)
    """

    def on_launch(command, process) -> None:
        if is_long_running(command):  # 바꿔 띄운 명령의 프로세스가 떴다 — 이때부터 센다
            threading.Timer(0.4, stop).start()

    processes = record_processes(monkeypatch, on_launch)
    run = _Run(
        server,
        "plain",
        tmp_path,
        [TimeRange(_seconds(5), _seconds(25)), TimeRange(_seconds(35), _seconds(80))],
    )
    real = cut_module.run_ffmpeg
    stopped = {}

    def stop() -> None:
        stopped["running"] = processes[-1].poll() is None
        stopped["made"] = sorted(os.listdir(run.folder))
        stopped["at"] = time.perf_counter()
        run.data.model.stop()
        # 제품이 멈추지 못하면 10분을 돈다 — 띄운 프로세스를 직접 끝내 run()이 돌아오게 한다
        guard = threading.Timer(4.0, end_all, [processes])
        guard.daemon = True
        guard.start()

    def slowed(args, **kwargs):
        if args[-1] == "0_head.mp4":  # 둘째 구간의 머리 재인코딩 — 첫 구간에는 이 조각이 없다
            kwargs.pop("on_out_time", None)
            return real(LONG_RUNNING, **kwargs)
        return real(args, **kwargs)

    monkeypatch.setattr(cut_module, "run_ffmpeg", slowed)
    try:
        run.start()
        returned = time.perf_counter()

        assert stopped["running"], "전제: 중단할 때 둘째 구간의 ffmpeg가 돌고 있었다"
        assert "구간 시험 144p_1.mp4" in stopped["made"], "전제: 첫 구간은 끝나 있었다"
        assert returned - stopped["at"] < 1.5
        assert all(process.poll() is not None for process in processes)
        assert (run.finished, run.failures, run.data.sections_failed) == (0, [], 0)
        assert run.listing() == []
    finally:
        end_all(processes)


def test_pausing_while_a_section_is_being_cut_holds_the_cut_until_resumed(
    server, tmp_path, monkeypatch
):
    """구간을 자르는 도중 일시정지하면 컷이 그 자리에서 서고, 재개하면 이어서 끝내야 한다.

    기본 입력, 구간 하나(프레임 35~80). 머리 재인코딩의 ffmpeg가 뜬 직후 일시정지(model.pause())하고
    1초 뒤 재개. 0.8초 시점에 적음(일시정지가 없으면 컷은 그 전에 끝난다)
    -> 0.8초 시점: 그 ffmpeg가 살아 있다, 완료 통지 없음, 끝낸 구간 0, 구간 파일이 아직 없다
    -> 재개 뒤: 완료 1회, 실패 없음, 구간 파일이 만들어진다, ffmpeg가 모두 종료 코드 0으로 끝났다
    """
    run = _Run(server, "plain", tmp_path, [TimeRange(_seconds(35), _seconds(80))])
    paused, during = [], []

    def on_launch(command, process) -> None:
        if command[-1] == "0_head.mp4" and not paused:
            paused.append(run.data.model.pause())
            threading.Timer(
                0.8,
                lambda: during.append(
                    (
                        process.poll() is None,
                        run.finished,
                        run.data.sections_done,
                        sorted(name for name in os.listdir(run.folder) if name.endswith("_1.mp4")),
                    )
                ),
            ).start()
            threading.Timer(1.0, run.data.model.resume).start()

    processes = record_processes(monkeypatch, on_launch)
    try:
        run.start()

        assert paused == [True], "전제: 컷 도중에 일시정지됐다"
        assert during == [
            (True, 0, 0, ["CVDv2_body_구간 시험 144p_1.mp4", "CVDv2_part_구간 시험 144p_1.mp4"])
        ]
        assert (run.finished, run.failures) == (1, [])
        assert run.listing() == ["구간 시험 144p_1.mp4"]
        assert all(process.returncode == 0 for process in processes)
    finally:
        end_all(processes)


def test_postprocess_log_time_leaves_out_the_time_paused_during_the_cut(
    server, tmp_path, monkeypatch
):
    """후처리 종료 로그의 시간은 컷 도중 일시정지한 시간을 뺀 값이어야 하고, 그 시간은 따로 넘어와야 한다.

    기본 입력, 구간 하나(프레임 35~80). 머리 재인코딩의 ffmpeg가 뜬 직후 일시정지하고 1초 뒤 재개
    -> log_postprocess_complete(시간, 크기, 일시정지): 일시정지 >= 0.9초,
       시간 + 일시정지 <= 후처리를 감싼 실제 시간, 시간 == 시간 구분 줄의 후처리 값,
       시간 구분 줄의 일시정지 == 같은 값
    """
    run = _Run(server, "plain", tmp_path, [TimeRange(_seconds(35), _seconds(80))])
    paused = []

    def on_launch(command, process) -> None:
        if command[-1] == "0_head.mp4" and not paused:
            paused.append(run.data.model.pause())
            threading.Timer(1.0, run.data.model.resume).start()

    processes = record_processes(monkeypatch, on_launch)
    try:
        began = time.perf_counter()
        run.start()
        wall = time.perf_counter() - began

        assert paused == [True], "전제: 컷 도중에 일시정지됐다"
        assert (run.finished, run.failures) == (1, [])
        ((elapsed, _size, held),) = _logged(run, "log_postprocess_complete")
        ((_transfer, cut, all_held),) = _logged(run, "log_total_breakdown")
        assert held >= 0.9
        assert elapsed + held <= wall
        assert elapsed == cut
        assert all_held == held
    finally:
        end_all(processes)


# ================================================================ 구간 파일을 바로 쓴다


def test_a_section_over_the_whole_video_is_the_source_file_itself(server, sources, tmp_path):
    """영상 전체를 덮는 구간의 구간 파일은 원본 파일과 바이트까지 같아야 한다.

    기본 입력, 구간 둘: 0초 ~ 영상 길이(프레임 0~179) · 프레임 35~80
    -> 완료 1회, `_1`의 bytes == 원본 파일의 bytes, `_2`가 판정 통과, 저장 폴더에 두 파일뿐
    """
    duration = _index(sources["plain"]).duration
    selections = [TimeRange(0.0, duration), TimeRange(_seconds(35), _seconds(80))]

    run = _Run(server, "plain", tmp_path, selections).start()

    assert (run.finished, run.failures) == (1, [])
    section = run.engine.sections[0]
    assert (section.first_frame, section.last_frame) == (0, 179)
    with open(run.paths[0], "rb") as made, open(sources["plain"], "rb") as source:
        assert made.read() == source.read()
    check = check_cut(_frames(sources["plain"]), run.engine.cut_results[1])
    assert check.ok, check.notes
    assert run.listing() == ["구간 시험 144p_1.mp4", "구간 시험 144p_2.mp4"]


def test_overlapping_sections_both_get_the_bytes_they_share(server, sources, tmp_path):
    """겹치는 두 구간은 겹친 범위를 한 번만 받고도 둘 다 판정을 통과해야 한다.

    기본 입력, 구간 프레임 35~130 · 60~160 (프레임 72~119의 GOP 둘을 두 구간이 함께 옮긴다)
    -> 완료 1회, 두 구간 파일 모두 check.ok,
       받은 바이트 < 두 구간을 따로 받은 실행 둘의 받은 바이트의 합
    """
    both = [TimeRange(_seconds(35), _seconds(130)), TimeRange(_seconds(60), _seconds(160))]

    folders = [tmp_path / name for name in ("both", "first", "second")]
    for folder in folders:
        folder.mkdir()

    run = _Run(server, "plain", folders[0], both).start()
    apart = [_Run(server, "plain", folder, [one]).start() for folder, one in zip(folders[1:], both)]

    assert (run.finished, run.failures) == (1, [])
    frames = _frames(sources["plain"])
    for result in run.engine.cut_results:
        check = check_cut(frames, result)
        assert check.ok, check.notes
    assert run.data.total_size < sum(one.data.total_size for one in apart)


def test_a_section_from_a_keyframe_to_the_end_is_made_without_encoding(server, sources, tmp_path):
    """키프레임에서 시작해 영상의 마지막 프레임에서 끝나는 구간은 다시 인코딩하지 않고 만들어야 한다.

    기본 입력, 구간 프레임 90~179 (프레임 90은 키프레임, 179는 마지막 프레임)
    -> 단계 이름 == [probe, write] (조각 단계가 없다),
       디코드한 프레임 == 원본을 디코드한 프레임의 90번째부터
    """
    duration = _index(sources["plain"]).duration

    run = _Run(server, "plain", tmp_path, [TimeRange(_seconds(90), duration)]).start()

    assert (run.finished, run.failures) == (1, [])
    section = run.engine.sections[0]
    assert (section.first_frame, section.last_frame) == (90, 179)
    ((_number, _total, stages),) = _logged(run, "log_cut_stages")
    assert [name for name, _seconds in stages] == ["probe", "write"]
    made = _decoded_frames(run.paths[0])
    assert len(made) == 90
    assert made == _decoded_frames(sources["plain"])[90:]


def test_retry_receives_the_failed_section_again_when_its_body_is_gone(
    server, sources, tmp_path, monkeypatch
):
    """남겨 둔 본문 파일이 없으면 이어받은 실행은 끝나지 않은 구간의 범위를 다시 받아야 한다.

    첫 실행: 구간 셋, 둘째가 도중에 실패. 둘째 구간의 본문 파일을 지움(경계 원본은 그대로). 이어받은 실행
    -> 완료 1회 · 실패 0건, 이어받은 실행의 요청이 있다, 끝내는 호출 1회(`_2`), `_2`가 판정 통과,
       `_1` · `_3`의 수정 시각 · 내용이 그대로, 저장 폴더에 `_1` · `_2` · `_3`뿐
    """
    cuts = CutCalls(monkeypatch, fd_module, "finish_section_file", source_arg=0)
    cuts.fail_on = (2,)
    failed = _Run(server, "plain", tmp_path, _three()).start()
    assert len(failed.failures) == 1
    kept = snapshot([failed.paths[0], failed.paths[2]])
    body = section_body_path_for(failed.paths[1])
    assert os.path.exists(body), "전제: 실패한 구간의 본문이 남아 있었다"
    os.remove(body)
    requests_before = len(server.requests)
    cuts.restart()

    again = hand_over(failed, _Run(server, "plain", tmp_path, _three())).start()

    assert (again.finished, again.failures) == (1, [])
    assert server.requests[requests_before:]
    assert cuts.outputs == [THREE_FILES[1]]
    check = check_cut(_frames(sources["plain"]), again.engine.cut_results[0])
    assert check.ok, check.notes
    assert snapshot(kept) == kept
    assert again.listing() == THREE_FILES


def test_retry_of_a_section_that_moves_no_bytes_receives_nothing(server, tmp_path, monkeypatch):
    """원본에서 옮기는 바이트가 없는 구간도, 실패한 뒤 이어받은 실행은 다시 받지 않고 끝내야 한다.

    오디오 없는 영상(기본 입력과 같은 영상 트랙)의 구간 프레임 100~110 — 한 GOP 안이라 구간 전체를
    다시 인코딩하고, 오디오가 없어 본문에 옮길 것이 없다. 첫 실행: 첫 ffmpeg 실행 자리에서 실패.
    이어받은 실행
    -> 첫 실행이 남긴 본문 파일은 비워 둔 머리 자리뿐이다(0이 아닌 바이트가 없다),
       이어받은 실행: 완료 1회 · 실패 0건, 요청 0건, 구간 파일의 프레임 11개
    """
    silent = str(tmp_path / "silent.mp4")
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=6",
        "-an", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast",
        "-bf", "2", "-force_key_frames", "0,1,1.4,2.4,3,4,5",
        "-x264-params", "b-pyramid=none:keyint=300:min-keyint=1:scenecut=0",
        "-movflags", "+faststart", silent,
    )  # fmt: skip
    with open(silent, "rb") as f:
        monkeypatch.setitem(server.files, "silent", f.read())
    selections = [TimeRange(_seconds(100), _seconds(110))]
    cuts = CutCalls(monkeypatch, fd_module, "finish_section_file", source_arg=0)
    cuts.fail_on = (1,)
    failed = _Run(server, "silent", tmp_path, selections).start()
    assert len(failed.failures) == 1
    body = section_body_path_for(failed.paths[0])
    with open(body, "rb") as f:
        kept = f.read()
    assert kept and not any(kept), "전제: 본문에는 비워 둔 머리 자리뿐이다(받은 바이트가 없다)"
    requests_before = len(server.requests)
    cuts.restart()

    again = hand_over(failed, _Run(server, "silent", tmp_path, selections)).start()

    assert (again.finished, again.failures) == (1, [])
    assert server.requests[requests_before:] == []
    assert len(_decoded_frames(again.paths[0])) == 11


def test_a_failed_section_leaves_nothing_under_its_final_name(server, tmp_path, monkeypatch):
    """끝내지 못한 구간은 산출물의 이름으로 아무것도 남기지 않고, 받은 바이트는 본문 파일에 남겨야 한다.

    기본 입력, 구간 프레임 35~80, 첫 ffmpeg 실행 자리에서 실패
    -> 실패 1건, `_1` 파일 없음, 본문 파일이 있고 그 크기 > 0, 본문은 mp4의 머리로 시작하지 않는다
    """
    cuts = CutCalls(monkeypatch, fd_module, "finish_section_file", source_arg=0)
    cuts.fail_on = (1,)

    run = _Run(server, "plain", tmp_path, [TimeRange(_seconds(35), _seconds(80))]).start()

    assert len(run.failures) == 1
    assert not os.path.exists(run.paths[0])
    body = section_body_path_for(run.paths[0])
    assert os.path.getsize(body) > 0
    with open(body, "rb") as f:
        assert f.read(8)[4:] != b"ftyp"


def test_map_writer_splits_a_chunk_across_runs_and_files(tmp_path):
    """받은 조각이 대응표의 여러 줄 · 여러 파일에 걸치면 줄마다 제자리에 나눠 써야 한다.

    파일 a: 원본 10~19 → 0, 원본 30~39 → 10 / 파일 b: 원본 15~34 → 5.
    원본 12부터 24바이트 "ABCDEFGHIJKLMNOPQRSTUVWX"(원본 12~35)를 8바이트씩 받음
    -> a == 00 00 "ABCDEFGH" "STUVWX" 00 00 00 00, b == 00×5 "DEFGHIJKLMNOPQRSTUVW"
    """
    a, b = str(tmp_path / "a"), str(tmp_path / "b")
    for path, size in ((a, 20), (b, 25)):
        with open(path, "wb") as f:
            f.write(bytes(size))
    outlets = (
        fd_module._Outlet(a, ((10, 10, 0), (30, 10, 10)), (10, 30)),
        fd_module._Outlet(b, ((15, 20, 5),), (15,)),
    )
    received = b"ABCDEFGHIJKLMNOPQRSTUVWX"

    with fd_module._MapWriter(outlets, 12) as writer:
        for start in range(0, len(received), 8):
            writer.write(received[start : start + 8])

    with open(a, "rb") as f:
        assert f.read() == b"\x00\x00ABCDEFGH" + b"STUVWX" + bytes(4)
    with open(b, "rb") as f:
        assert f.read() == bytes(5) + b"DEFGHIJKLMNOPQRSTUVW"


def test_map_writer_leaves_files_it_has_nothing_for_unopened(tmp_path):
    """받은 조각이 어느 줄에도 걸치지 않는 파일은 열지 않아야 한다 — 없는 파일이어도 된다.

    파일 a: 원본 0~9 → 0. 파일 b(없는 경로): 원본 100~109 → 0. 원본 0부터 10바이트를 받음
    -> a에 10바이트가 쓰이고 예외가 없다
    """
    a = str(tmp_path / "a")
    with open(a, "wb") as f:
        f.write(bytes(10))
    outlets = (
        fd_module._Outlet(a, ((0, 10, 0),), (0,)),
        fd_module._Outlet(str(tmp_path / "missing"), ((100, 10, 0),), (100,)),
    )

    with fd_module._MapWriter(outlets, 0) as writer:
        writer.write(b"0123456789")

    with open(a, "rb") as f:
        assert f.read() == b"0123456789"


# ================================================================ 시작 전 여유 공간 검사


def _needed_space(server, tmp_path, monkeypatch, selections) -> int:
    """여유 공간이 0일 때 엔진이 알린 필요한 크기 — 받기 전에 실패한 실행에서 읽는다."""
    monkeypatch.setattr(fd_module, "_free_bytes", lambda folder: 0)
    folder = tmp_path / "probe"
    folder.mkdir()
    run = _Run(server, "plain", folder, selections).start()
    (error,) = run.failures
    assert isinstance(error, DiskSpaceError)
    return error.needed


def test_section_download_does_not_start_when_the_folder_lacks_space(
    server, sources, tmp_path, monkeypatch
):
    """저장 폴더의 여유 공간이 구간 파일들보다 적으면 받기를 시작하지 않고 실패해야 한다.

    기본 입력, 구간 프레임 35~80, 여유 공간 1000바이트
    -> 실패 1건(DiskSpaceError, 키 "Not enough disk space", free == 1000),
       moov 뒤의 범위 요청 0건, 저장 폴더가 비어 있다
    """
    monkeypatch.setattr(fd_module, "_free_bytes", lambda folder: 1000)
    run = _Run(server, "plain", tmp_path, [TimeRange(_seconds(35), _seconds(80))])
    server.forget()

    run.start()

    assert run.finished == 0
    (error,) = run.failures
    assert isinstance(error, DiskSpaceError)
    assert (error.message_key, error.free) == (DISK_SPACE_SHORT, 1000)
    moov_last = _index(sources["plain"]).moov_range[1]
    assert _spans_after_moov(server.requests, moov_last) == []
    assert run.listing() == []


def test_needed_space_covers_the_section_files_and_one_byte_less_is_refused(
    server, tmp_path, monkeypatch
):
    """필요한 크기만큼의 여유 공간이면 받고, 1바이트 모자라면 받지 않아야 한다 — 그 크기는 만든 구간 파일들보다 작지 않다.

    기본 입력, 구간 셋. 필요한 크기 N을 여유 공간 0인 실행에서 읽음
    -> 여유 N − 1: 실패(DiskSpaceError) / 여유 N: 완료 1회, N >= 만든 구간 파일 크기의 합
    """
    needed = _needed_space(server, tmp_path, monkeypatch, _three())
    short, enough = tmp_path / "short", tmp_path / "enough"
    short.mkdir()
    enough.mkdir()

    monkeypatch.setattr(fd_module, "_free_bytes", lambda folder: needed - 1)
    refused = _Run(server, "plain", short, _three()).start()
    monkeypatch.setattr(fd_module, "_free_bytes", lambda folder: needed)
    run = _Run(server, "plain", enough, _three()).start()

    assert [type(error) for error in refused.failures] == [DiskSpaceError]
    assert (run.finished, run.failures) == (1, [])
    assert needed >= _sizes(run.paths) > 0


def _folder_bytes(folder) -> int:
    """folder 아래(하위 폴더까지)에 있는 파일 크기의 합."""
    return sum(
        os.path.getsize(os.path.join(root, name))
        for root, _folders, names in os.walk(folder)
        for name in names
    )


def test_needed_space_counts_a_boundary_gop_two_sections_share_for_each_section(
    server, sources, tmp_path, monkeypatch
):
    """같은 경계 GOP를 함께 쓰는 두 구간에 필요한 크기는 그 GOP를 다시 인코딩한 몫을 구간마다 세야 한다.

    기본 입력(키프레임 0 · 30 · 42 · 72 · 90 …), 구간 프레임 35~50 · 60~100 — 첫 구간의 꼬리와
    둘째 구간의 머리가 GOP 42~71에 있다. 구간마다 다시 인코딩하는 조각의 원본 바이트 범위의 합을
    R1 · R2라 함. 필요한 크기 N은 여유 공간 0인 실행에서 읽고, 여유가 넉넉한 실행에서 구간 파일을
    끝내기 직전의 경계 원본 · 본문의 크기와 조각을 붙인 직후 저장 폴더에 놓인 바이트를 읽음
    -> N == 경계 원본의 크기 + (본문 1 + R1) + (본문 2 + R2) + max(R1, R2),
       조각을 붙인 직후 저장 폴더에 놓인 바이트의 최대 <= N, 완료 1회,
       파일 크기 줄의 가장 큰 파일 == max(경계 원본의 크기, 본문 1 + R1, 본문 2 + R2)
    """
    selections = [TimeRange(_seconds(35), _seconds(50)), TimeRange(_seconds(60), _seconds(100))]
    index = _index(sources["plain"])
    ranges = []
    for first, last in ((35, 50), (60, 100)):
        pieces = [piece for piece in section_pieces(index, first, last) if piece.reencoded]
        spans = [frames_byte_range(index, piece.first, piece.end - 1) for piece in pieces]
        ranges.append(sum(end - begin for begin, end, _keyframe in spans))
    needed = _needed_space(server, tmp_path, monkeypatch, selections)
    monkeypatch.setattr(fd_module, "_free_bytes", lambda folder: 10**13)
    run = _Run(server, "plain", tmp_path, selections)
    real_finish, real_write = fd_module.finish_section_file, fd_module._write_section_file
    before, placed = {}, []

    def finish(source_path, *args, body_path, **kwargs):
        before.setdefault("source", os.path.getsize(source_path))
        before.setdefault("bodies", {})[body_path] = os.path.getsize(body_path)
        return real_finish(source_path, *args, body_path=body_path, **kwargs)

    def write_file(*args):
        real_write(*args)
        placed.append(_folder_bytes(run.folder))

    monkeypatch.setattr(fd_module, "finish_section_file", finish)
    monkeypatch.setattr(fd_module, "_write_section_file", write_file)

    run.start()

    assert (run.finished, run.failures) == (1, [])
    assert len(before["bodies"]) == 2 and len(placed) == 2
    bodies = [before["bodies"][path] for path in sorted(before["bodies"])]  # 구간 번호순
    assert needed == before["source"] + sum(bodies) + sum(ranges) + max(ranges)
    assert 0 < max(placed) <= needed
    files = [body + pieces for body, pieces in zip(bodies, ranges)]
    (largest,) = [args[1] for name, args in run.logger.calls if name == "log_file_size_check"]
    assert largest == max(before["source"], *files)


def test_needed_space_of_a_whole_video_section_is_at_least_the_source_file(
    server, sources, tmp_path, monkeypatch
):
    """영상 전체를 덮는 구간에 필요한 크기는 원본 파일의 크기 이상이어야 한다.

    기본 입력, 구간 0초 ~ 영상 길이 -> 필요한 크기 >= 원본 파일의 바이트 수
    """
    duration = _index(sources["plain"]).duration

    needed = _needed_space(server, tmp_path, monkeypatch, [TimeRange(0.0, duration)])

    assert needed >= os.path.getsize(sources["plain"])


def test_section_download_does_not_start_when_a_file_is_over_the_fat_limit(
    server, sources, tmp_path, monkeypatch
):
    """저장 폴더가 FAT 계열이고 만들 파일 하나가 그 한계를 넘으면 구간 다운로드를 받지 않고 실패해야 한다.

    기본 입력, 구간 셋, 파일 시스템 "FAT32". 한계를 1000바이트로 두고 돌려 가장 큰 파일의 크기 L을
    읽은 뒤, 한계 L − 1과 L로 다시 돌림
    -> 한계 1000 · L − 1: 실패 1건(FileSizeLimitError, 키 "File too large for a FAT32 drive"),
       moov 뒤의 범위 요청 0건, 저장 폴더가 비어 있다 / 한계 L: 완료 1회
       L은 만든 구간 파일 가운데 가장 큰 것보다 작지 않고, 파일 크기 줄에 ("FAT32", L)이 남는다
    """
    monkeypatch.setattr(base_module, "filesystem_name", lambda folder: "FAT32")
    folders = [tmp_path / name for name in ("probe", "short", "enough")]
    for folder in folders:
        folder.mkdir()

    monkeypatch.setattr(base_module, "FAT_MAX_FILE_BYTES", 1000)
    server.forget()
    probe = _Run(server, "plain", folders[0], _three()).start()
    (error,) = probe.failures
    assert isinstance(error, FileSizeLimitError)
    largest = error.largest
    moov_last = _index(sources["plain"]).moov_range[1]
    requested = _spans_after_moov(server.requests, moov_last)
    monkeypatch.setattr(base_module, "FAT_MAX_FILE_BYTES", largest - 1)
    refused = _Run(server, "plain", folders[1], _three()).start()
    monkeypatch.setattr(base_module, "FAT_MAX_FILE_BYTES", largest)
    run = _Run(server, "plain", folders[2], _three()).start()

    assert (error.message_key, error.filesystem) == (FILE_TOO_LARGE_FOR_FAT, "FAT32")
    assert (requested, probe.listing(), probe.finished) == ([], [], 0)
    assert [type(e) for e in refused.failures] == [FileSizeLimitError]
    assert (run.finished, run.failures) == (1, [])
    assert largest >= max(os.path.getsize(path) for path in run.paths) > 0
    assert ("log_file_size_check", ("FAT32", largest)) in run.logger.calls


def test_section_download_starts_when_the_free_space_is_unknown(server, tmp_path, monkeypatch):
    """여유 공간을 알 수 없으면(OSError) 검사하지 않고 받아야 한다.

    기본 입력, 구간 프레임 35~80, 여유 공간을 묻는 함수가 OSError를 냄 -> 완료 1회 · 실패 0건
    """

    def unknown(folder):
        raise OSError("시험")

    monkeypatch.setattr(fd_module, "_free_bytes", unknown)

    run = _Run(server, "plain", tmp_path, [TimeRange(_seconds(35), _seconds(80))]).start()

    assert (run.finished, run.failures) == (1, [])


def test_retry_from_kept_files_does_not_ask_for_space_again(server, tmp_path, monkeypatch):
    """남긴 것을 그대로 쓰는 이어받은 실행은 여유 공간을 다시 요구하지 않아야 한다 — 받을 것이 없다.

    첫 실행: 구간 셋, 둘째가 도중에 실패. 이어받은 실행은 여유 공간 0
    -> 완료 1회 · 실패 0건, 요청 0건
    """
    cuts = CutCalls(monkeypatch, fd_module, "finish_section_file", source_arg=0)
    cuts.fail_on = (2,)
    failed = _Run(server, "plain", tmp_path, _three()).start()
    assert len(failed.failures) == 1
    requests_before = len(server.requests)
    cuts.restart()
    monkeypatch.setattr(fd_module, "_free_bytes", lambda folder: 0)

    again = hand_over(failed, _Run(server, "plain", tmp_path, _three())).start()

    assert (again.finished, again.failures) == (1, [])
    assert server.requests[requests_before:] == []


# ================================================================ 끝낸 구간의 파일이 없거나 바뀐 재시도 (#309)


def _fail_the_second_of_three(server, tmp_path, monkeypatch):
    """구간 셋 가운데 둘째 컷만 실패한 실행과, 컷 호출을 적는 도구(다음 실행을 위해 비운 것)."""
    cuts = CutCalls(monkeypatch, fd_module, "finish_section_file", source_arg=0)
    cuts.fail_on = (2,)
    failed = _Run(server, "plain", tmp_path, _three()).start()
    assert len(failed.failures) == 1
    cuts.restart()
    return failed, cuts


def test_failed_run_records_the_size_of_each_section_file_it_finished(
    server, tmp_path, monkeypatch
):
    """일부 구간만 실패한 실행은 끝낸 구간 파일의 크기를 재시도 기록에 적어야 한다.

    구간 셋, 둘째 컷이 실패 -> 기록의 크기 == ((0, `_1`의 크기), (2, `_3`의 크기))
    """
    failed, _cuts = _fail_the_second_of_three(server, tmp_path, monkeypatch)

    assert failed.data.section_resume.done_sizes == (
        (0, os.path.getsize(failed.paths[0])),
        (2, os.path.getsize(failed.paths[2])),
    )


@pytest.mark.parametrize("check_files", [True, False], ids=["starter-checks", "engine-checks"])
def test_retry_makes_a_finished_section_again_when_its_file_was_removed(
    server, sources, tmp_path, monkeypatch, check_files
):
    """끝낸 구간의 파일을 지운 뒤 재시도하면 그 구간도 다시 만들어야 한다.

    첫 실행: 구간 셋, 둘째 컷이 실패. `_1`을 지움. 이어받은 실행(시작하는 쪽이 파일을 확인한
    경우와, 기록을 그대로 넘겨 엔진이 확인하는 경우)
    -> 완료 1회 · 실패 0건, 컷 호출은 `_1` · `_2`, `_3`의 수정 시각 · 내용이 그대로,
       저장 폴더에 `_1` · `_2` · `_3`뿐, 다시 만든 `_1`이 판정 통과, 이어받은 구간 수 1
    """
    failed, cuts = _fail_the_second_of_three(server, tmp_path, monkeypatch)
    kept = snapshot([failed.paths[2]])
    os.remove(failed.paths[0])

    again = hand_over(
        failed, _Run(server, "plain", tmp_path, _three()), check_files=check_files
    ).start()

    assert (again.finished, again.failures) == (1, [])
    assert sorted(cuts.outputs) == THREE_FILES[:2]
    assert snapshot(kept) == kept
    assert again.listing() == THREE_FILES
    check = check_cut(_frames(sources["plain"]), again.engine.cut_results[0])
    assert check.ok, check.notes
    assert again.data.sections_resumed == 1


def test_retry_makes_a_finished_section_again_when_its_file_changed_size(
    server, tmp_path, monkeypatch
):
    """끝낸 구간의 파일 크기가 달라졌으면 재시도는 그 구간을 다시 만들고, 바뀐 파일은 건드리지 않아야 한다.

    첫 실행: 구간 셋, 둘째 컷이 실패. `_3`의 자리에 10바이트짜리 다른 파일을 둠. 이어받은 실행
    -> 완료 1회 · 실패 0건, 셋째 구간은 새 이름(`_3`이 아닌 경로)으로 만들어지고 크기가 0보다 크다,
       `_3` 자리의 파일은 10바이트 그대로, `_1`의 수정 시각 · 내용이 그대로, 이어받은 구간 수 1
    """
    failed, cuts = _fail_the_second_of_three(server, tmp_path, monkeypatch)
    kept = snapshot([failed.paths[0]])
    with open(failed.paths[2], "wb") as f:
        f.write(b"0123456789")

    again = hand_over(failed, _Run(server, "plain", tmp_path, _three())).start()

    assert (again.finished, again.failures) == (1, [])
    assert again.paths[2] != failed.paths[2]
    assert os.path.getsize(again.paths[2]) > 0
    assert os.path.basename(again.paths[2]) in cuts.outputs
    with open(failed.paths[2], "rb") as f:
        assert f.read() == b"0123456789"
    assert snapshot(kept) == kept
    assert again.data.sections_resumed == 1
