"""M3U8Downloader의 구간 다운로드 — 인코딩 전 다시보기(HLS fMP4), 받기부터 구간 파일까지 (#309).

실제 ffmpeg(imageio-ffmpeg 동봉)로 테스트 안에서 짧은 영상을 만들어 HLS fMP4(초기화
세그먼트 + 세그먼트 + 플레이리스트)로 싸고, 범위 요청에 답하는 호스트
(tests/unit/core/range_host.py — 소켓을 열지 않는다)로 내준다. M3U8Downloader를 구간과
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
- 구간 파일의 영상·오디오 패킷은 세그먼트 전부를 이은 파일에서 자른 것과 모두 같다

- 오디오를 다시 인코딩하는 비트레이트는 초기화 세그먼트에 적힌 값이다 — 어느 세그먼트를
  받았는지와 무관하다
- 끝이 영상 길이와 같은 구간의 끝 프레임은 마지막 프레임이다

대조는 파일 바이트가 아니라 패킷(시각 + 내용의 CRC)으로 한다. 결과 파일의 메타데이터에는
ffmpeg가 입력에서 추정한 비트레이트가 들어가는데, 그 값은 입력이 세그먼트 전부인지
일부인지에 따라 조금 다르다 — 프레임과 소리는 같다.
"""

import os
import struct
import subprocess
from fractions import Fraction

import pytest

import core.api.hls_fmp4 as hls_fmp4_module
import core.downloaders.m3u8_downloader as m3u8_module
from core.api.fmp4 import build_fmp4_index, fmp4_origin, parse_init_segment, parse_media_segment
from core.api.hls import parse_media_playlist
from core.api.hls_fmp4 import fetch_fmp4_head, segment_file_name, segment_frames
from core.downloaders.base import PostprocessError
from core.downloaders.integrity import TruncatedSegmentError
from core.downloaders.m3u8_downloader import M3U8Downloader
from core.models.download_data import DownloadData
from core.models.plan import TimeRange
from core.utils.cut_check import check_cut
from core.utils.ffmpeg import get_ffmpeg_exe, run_ffmpeg
from core.utils.fmp4_sections import plan_fmp4_sections
from core.utils.hybrid_cut import CUT_FAILED, CutError, cut_frames_from_fmp4, hybrid_cut
from core.utils.paths import build_section_output_paths, temp_dir_for
from core.utils.selections import (
    SELECTION_CROSSES_BREAK,
    SELECTION_OUT_OF_RANGE,
    SELECTION_TOO_SHORT,
    SelectionError,
)
from tests.unit.core.range_host import RangeHost

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
        self.frames = cut_frames_from_fmp4(
            self.init, parsed, build_fmp4_index(self.init, parsed, origin)
        )
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
    """세그먼트 전부를 이은 파일에서 같은 프레임을 자른 파일의 패킷."""
    path = str(tmp_path / f"reference_{first}_{last}.mp4")
    hybrid_cut(source.joined, source.frames, first, last, path)
    return _packets(path)


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
    """받은 세그먼트만으로 자른 구간 파일의 패킷은 세그먼트 전부를 이은 파일에서 자른 파일의 패킷과 모두 같아야 한다.

    plain, 주석의 경우마다 구간 프레임 first~last
    -> 받은 세그먼트 범위 == segments, 영상·오디오 패킷이 전부 이은 파일에서 자른 것과 같다, check.ok
    """
    source = sources["plain"]

    run = _Run(host, tmp_path, [source.selection(first, last)]).start()

    assert run.failures == []
    section = run.engine.sections[0]
    assert (section.first_segment, section.last_segment) == segments
    assert _packets(run.paths[0]) == _reference(source, first, last, tmp_path)
    check = run.checks()[0]
    assert check.ok, check.notes


@pytest.mark.parametrize(
    ("first", "last", "segments"),
    [(40, 100, (0, 3)), (150, 179, (3, 5)), (60, 80, (1, 2))],
    ids=["from-first-segment", "last-segments", "middle-segments"],
)
def test_section_audio_is_encoded_at_the_declared_bitrate_whatever_was_received(
    host, sources, tmp_path, first, last, segments
):
    """구간의 오디오를 다시 인코딩하는 비트레이트는 어느 세그먼트를 받았든 초기화 세그먼트에 적힌 값이어야 한다.

    plain(초기화 세그먼트에 128000), 주석의 경우마다 구간 프레임 first~last
    -> 받은 세그먼트 범위 == segments, 컷이 쓴 오디오 비트레이트 == 128
    """
    run = _Run(host, tmp_path, [sources["plain"].selection(first, last)]).start()

    assert run.failures == []
    section = run.engine.sections[0]
    assert (section.first_segment, section.last_segment) == segments
    assert run.engine.cut_results[0].source.audio_bitrate == 128


def test_section_ending_at_the_video_length_ends_on_the_last_frame(host, sources, tmp_path):
    """끝이 영상 길이와 같은 구간은 마지막 프레임에서 끝나야 한다.

    plain, 구간 = 프레임 150의 시각 ~ 영상 길이(마지막 프레임 179가 끝나는 시각)
    -> 끝 프레임의 PTS == 프레임 179의 PTS, 받은 세그먼트 3~5,
       영상·오디오 패킷이 전부 이은 파일에서 프레임 150~179를 자른 것과 같다
    """
    source = sources["plain"]
    length = source.time_of(179) + source.frames.frame_duration
    selection = TimeRange(source.time_of(150), length)

    run = _Run(host, tmp_path, [selection]).start()

    assert run.failures == []
    section = run.engine.sections[0]
    assert section.last_pts == source.time_of(179)
    assert (section.first_segment, section.last_segment) == (3, 5)
    assert _packets(run.paths[0]) == _reference(source, 150, 179, tmp_path)


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
    """실제 다시보기 모양의 입력(moof 둘 · emsg · 긴 #EXTINF · 비트레이트 0)에서도 구간 파일은 전부 이은 파일에서 자른 것과 같아야 한다.

    real · nopdt, 범위 요청에 잘린 본문을 주고 캐시하는 호스트, 구간 프레임 100~170
    (둘 다 세그먼트의 둘째 moof에 든 프레임이다)
    -> 실패 0건, Range 머리가 든 요청 0건, 캐시에 잘린 본문 없음, 첫·끝 프레임의 PTS가 프레임 100 · 170의 것,
       오디오 비트레이트 192(적힌 값이 없을 때의 고정값), 패킷이 대조 파일과 같다, check.ok
    """
    source = sources["real"]
    host.truncating_cache = True

    run = _Run(host, tmp_path, [source.selection(100, 170)], playlist).start()

    assert run.failures == []
    assert [header for _m, _n, header in host.requests if header is not None] == []
    assert host.truncated() == []
    section = run.engine.sections[0]
    assert (section.first_pts, section.last_pts) == (source.time_of(100), source.time_of(170))
    assert run.engine.cut_results[0].source.audio_bitrate == 192
    assert _packets(run.paths[0]) == _reference(source, 100, 170, tmp_path)
    check = run.checks()[0]
    assert check.ok, check.notes


def test_section_of_uneven_input_passes_every_check(host, sources, tmp_path):
    """프레임 간격이 고르지 않은 입력에서도 구간 파일의 패킷은 전부 이은 파일에서 자른 것과 같고 판정을 통과해야 한다.

    uneven, 구간 프레임 33~100
    -> 영상·오디오 패킷이 전부 이은 파일에서 자른 것과 같다, check.ok
    """
    source = sources["uneven"]

    run = _Run(host, tmp_path, [source.selection(33, 100)], "uneven/media.m3u8").start()

    assert run.failures == []
    assert _packets(run.paths[0]) == _reference(source, 33, 100, tmp_path)
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
    assert _packets(run.paths[0]) == _reference(source, 40, 100, tmp_path)
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
    assert _packets(run.paths[0]) == _reference(source, 40, 100, tmp_path)


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
    assert _packets(run.paths[0]) == _reference(source, 40, 100, tmp_path)


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
    assert _packets(run.paths[0]) == _reference(source, 40, 80, tmp_path)


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
    assert _packets(run.paths[0]) == _reference(source, 40, 100, tmp_path)


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
    assert _packets(run.paths[0]) == _reference(source, 40, 100, tmp_path)


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

    plain, 구간 둘, 둘째 컷이 CutError를 내도록 바꿈
    -> 완료 0회, 실패 1건(PostprocessError, 원인 CutError), `_1` 파일과 임시 폴더가 남고
       임시 폴더에 초기화 세그먼트 · 세그먼트 · 둘째 구간의 이은 파일이 있다
    """
    source = sources["plain"]
    real = m3u8_module.hybrid_cut
    calls = []

    def second_fails(*args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            raise CutError(CUT_FAILED, "시험")
        return real(*args, **kwargs)

    monkeypatch.setattr(m3u8_module, "hybrid_cut", second_fails)

    run = _Run(host, tmp_path, [source.selection(5, 20), source.selection(40, 70)]).start()

    assert run.finished == 0
    assert len(run.failures) == 1
    assert isinstance(run.failures[0], PostprocessError)
    assert isinstance(run.failures[0].__cause__, CutError)
    assert run.listing() == ["CVDv2_temp_구간 시험 144p_1", "구간 시험 144p_1.mp4"]
    kept = sorted(os.listdir(temp_dir_for(run.paths[0])))
    assert kept == ["0.m4s", "1.m4v", "2.m4v", "3.m4v", "section_2.mp4"]


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
    real = m3u8_module.hybrid_cut

    def interrupted(*args, **kwargs):
        if how == "failure":
            raise RuntimeError("시험")
        run.data.model.stop()
        return real(*args, **kwargs)

    monkeypatch.setattr(m3u8_module, "hybrid_cut", interrupted)

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


def test_section_postprocess_copies_segments_in_fixed_size_chunks(
    host, sources, tmp_path, monkeypatch
):
    """구간 후처리는 받은 세그먼트를 통째로 읽지 않고 정해진 크기씩 옮겨 이어야 한다.

    plain, 구간 프레임 40~100 (세그먼트 0~3), 옮기는 크기를 1,000바이트로 낮추고 세그먼트 파일의 read를 기록
    -> 세그먼트 파일에서 한 번에 읽은 가장 큰 크기가 1,000바이트 이하, 패킷은 전부 이은 파일에서 자른 것과 같다
    """
    source = sources["plain"]
    monkeypatch.setattr(m3u8_module, "_JOIN_CHUNK_BYTES", 1000)
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

    monkeypatch.setattr(m3u8_module, "open", recording_open, raising=False)

    run = _Run(host, tmp_path, [source.selection(40, 100)]).start()

    assert run.failures == []
    assert largest  # 세그먼트 파일을 이 대역으로 읽었다
    assert max(largest) <= 1000
    assert min(len(source.files[name]) for name in source.playlist.segments[:4]) > 1000
    assert _packets(run.paths[0]) == _reference(source, 40, 100, tmp_path)


def test_engine_keeps_handed_in_segments_and_removes_other_files_from_the_segment_folder(
    host, sources, tmp_path, monkeypatch
):
    """엔진은 넘겨받은 세그먼트 폴더에서 받아 둔 세그먼트만 남기고, 이 다운로드의 것이 아닌 파일은 지워야 한다.

    plain, 구간 프레임 40~100(세그먼트 0~3). 해석 뒤 폴더에 이전 실행의 것처럼 "9.m4v"와 "old.bin"을 넣음.
    컷이 CutError를 내게 해 폴더가 남게 함
    -> 폴더에 초기화 세그먼트 · 세그먼트 1~4(0~3번째) · 이은 파일만 있다 — "9.m4v" · "old.bin"과,
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

    monkeypatch.setattr(m3u8_module, "hybrid_cut", fails)
    run = _Run(host, tmp_path, [selection])
    run.data.content.fmp4_head = head

    run.start()

    assert isinstance(run.failures[0], PostprocessError)
    kept = sorted(os.listdir(head.segment_dir))
    assert kept == ["0.m4s", "1.m4v", "2.m4v", "3.m4v", "4.m4v", "section_1.mp4"]


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
