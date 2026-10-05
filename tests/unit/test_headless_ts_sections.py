"""헤드리스 스크립트의 암호화 VOD 구간 다운로드(scripts/headless_download.py) (#309).

실제 ffmpeg로 만든 6초짜리 암호화 HLS(MPEG-TS, tests/unit/core/encrypted_hls.py)를 소켓 없는
호스트(tests/unit/core/range_host.py)로 내주고, 헤드리스의 main을 --section과 함께 실제로
돌린다. 조회(치지직 API)와 키 요청은 대역으로 바꾼다 — 엔진 · 구간 해석 · 컷은 실제 코드다.

핵심 계약:
- 암호화 VOD에 --section을 주면 구간 파일이 끝까지 만들어진다. 세그먼트는 한 번씩, 범위
  요청 없이 받고, 키는 구간 해석 한 번 + 엔진 한 번까지만 받는다
- 매니페스트가 고른 해상도에 선언한 프레임률이 있으면 구간 해석은 그 값을 쓴다
- --list는 암호화 VOD의 해상도마다 매니페스트가 선언한 프레임률을 적는다
- 구간 해석이 세그먼트를 받아 두는 폴더는 엔진이 혼자 돌 때 정하는 임시 폴더와 같다
- 키 값은 로그 · 돌려주는 것 어디에도 남지 않는다
"""

import logging
import os
import xml.etree.ElementTree as ET
from fractions import Fraction
from types import SimpleNamespace

import pytest

import core.api.hls_ts as hls_ts_module
import core.downloaders.hls_aes_downloader as aes_module
import core.utils.paths as paths_module
import scripts.headless_download as headless
from core.api.dash import parse_frame_rates, parse_sea_manifest
from core.downloaders.hls_aes_downloader import HlsAesDownloader
from core.models.content import VideoInfo
from core.models.download_data import DownloadData
from core.models.plan import TimeRange
from core.models.ts_index import TsHead
from core.utils.cut_check import check_cut
from core.utils.paths import build_section_output_paths, release_output_paths, temp_dir_for
from tests.unit.core.encrypted_hls import FPS, KEY_EVERY, LEAD, SEGMENT_FRAMES, make_encrypted_hls
from tests.unit.core.range_host import RangeHost

KEY = bytes.fromhex("9a3f5c0e71b2d4486e1d7f20c5a8b693")  # 테스트용 키 — 실제 키가 아니다
WRONG_KEY = bytes.fromhex("11223344556677889900aabbccddeeff")  # 테스트용 — 복호화가 틀어진다
KEY_URI = "https://key.test/k"
VOD_URL = "https://chzzk.naver.com/video/123"
TITLE = "구간 시험"
# 30fps의 타임코드 — 0.8 ~ 2.3초(세그먼트 0 ~ 2) · 3.4 ~ 5.2초(세그먼트 2 ~ 5).
# 양 끝이 키프레임이 아니고 세그먼트 경계를 걸친다
SECTIONS = ["00:00:00:24-00:00:02:09", "00:00:03:12-00:00:05:06"]
SECTION_SECONDS = [(0.8, 2.3), (3.4, 5.2)]
SECTION_FILES = [f"{TITLE} 144p_1.mp4", f"{TITLE} 144p_2.mp4"]
NTSC = Fraction(30000, 1001)  # 잰 값(30)과 다른 선언값 — 선언값이 쓰였는지 가른다


@pytest.fixture(scope="module")
def vod(tmp_path_factory):
    """ffmpeg로 만든 6초짜리 암호화 HLS 하나 — (플레이리스트, 파일 이름 → bytes)."""
    return make_encrypted_hls(tmp_path_factory.mktemp("headless_ts"), KEY, KEY_URI)


@pytest.fixture
def host(vod, monkeypatch) -> RangeHost:
    """vod의 파일을 "vod/" 아래로 내주는 호스트 — 엔진과 구간 해석의 요청이 이 호스트로 간다."""
    _playlist, files = vod
    served = RangeHost({f"vod/{name}": data for name, data in files.items()})
    monkeypatch.setattr(aes_module, "get_thread_session", served.session)
    monkeypatch.setattr(hls_ts_module, "get_thread_session", served.session)
    return served


class _Headless:
    """헤드리스의 main을 대역과 함께 돌린 한 번의 실행과 그 둘레에서 본 것."""

    def __init__(self, monkeypatch, host, tmp_path, rates=None, key=KEY):
        self.folder = tmp_path / "out"
        self.folder.mkdir(exist_ok=True)
        self.base_url = host.url("vod/media.m3u8")
        self.key_requests: list[str] = []  # 키 리졸버가 받은 키 주소
        self.engines: list[HlsAesDownloader] = []  # 만들어진 엔진
        self.heads: list[TsHead] = []  # 구간 해석이 돌려준 것
        self.segment_dirs: list[str] = []  # 구간 해석이 받은 segment_dir
        self.declared: list[Fraction | None] = []  # 구간 해석이 받은 선언 프레임률
        result = (VOD_URL, {"title": TITLE}, [[144, self.base_url]], 144, self.base_url)
        result += (str(self.folder), None)

        def resolve_key(_content, key_uri):
            self.key_requests.append(key_uri)
            return key

        resolve = headless._resolve_ts_sections

        def observed_resolve(item, texts, segment_dir=None, declared=None):
            self.segment_dirs.append(segment_dir)
            self.declared.append(declared)
            resolved = resolve(item, texts, segment_dir, declared)
            if resolved is not None:
                self.heads.append(resolved[1])
            return resolved

        init = HlsAesDownloader.__init__

        def observed_init(engine, *args, **kwargs):
            init(engine, *args, **kwargs)
            self.engines.append(engine)

        monkeypatch.setattr(headless, "setup_logging", lambda level: None)
        monkeypatch.setattr(headless, "_load_cookies", lambda: {})
        monkeypatch.setattr(headless, "_fetch", lambda url, cookies, path: (result, "hls_aes"))
        monkeypatch.setattr(
            headless,
            "_fetch_frame_rates",
            lambda url, cookies, kind: dict(rates or {}),
        )
        monkeypatch.setattr(headless, "resolve_aes_key", resolve_key)
        monkeypatch.setattr(headless, "_resolve_ts_sections", observed_resolve)
        # 임시 폴더는 산출물 폴더에 둔다 — 실제 디스크 속도 재기를 타지 않는다(엔진 쪽은 conftest가 같게 둔다)
        monkeypatch.setattr(headless, "choose_temp_dir", temp_dir_for)
        monkeypatch.setattr(HlsAesDownloader, "__init__", observed_init)
        monkeypatch.setattr(HlsAesDownloader, "_inspect_cuts", True)
        monkeypatch.setattr(HlsAesDownloader, "_slow_speed_threshold_kb_s", 0)

    def main(self, sections) -> int:
        """--section들과 함께 main을 부르고 종료 코드를 돌려준다."""
        argv = [VOD_URL, "--output", str(self.folder), "--timeout", "120"]
        for section in sections:
            argv += ["--section", section]
        return headless.main(argv)

    def listing(self) -> list[str]:
        """저장 폴더에 있는 이름들(오름차순)."""
        return sorted(os.listdir(self.folder))


def _segment_requests(host) -> list[str]:
    """호스트에 온 세그먼트 GET의 이름(오름차순)."""
    return sorted(name for _m, name, _h in host.requests if name.endswith(".ts"))


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


# ================================================================ 끝까지


def test_headless_downloads_sections_of_an_encrypted_vod_to_the_end(
    vod, host, tmp_path, monkeypatch
):
    """암호화 VOD에 --section 둘을 주면 헤드리스는 구간 파일 둘을 끝까지 만들고 0으로 끝나야 한다.

    구간 0.8 ~ 2.3초 · 3.4 ~ 5.2초(30fps의 타임코드), 매니페스트의 선언 프레임률 30
    -> 종료 코드 0, 저장 폴더에 구간 파일 둘뿐, 구간마다 check.ok,
       자른 프레임 == 구간 시각에서 따로 계산한 번호, Range 머리가 든 요청 0건,
       플레이리스트 1건 · 세그먼트 요청은 0 ~ 5와 마지막이 하나씩(해석과 엔진을 통틀어),
       키 요청 2회(구간 해석 1 + 엔진 1), 엔진의 임시 폴더 == 구간 해석이 받아 둔 폴더이고 끝나면 없다
    """
    playlist, _files = vod
    run = _Headless(monkeypatch, host, tmp_path, rates={host.url("vod/media.m3u8"): Fraction(30)})

    code = run.main(SECTIONS)

    assert code == 0
    assert run.listing() == SECTION_FILES
    engine = run.engines[-1]
    assert [(s.first_segment, s.last_segment) for s in engine.sections] == [(0, 2), (2, 5)]
    assert len(engine.cut_results) == 2
    for (start, end), section, frames, result in zip(
        SECTION_SECONDS, engine.sections, engine.cut_frames, engine.cut_results
    ):
        first, last = round((start - LEAD) * FPS), round((end - LEAD) * FPS)
        assert first % KEY_EVERY and last % KEY_EVERY  # 양 끝이 키프레임이 아니다
        base = section.first_segment * SEGMENT_FRAMES  # 다시 싼 mp4의 0번 프레임
        assert (result.plan.first, result.plan.last) == (first - base, last - base)
        check = check_cut(frames, result)
        assert check.ok, check.notes
    assert [header for _m, _n, header in host.requests if header is not None] == []
    assert [name for _m, name, _h in host.requests if ".m3u8" in name] == ["vod/media.m3u8"]
    last = len(playlist.segments) - 1
    assert _segment_requests(host) == [f"vod/segment-{n:06d}.ts" for n in (0, 1, 2, 3, 4, 5, last)]
    assert run.key_requests == [KEY_URI, KEY_URI]
    assert run.heads[0].frame_rate == Fraction(30)
    assert engine.temp_dir == run.segment_dirs[0] == run.heads[0].segment_dir
    assert not os.path.exists(engine.temp_dir)


# ================================================================ 선언 프레임률


@pytest.mark.parametrize(
    ("declared", "rate", "source"),
    [(NTSC, NTSC, "①"), (None, Fraction(30), "②")],
    ids=["declared", "measured-standard"],
)
def test_ts_sections_use_the_declared_frame_rate_when_there_is_one(
    vod, host, tmp_path, monkeypatch, caplog, declared, rate, source
):
    """암호화 VOD의 구간 해석은 선언 프레임률이 있으면 그 값을, 없으면 잰 값을 받은 것에 실어야 한다.

    30fps로 만든 영상, 선언값은 주석의 값(30000/1001 · 없음). --section 00:00:01:00-00:00:01:20
    -> head.frame_rate == 기대값(선언값이 있으면 잰 값 30이 아니라 선언값),
       "프레임률:" 로그에 그 값과 경로 번호, 구간의 끝 == 1 + 20 ÷ 프레임률
    """
    monkeypatch.setattr(headless, "resolve_aes_key", lambda content, key_uri: KEY)
    item = SimpleNamespace(base_url=host.url("vod/media.m3u8"), vod_url=VOD_URL, resolution=144)

    with caplog.at_level(logging.INFO, logger="headless"):
        resolved = headless._resolve_ts_sections(
            item, ["00:00:01:00-00:00:01:20"], str(tmp_path / "segments"), declared
        )

    assert resolved is not None
    selections, head = resolved
    assert head.frame_rate == rate
    assert selections[0].end == pytest.approx(float(1 + Fraction(20) / rate))
    messages = [r.getMessage() for r in caplog.records if r.name == "headless"]
    line = next(message for message in messages if message.startswith("프레임률:"))
    assert str(rate) in line
    assert source in line


def test_headless_hands_the_declared_frame_rate_of_the_picked_resolution_to_the_resolver(
    vod, host, tmp_path, monkeypatch
):
    """헤드리스는 매니페스트의 프레임률 가운데 고른 해상도의 것을 구간 해석에 넘기고, 엔진은 그 값으로 계획해야 한다.

    매니페스트의 프레임률 {고른 해상도의 주소: 30000/1001, 다른 주소: 60}, --section 00:00:01:00-00:00:02:00
    -> 종료 코드 0, 구간 해석이 받은 선언값 == 30000/1001, 넘긴 head.frame_rate == 30000/1001,
       엔진이 구간을 정한 프레임률 == 30000/1001
    """
    rates = {host.url("vod/media.m3u8"): NTSC, host.url("low/media.m3u8"): Fraction(60)}
    run = _Headless(monkeypatch, host, tmp_path, rates=rates)

    code = run.main(["00:00:01:00-00:00:02:00"])

    assert code == 0
    assert run.declared == [NTSC]
    assert run.heads[0].frame_rate == NTSC
    assert run.engines[-1]._frame_rate == NTSC


# ================================================================ --list


def _declared_in_manifest(xml_text: str) -> list[tuple[int, str]]:
    """매니페스트의 암호화 비디오 Representation마다 (짧은 변, frameRate 속성) — 짧은 변 오름차순.

    XML을 직접 읽는다 — 제품의 해석 함수를 거치지 않은 기대값이다.
    """
    found = []
    for adaptation in ET.fromstring(xml_text).iter("{urn:mpeg:dash:schema:mpd:2011}AdaptationSet"):
        for rep in adaptation.iter("{urn:mpeg:dash:schema:mpd:2011}Representation"):
            if rep.find("{urn:mpeg:dash:schema:mpd:2011}ContentProtection") is None:
                continue
            short = min(int(rep.get("width")), int(rep.get("height")))
            found.append((short, rep.get("frameRate") or adaptation.get("frameRate")))
    return sorted(found)


@pytest.mark.parametrize(
    "fixture_name", ["dash_manifest_sea_13714380.xml", "dash_manifest_sea_14283698.xml"]
)
def test_list_option_shows_the_declared_frame_rate_of_each_encrypted_resolution(
    monkeypatch, tmp_path, caplog, load_mock_response, fixture_name
):
    """--list는 암호화 VOD의 해상도마다 매니페스트가 선언한 프레임률을 적어야 한다.

    박제한 SEA 매니페스트(해상도 144 · 720 · 1080, frameRate가 정수로 적혀 있다).
    조회 결과의 해상도 목록과 프레임률 조회가 그 매니페스트에서 나온다
    -> 종료 코드 0, "사용 가능한 해상도:" 줄 == 해상도마다 "<짧은 변>p · <frameRate>fps", "fps 모름" 없음
    """
    xml_text = load_mock_response(fixture_name)
    reps, resolution, base_url = parse_sea_manifest(xml_text)
    result = (VOD_URL, {"title": TITLE}, reps, resolution, base_url, str(tmp_path), None)
    info = VideoInfo(
        video_id="vid",
        in_key="key",
        adult=False,
        vod_status=None,
        live_rewind_playback_json=None,
        membership_benefit_type=None,
        encryption_type=None,
        metadata={},
    )
    asked = []

    def frame_rates(video_id, in_key, cookies):
        asked.append((video_id, in_key))
        return parse_frame_rates(xml_text)

    monkeypatch.setattr(headless, "setup_logging", lambda level: None)
    monkeypatch.setattr(headless, "_load_cookies", lambda: {})
    monkeypatch.setattr(headless, "_fetch", lambda url, cookies, path: (result, "hls_aes"))
    monkeypatch.setattr(headless.NetworkManager, "get_video_info", lambda no, cookies: info)
    monkeypatch.setattr(headless.NetworkManager, "get_video_frame_rates", frame_rates)

    with caplog.at_level(logging.INFO, logger="headless"):
        code = headless.main([VOD_URL, "--list", "--output", str(tmp_path)])

    declared = _declared_in_manifest(xml_text)
    assert [short for short, _rate in declared] == [144, 720, 1080]
    assert all(rate and rate.isdigit() for _short, rate in declared)
    expected = ", ".join(f"{short}p · {rate}fps" for short, rate in declared)
    assert code == 0
    assert asked == [("vid", "key")]
    messages = [r.getMessage() for r in caplog.records if r.name == "headless"]
    assert f"사용 가능한 해상도: {expected}" in messages
    assert "fps 모름" not in "\n".join(messages)


# ================================================================ 임시 폴더


@pytest.mark.parametrize("separated", [True, False], ids=["other-volume", "same-folder"])
def test_headless_segment_folder_is_the_folder_an_engine_alone_would_choose(
    monkeypatch, tmp_path, separated
):
    """헤드리스가 구간 해석에 주는 segment_dir은 같은 조건에서 엔진이 혼자 돌 때 정하는 임시 폴더와 같아야 한다.

    임시 폴더 선택(choose_temp_dir)을 실제 함수로 두고 그 판단의 재료만 정함 —
    other-volume: 스크래치 폴더가 다른 볼륨이고 산출물 폴더보다 10배 빠르다(분리한다)
    same-folder: 스크래치 폴더가 산출물 폴더와 같은 볼륨이다(분리하지 않는다)
    -> 헤드리스의 segment_dir == 구간과 같은 산출물 경로를 받은 엔진의 temp_dir,
       other-volume이면 그 폴더가 스크래치 폴더 아래, same-folder면 저장 폴더 아래
    """
    out = tmp_path / "out"
    out.mkdir()
    scratch = str(tmp_path / "scratch")
    monkeypatch.setattr(aes_module, "choose_temp_dir", paths_module.choose_temp_dir)
    monkeypatch.setattr(paths_module, "_scratch_base_dir", lambda: scratch)
    monkeypatch.setattr(paths_module, "_same_volume", lambda a, b: not separated)
    monkeypatch.setattr(
        paths_module, "measure_write_speed", lambda folder: 100.0 if folder == scratch else 10.0
    )
    monkeypatch.setattr(
        paths_module.shutil, "disk_usage", lambda folder: SimpleNamespace(free=10**13)
    )
    result = (VOD_URL, {"title": TITLE}, [[144, "u144"]], 144, "u144", str(out), None)
    given = []

    def resolver(item, texts, segment_dir=None, declared=None):
        given.append(segment_dir)
        return None  # 여기서 멈춘다 — 폴더만 본다

    monkeypatch.setattr(headless, "setup_logging", lambda level: None)
    monkeypatch.setattr(headless, "_load_cookies", lambda: {})
    monkeypatch.setattr(headless, "_fetch", lambda url, cookies, path: (result, "hls_aes"))
    monkeypatch.setattr(headless, "_fetch_frame_rates", lambda url, cookies, kind: {})
    monkeypatch.setattr(headless, "_resolve_ts_sections", resolver)

    code = headless.main([VOD_URL, "--output", str(out), "--section", "00:00:01:00-00:00:02:00"])
    data = DownloadData(
        base_url="u144",
        vod_url=VOD_URL,
        output_path=str(out / "unused.mp4"),
        resolution=144,
        content_type="hls_aes",
    )
    data.content.selections = (TimeRange(1.0, 2.0),)
    data.content.selection_paths = build_section_output_paths(str(out), TITLE, 144, 1)
    release_output_paths(data.content.selection_paths)
    engine = HlsAesDownloader(data, SimpleNamespace())

    assert code == 2
    assert os.path.basename(data.content.selection_paths[0]) == SECTION_FILES[0]
    assert given == [engine.temp_dir]
    assert os.path.dirname(given[0]) == (scratch if separated else str(out))


def test_failed_ts_resolution_releases_names_and_removes_the_segment_folder(monkeypatch, tmp_path):
    """암호화 VOD의 구간 해석이 실패하면 배정한 구간 파일명을 풀고, 세그먼트를 받아 둔 폴더를 지우고, 2로 끝나야 한다.

    구간 해석 대역이 넘겨받은 폴더에 파일 하나를 쓰고 None을 돌려줌(해석 실패)
    -> 종료 코드 2, 그 폴더가 없다, 같은 이름을 다시 배정받을 수 있다(`_1`)
    """
    result = (VOD_URL, {"title": TITLE}, [[144, "u144"]], 144, "u144", str(tmp_path), None)
    monkeypatch.setattr(headless, "setup_logging", lambda level: None)
    monkeypatch.setattr(headless, "_load_cookies", lambda: {})
    monkeypatch.setattr(headless, "_fetch", lambda url, cookies, path: (result, "hls_aes"))
    monkeypatch.setattr(headless, "_fetch_frame_rates", lambda url, cookies, kind: {})
    monkeypatch.setattr(headless, "choose_temp_dir", temp_dir_for)
    folders = []

    def failing(item, texts, segment_dir=None, declared=None):
        os.makedirs(segment_dir)
        with open(os.path.join(segment_dir, "0.ts"), "wb") as f:
            f.write(b"segment")
        folders.append(segment_dir)
        return None

    monkeypatch.setattr(headless, "_resolve_ts_sections", failing)

    code = headless.main(
        [VOD_URL, "--output", str(tmp_path), "--section", "00:00:01:00-00:00:02:00"]
    )

    assert code == 2
    assert len(folders) == 1 and not os.path.exists(folders[0])
    again = build_section_output_paths(str(tmp_path), TITLE, 144, 1)
    release_output_paths(again)
    assert os.path.basename(again[0]) == SECTION_FILES[0]


# ================================================================ 키


@pytest.mark.parametrize("key", [KEY, WRONG_KEY], ids=["success", "wrong-key"])
def test_key_is_not_left_in_the_log_or_in_what_the_resolver_returns(
    vod, host, tmp_path, monkeypatch, caplog, key
):
    """암호화 VOD의 구간 해석이 끝나든 키가 틀려 실패하든, 받은 키 값은 로그와 돌려주는 것 어디에도 없어야 한다.

    --section 00:00:01:00-00:00:02:00, 키 리졸버가 맞는 키 · 다른 키를 줌. 로그는 모든 로거를 DEBUG로 캡처
    -> success는 (구간, TsHead)를 돌려주고 wrong-key는 None, 키 요청 1회,
       캡처한 로그 · repr(돌려준 것)에 키의 16진 · bytes 표기 없음
       (캡처가 살아 있는지 표식 레코드로 먼저 확인), TsHead에 bytes 값이 없다
    """
    asked = []

    def resolve_key(_content, key_uri):
        asked.append(key_uri)
        return key

    monkeypatch.setattr(headless, "resolve_aes_key", resolve_key)
    item = SimpleNamespace(base_url=host.url("vod/media.m3u8"), vod_url=VOD_URL, resolution=144)

    with caplog.at_level(logging.DEBUG):
        logging.getLogger("headless").debug("표식")
        resolved = headless._resolve_ts_sections(
            item, ["00:00:01:00-00:00:02:00"], str(tmp_path / "segments"), Fraction(30)
        )

    assert "표식" in _logged(caplog)
    assert asked == [KEY_URI]
    assert (resolved is not None) == (key == KEY)
    for text in (_logged(caplog), repr(resolved)):
        assert not any(form in text for form in _key_forms(key))
    if resolved is not None:
        assert all(not isinstance(value, bytes | bytearray) for value in vars(resolved[1]).values())


def test_runner_hands_the_ts_head_to_the_engine(monkeypatch, tmp_path):
    """러너는 구간을 해석하며 받은 TsHead를 제출하는 Content에 실어야 한다.

    구간 하나와 TsHead 대역을 받은 러너, DownloadService · 로거 · 태스크는 대역
    -> 제출된 content의 ts_head is 넘긴 대역, selections == 구간, mp4_head · fmp4_head는 None
    """
    submitted = []

    class FakeService:
        def __init__(self, **kwargs):
            pass

        def submit(self, content, **kwargs):
            submitted.append(content)
            return SimpleNamespace(wait=lambda timeout=None: True)

    monkeypatch.setattr(headless, "DownloadService", FakeService)
    monkeypatch.setattr(headless, "DownloadLogger", lambda: SimpleNamespace())
    monkeypatch.setattr(
        headless, "DownloadTask", lambda data, item, log: SimpleNamespace(start=lambda: None)
    )
    item = SimpleNamespace(
        base_url="https://example.invalid/media.m3u8",
        vod_url=VOD_URL,
        output_path=str(tmp_path / f"{TITLE} 144p.mp4"),
        resolution=144,
        content_type="hls_aes",
        title=TITLE,
        download_path=str(tmp_path),
    )
    selections = (TimeRange(1.0, 2.0),)
    handed = SimpleNamespace(playlist=None)

    runner = headless._HeadlessRunner(item, 60, selections, ts_head=handed)
    runner.run()
    release_output_paths(runner.section_paths)

    assert len(submitted) == 1
    assert submitted[0].ts_head is handed
    assert submitted[0].selections == selections
    assert (submitted[0].mp4_head, submitted[0].fmp4_head) == (None, None)
