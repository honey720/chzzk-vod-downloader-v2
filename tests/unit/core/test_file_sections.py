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
- 임시 원본의 크기는 머리(원본의 첫 샘플 앞까지) + 받은 바이트다. 성공하면 지우고 컷이
  실패하면 남긴다
- 구간 파일은 온전한 파일에서 자른 것과 바이트까지 같다
"""

import os
import re
import subprocess
from types import SimpleNamespace

import pytest

import core.api.mp4 as mp4_module
import core.downloaders.file_downloader as fd_module
import core.utils.mp4_partial as partial_module
from core.api.mp4 import MP4_UNSUPPORTED, Mp4Error, fetch_mp4_head, read_mp4_index
from core.downloaders.base import PostprocessError, TruncatedBodyError
from core.downloaders.file_downloader import FileDownloader
from core.downloaders.ranges import split_span
from core.models.download_data import DownloadData
from core.models.download_state import DownloadState
from core.models.plan import TimeRange
from core.utils.cut_check import check_cut
from core.utils.ffmpeg import get_ffmpeg_exe
from core.utils.hybrid_cut import CUT_FAILED, CutError, cut_frames_from_mp4, hybrid_cut
from core.utils.paths import build_section_output_paths, partial_source_path_for
from core.utils.selections import SELECTION_OUT_OF_RANGE, SelectionError
from tests.unit.core.range_host import RangeHost

FPS = 30
KEYFRAMES = (0, 30, 42, 72, 90, 120, 150)  # -force_key_frames 0,1,1.4,2.4,3,4,5 (30fps)


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

    def _on_finished(self) -> None:
        self.finished += 1

    def start(self) -> "_Run":
        self.data.model.start()
        self.engine.run()
        return self

    @property
    def source_path(self) -> str:
        return partial_source_path_for(self.paths[0])

    def listing(self) -> list[str]:
        return sorted(os.listdir(self.folder))


def _frames(path: str):
    return cut_frames_from_mp4(_index(path))


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
    """받은 범위만으로 자른 구간 파일은 온전한 파일에서 같은 프레임을 자른 파일과 바이트까지 같아야 한다.

    기본 입력, 구간 프레임 35~80
    -> 구간 파일의 bytes == hybrid_cut(온전한 파일, 35, 80)의 bytes
    """
    run = _Run(server, "plain", tmp_path, [TimeRange(_seconds(35), _seconds(80))]).start()
    reference = str(tmp_path / "reference.mp4")
    hybrid_cut(sources["plain"], _frames(sources["plain"]), 35, 80, reference)

    with open(run.paths[0], "rb") as made, open(reference, "rb") as wanted:
        assert made.read() == wanted.read()


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
    -> moov와 겹치는 범위 요청 0건, 구간 파일의 bytes == 온전한 파일에서 자른 파일의 bytes
    """
    run = _Run(server, "plain", tmp_path, [TimeRange(_seconds(35), _seconds(80))])
    run.data.content.mp4_head = fetch_mp4_head(server.url("plain"))
    server.forget()

    run.start()

    reference = str(tmp_path / "reference.mp4")
    hybrid_cut(sources["plain"], _frames(sources["plain"]), 35, 80, reference)
    assert (run.finished, run.failures) == (1, [])
    assert _moov_requests(server, "plain", _index(sources["plain"])) == []
    with open(run.paths[0], "rb") as made, open(reference, "rb") as wanted:
        assert made.read() == wanted.read()


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

    monkeypatch.setattr(fd_module, "hybrid_cut", broken)

    run = _Run(server, "plain", tmp_path, [TimeRange(_seconds(150), _seconds(170))]).start()

    head_size = _first_sample_offset(_index(sources["plain"]))
    assert os.path.getsize(run.source_path) == head_size + run.data.total_size
    assert head_size + run.data.total_size < os.path.getsize(sources["plain"])


def test_cut_failure_keeps_partial_source_and_finished_sections(server, tmp_path, monkeypatch):
    """둘째 구간의 컷이 실패하면 다운로드는 실패하고, 임시 원본과 먼저 만든 구간 파일은 남아야 한다.

    기본 입력, 구간 둘, 둘째 컷이 CutError를 내도록 바꿈
    -> 완료 콜백 0회, 실패 1건(PostprocessError, 원인 CutError), `_1` 파일과 임시 원본만 남음
    """
    real = fd_module.hybrid_cut
    calls = []

    def second_fails(*args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            raise CutError(CUT_FAILED, "시험")
        return real(*args, **kwargs)

    monkeypatch.setattr(fd_module, "hybrid_cut", second_fails)
    selections = [TimeRange(_seconds(35), _seconds(80)), TimeRange(_seconds(100), _seconds(110))]

    run = _Run(server, "plain", tmp_path, selections).start()

    assert run.finished == 0
    assert len(run.failures) == 1
    assert isinstance(run.failures[0], PostprocessError)
    assert isinstance(run.failures[0].__cause__, CutError)
    assert run.listing() == ["CVDv2_part_구간 시험 144p_1.mp4", "구간 시험 144p_1.mp4"]


def test_cut_failure_of_one_section_still_cuts_the_sections_after_it(server, tmp_path, monkeypatch):
    """구간 셋 가운데 둘째의 컷이 실패해도 셋째는 잘라야 하고, 다운로드는 그 뒤에 한 번 실패해야 한다.

    기본 입력, 구간 셋, 둘째 컷이 CutError를 내도록 바꿈
    -> 컷 호출 3회, 완료 콜백 0회, 실패 1건(PostprocessError, 원인 CutError),
       `_1` · `_3` 파일과 임시 원본이 남는다, 공유 데이터의 구간 상태 == (전체 3, 완료 2, 실패 1)
    """
    real = fd_module.hybrid_cut
    calls = []

    def second_fails(*args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            raise CutError(CUT_FAILED, "시험")
        return real(*args, **kwargs)

    monkeypatch.setattr(fd_module, "hybrid_cut", second_fails)
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
    assert run.listing() == [
        "CVDv2_part_구간 시험 144p_1.mp4",
        "구간 시험 144p_1.mp4",
        "구간 시험 144p_3.mp4",
    ]
    data = run.data
    assert (data.sections_total, data.sections_done, data.sections_failed) == (3, 2, 1)


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
    real = fd_module.hybrid_cut

    def interrupted(*args, **kwargs):
        if how == "failure":
            raise OSError("시험")
        run.data.model.stop()
        return real(*args, **kwargs)

    monkeypatch.setattr(fd_module, "hybrid_cut", interrupted)

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

    monkeypatch.setattr(fd_module, "hybrid_cut", broken)
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
