"""HlsAesDownloader의 구간 다운로드 — 암호화 VOD(HLS MPEG-TS), 받기부터 구간 파일까지 (#309).

실제 ffmpeg(imageio-ffmpeg 동봉)로 6초짜리 영상을 HLS MPEG-TS로 만들고, 세그먼트를
AES-128-CBC로 암호화해 tests/unit/core/range_host.py(소켓을 열지 않는다)로 내준다.
HlsAesDownloader를 구간과 함께 실제로 돌려 구간 파일이 나오는지, 정합 판정(check_cut)을
통과하는지 본다. 저장소에 영상 파일을 두지 않는다.

입력: 320x240 · 30fps · 6초 · 1초 세그먼트 · 키프레임 15프레임마다 · B프레임(재정렬 지연 1) ·
오디오 48kHz AAC. 영상이 오디오보다 0.042667초 늦게 시작한다.

호스트가 내주는 것:
- vod/media.m3u8 · vod/segment-*.ts — 받을 플레이리스트
- vod/media.m3u8?token=a · ?token=b — 같은 플레이리스트에 쿼리만 다른 주소
- low/media.m3u8 · low/segment-*.ts — 경로가 다른 플레이리스트(다른 해상도의 자리)

핵심 계약:
- 구간마다 필요한 세그먼트만, 한 번씩, 범위 요청 없이 받는다
- 구간을 해석하며 받아 둔 세그먼트(Content.ts_head)는 다시 받지 않는다. 온전하지 않으면
  다시 받고, 다른 플레이리스트의 것이면 쓰지 않는다
- 키는 엔진이 받을 세그먼트가 있을 때만 한 번 받는다. 키 확인용 세그먼트를 따로 받지 않는다
- 키 값은 로그 · repr · 실패 예외 어디에도 남지 않는다
- 구간을 정하는 일은 작업자가 시작하기 전에 끝나고, 작업자는 서로 다른 파일에 쓴다
- 받은 세그먼트는 완료 · 사용자 중단이면 지우고 후처리 실패면 남긴다. 끝난 뒤 저장 폴더에는
  구간 결과 파일만 남는다
- 전체 다운로드(구간 없음)의 경로는 그대로다
"""

import logging
import os
import subprocess
import threading

import pytest
from Crypto.Cipher import AES

import core.api.hls_ts as hls_ts_module
import core.downloaders.hls_aes_downloader as aes_module
from core.api.hls import parse_media_playlist
from core.api.hls_ts import fetch_ts_head, segment_streams
from core.downloaders.base import PostprocessError
from core.downloaders.decrypt import DecryptionError, sequence_iv
from core.downloaders.hls_aes_downloader import HlsAesDownloader
from core.models.download_data import DownloadData
from core.models.plan import TimeRange
from core.utils.cut_check import check_cut
from core.utils.ffmpeg import get_ffmpeg_exe
from core.utils.hybrid_cut import CUT_FAILED, CutError
from core.utils.paths import build_section_output_paths
from core.utils.ts_sections import choose_ts_frame_rate, plan_ts_sections
from tests.unit.core.range_host import RangeHost

KEY = bytes.fromhex("7c1d9e42a05b63f8817e2c4d6a9b0f35")  # 테스트용 키 — 실제 키가 아니다
WRONG_KEY = bytes.fromhex("0f1e2d3c4b5a69788796a5b4c3d2e1f0")  # 테스트용 — 복호화가 틀어진다
KEY_URI = "https://key.test/k"
FPS = 30
KEY_EVERY = 15  # 키프레임 간격(프레임)
SEGMENT_FRAMES = 30  # 세그먼트 하나의 프레임 수(1초)
LEAD = 0.042667  # 영상이 오디오보다 늦게 시작하는 만큼(초) — 첫 영상 프레임의 VOD 시각
FIRST = TimeRange(0.8, 2.3)  # 세그먼트 0 · 1 · 2에 걸치고, 양 끝이 키프레임이 아니다
SECOND = TimeRange(3.4, 5.2)  # 세그먼트 2 ~ 5에 걸치고, 양 끝이 키프레임이 아니다
MIDDLE = TimeRange(2.2, 3.6)  # 첫 · 마지막 세그먼트를 쓰지 않는 구간 (세그먼트 1 ~ 3)
# 첫 · 마지막 세그먼트를 쓰지 않고, 구간 해석이 읽지 않는 가운데 세그먼트(3)가 있는 구간 (1 ~ 4)
WIDE = TimeRange(2.2, 4.6)
SECTION_FILES = ["구간 시험 144p_1.mp4", "구간 시험 144p_2.mp4"]


def _ffmpeg(*args: str, cwd=None) -> None:
    done = subprocess.run(
        [get_ffmpeg_exe(), "-hide_banner", "-v", "error", "-y", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert done.returncode == 0, done.stderr


class _Vod:
    """만든 HLS 하나 — 암호화해 내줄 파일들(이름 → bytes)과 플레이리스트."""

    def __init__(self, folder):
        _ffmpeg(
            "-itsoffset", str(LEAD), "-f", "lavfi",
            "-i", f"testsrc2=size=320x240:rate={FPS}:duration=6",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6",
            "-c:a", "aac", "-ac", "2", "-pix_fmt", "yuv420p", "-c:v", "libx264",
            "-preset", "veryfast", "-bf", "2",
            "-force_key_frames", f"expr:gte(n,n_forced*{KEY_EVERY})",
            "-x264-params", "b-pyramid=none:keyint=300:min-keyint=1:scenecut=0",
            "-f", "hls", "-hls_time", "1", "-hls_list_size", "0",
            "-hls_segment_filename", "segment-%06d.ts", "media.m3u8",
            cwd=str(folder),
        )  # fmt: skip
        text = (folder / "media.m3u8").read_text(encoding="utf-8")
        self.playlist = parse_media_playlist(text)
        keyed = text.replace(
            "#EXTINF", f'#EXT-X-KEY:METHOD=AES-128,URI="{KEY_URI}"\n#EXTINF', 1
        ).encode("utf-8")
        self.files = {}
        for prefix in ("vod", "low"):
            self.files[f"{prefix}/media.m3u8"] = keyed
            for number, name in enumerate(self.playlist.segments):
                plain = (folder / name).read_bytes()
                pad = 16 - len(plain) % 16
                cipher = AES.new(KEY, AES.MODE_CBC, sequence_iv(number))
                self.files[f"{prefix}/{name}"] = cipher.encrypt(plain + bytes([pad]) * pad)
        for token in ("a", "b"):
            self.files[f"vod/media.m3u8?token={token}"] = keyed
        self.last = len(self.playlist.segments) - 1  # 마지막 세그먼트의 인덱스


@pytest.fixture(scope="module")
def vod(tmp_path_factory) -> _Vod:
    """ffmpeg로 만든 6초짜리 암호화 HLS 하나 — 모듈의 테스트가 함께 쓴다."""
    return _Vod(tmp_path_factory.mktemp("hls_aes_sections"))


@pytest.fixture
def host(vod, monkeypatch) -> RangeHost:
    """vod의 파일을 내주는 호스트 — 엔진과 구간 해석의 요청이 이 호스트로 간다."""
    served = RangeHost(vod.files)
    monkeypatch.setattr(aes_module, "get_thread_session", served.session)
    monkeypatch.setattr(hls_ts_module, "get_thread_session", served.session)
    return served


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

    def __init__(
        self, host, tmp_path, selections, playlist: str = "vod/media.m3u8", head=None, key=KEY
    ):
        self.folder = tmp_path / "out"
        self.folder.mkdir(exist_ok=True)
        self.data = DownloadData(
            base_url=host.url(playlist),
            vod_url="https://chzzk.naver.com/video/1",
            output_path=str(self.folder / "unused.mp4"),
            resolution=144,
            content_type="hls_aes",
        )
        self.data.content.selections = tuple(selections)
        self.data.content.selection_paths = build_section_output_paths(
            str(self.folder), "구간 시험", 144, len(selections)
        )
        self.data.content.ts_head = head
        self.paths = self.data.content.selection_paths
        self.finished = 0
        self.failures: list[BaseException] = []
        self.key_requests: list[str] = []  # 키 리졸버가 받은 키 주소
        self._key = key
        self.logger = _Logger()
        self.engine = HlsAesDownloader(
            self.data, self.logger, on_finished=self._on_finished, on_failed=self.failures.append
        )
        self.engine.set_key_resolver(self._resolve_key)
        self.engine._inspect_cuts = True
        self.engine._slow_speed_threshold_kb_s = 0  # 러너 속도와 무관하게 — 저속 재큐를 끈다

    def _on_finished(self) -> None:
        self.finished += 1

    def _resolve_key(self, _content, key_uri: str) -> bytes:
        self.key_requests.append(key_uri)
        return self._key

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

    def wanted(self) -> list[int]:
        """구간들이 쓰는 세그먼트의 인덱스(오름차순)."""
        return sorted(
            {
                index
                for section in self.engine.sections
                for index in range(section.first_segment, section.last_segment + 1)
            }
        )


def _resolve(host, tmp_path, selections, playlist: str = "vod/media.m3u8", everything=False):
    """헤드리스처럼 구간을 먼저 해석한다 — 받은 세그먼트는 tmp_path/segments에 둔다.

    everything이면 구간의 세그먼트를 모두 받아 둔다. 아니면 계획이 읽은 것만 받아 둔다.
    """
    url = host.url(playlist)
    head = fetch_ts_head(url, str(tmp_path / "segments"))

    def segment_at(index: int):
        return segment_streams(head, url, index, KEY)

    head.frame_rate = choose_ts_frame_rate([segment_at(0)]).rate
    sections = plan_ts_sections(head.playlist, selections, segment_at, head.frame_rate)
    if everything:
        for section in sections:
            for index in range(section.first_segment, section.last_segment + 1):
                segment_at(index)
    return head


def _names(indexes, prefix: str = "vod") -> list[str]:
    """세그먼트 인덱스들의 호스트 이름(오름차순)."""
    return [f"{prefix}/segment-{index:06d}.ts" for index in sorted(indexes)]


def _segment_requests(host) -> list[str]:
    """호스트에 온 세그먼트 GET의 이름(오름차순)."""
    return sorted(
        name for method, name, _h in host.requests if method == "GET" and name.endswith(".ts")
    )


def _playlist_requests(host) -> list[str]:
    """호스트에 온 플레이리스트 GET의 이름(온 순서)."""
    return [name for _m, name, _h in host.requests if ".m3u8" in name]


def _ranged(host) -> list[str]:
    """호스트에 온 요청 가운데 Range 머리가 든 것의 이름."""
    return [name for _m, name, header in host.requests if header is not None]


def _nearest_frame(seconds: float) -> int:
    """seconds에 가장 가까운 프레임의 번호 — 프레임은 LEAD + n ÷ 30초에 있다."""
    return round((seconds - LEAD) * FPS)


def _temp_names(run: _Run) -> list[str]:
    """엔진 임시 폴더에 있는 이름들(오름차순)."""
    return sorted(os.listdir(run.engine.temp_dir))


# ================================================================ 끝까지


def test_two_sections_across_segment_boundaries_are_cut_and_pass_every_check(vod, host, tmp_path):
    """세그먼트 경계를 걸치는 구간 둘을 받으면 구간 파일이 둘 생기고 판정 다섯 항목을 통과해야 한다.

    구간 0.8 ~ 2.3초 · 3.4 ~ 5.2초, 범위 요청에 잘린 본문을 주고 캐시하는 호스트, 넘겨받은 것 없음
    -> 완료 1회 · 실패 0건, 받은 세그먼트 0 ~ 2 · 2 ~ 5, 구간마다 check.ok,
       자른 프레임 == 구간 시각에서 따로 계산한 번호(양 끝이 키프레임이 아니다),
       저장 폴더에 구간 파일 둘뿐, 임시 폴더 없음, 병합 진행 == 받은 세그먼트 수
    """
    host.truncating_cache = True

    run = _Run(host, tmp_path, [FIRST, SECOND]).start()

    assert (run.finished, run.failures) == (1, [])
    sections = run.engine.sections
    assert [(s.first_segment, s.last_segment) for s in sections] == [(0, 2), (2, 5)]
    for selection, section, result, check in zip(
        (FIRST, SECOND), sections, run.engine.cut_results, run.checks()
    ):
        first, last = _nearest_frame(selection.start), _nearest_frame(selection.end)
        assert first % KEY_EVERY and last % KEY_EVERY  # 양 끝이 키프레임이 아니다
        base = section.first_segment * SEGMENT_FRAMES  # 다시 싼 mp4의 0번 프레임
        assert (result.plan.first, result.plan.last) == (first - base, last - base)
        assert check.ok, check.notes
    assert run.listing() == SECTION_FILES
    assert not os.path.exists(run.engine.temp_dir)
    assert run.data.merged_segments == len(run.wanted()) == 6
    assert (_ranged(host), host.truncated()) == ([], [])


def test_engine_requests_the_playlist_the_key_and_each_segment_once(vod, host, tmp_path):
    """넘겨받은 것이 없으면 엔진은 플레이리스트 · 키 · 세그먼트를 한 번씩만, 범위 머리 없이 요청해야 한다.

    구간 2.2 ~ 3.6초 (세그먼트 1 ~ 3)
    -> 플레이리스트 1건, 키 요청 1회(플레이리스트의 키 주소), 세그먼트 요청은 구간의 1 ~ 3과
       시각 축을 재는 첫 · 마지막 세그먼트가 하나씩, Range 머리가 든 요청 0건
    """
    run = _Run(host, tmp_path, [MIDDLE]).start()

    assert run.failures == []
    assert run.wanted() == [1, 2, 3]
    assert _playlist_requests(host) == ["vod/media.m3u8"]
    assert run.key_requests == [KEY_URI]
    assert _segment_requests(host) == _names({0, 1, 2, 3, vod.last})
    assert _ranged(host) == []


def test_engine_requests_nothing_when_every_segment_was_handed_in(vod, host, tmp_path):
    """구간의 세그먼트를 모두 받아 둔 채 넘기면 엔진은 플레이리스트도 세그먼트도 키도 요청하지 않아야 한다.

    구간 둘을 먼저 해석하며 세그먼트 0 ~ 5를 모두 받아 둠(Content.ts_head)
    -> 엔진 단계의 호스트 요청 0건, 키 요청 0회, 완료 1회, 구간마다 check.ok,
       받아 둔 폴더가 엔진의 임시 폴더이고 끝나면 없다
    """
    head = _resolve(host, tmp_path, [FIRST, SECOND], everything=True)
    assert head.stored >= {0, 1, 2, 3, 4, 5}
    host.forget()

    run = _Run(host, tmp_path, [FIRST, SECOND], head=head).start()

    assert (run.finished, run.failures) == (1, [])
    assert host.requests == []
    assert run.key_requests == []
    for check in run.checks():
        assert check.ok, check.notes
    assert run.engine.temp_dir == head.segment_dir
    assert not os.path.exists(head.segment_dir)
    assert run.listing() == SECTION_FILES


def test_engine_requests_only_the_segments_that_were_not_handed_in(vod, host, tmp_path):
    """구간을 해석하며 받아 둔 세그먼트는 엔진이 다시 요청하지 않고, 받아 두지 않은 것만 요청해야 한다.

    구간 0.8 ~ 2.3초(세그먼트 0 ~ 2). 해석은 양 끝(0 · 2)과 마지막 세그먼트만 받아 둔다
    -> 엔진 단계: 플레이리스트 0건, 세그먼트 요청 == [1], 키 요청 1회.
       해석과 엔진을 통틀어 같은 세그먼트 요청이 두 번 없다, check.ok
    """
    head = _resolve(host, tmp_path, [FIRST])
    resolved = _segment_requests(host)
    assert head.stored == {0, 2, vod.last}
    host.forget()

    run = _Run(host, tmp_path, [FIRST], head=head).start()

    assert run.failures == []
    assert _playlist_requests(host) == []
    assert _segment_requests(host) == _names({1})
    assert run.key_requests == [KEY_URI]
    everything = resolved + _segment_requests(host)
    assert len(everything) == len(set(everything))
    check = run.checks()[0]
    assert check.ok, check.notes


@pytest.mark.parametrize("damage", ["cut-inside-a-packet", "removed"])
def test_engine_downloads_a_handed_in_segment_again_when_its_file_is_not_whole(
    vod, host, tmp_path, damage
):
    """받아 둔 세그먼트 파일이 온전하지 않으면 엔진은 그 세그먼트를 다시 받아야 한다.

    구간 0.8 ~ 2.3초의 세그먼트 0 ~ 2를 모두 받아 둔 뒤 세그먼트 1의 파일을 망가뜨림 —
    cut-inside-a-packet: 끝 100바이트를 뺌(188의 배수가 아니다) / removed: 파일을 지움
    -> 엔진 단계의 세그먼트 요청 == [1], 키 요청 1회, 실패 0건, check.ok
    """
    head = _resolve(host, tmp_path, [FIRST], everything=True)
    kept = os.path.join(head.segment_dir, "1.ts")
    if damage == "removed":
        os.remove(kept)
    else:
        with open(kept, "rb+") as f:
            f.truncate(os.path.getsize(kept) - 100)
    host.forget()

    run = _Run(host, tmp_path, [FIRST], head=head).start()

    assert run.failures == []
    assert _segment_requests(host) == _names({1})
    assert run.key_requests == [KEY_URI]
    check = run.checks()[0]
    assert check.ok, check.notes


# ================================================================ 받아 둔 세그먼트의 정리


def _failing_cut(*_args, **_kwargs):
    raise CutError(CUT_FAILED, "시험")


def test_engine_keeps_only_the_handed_in_segments_its_sections_use(
    vod, host, tmp_path, monkeypatch
):
    """엔진은 받아 둔 폴더에서 구간에 쓰이는 세그먼트만 남기고, 쓰이지 않는 것과 이 다운로드의 것이 아닌 파일은 지워야 한다.

    구간 2.2 ~ 4.6초(세그먼트 1 ~ 4). 해석이 시각 축을 재려고 받은 첫 · 마지막 세그먼트는 구간에
    쓰이지 않는다. 해석 뒤 폴더에 "old.bin"과, 받아 둔 목록(stored)에 없는 "3.ts"(이전 실행이
    남긴 것의 자리)를 넣음. 컷이 CutError를 내게 해 폴더가 남게 함
    -> 폴더에 "1.ts" ~ "4.ts"만 있다, "3.ts"는 엔진이 다시 받은 것이다(요청 1건, 넣어 둔 내용이 아니다)
    """
    head = _resolve(host, tmp_path, [WIDE])
    assert head.stored == {0, 1, 2, 4, vod.last}
    for name in ("old.bin", "3.ts"):
        with open(os.path.join(head.segment_dir, name), "wb") as f:
            f.write(b"stale")
    monkeypatch.setattr(aes_module, "cut_ts_section", _failing_cut)
    host.forget()

    run = _Run(host, tmp_path, [WIDE], head=head).start()

    assert isinstance(run.failures[0], PostprocessError)
    assert _temp_names(run) == ["1.ts", "2.ts", "3.ts", "4.ts"]
    assert _segment_requests(host) == _names({3})
    with open(os.path.join(head.segment_dir, "3.ts"), "rb") as f:
        assert f.read(5) != b"stale"


@pytest.mark.parametrize("how", ["finished", "cut-failure", "stop-in-cut", "stop-in-transfer"])
def test_received_segments_are_removed_unless_the_cut_failed(vod, host, tmp_path, monkeypatch, how):
    """받은 세그먼트는 완료 · 사용자 중단이면 지워지고, 후처리 실패면 남아야 한다.

    구간 0.8 ~ 2.3초 · 3.4 ~ 5.2초, 해석이 일부를 받아 둔 채 넘김. output_path 자리에 b"keep" 파일을 미리 둠.
    finished: 그대로 / cut-failure: 둘째 컷이 CutError / stop-in-cut: 첫 컷 도중 model.stop() /
    stop-in-transfer: 첫 작업 항목에서 model.stop()
    -> finished: 임시 폴더 없음, 저장 폴더에 구간 파일 둘 + 미리 둔 파일
       cut-failure: 실패 1건(PostprocessError, 원인 CutError), 임시 폴더에 세그먼트 0 ~ 5만(다시 싼 파일 없음),
                    `_1` 파일이 남는다
       stop-*: 완료 0회 · 실패 0건, 임시 폴더 없음, 저장 폴더에 미리 둔 파일뿐
    """
    head = _resolve(host, tmp_path, [FIRST, SECOND])
    run = _Run(host, tmp_path, [FIRST, SECOND], head=head)
    with open(run.data.output_path, "wb") as f:
        f.write(b"keep")
    real_cut = aes_module.cut_ts_section
    real_item = run.engine._download_item
    cuts = []

    def cut(*args, **kwargs):
        cuts.append(args)
        if how == "cut-failure" and len(cuts) == 2:
            raise CutError(CUT_FAILED, "시험")
        if how == "stop-in-cut":
            run.data.model.stop()
        return real_cut(*args, **kwargs)

    def item(*args, **kwargs):
        if how == "stop-in-transfer":
            run.data.model.stop()
        return real_item(*args, **kwargs)

    monkeypatch.setattr(aes_module, "cut_ts_section", cut)
    monkeypatch.setattr(run.engine, "_download_item", item)

    run.start()

    if how == "finished":
        assert (run.finished, run.failures) == (1, [])
        assert not os.path.exists(head.segment_dir)
        assert run.listing() == sorted([*SECTION_FILES, "unused.mp4"])
    elif how == "cut-failure":
        assert run.finished == 0
        assert len(run.failures) == 1
        assert isinstance(run.failures[0], PostprocessError)
        assert isinstance(run.failures[0].__cause__, CutError)
        assert _temp_names(run) == [f"{index}.ts" for index in range(6)]
        assert run.listing() == sorted([SECTION_FILES[0], "unused.mp4"])
    else:
        assert (run.finished, run.failures) == (0, [])
        assert not os.path.exists(head.segment_dir)
        assert run.listing() == ["unused.mp4"]
    with open(run.data.output_path, "rb") as f:
        assert f.read() == b"keep"


def test_remuxed_file_of_a_section_is_in_the_temp_folder_and_gone_before_the_next_cut(
    vod, host, tmp_path, monkeypatch
):
    """구간마다 다시 싼 mp4는 엔진 임시 폴더에 놓이고, 다음 구간의 컷이 시작할 때는 없어야 한다.

    구간 둘. 컷이 불릴 때마다 다시 쌀 경로와, 그때 임시 폴더에 있는 세그먼트가 아닌 이름을 적음
    -> 다시 쌀 경로 == 임시 폴더의 "section_1.mp4" · "section_2.mp4",
       컷이 시작할 때마다 임시 폴더에 세그먼트가 아닌 파일 없음, 저장 폴더에 구간 파일 둘뿐
    """
    real_cut = aes_module.cut_ts_section
    seen = []

    def cut(segment_paths, ts_frames, first_pts, last_pts, output_path, joined_path, **kwargs):
        folder = os.path.dirname(joined_path)
        others = sorted(name for name in os.listdir(folder) if not name.endswith(".ts"))
        seen.append((joined_path, others))
        return real_cut(
            segment_paths, ts_frames, first_pts, last_pts, output_path, joined_path, **kwargs
        )

    monkeypatch.setattr(aes_module, "cut_ts_section", cut)

    run = _Run(host, tmp_path, [FIRST, SECOND]).start()

    assert run.failures == []
    temp = run.engine.temp_dir
    assert seen == [
        (os.path.join(temp, "section_1.mp4"), []),
        (os.path.join(temp, "section_2.mp4"), []),
    ]
    assert run.listing() == SECTION_FILES


# ================================================================ 플레이리스트에 묶기


@pytest.mark.parametrize("ref", ["other-path", "none"])
def test_handed_in_segments_of_another_playlist_are_removed_and_not_used(vod, host, tmp_path, ref):
    """받아 둔 세그먼트가 엔진이 받을 플레이리스트의 것이 아니면 엔진은 그것을 지우고 처음부터 받아야 한다.

    other-path: "low/media.m3u8"로 구간 2.2 ~ 3.6초를 해석해 세그먼트를 모두 받아 둔 뒤 엔진은 "vod/media.m3u8"을 받는다
    none: "vod/media.m3u8"로 해석한 뒤 playlist_ref를 None으로 비움
    -> 받아 둔 폴더가 없다, 엔진 단계: 플레이리스트 "vod/media.m3u8" 1건,
       세그먼트 요청은 vod의 1 ~ 3 · 첫 · 마지막이 하나씩, 키 요청 1회, 실패 0건, check.ok
    """
    source = "low/media.m3u8" if ref == "other-path" else "vod/media.m3u8"
    head = _resolve(host, tmp_path, [MIDDLE], source, everything=True)
    if ref == "none":
        head.playlist_ref = None
    assert os.listdir(head.segment_dir)
    host.forget()

    run = _Run(host, tmp_path, [MIDDLE], head=head).start()

    assert run.failures == []
    assert not os.path.exists(head.segment_dir)
    assert run.engine.temp_dir != head.segment_dir
    assert _playlist_requests(host) == ["vod/media.m3u8"]
    assert _segment_requests(host) == _names({0, 1, 2, 3, vod.last})
    assert run.key_requests == [KEY_URI]
    check = run.checks()[0]
    assert check.ok, check.notes


def test_handed_in_segments_of_the_same_playlist_with_another_query_are_used(vod, host, tmp_path):
    """받아 둔 세그먼트의 플레이리스트가 쿼리만 다른 같은 경로이면 엔진은 그것을 써야 한다.

    "vod/media.m3u8?token=a"로 구간 2.2 ~ 3.6초를 해석해 세그먼트를 모두 받아 둔 뒤 엔진은 "?token=b"를 받는다
    -> 엔진 단계의 호스트 요청 0건, 키 요청 0회, 실패 0건, check.ok
    """
    head = _resolve(host, tmp_path, [MIDDLE], "vod/media.m3u8?token=a", everything=True)
    host.forget()

    run = _Run(host, tmp_path, [MIDDLE], "vod/media.m3u8?token=b", head=head).start()

    assert run.failures == []
    assert host.requests == []
    assert run.key_requests == []
    check = run.checks()[0]
    assert check.ok, check.notes


# ================================================================ 키


def test_wrong_key_fails_at_the_first_segment_the_engine_fetches(vod, host, tmp_path):
    """키가 틀리면 엔진이 처음 받은 세그먼트에서 복호화 오류로 실패하고 아무것도 남기지 않아야 한다.

    넘겨받은 것 없음, 키 리졸버가 다른 키를 줌, 구간 0.8 ~ 2.3초
    -> 실패 1건(DecryptionError), 세그먼트 요청 == [0] 1건, 키 요청 1회, 저장 폴더가 비어 있다
    """
    run = _Run(host, tmp_path, [FIRST], key=WRONG_KEY).start()

    assert run.finished == 0
    assert len(run.failures) == 1
    assert isinstance(run.failures[0], DecryptionError)
    assert _segment_requests(host) == _names({0})
    assert run.key_requests == [KEY_URI]
    assert run.listing() == []


def test_wrong_key_fails_at_the_first_segment_a_worker_fetches(vod, host, tmp_path):
    """구간 해석이 받을 것이 없어 키를 확인하지 못했으면, 작업자가 처음 받은 세그먼트에서 복호화 오류로 실패해야 한다.

    구간 0.8 ~ 2.3초를 맞는 키로 해석해 세그먼트 0 · 2를 받아 둔 채 넘기고, 엔진의 키 리졸버는 다른 키를 줌
    -> 실패 1건(DecryptionError), 세그먼트 1 요청 1건(다시 받지 않는다), 구간 파일 없음, 받아 둔 폴더 없음
    """
    head = _resolve(host, tmp_path, [FIRST])
    host.forget()

    run = _Run(host, tmp_path, [FIRST], head=head, key=WRONG_KEY).start()

    assert run.finished == 0
    assert len(run.failures) == 1
    assert isinstance(run.failures[0], DecryptionError)
    assert _segment_requests(host) == _names({1})
    assert run.listing() == []
    assert not os.path.exists(head.segment_dir)


def test_wrong_key_met_by_several_workers_at_once_is_reported_once(
    vod, host, tmp_path, monkeypatch
):
    """작업자 여럿이 동시에 틀린 키로 복호화에 실패해도 실패 통지는 한 번만 나가야 한다.

    구간 둘을 맞는 키로 해석해 일부만 받아 둔 채 넘기고(받을 세그먼트가 둘 이상 넷 이하),
    엔진의 키 리졸버는 다른 키를 줌. 받을 세그먼트 수만큼의 작업자가 복호화 자리에 모일 때까지
    서로 기다리게 한 뒤 함께 복호화하게 함
    -> 복호화 자리에 닿은 작업자 수 == 받을 세그먼트 수(둘 이상), 실패 1건(DecryptionError),
       "Segment decryption failed" 로그 1건, 구간 파일 없음
    """
    head = _resolve(host, tmp_path, [FIRST, SECOND])
    missing = {0, 1, 2, 3, 4, 5} - head.stored
    assert 2 <= len(missing) <= 4  # 처음 띄우는 작업자 수(4) 안에서 모두 한꺼번에 받는다
    together = threading.Barrier(len(missing))
    reached: list[str] = []
    real_open = aes_module.open_ts_segment

    def decrypt_together(*args, **kwargs):
        reached.append(threading.current_thread().name)
        together.wait(timeout=30)
        return real_open(*args, **kwargs)

    monkeypatch.setattr(aes_module, "open_ts_segment", decrypt_together)

    run = _Run(host, tmp_path, [FIRST, SECOND], head=head, key=WRONG_KEY).start()

    assert len(set(reached)) == len(reached) == len(missing)
    assert len(run.failures) == 1
    assert isinstance(run.failures[0], DecryptionError)
    logged = [args for name, args in run.logger.calls if name == "log_error"]
    assert [args[0] for args in logged] == ["Segment decryption failed"]
    assert run.listing() == []


def _key_forms(key: bytes) -> list[str]:
    """키 값이 글에 섞여 나올 수 있는 모양들."""
    return [key.hex(), key.hex().upper(), repr(key), repr(key)[2:-1], str(list(key))]


def _logged(caplog) -> str:
    """캡처한 로그 레코드 전체를 한 글로 — 메시지 · 인자 · 예외 글."""
    parts = []
    for record in caplog.records:
        parts += [record.getMessage(), repr(record.args), record.exc_text or ""]
        if record.exc_info and record.exc_info[1] is not None:
            parts.append(repr(record.exc_info[1]))
    return "\n".join(parts)


@pytest.mark.parametrize("key", [KEY, WRONG_KEY], ids=["success", "wrong-key"])
def test_key_is_not_left_in_the_log_the_reprs_or_the_failure(vod, host, tmp_path, caplog, key):
    """구간 다운로드가 끝나든 키가 틀려 실패하든, 엔진이 받은 키 값은 로그 · repr · 실패 예외 어디에도 없어야 한다.

    구간 0.8 ~ 2.3초, 넘겨받은 것 없음. 로그는 모든 로거를 DEBUG로 캡처하고 엔진 로거의 호출 인자도 모음
    -> success는 완료 1회 · wrong-key는 실패 1건, 키 요청 1회(엔진이 키를 받았다),
       캡처한 로그 · 엔진 로거의 호출 · repr(Content) · repr(TsHead) · repr(구간 계획) · 실패 예외의 str · repr에
       키의 16진 · bytes 표기 없음 (캡처가 살아 있는지 표식 레코드로 먼저 확인)
    """
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("core.downloaders.hls_aes_downloader").debug("표식")
        run = _Run(host, tmp_path, [FIRST], key=key).start()

    assert "표식" in _logged(caplog)
    assert run.logger.calls  # 엔진이 로거를 불렀다
    assert run.key_requests == [KEY_URI]
    assert (run.finished, len(run.failures)) == ((1, 0) if key == KEY else (0, 1))
    texts = [
        _logged(caplog),
        repr(run.logger.calls),
        repr(run.data.content),
        repr(run.engine._head),
        repr(run.engine.sections),
        *(str(failure) for failure in run.failures),
        *(repr(failure) for failure in run.failures),
    ]
    for text in texts:
        assert not any(form in text for form in _key_forms(key))
    head = run.engine._head
    if head is not None:
        assert all(not isinstance(value, bytes | bytearray) for value in vars(head).values())


# ================================================================ 쓰는 쪽은 한 번에 하나


def test_sections_are_resolved_on_the_run_thread_before_any_worker_starts(
    vod, host, tmp_path, monkeypatch
):
    """구간을 정하려고 세그먼트를 받는 일은 모두 run()을 부른 스레드에서, 첫 작업 항목이 시작하기 전에 끝나야 한다.

    구간 둘, 넘겨받은 것 없음. segment_streams 호출과 작업 항목 시작을 온 순서대로 스레드 이름과 함께 적음.
    첫 작업 항목이 시작할 때의 받아 둔 목록(TsHead.stored · segments)을 적어 둠
    -> 적힌 것의 앞쪽이 모두 "해석"이고 그 뒤가 모두 "항목"이다(둘 다 한 건 이상),
       "해석"의 스레드는 run()을 부른 스레드 하나, "항목"의 스레드 이름은 작업자 풀의 접두사로 시작한다,
       끝난 뒤의 받아 둔 목록이 첫 작업 항목이 시작할 때와 같다
    """
    run = _Run(host, tmp_path, [FIRST, SECOND])
    events: list[tuple[str, str]] = []
    snapshots = []
    lock = threading.Lock()
    real_streams = aes_module.segment_streams
    real_item = run.engine._download_item

    def streams(*args, **kwargs):
        with lock:
            events.append(("해석", threading.current_thread().name))
        return real_streams(*args, **kwargs)

    def item(*args, **kwargs):
        with lock:
            if not snapshots:
                head = run.engine._head
                snapshots.append((set(head.stored), set(head.segments)))
            events.append(("항목", threading.current_thread().name))
        return real_item(*args, **kwargs)

    monkeypatch.setattr(aes_module, "segment_streams", streams)
    monkeypatch.setattr(run.engine, "_download_item", item)

    run.start()

    assert run.failures == []
    kinds = [kind for kind, _thread in events]
    resolved = kinds.count("해석")
    assert resolved >= 1 and kinds.count("항목") >= 1
    assert kinds == ["해석"] * resolved + ["항목"] * (len(kinds) - resolved)
    assert {thread for kind, thread in events if kind == "해석"} == {
        threading.current_thread().name
    }
    assert all(
        thread.startswith(HlsAesDownloader.worker_pool_prefix)
        for kind, thread in events
        if kind == "항목"
    )
    head = run.engine._head
    assert snapshots == [(set(head.stored), set(head.segments))]


def test_workers_write_each_segment_to_its_own_file(vod, host, tmp_path, monkeypatch):
    """작업자는 세그먼트마다 다른 이름의 파일에 한 번씩만 쓰고, 받아 둔 세그먼트의 파일에는 쓰지 않아야 한다.

    구간 0.8 ~ 2.3초 · 3.4 ~ 5.2초(세그먼트 0 ~ 5), 해석이 일부를 받아 둔 채 넘김.
    엔진 모듈이 쓰기로 연 파일의 이름을 적음
    -> 쓰기로 연 이름에 겹치는 것이 없다, 그 이름들 == 받아 두지 않은 구간 세그먼트의 파일 이름,
       받아 둔 세그먼트의 파일 이름과 겹치지 않는다
    """
    head = _resolve(host, tmp_path, [FIRST, SECOND])
    stored = set(head.stored)
    written: list[str] = []
    lock = threading.Lock()
    real_open = open

    def recording_open(path, mode="r", *args, **kwargs):
        if "w" in mode:
            with lock:
                written.append(os.path.basename(path))
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(aes_module, "open", recording_open, raising=False)

    run = _Run(host, tmp_path, [FIRST, SECOND], head=head).start()

    assert run.failures == []
    missing = set(run.wanted()) - stored
    assert missing  # 작업자가 받을 것이 있었다
    assert len(written) == len(set(written))
    assert sorted(written) == [f"{index}.ts" for index in sorted(missing)]
    assert not set(written) & {f"{index}.ts" for index in stored}


# ================================================================ 그 밖의 계약


def test_section_log_reports_cut_and_total_size_of_section_files(vod, host, tmp_path):
    """구간 다운로드의 후처리 로그는 종류 "cut"으로 시작하고, 끝 로그의 크기는 구간 파일 크기의 합이어야 한다.

    구간 둘
    -> log_postprocess_start("cut") 1회, log_postprocess_complete의 크기 == 두 파일 크기의 합
    """
    run = _Run(host, tmp_path, [FIRST, SECOND]).start()

    calls = dict(run.logger.calls)
    assert calls["log_postprocess_start"] == ("cut",)
    assert calls["log_postprocess_complete"][1] == sum(os.path.getsize(p) for p in run.paths)


def test_section_count_that_differs_from_the_path_count_fails(vod, host, tmp_path):
    """구간 수와 산출물 경로 수가 다르면 아무것도 요청하지 않고 실패해야 한다.

    구간 둘에 산출물 경로 하나
    -> 실패 1건(ValueError), 호스트 요청 0건, 키 요청 0회
    """
    run = _Run(host, tmp_path, [FIRST, SECOND])
    run.data.content.selection_paths = run.paths[:1]

    run.start()

    assert len(run.failures) == 1
    assert isinstance(run.failures[0], ValueError)
    assert host.requests == []
    assert run.key_requests == []


def test_second_run_of_the_same_engine_that_fails_leaves_the_first_runs_files(vod, host, tmp_path):
    """같은 엔진을 다시 돌려 두 번째 실행이 실패해도 첫 실행이 만든 구간 파일은 남아야 한다.

    첫 실행: 구간 0.8 ~ 2.3초를 맞는 키로 받아 `_1` 파일을 만듦.
    두 번째 실행: 같은 엔진에 새 산출물 경로를 배정하고 키 리졸버가 다른 키를 주게 함
    -> 두 번째 실행은 실패 1건(DecryptionError), 저장 폴더에 첫 실행의 `_1` 파일 하나뿐이고 내용이 그대로다
    """
    run = _Run(host, tmp_path, [FIRST]).start()
    assert (run.finished, run.failures) == (1, [])
    with open(run.paths[0], "rb") as f:
        made = f.read()
    run.data.content.selection_paths = build_section_output_paths(
        str(run.folder), "구간 시험", 144, 1
    )
    assert run.data.content.selection_paths != run.paths  # 첫 실행의 파일을 덮어쓰지 않는 이름
    run._key = WRONG_KEY

    run.engine.run()

    assert len(run.failures) == 1
    assert isinstance(run.failures[0], DecryptionError)
    assert run.listing() == [SECTION_FILES[0]]
    with open(run.paths[0], "rb") as f:
        assert f.read() == made


def test_download_without_sections_keeps_the_remux_path(vod, host, tmp_path):
    """구간이 없으면 지금처럼 세그먼트 전부를 받아 output_path로 재포장해야 한다.

    selections 빈 튜플
    -> 완료 1회, output_path가 있고, 후처리 종류 "remux", 키 요청 1회,
       세그먼트 요청은 전부가 하나씩 + 키를 확인하는 첫 세그먼트 한 번 더
    """
    run = _Run(host, tmp_path, []).start()

    assert (run.finished, run.failures) == (1, [])
    assert os.path.getsize(run.data.output_path) > 0
    assert dict(run.logger.calls)["log_postprocess_start"] == ("remux",)
    assert run.key_requests == [KEY_URI]
    assert _segment_requests(host) == sorted([*_names(range(vod.last + 1)), *_names({0})])
    assert run.listing() == ["unused.mp4"]
