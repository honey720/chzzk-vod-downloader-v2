"""M3U8Downloader의 구간 다운로드 — 인코딩 전 다시보기(HLS fMP4), 받기부터 구간 파일까지 (#309).

실제 ffmpeg(imageio-ffmpeg 동봉)로 테스트 안에서 짧은 영상을 만들어 HLS fMP4(초기화
세그먼트 + 세그먼트 + 플레이리스트)로 싸고, 범위 요청에 답하는 호스트
(tests/support/range_host.py — 소켓을 열지 않는다)로 내준다. M3U8Downloader를 구간과
함께 실제로 돌려 구간 파일이 나오는지, 정합 판정(check_cut)을 통과하는지 본다.

입력 (둘 다 320x240 · 30fps · 6초 · 1초 세그먼트 6개 · 키프레임 0·30·36·60·90·120·150 —
30~36은 6프레임짜리 짧은 GOP):
- plain: 재정렬 지연 2프레임
- uneven: 재정렬 지연 1프레임, 7프레임마다 한 프레임이 200틱(timescale 15360) 늦다

플레이리스트 변형(세그먼트는 plain의 것):
- broken.m3u8: 넷째 세그먼트 앞에 #EXT-X-DISCONTINUITY
- twice.m3u8: 세그먼트 여섯을 한 번 더 잇고 그 사이에 #EXT-X-DISCONTINUITY — 뒤쪽의
  타임스탬프가 0으로 돌아간다
- late.m3u8: 뒤쪽 세그먼트 셋(넷째 ~ 여섯째) · #EXT-X-DISCONTINUITY · 세그먼트 여섯 — 첫
  세그먼트의 타임스탬프가 3초이고 끊긴 자리 뒤가 0이다

실제 다시보기의 모양을 흉내 낸 입력 "real" (plain의 세그먼트를 둘씩 묶은 것):
- 세그먼트 하나에 moof가 둘이고 그 사이에 emsg 상자가 있다 (세그먼트 셋, 2초씩)
- #EXTINF는 2.001333으로 실제 간격(2.000초)보다 길고, 세그먼트마다 #EXT-X-PROGRAM-DATE-TIME이 있다
- 초기화 세그먼트의 esds · btrt에 비트레이트가 0으로 적혀 있다
- nopdt.m3u8: 같은 것에서 #EXT-X-PROGRAM-DATE-TIME을 뺀 것
- padded.m3u8: nopdt의 #EXTINF를 2.5로 늘린 것 — 플레이리스트가 말하는 길이가 실제보다 길다

핵심 계약:
- 받는 세그먼트는 구간들의 세그먼트를 합친 것이고(앞 세그먼트 하나 포함) 한 번씩만 받는다
- 세그먼트에 범위 요청을 보내지 않는다 — 범위 요청에 잘린 본문을 주고 그것을 캐시하는
  서버에서도 온전한 파일이 나온다
- 플레이리스트·초기화 세그먼트·세그먼트는 한 번씩만 받는다. 구간을 해석하며 받아 둔
  세그먼트는 엔진이 다시 받지 않는다
- 잘린 세그먼트로 컷을 하지 않는다
- 구간 파일의 영상 패킷은 세그먼트 전부를 이은 파일에서 하이브리드 컷으로 자른 것과 모두
  같다. 끝이 세그먼트의 끝과 맞는 구간만 다르다 — 마지막 GOP를 다시 만들지 않아 원본의
  패킷 그대로다
- 구간 파일의 오디오 패킷은 원본의 것 그대로다(다시 인코딩하지 않는다) — 구간 시작에 걸친
  덩어리부터 구간 끝에 걸친 덩어리까지
- 끝이 영상 길이와 같은 구간의 끝 프레임은 마지막 프레임이다
- 구간 파일로 넘긴 세그먼트는 남은 구간이 쓰지 않으면 곧바로 지운다. 구간은 긴 것부터 만든다

대조는 파일 바이트가 아니라 패킷(시각 + 내용의 CRC)으로 한다.
"""

import os
import struct
import subprocess
from fractions import Fraction

import pytest

import core.api.hls_fmp4 as hls_fmp4_module
import core.downloaders.base as base_module
import core.downloaders.m3u8_downloader as m3u8_module
import core.downloaders.segment_space as space_module
import core.utils.ffmpeg as ffmpeg_module
import core.cutting.fmp4_cut as fmp4_cut_module
from core.container.fmp4 import (
    build_fmp4_index,
    fmp4_origin,
    parse_init_segment,
    parse_media_segment,
)
from core.api.hls import parse_media_playlist
from core.api.hls_fmp4 import fetch_fmp4_head, segment_file_name, segment_frames
from core.downloaders.base import (
    DISK_SPACE_SHORT,
    FILE_TOO_LARGE_FOR_FAT,
    DiskSpaceError,
    FileSizeLimitError,
    PostprocessError,
)
from core.downloaders.integrity import TruncatedSegmentError
from core.downloaders.m3u8_downloader import M3U8Downloader
from core.models.content import StreamKey
from core.models.download_data import DownloadData
from core.models.plan import TimeRange
from core.cutting.check import check_cut
from core.utils.ffmpeg import RemuxError, get_ffmpeg_exe, run_ffmpeg
from core.sections.fmp4_sections import plan_fmp4_sections
from core.cutting.hybrid import CUT_FAILED, CutError, cut_frames_from_fmp4
from core.utils.paths import build_section_output_paths, temp_dir_for
from core.sections.selections import (
    SELECTION_CROSSES_BREAK,
    SELECTION_OUT_OF_RANGE,
    SELECTION_TOO_SHORT,
    SelectionError,
)
from tests.ffmpeg.sections.midway_cut_failure import MidwayCutFailure
from tests.support.range_host import RangeHost
from tests.ffmpeg.reference_cut import hybrid_cut
from tests.ffmpeg.sections.section_retry import OUTPUT_ARG, CutCalls, hand_over, snapshot
from tests.support.undeletable import Undeletable

KEYFRAMES = (0, 30, 36, 60, 90, 120, 150)  # -force_key_frames 0,1,1.2,2,3,4,5 (30fps)
SEGMENTS = 6  # 1초 세그먼트 — 키프레임 0·30·60·90·120·150에서 갈린다
REAL_EXTINF = 2.001333  # real의 #EXTINF — 실제 세그먼트 간격은 2.000초다
PADDED_EXTINF = 2.5  # padded.m3u8의 #EXTINF — 합(7.5초)이 실제 길이(약 6초)보다 훨씬 길다


def _make_hls(folder, *extra: str, b_frames: str, pyramid: str) -> None:
    done = subprocess.run(
        [get_ffmpeg_exe(), "-hide_banner", "-v", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=6",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6",
         *extra,
         "-c:a", "aac", "-ac", "2", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast",
         "-bf", b_frames, "-force_key_frames", "0,1,1.2,2,3,4,5",
         "-x264-params", f"b-pyramid={pyramid}:keyint=300:min-keyint=1:scenecut=0",
         "-f", "hls", "-hls_time", "1", "-hls_list_size", "0", "-hls_segment_type", "fmp4",
         "-hls_fmp4_init_filename", "init.mp4", "-hls_segment_filename", "seg-%03d.m4s",
         "media.m3u8"],
        cwd=str(folder),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )  # fmt: skip
    assert done.returncode == 0, done.stderr


class _Source:
    """만든 HLS 하나 — 파일들과, 세그먼트 전부를 이어 해석한 프레임 정보(대조용)."""

    def __init__(self, folder):
        self.files = {path.name: path.read_bytes() for path in folder.iterdir()}
        self.playlist = parse_media_playlist(self.files["media.m3u8"].decode("utf-8"))
        self.init = parse_init_segment(self.files["init.mp4"])
        parsed = [
            parse_media_segment(self.files[name], self.init) for name in self.playlist.segments
        ]
        origin = fmp4_origin(self.init, parsed[0])
        index = build_fmp4_index(self.init, parsed, origin)
        self.frames = cut_frames_from_fmp4(self.init, parsed, index)
        self.audio_pts = index.audio_pts  # 오디오 덩어리마다의 PTS(플레이리스트 시각, 초)
        # 오디오 덩어리 하나의 길이(초) — AAC 1024샘플
        self.audio_span = parsed[0].audio.durations[0] / self.init.audio.timescale
        self.joined = str(folder / "joined.mp4")  # 세그먼트 전부를 이은 파일 — 대조용 컷의 입력
        with open(self.joined, "wb") as f:
            f.write(self.files["init.mp4"])
            for name in self.playlist.segments:
                f.write(self.files[name])

    def time_of(self, frame: int) -> float:
        """그 프레임의 PTS(플레이리스트 시각, 초)."""
        return self.frames.frame_pts[frame]

    def selection(self, first: int, last: int) -> TimeRange:
        """프레임 first의 PTS부터 프레임 last의 PTS까지의 구간."""
        return TimeRange(self.time_of(first), self.time_of(last))


@pytest.fixture(scope="module")
def sources(tmp_path_factory) -> dict[str, _Source]:
    """이름 → 만든 HLS."""
    plain = tmp_path_factory.mktemp("hls_plain")
    _make_hls(plain, b_frames="3", pyramid="normal")
    uneven = tmp_path_factory.mktemp("hls_uneven")
    _make_hls(
        uneven,
        "-vf", "settb=1/15360,setpts='N*512+if(eq(mod(N,7),3),200,0)'",
        "-fps_mode", "passthrough", "-enc_time_base:v", "1:15360",
        b_frames="2", pyramid="none",
    )  # fmt: skip
    return {
        "plain": _Source(plain),
        "uneven": _Source(uneven),
        "real": _Source(_make_real(plain, tmp_path_factory.mktemp("hls_real"))),
    }


def _make_real(plain, folder):
    """plain의 세그먼트를 둘씩 묶어 실제 다시보기의 모양으로 만든 HLS를 folder에 쓴다."""
    names = sorted(path.name for path in plain.glob("seg-*.m4s"))
    init = (plain / "init.mp4").read_bytes()
    declared = struct.pack(">II", 128_000, 128_000)
    assert init.count(declared) >= 1  # esds(· btrt)에 적힌 max · avg 비트레이트
    (folder / "init.mp4").write_bytes(init.replace(declared, struct.pack(">II", 0, 0)))
    emsg = struct.pack(">I4s", 48, b"emsg") + bytes(40)
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-TARGETDURATION:2", '#EXT-X-MAP:URI="init.mp4"']
    for number in range(len(names) // 2):
        first, second = names[2 * number], names[2 * number + 1]
        paired = (plain / first).read_bytes() + emsg + (plain / second).read_bytes()
        (folder / f"seg-00{number}.m4s").write_bytes(paired)
        lines += [
            f"#EXT-X-PROGRAM-DATE-TIME:2026-01-01T00:00:{2 * number + 2:02d}.000Z",
            f"#EXTINF:{REAL_EXTINF},",
            f"seg-00{number}.m4s",
        ]
    (folder / "media.m3u8").write_text("\n".join([*lines, "#EXT-X-ENDLIST"]), encoding="utf-8")
    return folder


@pytest.fixture(scope="module")
def host(sources) -> RangeHost:
    """만든 파일과 플레이리스트 변형을 내주는 호스트."""
    files = {}
    for name, source in sources.items():
        for file_name, data in source.files.items():
            files[f"{name}/{file_name}"] = data
    lines = sources["plain"].files["media.m3u8"].decode("utf-8").splitlines()
    entries = [n for n, line in enumerate(lines) if line.startswith("#EXTINF:")]

    broken = list(lines)
    broken.insert(entries[3], "#EXT-X-DISCONTINUITY")
    files["plain/broken.m3u8"] = "\n".join(broken).encode("utf-8")

    body = lines[entries[0] : entries[-1] + 2]  # 첫 #EXTINF부터 마지막 세그먼트 줄까지
    twice = lines[: entries[-1] + 2] + ["#EXT-X-DISCONTINUITY", *body, "#EXT-X-ENDLIST"]
    files["plain/twice.m3u8"] = "\n".join(twice).encode("utf-8")

    pairs = [lines[n : n + 2] for n in entries]  # (#EXTINF 줄, 세그먼트 줄)
    late = lines[: entries[0]]
    late += [line for pair in pairs[3:] for line in pair]
    late += ["#EXT-X-DISCONTINUITY", *body, "#EXT-X-ENDLIST"]
    files["plain/late.m3u8"] = "\n".join(late).encode("utf-8")

    real = sources["real"].files["media.m3u8"].decode("utf-8").splitlines()
    nopdt = [line for line in real if not line.startswith("#EXT-X-PROGRAM-DATE-TIME")]
    files["real/nopdt.m3u8"] = "\n".join(nopdt).encode("utf-8")
    padded = [f"#EXTINF:{PADDED_EXTINF}," if line.startswith("#EXTINF") else line for line in nopdt]
    files["real/padded.m3u8"] = "\n".join(padded).encode("utf-8")
    return RangeHost(files)


@pytest.fixture(autouse=True)
def _requests_go_to_host(host, monkeypatch):
    """엔진과 moof 읽기가 쓰는 세션을 호스트로 가는 세션으로 바꾼다. 호스트의 상태는 테스트마다 되돌린다."""
    monkeypatch.setattr(m3u8_module, "get_thread_session", host.session)
    monkeypatch.setattr(hls_fmp4_module, "get_thread_session", host.session)
    host.ignore_range = False
    host.reset_cache()
    host.forget()


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

    def __init__(self, host, tmp_path, selections, playlist: str = "plain/media.m3u8"):
        self.folder = tmp_path / "out"
        self.folder.mkdir(exist_ok=True)
        self.data = DownloadData(
            base_url=host.url(playlist),
            vod_url="https://chzzk.naver.com/video/1",
            output_path=str(self.folder / "unused.mp4"),
            resolution=144,
            content_type="m3u8",
        )
        self.data.content.selections = tuple(selections)
        self.data.content.selection_paths = build_section_output_paths(
            str(self.folder), "구간 시험", 144, len(selections)
        )
        self.paths = self.data.content.selection_paths
        self.finished = 0
        self.failures: list[BaseException] = []
        self.logger = _Logger()
        self.engine = M3U8Downloader(
            self.data, self.logger, on_finished=self._on_finished, on_failed=self.failures.append
        )
        self.engine._inspect_cuts = True
        self.engine._slow_speed_threshold_kb_s = 0  # 러너 속도와 무관하게 — 저속 재큐를 끈다

    def _on_finished(self) -> None:
        self.finished += 1

    def start(self) -> "_Run":
        """RUNNING으로 옮기고 엔진을 끝까지 돌린다."""
        self.data.model.start()
        self.engine.run()
        return self

    def listing(self) -> list[str]:
        """저장 폴더에 있는 이름들(오름차순)."""
        return sorted(os.listdir(self.folder))

    def checks(self) -> list:
        """구간마다의 정합 판정(check_cut) 결과."""
        return [
            check_cut(frames, result)
            for frames, result in zip(self.engine.cut_frames, self.engine.cut_results)
        ]


def _packets(path: str) -> dict[str, list[str]]:
    """파일의 영상·오디오 패킷 — 스트림마다 framecrc 줄(시각 · 길이 · 크기 · 내용의 CRC) 목록."""
    found = {}
    for stream in ("v:0", "a:0"):
        done = run_ffmpeg(
            ["-v", "error", "-i", path, "-map", f"0:{stream}", "-c", "copy", "-f", "framecrc", "-"],
            timeout=60,
        )
        assert done.returncode == 0, done.stderr
        found[stream] = [line for line in done.stdout.splitlines() if not line.startswith("#")]
        assert found[stream]  # 빈 목록끼리 같다고 통과하지 않게
    return found


def _reference(source: _Source, first: int, last: int, tmp_path) -> dict[str, list[str]]:
    """세그먼트 전부를 이은 파일에서 같은 프레임을 하이브리드 컷으로 자른 파일의 패킷."""
    path = str(tmp_path / f"reference_{first}_{last}.mp4")
    hybrid_cut(source.joined, source.frames, first, last, path)
    return _packets(path)


def _contents(lines: list[str]) -> list[tuple[str, str]]:
    """framecrc 줄마다의 (크기, 내용의 CRC) — 시각을 뺀, 패킷의 내용만."""
    cells = [[cell.strip() for cell in line.split(",")] for line in lines]
    return [(row[4], row[5]) for row in cells]


def _stamps(lines: list[str]) -> list[int]:
    """framecrc 줄마다의 PTS(틱) — 오름차순."""
    return sorted(int(line.split(",")[2]) for line in lines)


def _assert_section_matches(
    source: _Source, path: str, first: int, last: int, tmp_path, original_tail: bool = False
) -> None:
    """구간 파일(path)이 source의 프레임 first~last와 맞는지 단언한다.

    - 영상: 세그먼트 전부를 이은 파일에서 하이브리드 컷으로 자른 것과 패킷(시각 · 내용)이 같다.
      original_tail(구간의 끝이 세그먼트의 끝과 맞는다)이면 마지막 GOP 앞까지만 그렇고, 마지막
      GOP는 원본의 패킷 그대로이며 PTS는 대조 파일과 같다
    - 오디오: 원본의 패킷 그대로다 — 구간 시작에 걸친 덩어리부터 구간 끝에 걸친 덩어리까지
    """
    made = _packets(path)
    reference = _reference(source, first, last, tmp_path)
    original = _packets(source.joined)
    frames = source.frames
    if original_tail:
        key = max(k for k in frames.keyframes if k <= last)
        before = key - first  # 마지막 GOP 앞의 패킷 수 — 닫힌 GOP라 디코드 순서로도 앞이다
        assert made["v:0"][:before] == reference["v:0"][:before]
        # 원본의 디코드 순서에서 그 GOP는 프레임 key번째 패킷부터다
        tail = _contents(original["v:0"])[key : last + 1]
        assert _contents(made["v:0"])[before:] == tail
        assert _stamps(made["v:0"]) == _stamps(reference["v:0"])
    else:
        assert made["v:0"] == reference["v:0"]

    start = frames.frame_pts[first]
    if last + 1 < len(frames.frame_pts):
        end = frames.frame_pts[last + 1]  # 구간은 다음 프레임의 PTS에서 끝난다
    else:
        end = frames.frame_pts[last] + frames.frame_duration
    wanted = [
        packet
        for pts, packet in zip(source.audio_pts, _contents(original["a:0"]), strict=True)
        if pts + source.audio_span > start and pts < end
    ]
    assert wanted  # 빈 목록끼리 같다고 통과하지 않게
    assert _contents(made["a:0"]) == wanted


def _requests_for(host, suffix: str, ranged: bool) -> list[str]:
    """호스트에 온 GET 가운데 이름이 suffix로 끝나고 Range 머리 유무가 ranged인 것의 이름."""
    return [
        name
        for method, name, header in host.requests
        if method == "GET" and name.endswith(suffix) and (header is not None) == ranged
    ]


# ================================================================ 입력의 전제


def test_generated_hls_has_six_segments_and_a_short_gop(sources):
    """만든 HLS는 세그먼트 6개와 초기화 세그먼트, 짧은 GOP를 가져야 한다.

    -hls_time 1 · -force_key_frames 0,1,1.2,2,3,4,5
    -> 세그먼트 6개, EXT-X-MAP "init.mp4", 키프레임 (0, 30, 36, 60, 90, 120, 150), 프레임 180개
    """
    source = sources["plain"]

    assert len(source.playlist.segments) == SEGMENTS
    assert source.playlist.init_uri == "init.mp4"
    assert source.frames.keyframes == KEYFRAMES
    assert len(source.frames.frame_pts) == 180


@pytest.mark.parametrize(("name", "delay"), [("plain", 2), ("uneven", 1)])
def test_generated_hls_has_the_wanted_reorder_delay(sources, name, delay):
    """만든 HLS의 재정렬 지연은 plain이 2프레임, uneven이 1프레임이어야 한다.

    b-pyramid=normal · -bf 3 (plain) / b-pyramid=none · -bf 2 (uneven)
    -> 키프레임 30의 (PTS − DTS) ÷ 한 프레임 == 지연
    """
    frames = sources[name].frames

    lead = frames.frame_pts[30] - frames.frame_dts[30]

    assert round(lead / frames.frame_duration) == delay


def test_generated_hls_declares_its_audio_bitrate_in_the_init_segment(sources):
    """만든 HLS의 초기화 세그먼트에는 오디오 비트레이트가 적혀 있고 영상 트랙에는 없어야 한다.

    ffmpeg aac 기본값(스테레오 128kb/s)
    -> 오디오 declared_bitrate == 128000, 영상 is None
    """
    init = sources["plain"].init

    assert (init.audio.declared_bitrate, init.video.declared_bitrate) == (128_000, None)


def test_real_shaped_hls_has_two_moofs_per_segment_and_no_declared_bitrate(sources):
    """real의 세그먼트에는 moof가 둘씩 있고, 초기화 세그먼트에는 오디오 비트레이트가 적혀 있지 않아야 한다.

    plain의 세그먼트를 둘씩 묶고 esds의 비트레이트를 0으로 고친 입력
    -> 세그먼트 셋, 세그먼트마다 moof 2개 · 영상 60프레임, declared_bitrate is None,
       #EXTINF가 모두 2.001333이고 세그먼트마다 PROGRAM-DATE-TIME이 있다
    """
    source = sources["real"]
    parsed = [
        parse_media_segment(source.files[name], source.init) for name in source.playlist.segments
    ]

    assert [(s.fragments, len(s.video.decode_times)) for s in parsed] == [(2, 60)] * 3
    assert source.init.audio.declared_bitrate is None
    assert source.playlist.durations == (REAL_EXTINF,) * 3
    assert all(time is not None for time in source.playlist.program_times)


# ================================================================ 끝까지 경로


def test_section_in_the_middle_makes_a_file_that_passes_every_check(host, sources, tmp_path):
    """세그먼트 여럿에 걸친 구간을 받으면 `_1` 파일이 생기고 판정 다섯 항목을 통과해야 한다.

    plain, 구간 프레임 40~100 (둘째 ~ 넷째 세그먼트 — 짧은 GOP 30~36을 지난다)
    -> 완료 1회, 실패 0건, "구간 시험 144p_1.mp4", 받은 세그먼트 0~3, check.ok
    """
    source = sources["plain"]

    run = _Run(host, tmp_path, [source.selection(40, 100)]).start()

    assert (run.finished, run.failures) == (1, [])
    assert run.listing() == ["구간 시험 144p_1.mp4"]
    section = run.engine.sections[0]
    assert (section.first_segment, section.last_segment) == (0, 3)
    assert (section.first_pts, section.last_pts) == (source.time_of(40), source.time_of(100))
    check = run.checks()[0]
    assert check.ok, check.notes


@pytest.mark.parametrize(
    ("first", "last", "segments"),
    [
        (40, 100, (0, 3)),  # 가운데 — 앞 세그먼트 하나를 더 받는다
        (5, 20, (0, 0)),  # 첫 세그먼트 안
        (
            150,
            179,
            (3, 5),
        ),  # 마지막 프레임까지 — 앞 키프레임의 DTS를 덮는 오디오가 한 세그먼트 더 앞에 있다
        (60, 80, (1, 2)),  # 세그먼트 경계의 키프레임에서 시작 — 앞 키프레임은 앞 세그먼트에 있다
        (29, 40, (0, 1)),  # 키프레임의 한 프레임 앞에서 시작
    ],
    ids=["middle", "first-segment", "to-last-frame", "starts-on-boundary", "one-before-keyframe"],
)
def test_section_file_equals_the_cut_from_all_segments(
    host, sources, tmp_path, first, last, segments
):
    """받은 세그먼트만으로 만든 구간 파일의 영상 패킷은 세그먼트 전부를 이은 파일에서 자른 것과 같고 오디오 패킷은 원본의 것이어야 한다.

    plain, 주석의 경우마다 구간 프레임 first~last
    -> 받은 세그먼트 범위 == segments, 영상 패킷이 전부 이은 파일에서 자른 것과 같다
       (to-last-frame은 구간 전체가 마지막 GOP라 원본의 패킷 그대로다),
       오디오 패킷이 원본에서 구간에 걸친 덩어리들과 같다, check.ok
    """
    source = sources["plain"]

    run = _Run(host, tmp_path, [source.selection(first, last)]).start()

    assert run.failures == []
    section = run.engine.sections[0]
    assert (section.first_segment, section.last_segment) == segments
    _assert_section_matches(
        source, run.paths[0], first, last, tmp_path, original_tail=last == 179
    )  # 179는 마지막 세그먼트의 마지막 프레임이다
    check = run.checks()[0]
    assert check.ok, check.notes


def test_section_ending_at_the_video_length_ends_on_the_last_frame(host, sources, tmp_path):
    """끝이 영상 길이와 같은 구간은 마지막 프레임에서 끝나야 한다.

    plain, 구간 = 프레임 150의 시각 ~ 영상 길이(마지막 프레임 179가 끝나는 시각)
    -> 끝 프레임의 PTS == 프레임 179의 PTS, 받은 세그먼트 3~5,
       영상 패킷이 원본의 프레임 150~179의 것 그대로이고 PTS가 전부 이은 파일에서 자른 것과 같다,
       오디오 패킷이 원본에서 구간에 걸친 덩어리들과 같다
    """
    source = sources["plain"]
    length = source.time_of(179) + source.frames.frame_duration
    selection = TimeRange(source.time_of(150), length)

    run = _Run(host, tmp_path, [selection]).start()

    assert run.failures == []
    section = run.engine.sections[0]
    assert section.last_pts == source.time_of(179)
    assert (section.first_segment, section.last_segment) == (3, 5)
    _assert_section_matches(source, run.paths[0], 150, 179, tmp_path, original_tail=True)


def test_section_past_the_real_video_length_is_out_of_range(host, sources, tmp_path):
    """구간의 끝이 실제 영상 길이를 넘으면 #EXTINF의 합 안이어도 범위 위반으로 실패해야 한다.

    padded.m3u8(#EXTINF의 합 7.5초, 실제 길이는 프레임 179가 끝나는 시각 약 6.02초), 구간 = 프레임 150의 시각 ~ 7.0초
    -> 실패 1건(SelectionError, SELECTION_OUT_OF_RANGE), 구간 파일 없음
    """
    source = sources["real"]
    selection = TimeRange(source.time_of(150), 7.0)

    run = _Run(host, tmp_path, [selection], "real/padded.m3u8").start()

    assert len(run.failures) == 1
    assert isinstance(run.failures[0], SelectionError)
    assert run.failures[0].message_key == SELECTION_OUT_OF_RANGE
    assert run.listing() == []


@pytest.mark.parametrize("playlist", ["real/media.m3u8", "real/nopdt.m3u8"], ids=["pdt", "no-pdt"])
def test_real_shaped_replay_section_equals_the_cut_from_all_segments(
    host, sources, tmp_path, playlist
):
    """실제 다시보기 모양의 입력(moof 둘 · emsg · 긴 #EXTINF · 비트레이트 0)에서도 구간 파일의 영상은 전부 이은 파일에서 자른 것과 같고 오디오는 원본의 것이어야 한다.

    real · nopdt, 범위 요청에 잘린 본문을 주고 캐시하는 호스트, 구간 프레임 100~170
    (둘 다 세그먼트의 둘째 moof에 든 프레임이다)
    -> 실패 0건, Range 머리가 든 요청 0건, 캐시에 잘린 본문 없음, 첫·끝 프레임의 PTS가 프레임 100 · 170의 것,
       영상 패킷이 대조 파일과 같다, 오디오 패킷이 원본의 것이다, check.ok
    """
    source = sources["real"]
    host.truncating_cache = True

    run = _Run(host, tmp_path, [source.selection(100, 170)], playlist).start()

    assert run.failures == []
    assert [header for _m, _n, header in host.requests if header is not None] == []
    assert host.truncated() == []
    section = run.engine.sections[0]
    assert (section.first_pts, section.last_pts) == (source.time_of(100), source.time_of(170))
    _assert_section_matches(source, run.paths[0], 100, 170, tmp_path)
    check = run.checks()[0]
    assert check.ok, check.notes


def test_section_of_uneven_input_passes_every_check(host, sources, tmp_path):
    """프레임 간격이 고르지 않은 입력에서도 구간 파일의 영상 패킷은 전부 이은 파일에서 자른 것과 같고 판정을 통과해야 한다.

    uneven, 구간 프레임 33~100
    -> 영상 패킷이 전부 이은 파일에서 자른 것과 같다, 오디오 패킷이 원본의 것이다, check.ok
    """
    source = sources["uneven"]

    run = _Run(host, tmp_path, [source.selection(33, 100)], "uneven/media.m3u8").start()

    assert run.failures == []
    _assert_section_matches(source, run.paths[0], 33, 100, tmp_path)
    check = run.checks()[0]
    assert check.ok, check.notes


def test_sections_sharing_segments_download_each_segment_once(host, sources, tmp_path):
    """두 구간이 같은 세그먼트를 쓰면 그 세그먼트는 한 번만 받고, 구간마다 파일이 하나씩 생겨야 한다.

    plain, 구간 프레임 5~130(세그먼트 0~4) · 35~170(세그먼트 0~5) — 세그먼트 2 · 3은 두 구간이 함께 쓰고,
    어느 구간의 끝에도 놓이지 않아 구간을 정할 때 미리 받지 않는다
    -> 파일 `_1` · `_2`, 세그먼트 요청은 0~5번이 하나씩 6건, 구간마다 check.ok
    """
    source = sources["plain"]

    run = _Run(host, tmp_path, [source.selection(5, 130), source.selection(35, 170)]).start()

    assert (run.finished, run.failures) == (1, [])
    assert run.listing() == ["구간 시험 144p_1.mp4", "구간 시험 144p_2.mp4"]
    sections = run.engine.sections
    assert [(s.first_segment, s.last_segment) for s in sections] == [(0, 4), (0, 5)]
    assert sorted(_requests_for(host, ".m4s", ranged=False)) == [
        f"plain/seg-00{n}.m4s" for n in range(6)
    ]
    for check in run.checks():
        assert check.ok, check.notes


# ================================================================ 요청 수


def test_engine_requests_playlist_init_and_each_segment_once(host, sources, tmp_path):
    """넘겨받은 것이 없으면 엔진은 플레이리스트·초기화 세그먼트·세그먼트를 한 번씩만, 범위 머리 없이 요청해야 한다.

    plain, 구간 프레임 40~100 (세그먼트 0~3)
    -> 플레이리스트 1건, 초기화 세그먼트 1건, 세그먼트 요청은 0~3번과 길이를 재는 5번이 하나씩,
       Range 머리가 든 요청 0건
    """
    source = sources["plain"]

    run = _Run(host, tmp_path, [source.selection(40, 100)]).start()

    assert run.failures == []
    assert _requests_for(host, "media.m3u8", ranged=False) == ["plain/media.m3u8"]
    assert _requests_for(host, "init.mp4", ranged=False) == ["plain/init.mp4"]
    assert sorted(_requests_for(host, ".m4s", ranged=False)) == [
        f"plain/seg-00{n}.m4s" for n in (0, 1, 2, 3, 5)
    ]
    assert [header for _m, _n, header in host.requests if header is not None] == []


def _resolve(host, tmp_path, selection, playlist: str = "plain/media.m3u8"):
    """헤드리스처럼 구간을 먼저 해석한다 — 받은 세그먼트는 tmp_path/segments에 둔다. (head, 구간 계획)"""
    url = host.url(playlist)
    head = fetch_fmp4_head(url, str(tmp_path / "segments"))
    sections = plan_fmp4_sections(
        head.playlist, head.init, [selection], lambda i: segment_frames(head, url, i)
    )
    return head, sections


def test_engine_requests_nothing_that_was_handed_in(host, sources, tmp_path):
    """구간을 해석하며 받은 것(Content.fmp4_head)을 넘기면 엔진은 플레이리스트·초기화 세그먼트와 이미 받아 둔 세그먼트를 요청하지 않아야 한다.

    plain, 구간 프레임 40~100(세그먼트 0~3). 먼저 해석한 뒤(받은 세그먼트는 폴더에 둠) 엔진에 넘김
    -> 해석 단계: 플레이리스트 1 · 초기화 세그먼트 1 · 세그먼트마다 1건 이하.
       엔진 단계: 플레이리스트 · 초기화 세그먼트 0건, 세그먼트는 해석이 받아 두지 않은 것만.
       해석과 엔진을 통틀어 같은 세그먼트 요청이 두 번 없다. 구간 파일의 패킷은 대조 파일과 같다
    """
    source = sources["plain"]
    selection = source.selection(40, 100)
    head, _sections = _resolve(host, tmp_path, selection)
    resolved = [name for _m, name, _h in host.requests]
    run = _Run(host, tmp_path, [selection])
    run.data.content.fmp4_head = head
    host.forget()

    run.start()

    stored = {f"plain/seg-00{n}.m4s" for n in head.stored}
    assert resolved[:2] == ["plain/media.m3u8", "plain/init.mp4"]
    assert sorted(resolved[2:]) == sorted(stored)  # 세그먼트마다 한 번
    assert {"plain/seg-000.m4s", "plain/seg-003.m4s"} <= stored  # 구간의 양 끝은 해석이 받았다
    assert run.failures == []
    assert _requests_for(host, "media.m3u8", ranged=False) == []
    assert _requests_for(host, "init.mp4", ranged=False) == []
    wanted = {f"plain/seg-00{n}.m4s" for n in range(4)}
    assert sorted(_requests_for(host, ".m4s", ranged=False)) == sorted(wanted - stored)
    _assert_section_matches(source, run.paths[0], 40, 100, tmp_path)
    assert not os.path.exists(head.segment_dir)  # 받아 둔 폴더가 엔진의 임시 폴더다 — 끝나면 지운다


def _last_mdat_offset(data: bytes) -> int:
    """세그먼트의 마지막 mdat 상자가 시작하는 위치(바이트) — 최상위 상자의 머리를 따라가 찾는다."""
    offset = found = 0
    while offset < len(data):
        size = int.from_bytes(data[offset : offset + 4], "big")
        if data[offset + 4 : offset + 8] == b"mdat":
            found = offset
        offset += size
    return found


@pytest.mark.parametrize("cut", ["inside-mdat", "after-moof"])
def test_engine_downloads_a_handed_in_segment_again_when_its_file_is_damaged(
    host, sources, tmp_path, cut
):
    """구간을 해석하며 받아 둔 세그먼트 파일이 온전하지 않으면 엔진은 그 세그먼트를 다시 받아야 한다.

    plain, 구간 프레임 40~100. 해석 뒤 받아 둔 첫 세그먼트 파일을 자름 —
    inside-mdat: 끝 100바이트를 뺌 / after-moof: 마지막 mdat가 시작하는 자리에서 끊음(moof로 끝난다)
    -> 엔진 단계의 세그먼트 요청에 첫 세그먼트가 있다, 실패 0건, 패킷이 대조 파일과 같다
    """
    source = sources["plain"]
    selection = source.selection(40, 100)
    head, _sections = _resolve(host, tmp_path, selection)
    kept = os.path.join(head.segment_dir, segment_file_name(SEGMENTS, 0))
    whole = source.files["seg-000.m4s"]
    # after-moof는 상자 크기의 합이 파일 크기와 같다 — 크기만 견주는 검사로는 가려지지 않는다
    length = len(whole) - 100 if cut == "inside-mdat" else _last_mdat_offset(whole)
    assert 0 < length < len(whole)
    with open(kept, "rb+") as f:
        f.truncate(length)
    run = _Run(host, tmp_path, [selection])
    run.data.content.fmp4_head = head
    host.forget()

    run.start()

    assert run.failures == []
    assert "plain/seg-000.m4s" in _requests_for(host, ".m4s", ranged=False)
    _assert_section_matches(source, run.paths[0], 40, 100, tmp_path)


def test_section_download_sends_no_range_request_to_a_truncating_cache(host, sources, tmp_path):
    """범위 요청에 잘린 본문을 주고 그것을 캐시하는 서버에서 구간 다운로드는 범위 요청을 보내지 않고 온전한 구간 파일을 만들어야 한다.

    plain, truncating_cache를 켠 호스트, 구간 프레임 40~100
    -> 실패 0건, Range 머리가 든 요청 0건, 캐시에 잘린 본문 없음, 패킷이 대조 파일과 같다
    """
    source = sources["plain"]
    host.truncating_cache = True

    run = _Run(host, tmp_path, [source.selection(40, 100)]).start()

    assert run.failures == []
    assert [header for _m, _n, header in host.requests if header is not None] == []
    assert host.truncated() == []
    _assert_section_matches(source, run.paths[0], 40, 100, tmp_path)


def test_section_download_fails_instead_of_cutting_a_truncated_segment(host, sources, tmp_path):
    """받은 세그먼트가 잘려 있으면 구간 다운로드는 컷을 하지 않고 잘림 오류로 실패해야 한다.

    plain, truncating_cache를 켠 호스트에서 셋째 세그먼트를 누군가 범위 요청으로 먼저 받아 캐시에
    잘린 본문이 들어 있다(전체 요청에도 잘린 본문이 온다). 구간 프레임 40~100(세그먼트 0~3)
    -> 완료 0회, 실패 1건(TruncatedSegmentError), 컷 0회, 저장 폴더가 비어 있다
    """
    source = sources["plain"]
    host.truncating_cache = True
    host.session().get(host.url("plain/seg-002.m4s"), headers={"Range": "bytes=0-999"})
    assert host.truncated() == ["plain/seg-002.m4s"]

    run = _Run(host, tmp_path, [source.selection(40, 100)]).start()

    assert run.finished == 0
    assert len(run.failures) == 1
    assert isinstance(run.failures[0], TruncatedSegmentError)
    assert run.engine.cut_results == []
    assert run.listing() == []


# ================================================================ 끊긴 녹화


def test_section_crossing_a_discontinuity_is_rejected(host, sources, tmp_path):
    """구간이 #EXT-X-DISCONTINUITY를 넘으면 구간 위반 키로 실패해야 한다.

    broken.m3u8(넷째 세그먼트 앞에서 끊김), 구간 프레임 70~100 (셋째 ~ 넷째 세그먼트)
    -> 실패 1건(SelectionError, message_key == SELECTION_CROSSES_BREAK), 저장 폴더가 비어 있다
    """
    source = sources["plain"]

    run = _Run(host, tmp_path, [source.selection(70, 100)], "plain/broken.m3u8").start()

    assert run.finished == 0
    assert len(run.failures) == 1
    assert isinstance(run.failures[0], SelectionError)
    assert run.failures[0].message_key == SELECTION_CROSSES_BREAK
    assert run.listing() == []


def test_section_beside_a_discontinuity_is_downloaded(host, sources, tmp_path):
    """구간이 #EXT-X-DISCONTINUITY를 넘지 않으면 끊긴 자리 바로 앞에서도 받아야 한다.

    broken.m3u8(넷째 세그먼트 앞에서 끊김), 구간 프레임 40~80 (둘째 ~ 셋째 세그먼트)
    -> 실패 0건, 영상·오디오 패킷이 전부 이은 파일에서 자른 것과 같다
    """
    source = sources["plain"]

    run = _Run(host, tmp_path, [source.selection(40, 80)], "plain/broken.m3u8").start()

    assert run.failures == []
    _assert_section_matches(source, run.paths[0], 40, 80, tmp_path)


@pytest.mark.parametrize("to_end", [False, True], ids=["across-the-break", "to-the-video-length"])
def test_section_crossing_a_discontinuity_is_rejected_before_reading_its_segments(
    host, sources, tmp_path, to_end
):
    """끊긴 자리를 넘는 구간은 그 구간의 세그먼트를 받기 전에 거부해야 한다.

    across-the-break: broken.m3u8(세그먼트 0~2 · 끊김 · 3~5), 구간 프레임 70~100
    to-the-video-length: twice.m3u8(세그먼트 여섯 · 끊김 · 같은 여섯), 구간 = 프레임 70의 시각 ~ 영상 길이
    -> 실패 1건(SELECTION_CROSSES_BREAK), 받은 세그먼트는 묶음마다의 첫 · 마지막 세그먼트뿐
       (시각 축을 재는 데 쓴다)
    """
    source = sources["plain"]
    length = source.time_of(179) + source.frames.frame_duration  # 묶음 하나(세그먼트 여섯)의 길이
    if to_end:
        selection = TimeRange(source.time_of(70), 2 * length)
        playlist, read = "plain/twice.m3u8", [0, 0, 5, 5]
    else:
        selection, playlist, read = source.selection(70, 100), "plain/broken.m3u8", [0, 2, 3, 5]

    run = _Run(host, tmp_path, [selection], playlist).start()

    assert len(run.failures) == 1
    assert isinstance(run.failures[0], SelectionError)
    assert run.failures[0].message_key == SELECTION_CROSSES_BREAK
    assert sorted(_requests_for(host, ".m4s", ranged=False)) == [
        f"plain/seg-00{n}.m4s" for n in read
    ]


def test_section_after_a_discontinuity_counts_time_from_the_break(host, sources, tmp_path):
    """끊긴 자리 뒤의 구간은 앞 묶음이 끝난 시각에서 이어 세어 프레임을 골라야 한다.

    twice.m3u8(세그먼트 여섯 · 끊김 · 같은 여섯 — 뒤쪽 타임스탬프가 0으로 돌아간다),
    구간 = 앞 묶음의 실제 길이(프레임 179가 끝나는 시각) + 프레임 40~100의 시각
    -> 받은 세그먼트 6~9, 영상·오디오 패킷이 앞쪽에서 프레임 40~100을 자른 것과 같다
    """
    source = sources["plain"]
    half = source.time_of(179) + source.frames.frame_duration  # 앞 묶음이 끝나는 VOD 시각
    selection = TimeRange(half + source.time_of(40), half + source.time_of(100))

    run = _Run(host, tmp_path, [selection], "plain/twice.m3u8").start()

    assert run.failures == []
    section = run.engine.sections[0]
    assert (section.first_segment, section.last_segment) == (6, 9)
    assert section.first_pts == pytest.approx(half + source.time_of(40))
    _assert_section_matches(source, run.paths[0], 40, 100, tmp_path)


def test_section_after_a_discontinuity_ignores_timestamps_before_the_break(host, sources, tmp_path):
    """끊긴 자리 뒤의 구간은 플레이리스트 첫 세그먼트의 타임스탬프가 아니라 끊긴 자리의 첫 세그먼트를 기준으로 프레임을 골라야 한다.

    late.m3u8(세그먼트 넷째 ~ 여섯째 · 끊김 · 세그먼트 여섯 — 앞쪽 타임스탬프는 3초부터, 뒤쪽은 0부터),
    구간 = 앞 묶음의 실제 길이 + 프레임 40~100의 시각
    -> 받은 세그먼트 3~6, 영상·오디오 패킷이 plain에서 프레임 40~100을 자른 것과 같다
    """
    source = sources["plain"]
    fourth = parse_media_segment(source.files[source.playlist.segments[3]], source.init)
    first = parse_media_segment(source.files[source.playlist.segments[0]], source.init)
    began = float(fmp4_origin(source.init, fourth) - fmp4_origin(source.init, first))
    # 앞 묶음(넷째 ~ 여섯째 세그먼트)의 실제 길이 — 마지막 프레임이 끝나는 시각 − 가장 이른 PTS
    before = source.time_of(179) + source.frames.frame_duration - began
    selection = TimeRange(before + source.time_of(40), before + source.time_of(100))

    run = _Run(host, tmp_path, [selection], "plain/late.m3u8").start()

    assert run.failures == []
    section = run.engine.sections[0]
    assert (section.first_segment, section.last_segment) == (3, 6)
    assert section.first_pts == pytest.approx(before + source.time_of(40))
    _assert_section_matches(source, run.paths[0], 40, 100, tmp_path)


# ================================================================ 임시 파일 · 실패


def test_temp_folder_is_removed_after_success(host, sources, tmp_path):
    """구간을 모두 만들면 받은 세그먼트와 이은 파일이 든 임시 폴더가 남지 않아야 한다.

    plain, 구간 프레임 40~100
    -> 저장 폴더에 구간 파일 하나뿐
    """
    run = _Run(host, tmp_path, [sources["plain"].selection(40, 100)]).start()

    assert not os.path.exists(temp_dir_for(run.paths[0]))
    assert run.listing() == ["구간 시험 144p_1.mp4"]


def test_cut_failure_keeps_segments_and_finished_sections(host, sources, tmp_path, monkeypatch):
    """둘째 구간의 컷이 실패하면 다운로드는 실패하고, 임시 폴더와 먼저 만든 구간 파일은 남아야 한다.

    plain, 구간 둘, 둘째 구간의 컷이 CutError를 내도록 바꿈
    -> 완료 0회, 실패 1건(PostprocessError, 원인 CutError), `_1` 파일과 임시 폴더가 남고
       임시 폴더에 초기화 세그먼트 · 세그먼트만 있다(구간의 작업 폴더는 없다)
    """
    source = sources["plain"]
    real = m3u8_module.cut_fmp4_section

    def second_fails(*args, **kwargs):
        if args[OUTPUT_ARG].endswith("_2.mp4"):
            raise CutError(CUT_FAILED, "시험")
        return real(*args, **kwargs)

    monkeypatch.setattr(m3u8_module, "cut_fmp4_section", second_fails)

    run = _Run(host, tmp_path, [source.selection(5, 20), source.selection(40, 70)]).start()

    assert run.finished == 0
    assert len(run.failures) == 1
    assert isinstance(run.failures[0], PostprocessError)
    assert isinstance(run.failures[0].__cause__, CutError)
    assert run.listing() == ["CVDv2_temp_구간 시험 144p_1", "구간 시험 144p_1.mp4"]
    kept = sorted(os.listdir(temp_dir_for(run.paths[0])))
    assert kept == ["0.m4s", "1.m4v", "2.m4v", "3.m4v"]


def test_cut_failure_of_one_section_still_cuts_the_sections_after_it(
    host, sources, tmp_path, monkeypatch
):
    """구간 셋 가운데 둘째의 컷이 실패해도 셋째는 잘라야 하고, 다운로드는 그 뒤에 한 번 실패해야 한다.

    plain, 구간 프레임 5~20 · 40~70 · 100~130, 둘째 구간의 컷이 첫 조각의 ffmpeg 실행 자리에서
    출력 파일을 반쯤 쓰고 CutError를 냄
    -> 컷 호출 3회, 완료 0회, 실패 1건(PostprocessError, 원인 CutError),
       저장 폴더에 `_1` · `_3` 파일과 임시 폴더뿐(`_2` 파일 없음),
       임시 폴더에 구간의 작업 폴더(section_*) 없음,
       공유 데이터의 구간 상태 == (전체 3, 완료 2, 실패 1)
    """
    source = sources["plain"]
    real = m3u8_module.cut_fmp4_section
    failure = MidwayCutFailure(monkeypatch)
    calls = []

    def second_fails(*args, **kwargs):
        calls.append(args)
        if args[OUTPUT_ARG].endswith("_2.mp4"):
            failure.arm(source_path=args[0], output_path=args[OUTPUT_ARG])  # args[0]은 작업 폴더
        return real(*args, **kwargs)

    monkeypatch.setattr(m3u8_module, "cut_fmp4_section", second_fails)
    selections = [source.selection(5, 20), source.selection(40, 70), source.selection(100, 130)]

    run = _Run(host, tmp_path, selections).start()

    assert len(calls) == 3
    assert run.finished == 0
    assert len(run.failures) == 1
    assert isinstance(run.failures[0], PostprocessError)
    assert isinstance(run.failures[0].__cause__, CutError)
    # 실패시킨 순간에는 있었다 — 아래의 "없다"가 정리의 결과임을 먼저 확인한다
    assert (failure.fired, failure.source_existed, failure.work_dir_existed) == (1, True, True)
    assert os.path.basename(failure.source_path) == "section_2"
    assert os.path.basename(failure.output_path) == "구간 시험 144p_2.mp4"
    assert not os.path.exists(failure.source_path)
    assert not os.path.exists(failure.output_path)
    assert not os.path.exists(failure.work_dir)
    assert run.listing() == [
        "CVDv2_temp_구간 시험 144p_1",
        "구간 시험 144p_1.mp4",
        "구간 시험 144p_3.mp4",
    ]
    temp_names = os.listdir(run.engine.temp_dir)
    assert temp_names  # 받은 세그먼트는 남는다
    assert [name for name in temp_names if name.startswith("section_")] == []
    data = run.data
    assert (data.sections_total, data.sections_done, data.sections_failed) == (3, 2, 1)


def test_section_outside_the_playlist_fails_with_selection_key(host, tmp_path):
    """구간이 영상 길이를 넘으면 구간 위반 키로 실패하고 아무것도 남기지 않아야 한다.

    plain(6초), 구간 5~7초
    -> 실패 1건(SelectionError, message_key == SELECTION_OUT_OF_RANGE), 저장 폴더가 비어 있다
    """
    run = _Run(host, tmp_path, [TimeRange(5.0, 7.0)]).start()

    assert len(run.failures) == 1
    assert isinstance(run.failures[0], SelectionError)
    assert run.failures[0].message_key == SELECTION_OUT_OF_RANGE
    assert run.listing() == []


def test_section_download_checks_the_free_space_before_the_transfer(
    host, sources, tmp_path, monkeypatch
):
    """구간 다운로드도 선언된 비트레이트로 어림한 크기가 여유 공간에 안 들어가면 받기 전에 실패해야 한다.

    plain, 구간 프레임 40~100(세그먼트 0~3), 선언 800,000 bit/s(100,000바이트/초), 여유 0
    -> 실패 1건(DiskSpaceError, 키 "Not enough disk space" — 구간 다운로드의 키), 완료 0회,
       구간 파일 없음, 임시 폴더 없음.
       시작 줄의 세그먼트 어림 = 받아 둔 세그먼트는 실제 크기 + 나머지는 100,000 x #EXTINF,
       산출물 어림 = 세그먼트 어림 x (구간 길이 / 세그먼트 0~3의 #EXTINF 합)
    """
    source = sources["plain"]
    run = _Run(host, tmp_path, [source.selection(40, 100)])
    run.data.content.stream = StreamKey(256, 144, 30.0, 800_000)
    monkeypatch.setattr(space_module, "same_volume", lambda a, b: True)
    monkeypatch.setattr(space_module, "free_bytes", lambda folder: 0)

    run.start()

    assert ([type(e) for e in run.failures], run.finished) == ([DiskSpaceError], 0)
    assert run.failures[0].message_key == DISK_SPACE_SHORT
    assert run.listing() == []
    assert not os.path.exists(run.engine.temp_dir)
    on_disk = run.engine._prefetched  # 구간을 정하며 받아 둔 세그먼트 — 인덱스 → 크기
    durations = source.playlist.durations[:4]
    segments = sum(on_disk.get(index, 100_000 * durations[index]) for index in range(4))
    covered = (source.time_of(100) - source.time_of(40)) / sum(durations)
    stage, logged_segments, logged_output, checks = dict(run.logger.calls)["log_space_check"]
    assert stage == "start"
    assert logged_segments == pytest.approx(segments, abs=1)
    assert logged_output == pytest.approx(segments * covered, abs=1)
    assert 0 < covered < 1
    assert [(where, free) for where, _needed, free in checks] == [("both", 0)]


def test_section_download_does_not_start_when_a_section_file_is_over_the_fat_limit(
    host, sources, tmp_path, monkeypatch
):
    """구간 다운로드는 저장 폴더가 FAT 계열이고 가장 큰 구간 파일의 어림이 한계를 넘으면 받기 전에 실패해야 한다.

    plain, 구간 둘(프레임 5~20 · 40~100), 선언 800,000 bit/s, 파일 시스템 "FAT32", 한계 1,000바이트,
    여유 공간 넉넉함
    -> 실패 1건(FileSizeLimitError), 완료 0회, 구간 파일 없음.
       가장 큰 파일의 어림은 산출물 어림의 합보다 작다(구간 하나의 것이다) — 파일 크기 줄과
       여유 공간 줄을 견준다. 한계를 그 어림과 같게 두면 받는다
    """
    source = sources["plain"]
    selections = [source.selection(5, 20), source.selection(40, 100)]
    monkeypatch.setattr(space_module, "same_volume", lambda a, b: True)
    monkeypatch.setattr(space_module, "free_bytes", lambda folder: 10**13)
    monkeypatch.setattr(base_module, "filesystem_name", lambda folder: "FAT32")
    for name in ("a", "b"):
        (tmp_path / name).mkdir()

    monkeypatch.setattr(base_module, "FAT_MAX_FILE_BYTES", 1000)
    refused = _Run(host, tmp_path / "a", selections)
    refused.data.content.stream = StreamKey(256, 144, 30.0, 800_000)
    refused.start()
    (error,) = refused.failures
    monkeypatch.setattr(base_module, "FAT_MAX_FILE_BYTES", error.largest)
    allowed = _Run(host, tmp_path / "b", selections)
    allowed.data.content.stream = StreamKey(256, 144, 30.0, 800_000)
    allowed.start()

    assert isinstance(error, FileSizeLimitError)
    assert (refused.finished, refused.listing()) == (0, [])
    _stage, _segments, output, _checks = dict(allowed.logger.calls)["log_space_check"]
    assert 1000 < error.largest < output
    assert (allowed.finished, allowed.failures) == (1, [])


def test_section_log_reports_cut_and_total_size_of_section_files(host, sources, tmp_path):
    """구간 다운로드의 후처리 로그는 종류 "cut"으로 시작하고, 끝 로그의 크기는 구간 파일 크기의 합이어야 한다.

    plain, 구간 둘
    -> log_postprocess_start("cut") 1회, log_postprocess_complete의 크기 == 두 파일 크기의 합
    """
    source = sources["plain"]

    run = _Run(host, tmp_path, [source.selection(5, 20), source.selection(40, 70)]).start()

    calls = dict(run.logger.calls)
    assert calls["log_postprocess_start"] == ("cut",)
    assert calls["log_postprocess_complete"][1] == sum(os.path.getsize(p) for p in run.paths)


def test_finished_section_run_reports_the_saved_size_as_the_sum_of_its_section_files(
    host, sources, tmp_path
):
    """구간을 모두 만든 fMP4 실행은 저장된 크기로 구간 파일 크기의 합을 적어야 한다 — 받은 양이 아니다.

    plain, 구간 둘 -> 완료 1회, output_size == 두 파일 크기의 합 == 로그의 Output size,
    받은 양(total_downloaded_size)과 다르다
    """
    source = sources["plain"]
    run = _Run(host, tmp_path, [source.selection(5, 20), source.selection(40, 70)])
    assert run.data.output_size is None

    run.start()

    assert (run.finished, run.failures) == (1, [])
    expected = sum(os.path.getsize(p) for p in run.paths)
    assert run.data.output_size == expected == dict(run.logger.calls)["log_postprocess_complete"][1]
    assert expected not in (0, run.data.total_downloaded_size)


@pytest.mark.parametrize("how", ["failure", "stop"])
def test_section_run_that_fails_or_stops_leaves_the_file_at_output_path_alone(
    host, sources, tmp_path, monkeypatch, how
):
    """구간 다운로드가 실패하거나 중단돼도 output_path 자리에 있던 파일은 그대로 남아야 한다.

    plain, 구간 프레임 40~100, output_path 자리에 내용이 b"keep"인 파일을 미리 둠.
    failure: 컷이 RuntimeError를 냄 / stop: 컷 도중 model.stop()
    -> 저장 폴더에 그 파일 하나뿐이고 내용이 b"keep"
    """
    run = _Run(host, tmp_path, [sources["plain"].selection(40, 100)])
    with open(run.data.output_path, "wb") as f:
        f.write(b"keep")
    real = m3u8_module.cut_fmp4_section

    def interrupted(*args, **kwargs):
        if how == "failure":
            raise RuntimeError("시험")
        run.data.model.stop()
        return real(*args, **kwargs)

    monkeypatch.setattr(m3u8_module, "cut_fmp4_section", interrupted)

    run.start()

    assert run.finished == 0
    assert len(run.failures) == (1 if how == "failure" else 0)
    assert run.listing() == ["unused.mp4"]
    with open(run.data.output_path, "rb") as f:
        assert f.read() == b"keep"


def test_engine_validates_sections_with_the_frame_rate_handed_in(host, sources, tmp_path):
    """구간을 해석한 쪽이 정한 프레임률(Fmp4Head.frame_rate)을 넘기면 엔진은 그 값으로 구간을 검증해야 한다.

    plain(30fps), 구간 1.1 ~ 1.3초. 넘긴 프레임률 1(프레임 번호 1 ~ 1) · 넘기지 않음(프레임 번호 33 ~ 39)
    -> 1을 넘기면 실패 1건(SELECTION_TOO_SHORT), 넘기지 않으면 실패 0건
    """
    selection = TimeRange(1.1, 1.3)
    head = fetch_fmp4_head(host.url("plain/media.m3u8"))
    head.frame_rate = Fraction(1)
    handed = _Run(host, tmp_path, [selection])
    handed.data.content.fmp4_head = head

    handed.start()
    plain = _Run(host, tmp_path, [selection]).start()

    assert len(handed.failures) == 1
    assert handed.failures[0].message_key == SELECTION_TOO_SHORT
    assert plain.failures == []


def test_section_postprocess_reads_segments_in_fixed_size_chunks(
    host, sources, tmp_path, monkeypatch
):
    """구간 후처리는 받은 세그먼트를 통째로 읽지 않고 정해진 크기씩 읽어야 한다.

    plain, 구간 프레임 40~100 (세그먼트 0~3), 읽는 크기를 1,000바이트로 낮추고 세그먼트 파일의 read를 기록
    (오디오 통과 · 경계 조각의 입력 만들기 · 구간 파일로 넘기기가 모두 이 읽기를 쓴다)
    -> 세그먼트 파일에서 한 번에 읽은 가장 큰 크기가 1,000바이트 이하, 구간 파일이 대조와 맞는다
    """
    source = sources["plain"]
    monkeypatch.setattr(fmp4_cut_module, "_COPY_CHUNK_BYTES", 1000)
    largest = []
    real_open = open

    class Recording:
        """세그먼트 파일의 read가 돌려준 길이를 적는 대역."""

        def __init__(self, file):
            self._file = file

        def read(self, size=-1):
            data = self._file.read(size)
            largest.append(len(data))
            return data

        def seek(self, *args):
            return self._file.seek(*args)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self._file.close()

    def recording_open(path, mode="r", *args, **kwargs):
        file = real_open(path, mode, *args, **kwargs)
        return Recording(file) if mode == "rb" and str(path).endswith(".m4v") else file

    # 경계 조각의 입력은 fmp4_cut이, 넘기는 읽기는 ffmpeg 모듈의 read_in_chunks가 연다
    monkeypatch.setattr(fmp4_cut_module, "open", recording_open, raising=False)
    monkeypatch.setattr(ffmpeg_module, "open", recording_open, raising=False)

    run = _Run(host, tmp_path, [source.selection(40, 100)]).start()

    assert run.failures == []
    # 세그먼트 0~3을 오디오 통과가 한 번씩, 1 · 3을 경계 조각이, 2를 넘기기가 읽는다
    assert len(largest) > sum(len(source.files[n]) for n in source.playlist.segments[:4]) // 1000
    assert max(largest) <= 1000
    assert min(len(source.files[name]) for name in source.playlist.segments[:4]) > 1000
    _assert_section_matches(source, run.paths[0], 40, 100, tmp_path)


def test_engine_keeps_handed_in_segments_and_removes_other_files_from_the_segment_folder(
    host, sources, tmp_path, monkeypatch
):
    """엔진은 넘겨받은 세그먼트 폴더에서 받아 둔 세그먼트만 남기고, 이 다운로드의 것이 아닌 파일은 지워야 한다.

    plain, 구간 프레임 40~100(세그먼트 0~3). 해석 뒤 폴더에 이전 실행의 것처럼 "9.m4v"와 "old.bin"을 넣음.
    컷이 CutError를 내게 해 폴더가 남게 함
    -> 폴더에 초기화 세그먼트 · 세그먼트 1~4(0~3번째)만 있다 — "9.m4v" · "old.bin"과,
       해석이 길이를 재려고 받았지만 이 구간에는 쓰지 않는 "6.m4v"는 없다
    """
    source = sources["plain"]
    selection = source.selection(40, 100)
    head, _sections = _resolve(host, tmp_path, selection)
    for name in ("9.m4v", "old.bin"):
        with open(os.path.join(head.segment_dir, name), "wb") as f:
            f.write(b"stale")

    def fails(*args, **kwargs):
        raise CutError(CUT_FAILED, "시험")

    monkeypatch.setattr(m3u8_module, "cut_fmp4_section", fails)
    run = _Run(host, tmp_path, [selection])
    run.data.content.fmp4_head = head

    run.start()

    assert isinstance(run.failures[0], PostprocessError)
    kept = sorted(os.listdir(head.segment_dir))
    assert kept == ["0.m4s", "1.m4v", "2.m4v", "3.m4v", "4.m4v"]


# ================================================================ 전체 다운로드 (보존)


def test_download_without_sections_keeps_the_remux_path(host, sources, tmp_path):
    """구간이 없으면 지금처럼 세그먼트 전부를 받아 output_path로 재포장해야 한다.

    plain, selections 빈 튜플
    -> 완료 1회, output_path가 있고, 세그먼트 전체 요청 6건, 후처리 종류 "remux"
    """
    run = _Run(host, tmp_path, []).start()

    assert (run.finished, run.failures) == (1, [])
    assert os.path.getsize(run.data.output_path) > 0
    assert len(_requests_for(host, ".m4s", ranged=False)) == SEGMENTS
    assert _requests_for(host, ".m4s", ranged=True) == []
    assert dict(run.logger.calls)["log_postprocess_start"] == ("remux",)


# ================================================================ 실패한 구간만 다시 처리 (#309)

THREE_FILES = ["구간 시험 144p_1.mp4", "구간 시험 144p_2.mp4", "구간 시험 144p_3.mp4"]
TEMP_FOLDER = "CVDv2_temp_구간 시험 144p_1"


def _three(source) -> list[TimeRange]:
    """구간 셋 — 프레임 5~20 · 40~130 · 135~145.

    둘째 구간은 세그먼트 넷에 걸친다 — 가운데 둘은 구간을 정할 때 받지 않고 전송 단계가 받는다.
    엔진은 긴 구간부터 만든다 — 컷이 불리는 순서는 둘째 · 첫째 · 셋째다.
    """
    return [source.selection(5, 20), source.selection(40, 130), source.selection(135, 145)]


def test_retry_cuts_only_the_failed_section_from_the_kept_segments_without_requests(
    host, sources, tmp_path, monkeypatch
):
    """둘째 구간만 실패한 실행을 이어받은 실행은 둘째만 잘라야 하고 아무것도 요청하지 않아야 한다.

    첫 실행: plain, 구간 셋, 둘째 구간의 컷이 세그먼트를 넘기기 전에 실패. 이어받은 실행: 새 엔진에 첫 실행이 남긴 것과 그때의 경로를 넘김
    -> 컷 호출 1회(`_2`), 완료 1회 · 실패 0건, 이어받은 실행 동안의 요청 0건,
       `_1` · `_3`의 수정 시각 · 내용이 그대로, 경로가 첫 실행과 같다,
       저장 폴더에 `_1` · `_2` · `_3`뿐(임시 폴더 없음), `_2`가 판정 통과,
       구간 상태 == (전체 3, 완료 3, 실패 0, 이어받음 2)
    """
    selections = _three(sources["plain"])
    cuts = CutCalls(monkeypatch, m3u8_module, "cut_fmp4_section", source_arg=0)
    cuts.fail_on = (1,)  # 첫 호출이 둘째 구간이다
    failed = _Run(host, tmp_path, selections).start()
    assert len(failed.failures) == 1
    kept = snapshot([failed.paths[0], failed.paths[2]])
    requests_before = len(host.requests)
    cuts.restart()

    again = hand_over(failed, _Run(host, tmp_path, selections)).start()

    assert (again.finished, again.failures) == (1, [])
    assert cuts.outputs == [THREE_FILES[1]]
    assert host.requests[requests_before:] == []
    assert snapshot(kept) == kept
    assert again.paths == failed.paths
    assert again.listing() == THREE_FILES
    checks = again.checks()
    assert len(checks) == 1
    assert checks[0].ok, checks[0].notes
    data = again.data
    assert (
        data.sections_total,
        data.sections_done,
        data.sections_failed,
        data.sections_resumed,
    ) == (3, 3, 0, 2)


@pytest.mark.parametrize("undeletable", [False, True], ids=["지울 수 있음", "지울 수 없음"])
def test_cut_failure_whose_output_cannot_be_removed_still_ends_as_a_partial_failure(
    host, sources, tmp_path, monkeypatch, undeletable
):
    """구간의 컷이 실패하면 그 출력을 지울 수 없어도 일부 실패로 끝나고, 이어받은 실행은 그 구간만 다시 잘라야 한다 (#389).

    plain, 구간 셋, 둘째 구간의 컷이 출력 파일을 반쯤 쓰고 CutError. 그 출력의 os.remove가
    PermissionError(대조: 지울 수 있음)
    -> 실패 1건(PostprocessError, 원인 CutError), 구간 상태 == (전체 3, 완료 2, 실패 1), 이어받기 기록이
       있다, `_1` · `_3` 파일과 임시 폴더가 남는다, 지울 수 없을 때만 `_2`가 남고 지우기 거절이 1번 이상
    -> 이어받은 실행: 컷 호출 1회(`_2`로 시작하는 이름), 완료 1회 · 실패 0건, 요청 0건
    """
    selections = _three(sources["plain"])
    blocker = Undeletable(monkeypatch)
    cuts = CutCalls(monkeypatch, m3u8_module, "cut_fmp4_section", source_arg=0)
    cuts.fail_on = (1,)  # 첫 호출이 둘째 구간이다
    failed = _Run(host, tmp_path, selections)
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
    assert os.path.isdir(failed.engine.temp_dir)
    assert os.path.exists(failed.paths[1]) == undeletable
    assert bool(blocker.refused) == undeletable

    blocker.release()
    requests_before = len(host.requests)
    cuts.restart()
    again = hand_over(failed, _Run(host, tmp_path, selections)).start()

    assert (again.finished, again.failures) == (1, [])
    assert len(cuts.outputs) == 1 and cuts.outputs[0].startswith("구간 시험 144p_2")
    assert host.requests[requests_before:] == []


def test_section_whose_output_ffmpeg_cannot_write_or_remove_fails_alone(
    host, sources, tmp_path, monkeypatch
):
    """세그먼트를 흘려 잇는 구간의 출력을 ffmpeg가 쓰지 못하고 지울 수도 없으면 그 구간만 실패해야 한다 (#389).

    plain, 구간 셋. 둘째 구간(세그먼트 넷에 걸쳐 가운데 둘을 흘린다)의 출력 경로에 폴더가 있고 그 경로의
    os.remove가 PermissionError
    -> 실패 1건(PostprocessError, 원인 CutError, 그 원인 RemuxError), 구간 상태 == (3, 2, 1), 이어받기
       기록이 있다, `_1` · `_3` 파일과 임시 폴더가 남는다, 지우기 거절이 1번 이상
    -> 폴더를 치우고 이어받은 실행: 완료 1회 · 실패 0건, 요청 == 둘째 구간의 세그먼트 가운데 기록에
       남지 않은 것(넘기면서 지운 것)뿐
    """
    selections = _three(sources["plain"])
    blocker = Undeletable(monkeypatch)
    failed = _Run(host, tmp_path, selections)
    os.makedirs(failed.paths[1])  # ffmpeg가 이 자리에 파일을 쓰지 못한다
    blocker.block(failed.paths[1])

    failed.start()

    assert len(failed.failures) == 1
    assert isinstance(failed.failures[0], PostprocessError)
    assert isinstance(failed.failures[0].__cause__, CutError)
    assert isinstance(failed.failures[0].__cause__.__cause__, RemuxError)
    data = failed.data
    assert (data.sections_total, data.sections_done, data.sections_failed) == (3, 2, 1)
    assert data.section_resume is not None
    assert os.path.isfile(failed.paths[0]) and os.path.isfile(failed.paths[2])
    assert os.path.isdir(failed.engine.temp_dir)
    assert blocker.refused

    second = failed.engine.sections[1]
    gone = sorted(
        set(range(second.first_segment, second.last_segment + 1))
        - data.section_resume.fmp4_head.stored
    )
    blocker.release()
    os.rmdir(failed.paths[1])
    requests_before = len(host.requests)
    again = hand_over(failed, _Run(host, tmp_path, selections)).start()

    assert (again.finished, again.failures) == (1, [])
    assert host.requests[requests_before:] == [
        ("GET", f"plain/seg-{index:03d}.m4s", None) for index in gone
    ]


def test_stop_during_retry_removes_only_what_the_retry_made(host, sources, tmp_path, monkeypatch):
    """이어받은 실행을 컷 사이에서 중단하면 그 실행이 만든 구간 파일만 지워야 한다.

    같은 엔진. 첫 실행: 둘째 · 셋째 구간의 컷이 도중에 실패(`_1`만 만듦). 이어받은 실행: 둘째 구간의 컷이 끝난 뒤 중단
    -> 중단한 순간 `_2`가 있었다, 컷 호출 1회(`_2`), 완료 0회, 실패는 첫 실행의 1건뿐,
       `_1`의 수정 시각 · 내용이 그대로, 저장 폴더에 임시 폴더와 `_1`뿐, 임시 폴더에 세그먼트가 남는다
    """
    cuts = CutCalls(monkeypatch, m3u8_module, "cut_fmp4_section", source_arg=0)
    cuts.fail_on = (1, 3)  # 첫 호출이 둘째 구간, 셋째 호출이 셋째 구간이다
    run = _Run(host, tmp_path, _three(sources["plain"])).start()
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
    assert run.listing() == [TEMP_FOLDER, THREE_FILES[0]]
    assert [name for name in os.listdir(run.engine.temp_dir) if name.endswith(".m4v")]


def test_retry_receives_only_the_segment_that_is_gone(host, sources, tmp_path, monkeypatch):
    """받아 둔 세그먼트 하나가 없어졌으면 이어받은 실행은 그 세그먼트만 다시 받아야 한다.

    첫 실행: plain, 구간 셋, 둘째 구간의 컷이 도중에 실패. 둘째 구간의 첫 세그먼트 파일을 지움. 이어받은 실행
    -> 완료 1회 · 실패 0건, 이어받은 실행의 요청 == 그 세그먼트 하나(범위 요청 아님),
       컷 호출 1회(`_2`), `_1` · `_3`의 수정 시각 · 내용이 그대로
    """
    source = sources["plain"]
    selections = _three(source)
    cuts = CutCalls(monkeypatch, m3u8_module, "cut_fmp4_section", source_arg=0)
    cuts.fail_on = (1,)  # 첫 호출이 둘째 구간이다
    failed = _Run(host, tmp_path, selections).start()
    assert len(failed.failures) == 1
    kept = snapshot([failed.paths[0], failed.paths[2]])
    gone = failed.engine.sections[1].first_segment
    count = len(source.playlist.segments)
    os.remove(os.path.join(failed.engine.temp_dir, segment_file_name(count, gone)))
    requests_before = len(host.requests)
    cuts.restart()

    again = hand_over(failed, _Run(host, tmp_path, selections)).start()

    assert (again.finished, again.failures) == (1, [])
    assert host.requests[requests_before:] == [
        ("GET", f"plain/{source.playlist.segments[gone]}", None)
    ]
    assert cuts.outputs == [THREE_FILES[1]]
    assert snapshot(kept) == kept
    assert again.listing() == THREE_FILES


# ================================================================ 다시 쓴 세그먼트는 받은 양이 아니다 (#376)


def _logged(run: _Run, name: str) -> list[tuple]:
    """그 로그 메서드가 불린 인자들(부른 순서)."""
    return [args for called, args in run.logger.calls if called == name]


def _speed_over_one_second(run: _Run) -> float:
    """실행이 끝난 엔진이 1초 전부터 받은 양으로 낸 속도(MB/s) — 관측 틱 하나와 같은 길을 탄다."""
    engine = run.engine
    engine.s.prev_size = 0
    engine._now = lambda: 101.0
    engine._measured_at = 100.0
    engine._measured_paused = engine.s.model.paused_seconds
    engine.emit_progress()
    engine.measure_speed()
    return engine.s.speed_mb


def _retry_with_one_segment_gone(host, sources, tmp_path, monkeypatch):
    """둘째 구간의 컷이 실패한 실행과, 그 구간의 첫 세그먼트를 지운 뒤 이어받은 실행.

    Returns:
        (첫 실행, 이어받은 실행, 다시 받은 세그먼트의 크기, 다시 쓴 세그먼트들의 크기)
    """
    source = sources["plain"]
    selections = _three(source)
    cuts = CutCalls(monkeypatch, m3u8_module, "cut_fmp4_section", source_arg=0)
    cuts.fail_on = (1,)  # 첫 호출이 둘째 구간이다
    failed = _Run(host, tmp_path, selections).start()
    assert len(failed.failures) == 1
    second = failed.engine.sections[1]
    gone = second.first_segment
    count = len(source.playlist.segments)
    os.remove(os.path.join(failed.engine.temp_dir, segment_file_name(count, gone)))
    cuts.restart()
    progress = []
    again = hand_over(failed, _Run(host, tmp_path, selections))
    again.engine.set_on_progress(progress.append)
    again.start()
    assert (again.finished, again.failures) == (1, [])
    again.progress = progress
    received = len(host.files[f"plain/{source.playlist.segments[gone]}"])
    reused = [
        len(host.files[f"plain/{source.playlist.segments[index]}"])
        for index in range(second.first_segment, second.last_segment + 1)
        if index != gone
    ]
    return failed, again, received, reused


def test_retry_counts_only_the_segment_it_received_as_received_bytes(
    host, sources, tmp_path, monkeypatch
):
    """이어받은 실행의 받은 양 · 전송 줄의 Bytes · 진행 통지의 받은 양은 다시 받은 세그먼트 하나의 크기여야 한다.

    `test_retry_receives_only_the_segment_that_is_gone`의 실행. 둘째 구간의 세그먼트 중 하나를 다시 받음
    -> total_downloaded_size == 그 세그먼트 크기, log_transfer_complete의 Bytes == 같은 값,
       진행 통지의 downloaded_size 최댓값 <= 같은 값,
       log_transfer_reused == (둘째 구간의 나머지 세그먼트 수, 그 크기의 합)
    """
    _failed, again, received, reused = _retry_with_one_segment_gone(
        host, sources, tmp_path, monkeypatch
    )

    assert reused  # 다시 쓴 것이 있어야 이 단언이 뜻이 있다
    assert again.data.total_downloaded_size == received
    assert [args[1] for args in _logged(again, "log_transfer_complete")] == [received]
    assert max(event.downloaded_size for event in again.progress) <= received
    assert _logged(again, "log_transfer_reused") == [(len(reused), sum(reused))]


def test_retry_speed_comes_only_from_the_segment_it_received(host, sources, tmp_path, monkeypatch):
    """이어받은 실행이 1초 동안 받은 양으로 낸 속도는 다시 받은 세그먼트 하나의 크기여야 한다.

    `test_retry_receives_only_the_segment_that_is_gone`의 실행이 끝난 엔진, 직전 측정 0바이트 · 1초 전
    -> speed_mb == 그 세그먼트 크기 / 1 MiB
    """
    _failed, again, received, _reused = _retry_with_one_segment_gone(
        host, sources, tmp_path, monkeypatch
    )

    assert _speed_over_one_second(again) == received / (1024 * 1024)


def test_retry_counts_every_segment_as_done_so_the_progress_reaches_the_end(
    host, sources, tmp_path, monkeypatch
):
    """이어받은 실행은 다시 쓴 세그먼트도 끝낸 세그먼트로 세어야 한다.

    `test_retry_receives_only_the_segment_that_is_gone`의 실행. 둘째 구간의 세그먼트 중 하나를 다시 받음
    -> completed_threads == max_threads == 둘째 구간의 세그먼트 수, reused_threads == 그보다 1 작다
    """
    _failed, again, _received, reused = _retry_with_one_segment_gone(
        host, sources, tmp_path, monkeypatch
    )

    data = again.data
    assert (data.completed_threads, data.max_threads) == (len(reused) + 1, len(reused) + 1)
    assert data.reused_threads == len(reused)


def test_retry_tells_how_many_segments_it_will_use_without_receiving(
    host, sources, tmp_path, monkeypatch
):
    """이어받은 실행은 받지 않고 다시 쓸 세그먼트의 수를 적어 두고, 다시 쓴 수가 그와 같아야 한다 (#390).

    `test_retry_receives_only_the_segment_that_is_gone`의 실행. 둘째 구간의 세그먼트 중 하나를 다시 받음
    -> reusable_threads == reused_threads == 둘째 구간의 세그먼트 수 − 1
    """
    _failed, again, _received, reused = _retry_with_one_segment_gone(
        host, sources, tmp_path, monkeypatch
    )

    assert (again.data.reusable_threads, again.data.reused_threads) == (len(reused), len(reused))


def test_download_without_sections_counts_every_segment_as_received_bytes(host, sources, tmp_path):
    """받아 둔 것이 없는 실행의 받은 양 · 전송 줄의 Bytes · 속도는 세그먼트 전부의 크기여야 한다.

    plain, selections 빈 튜플
    -> total_downloaded_size == 세그먼트 6개 크기의 합, log_transfer_complete의 Bytes == 같은 값,
       speed_mb == 같은 값 / 1 MiB, log_transfer_reused 0회, reused_threads == 0
    """
    source = sources["plain"]
    every = sum(len(host.files[f"plain/{name}"]) for name in source.playlist.segments)

    run = _Run(host, tmp_path, []).start()

    assert (run.finished, run.failures) == (1, [])
    assert run.data.total_downloaded_size == every
    assert [args[1] for args in _logged(run, "log_transfer_complete")] == [every]
    assert _speed_over_one_second(run) == every / (1024 * 1024)
    assert _logged(run, "log_transfer_reused") == []
    assert run.data.reused_threads == 0


def test_first_section_run_counts_the_segments_read_to_place_the_sections_as_stored(
    host, sources, tmp_path
):
    """구간을 정할 때 받아 둔 세그먼트는 첫 실행에서도 받은 양이 아니라 받아 둔 것으로 세어야 한다.

    plain, 구간 셋(`_three`). 받기 전 첫 진행 통지 시점의 호스트 요청 수로 그 전(구간을 정하며 받음) ·
    그 뒤(전송 단계가 받음)의 세그먼트 요청을 가름
    -> total_downloaded_size == 그 뒤 세그먼트 크기의 합, log_transfer_complete의 Bytes == 같은 값,
       log_transfer_reused == (그 전 세그먼트 중 구간들이 쓰는 것의 수, 그 크기의 합),
       그 전 · 그 뒤 모두 1개 이상
    """
    source = sources["plain"]
    marks = []
    run = _Run(host, tmp_path, _three(source))
    run.engine.set_on_progress(lambda _event: marks.append(len(host.requests)))

    run.start()

    assert (run.finished, run.failures) == (1, [])
    used = {
        f"plain/{source.playlist.segments[index]}"
        for section in run.engine.sections
        for index in range(section.first_segment, section.last_segment + 1)
    }
    segments = [name for _method, name, _range in host.requests if name.endswith(".m4s")]
    before = [name for _m, name, _r in host.requests[: marks[0]] if name.endswith(".m4s")]
    after = segments[len(before) :]
    stored = [name for name in before if name in used]
    assert stored and after
    received = sum(len(host.files[name]) for name in after)
    assert run.data.total_downloaded_size == received
    assert [args[1] for args in _logged(run, "log_transfer_complete")] == [received]
    assert _logged(run, "log_transfer_reused") == [
        (len(stored), sum(len(host.files[name]) for name in stored))
    ]


# ================================================================ 넘긴 세그먼트를 곧바로 지운다 (#309)


class _JoinSpy:
    """구간 파일을 쓰는 실행(remux_through)을 감싸, 그것이 끝난 순간 임시 폴더에 남은 세그먼트를 적는다.

    그 순간은 구간이 만들어졌다고 엔진이 알기 전이다 — 그때 이미 없는 세그먼트는 넘기는
    도중에 지워진 것이다. ``fail``이 참이면 다 쓴 뒤에 RemuxError를 낸다.
    """

    def __init__(self, monkeypatch, run: _Run) -> None:
        self.left: list[list[str]] = []  # 호출마다, 끝난 순간 남아 있던 세그먼트 파일 이름
        self.fail = False
        self._run = run
        self._real = fmp4_cut_module.remux_through
        monkeypatch.setattr(fmp4_cut_module, "remux_through", self._call)

    def _call(self, *args, **kwargs):
        result = self._real(*args, **kwargs)
        names = sorted(os.listdir(self._run.engine.temp_dir))
        self.left.append([name for name in names if name.endswith(".m4v")])
        if self.fail:
            raise RemuxError("시험")
        return result


def test_segment_fed_to_the_section_file_is_removed_before_the_section_is_done(
    host, sources, tmp_path, monkeypatch
):
    """구간 파일로 넘긴 세그먼트는 그 구간 파일을 다 쓰기 전에 지워져 있어야 한다.

    plain, 구간 프레임 40~100(세그먼트 0~3 — 프레임 40은 세그먼트 1, 100은 세그먼트 3에 있다.
    통째로 넘기는 것은 세그먼트 2뿐이고 1 · 3은 경계 조각으로만 읽힌다)
    -> 구간 파일 쓰기가 끝난 순간 임시 폴더의 세그먼트 == 1 · 2 · 4번 파일(세그먼트 0 · 1 · 3),
       실패 0건, 끝난 뒤 임시 폴더 없음
    """
    run = _Run(host, tmp_path, [sources["plain"].selection(40, 100)])
    spy = _JoinSpy(monkeypatch, run)

    run.start()

    assert run.failures == []
    assert spy.left == [["1.m4v", "2.m4v", "4.m4v"]]
    assert not os.path.exists(run.engine.temp_dir)


def test_segment_another_section_still_needs_is_kept_until_that_section_is_made(
    host, sources, tmp_path, monkeypatch
):
    """남은 구간이 쓰는 세그먼트는 넘긴 뒤에도 남아 있다가 마지막으로 쓰는 구간이 넘길 때 지워져야 한다.

    plain, 구간 프레임 5~130(세그먼트 0~4) · 35~170(세그먼트 0~5). 긴 둘째 구간을 먼저 만든다 —
    둘째가 통째로 넘기는 세그먼트 2 · 3 · 4는 첫째도 쓴다. 첫째는 1 · 2 · 3을 통째로 넘긴다
    -> 둘째 구간 파일 쓰기가 끝난 순간 세그먼트 여섯이 모두 남아 있다,
       첫째 구간 파일 쓰기가 끝난 순간 남은 것은 첫째의 경계 세그먼트 0 · 4뿐(1번 · 5번 파일),
       구간마다 check.ok
    """
    source = sources["plain"]
    run = _Run(host, tmp_path, [source.selection(5, 130), source.selection(35, 170)])
    spy = _JoinSpy(monkeypatch, run)

    run.start()

    assert run.failures == []
    assert spy.left == [[f"{n}.m4v" for n in range(1, 7)], ["1.m4v", "5.m4v"]]
    for check in run.checks():
        assert check.ok, check.notes


def test_retry_after_a_failed_section_file_receives_only_the_segments_that_were_fed(
    host, sources, tmp_path, monkeypatch
):
    """구간 파일을 쓰다 실패한 실행을 이어받은 실행은 넘기면서 지운 세그먼트만 다시 받아야 한다.

    첫 실행: plain, 구간 프레임 40~100(세그먼트 0~3, 통째로 넘기는 것은 세그먼트 2),
    구간 파일 쓰기가 끝에서 RemuxError. 이어받은 실행: 새 엔진에 첫 실행이 남긴 것을 넘김
    -> 첫 실행: 실패 1건(PostprocessError), 구간 파일 없음, 남긴 것의 stored == {0, 1, 3}.
       이어받은 실행: 요청 == 세그먼트 2 하나(범위 요청 아님), 완료 1회 · 실패 0건, 구간 파일이 대조와 맞는다
    """
    source = sources["plain"]
    selections = [source.selection(40, 100)]
    failed = _Run(host, tmp_path, selections)
    spy = _JoinSpy(monkeypatch, failed)
    spy.fail = True
    failed.start()
    assert len(failed.failures) == 1
    assert isinstance(failed.failures[0], PostprocessError)
    assert not os.path.exists(failed.paths[0])
    assert failed.data.section_resume.fmp4_head.stored == {0, 1, 3}
    requests_before = len(host.requests)
    spy.fail = False

    again = hand_over(failed, _Run(host, tmp_path, selections)).start()

    assert host.requests[requests_before:] == [("GET", "plain/seg-002.m4s", None)]
    assert (again.finished, again.failures) == (1, [])
    _assert_section_matches(source, again.paths[0], 40, 100, tmp_path)


# ================================================================ 끝낸 구간의 파일이 없거나 바뀐 재시도 (#309)


def _fail_the_second_of_three(host, sources, tmp_path, monkeypatch):
    """구간 셋 가운데 둘째 구간의 컷만 실패한 실행과, 컷 호출을 적는 도구(다음 실행을 위해 비운 것)."""
    cuts = CutCalls(monkeypatch, m3u8_module, "cut_fmp4_section", source_arg=0)
    cuts.fail_on = (1,)
    failed = _Run(host, tmp_path, _three(sources["plain"])).start()
    assert len(failed.failures) == 1
    cuts.restart()
    return failed, cuts


def test_failed_run_records_the_size_of_each_section_file_it_finished(
    host, sources, tmp_path, monkeypatch
):
    """일부 구간만 실패한 실행은 끝낸 구간 파일의 크기를 재시도 기록에 적어야 한다.

    구간 셋, 둘째 구간의 컷이 실패 -> 기록의 크기 == ((0, `_1`의 크기), (2, `_3`의 크기))
    """
    failed, _cuts = _fail_the_second_of_three(host, sources, tmp_path, monkeypatch)

    assert failed.data.section_resume.done_sizes == (
        (0, os.path.getsize(failed.paths[0])),
        (2, os.path.getsize(failed.paths[2])),
    )


@pytest.mark.parametrize("check_files", [True, False], ids=["starter-checks", "engine-checks"])
def test_retry_makes_a_finished_section_again_when_its_file_was_removed(
    host, sources, tmp_path, monkeypatch, check_files
):
    """끝낸 구간의 파일을 지운 뒤 재시도하면 그 구간도 다시 만들어야 한다.

    첫 실행: 구간 셋, 둘째 구간의 컷이 실패. `_1`을 지움. 이어받은 실행(시작하는 쪽이 파일을
    확인한 경우와, 기록을 그대로 넘겨 엔진이 확인하는 경우)
    -> 완료 1회 · 실패 0건, 컷 호출은 `_1` · `_2`, `_3`의 수정 시각 · 내용이 그대로,
       저장 폴더에 `_1` · `_2` · `_3`뿐, 다시 만든 두 구간이 판정 통과, 이어받은 구간 수 1
    """
    failed, cuts = _fail_the_second_of_three(host, sources, tmp_path, monkeypatch)
    kept = snapshot([failed.paths[2]])
    os.remove(failed.paths[0])

    again = hand_over(
        failed, _Run(host, tmp_path, _three(sources["plain"])), check_files=check_files
    ).start()

    assert (again.finished, again.failures) == (1, [])
    assert sorted(cuts.outputs) == THREE_FILES[:2]
    assert snapshot(kept) == kept
    assert again.listing() == THREE_FILES
    checks = again.checks()
    assert len(checks) == 2
    assert all(check.ok for check in checks), [check.notes for check in checks]
    assert again.data.sections_resumed == 1


def test_retry_makes_a_finished_section_again_when_its_file_changed_size(
    host, sources, tmp_path, monkeypatch
):
    """끝낸 구간의 파일 크기가 달라졌으면 재시도는 그 구간을 다시 만들고, 바뀐 파일은 건드리지 않아야 한다.

    첫 실행: 구간 셋, 둘째 구간의 컷이 실패. `_3`의 자리에 10바이트짜리 다른 파일을 둠. 이어받은 실행
    -> 완료 1회 · 실패 0건, 셋째 구간은 새 이름(`_3`이 아닌 경로)으로 만들어지고 크기가 0보다 크다,
       `_3` 자리의 파일은 10바이트 그대로, `_1`의 수정 시각 · 내용이 그대로, 이어받은 구간 수 1
    """
    failed, cuts = _fail_the_second_of_three(host, sources, tmp_path, monkeypatch)
    kept = snapshot([failed.paths[0]])
    with open(failed.paths[2], "wb") as f:
        f.write(b"0123456789")

    again = hand_over(failed, _Run(host, tmp_path, _three(sources["plain"]))).start()

    assert (again.finished, again.failures) == (1, [])
    assert again.paths[2] != failed.paths[2]
    assert os.path.getsize(again.paths[2]) > 0
    assert os.path.basename(again.paths[2]) in cuts.outputs
    with open(failed.paths[2], "rb") as f:
        assert f.read() == b"0123456789"
    assert snapshot(kept) == kept
    assert again.data.sections_resumed == 1


# ================================================================ 자르기 전 프레임 정보 읽기


def test_reading_before_the_cut_raises_the_cut_progress_and_is_marked_as_preparing(
    host, sources, tmp_path
):
    """구간을 자르기 전 세그먼트를 읽는 동안 준비 중으로 적히고 컷 진행이 읽기 몫 안에서 올라야 한다.

    plain, 구간 하나(프레임 40~100 — 엔진이 읽는 세그먼트 0~3, 넷). 읽기 몫 0.1
    -> 준비 중인 통지의 컷 진행 == [없음, 0.025, 0.05, 0.075, 0.1], 준비 중이 아닌 통지의 컷 진행은
       모두 0.1 이상이고 마지막이 1, 끝난 뒤 준비 중이 아니다, 완료 1회
    """
    run = _Run(host, tmp_path, [sources["plain"].selection(40, 100)])
    seen = []

    def on_progress(_event) -> None:
        if run.data.cut_preparing or run.data.cut_progress is not None:
            seen.append((run.data.cut_preparing, run.data.cut_progress))

    run.engine.set_on_progress(on_progress)

    run.start()

    assert (run.finished, run.failures) == (1, [])
    preparing = [progress for marked, progress in seen if marked]
    cutting = [progress for marked, progress in seen if not marked]
    section = run.engine.sections[0]
    assert (section.first_segment, section.last_segment) == (0, 3)
    assert preparing == [None, *(pytest.approx(0.1 * count / 4) for count in (1, 2, 3, 4))]
    assert min(cutting) >= preparing[-1] and cutting[-1] == 1.0
    assert run.data.cut_preparing is False


def test_progress_the_cut_reports_is_placed_after_the_share_of_the_reading(
    host, sources, tmp_path, monkeypatch
):
    """컷이 알린 진행은 읽기 몫 뒤의 구간으로 옮겨 적혀야 한다.

    plain, 구간 하나, 읽기 몫 0.1. 컷 함수가 일을 시작하기 전에 진행 0.5를 알리게 함
    -> 그 직후의 컷 진행 == 0.55 (0.1 + 0.9 x 0.5), 완료 1회
    """
    run = _Run(host, tmp_path, [sources["plain"].selection(40, 100)])
    real_cut = m3u8_module.cut_fmp4_section
    seen = []

    def cut(*args, **kwargs):
        kwargs["on_progress"](0.5)
        seen.append(run.data.cut_progress)
        return real_cut(*args, **kwargs)

    monkeypatch.setattr(m3u8_module, "cut_fmp4_section", cut)

    run.start()

    assert seen == [pytest.approx(0.55)]
    assert (run.finished, run.failures) == (1, [])


# ================================================================ 파일 하나의 한계(FAT32)에 닿은 쓰기 (#309)

# ffmpeg가 FAT32에 4GiB를 넘겨 쓰다 낸 글(rc1 macOS 실기의 stderr 꼴)
_FFMPEG_FILE_TOO_LARGE = "Error submitting a packet to the muxer: File too large"

# 저장 폴더의 파일 시스템 이름과 그때 파일 하나의 한계에 닿은 실패에 적혀야 할 키
_SIZE_LIMIT_KEYS = pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("msdos", FILE_TOO_LARGE_FOR_FAT),
        (None, FILE_TOO_LARGE_FOR_FAT),  # 파일 시스템 이름을 읽지 못함
        ("NTFS", "Failed to save file"),
    ],
    ids=["fat", "unknown", "not-fat"],
)


@_SIZE_LIMIT_KEYS
def test_section_cut_that_hits_the_file_size_limit_carries_a_key_by_the_filesystem(
    host, sources, tmp_path, monkeypatch, name, expected
):
    """구간 파일을 쓰는 ffmpeg가 "File too large"로 실패하면 저장 폴더의 파일 시스템에 맞는 키가 적혀야 한다.

    plain, 구간 프레임 40~100. 구간 파일을 쓰는 실행(remux_through)이 stderr에
    "Error submitting a packet to the muxer: File too large"가 든 RemuxError를 냄
    -> 실패 1건(PostprocessError), space_key가 "msdos" · 읽지 못함(None)이면
       "File too large for a FAT32 drive", "NTFS"면 "Failed to save file",
       저장 폴더에 구간 파일이 없다
    """
    run = _Run(host, tmp_path, [sources["plain"].selection(40, 100)])
    monkeypatch.setattr(base_module, "filesystem_name", lambda folder: name)

    def too_large(*args, **kwargs):
        raise RemuxError(f"ffmpeg remux 실패 (변환 exit 0 · 잇기 exit 1): {_FFMPEG_FILE_TOO_LARGE}")

    monkeypatch.setattr(fmp4_cut_module, "remux_through", too_large)

    run.start()

    assert [type(e) for e in run.failures] == [PostprocessError]
    assert getattr(run.failures[0], "space_key", None) == expected
    assert not os.path.exists(run.paths[0])


@_SIZE_LIMIT_KEYS
def test_remux_that_hits_the_file_size_limit_carries_a_key_by_the_filesystem(
    host, tmp_path, monkeypatch, name, expected
):
    """구간 없는 다운로드의 재포장이 "File too large"로 실패하면 저장 폴더의 파일 시스템에 맞는 키가 적혀야 한다.

    plain, selections 빈 튜플. 재포장(remux_stream)이 세그먼트를 다 받아 넘긴 뒤 stderr에
    "Error submitting a packet to the muxer: File too large"가 든 RemuxError를 냄
    -> 실패 1건(PostprocessError), space_key가 "msdos" · 읽지 못함(None)이면
       "File too large for a FAT32 drive", "NTFS"면 "Failed to save file",
       output_path가 없다, 완료 0회
    """
    run = _Run(host, tmp_path, [])
    monkeypatch.setattr(base_module, "filesystem_name", lambda folder: name)

    def too_large(chunks, dst_path, **kwargs):
        for _chunk in chunks:  # 제품의 재포장처럼 세그먼트를 끝까지 넘긴다(넘긴 것은 지워진다)
            pass
        raise RemuxError(f"ffmpeg remux 실패 (exit 1): {_FFMPEG_FILE_TOO_LARGE}")

    monkeypatch.setattr(base_module, "remux_stream", too_large)

    run.start()

    assert [type(e) for e in run.failures] == [PostprocessError]
    assert getattr(run.failures[0], "space_key", None) == expected
    assert (run.finished, os.path.exists(run.data.output_path)) == (0, False)
