"""구간 기준값 조회의 단계별 시간 기록 (#309).

조회(`app.section_basis`)가 남기는 로그 한 줄에 무엇이 들어가고 무엇이 들어가지 않는지,
그리고 재는 일이 실패해도 조회가 그대로 끝나는지를 잰다. 네트워크는 타지 않는다.
"""

import logging
from datetime import timedelta
from fractions import Fraction
from types import SimpleNamespace

import pytest

import threading

import app.probe_timing as probe_timing
import app.section_basis as section_basis
import core.api.session as session_module
from app.probe_timing import LOG_PREFIX, ProbeTiming
from app.section_basis import SectionBasis, probe_fmp4, probe_mp4, probe_ts

LOGGER = "app.section_basis"
# 로그에 나오면 안 되는 것 — 주소 · 쿼리의 토큰 · 영상 번호 · 쿠키 · 키 · 제목
SECRETS = {
    "host": "media-secret.invalid",
    "token": "tokenSECRET123",
    "video": "98765432",
    "cookie": "NIDcookieSECRET",
    "key": "4b" * 16,
    "title": "비밀제목",
}
URL = f"https://{SECRETS['host']}/v/{SECRETS['video']}/stream.m3u8?sig={SECRETS['token']}"


def _lines(caplog) -> list[str]:
    """조회가 남긴 시간 기록 줄 — 로거 이름까지 본다."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == LOGGER and record.getMessage().startswith(LOG_PREFIX)
    ]


def _response(length: str | None, ms: float, status: int = 200) -> SimpleNamespace:
    """응답 대역 — 주소 · 쿠키가 든 머리를 일부러 싣는다. 훅은 그것을 읽지 않아야 한다."""
    headers = {"Set-Cookie": SECRETS["cookie"], "X-Title": SECRETS["title"]}
    if length is not None:
        headers["Content-Length"] = length
    return SimpleNamespace(
        url=URL, status_code=status, headers=headers, elapsed=timedelta(milliseconds=ms)
    )


@pytest.fixture
def item() -> SimpleNamespace:
    return SimpleNamespace(
        content_type="m3u8",
        vod_url=f"https://chzzk.naver.com/video/{SECRETS['video']}",
        base_url=URL,
        resolution=1080,
        stream=None,
        title=SECRETS["title"],
    )


@pytest.fixture
def fake_fmp4(monkeypatch):
    """인코딩 전 다시보기의 조회 단계 대역 — 단계마다 응답 훅을 불러 요청을 흉내 낸다."""
    hooks: list = []

    def request(length: str | None, ms: float) -> None:
        for hook in list(hooks):
            hook(_response(length, ms))

    def resolve(content):
        request("1256", 210.0)
        return URL, Fraction(60)

    def fetch_head(url, segment_dir):
        request("2314962", 95.0)
        request(None, 40.0)  # Content-Length가 없는 응답
        playlist = SimpleNamespace(segments=["s"] * 19960)
        return SimpleNamespace(init="init", playlist=playlist)

    def frames(head, url, index):
        request("1580043", 480.0)
        return f"segment{index}"

    def timeline(playlist, init, segment_at):
        segment_at(19959)
        return SimpleNamespace(duration=39922.345, groups=("g",))

    session = SimpleNamespace(hooks={"response": hooks})
    monkeypatch.setattr(probe_timing, "get_thread_session", lambda: session)
    monkeypatch.setattr(section_basis, "resolve_m3u8_variant", resolve)
    monkeypatch.setattr(section_basis, "fetch_fmp4_head", fetch_head)
    monkeypatch.setattr(section_basis, "segment_frames", frames)
    monkeypatch.setattr(
        section_basis,
        "choose_frame_rate",
        lambda init, segments, declared: SimpleNamespace(rate=Fraction(60), source="declared"),
    )
    monkeypatch.setattr(section_basis, "fmp4_timeline", timeline)
    return hooks


def test_replay_lookup_logs_one_line_with_each_stage_and_its_requests(caplog, item, fake_fmp4):
    """다시보기 조회는 단계마다의 시간 · 요청 수 · 응답 크기를 INFO 로그 한 줄로 남겨야 한다.

    변형 해석 1건(1,256B) · 플레이리스트와 초기화 2건(2,314,962B · 크기 모름) · 첫 세그먼트 1건 ·
    끝 세그먼트 1건(각 1,580,043B), 세그먼트 19,960개 · 묶음 1개 · 길이 39922.345초
    -> 줄 하나, "[fmp4]", "요청 5건", "응답이 말한 크기 5,476,304바이트", 단계 이름 다섯,
       "210ms/1,256B", "40ms/?", "세그먼트 19,960개", "묶음 1개", "영상 길이 39922.345초"
    -> 조회가 끝난 뒤 세션에 훅이 남아 있지 않다
    """
    with caplog.at_level(logging.INFO, logger=LOGGER):
        probe = probe_fmp4(item)

    lines = _lines(caplog)
    assert len(lines) == 1, lines
    line = lines[0]
    assert caplog.records[-1].levelno == logging.INFO
    assert "[fmp4]" in line and "요청 5건" in line
    assert "응답이 말한 크기 5,476,304바이트" in line  # 1,256 + 2,314,962 + 1,580,043 × 2
    for stage in ("변형 해석", "플레이리스트 · 초기화 세그먼트", "첫 세그먼트", "프레임률 결정"):
        assert stage in line
    assert "묶음별 끝 세그먼트" in line
    assert "210ms/1,256B" in line and "40ms/?" in line
    assert "세그먼트 19,960개" in line and "묶음 1개" in line
    assert "영상 길이 39922.345초" in line
    assert probe.basis == SectionBasis(fps=Fraction(60), duration=39922.345)
    assert fake_fmp4 == [], "조회가 끝났는데 세션에 훅이 남았다"


def test_the_log_line_carries_no_address_token_cookie_key_or_title(caplog, item, fake_fmp4):
    """시간 기록 줄에는 주소 · 쿼리의 토큰 · 영상 번호 · 쿠키 · 키 · 제목이 없어야 한다.

    주소 · 영상 번호 · 제목이 든 아이템, 주소 · 쿠키 · 제목이 든 응답으로 조회
    -> 캡처가 살아 있다(표식 레코드 확인), 시간 기록 줄 어디에도 위 값이 없다
    """
    with caplog.at_level(logging.INFO, logger=LOGGER):
        logging.getLogger(LOGGER).info("표식")
        probe_fmp4(item)

    assert "표식" in caplog.text, "전제: 로그 캡처가 살아 있어야 한다"
    lines = _lines(caplog)
    assert len(lines) == 1
    for name, secret in SECRETS.items():
        assert secret not in lines[0], f"{name}이(가) 로그 줄에 들어갔다"
    assert "http" not in lines[0] and "?" not in lines[0].replace("ms/?", "")


def test_encrypted_lookup_keeps_the_key_out_of_the_line(caplog, monkeypatch):
    """암호화 VOD 조회의 시간 기록 줄에는 키가 없어야 하고, 단계에 키 · 첫 · 끝 세그먼트가 있어야 한다.

    키 리졸버가 bytes.fromhex("4b" × 16)을 줌
    -> 줄 하나 "[ts]", 단계 "플레이리스트" · "키" · "첫 세그먼트" · "끝 세그먼트", 키의 16진 · bytes 표기 없음
    """
    key = bytes.fromhex(SECRETS["key"])
    head = SimpleNamespace(playlist=SimpleNamespace(segments=["s"] * 3))
    monkeypatch.setattr(section_basis, "fetch_ts_head", lambda url, folder: head)
    monkeypatch.setattr(section_basis, "ts_key_uri", lambda url, head: URL)
    monkeypatch.setattr(section_basis, "resolve_aes_key", lambda content, uri: key)
    monkeypatch.setattr(section_basis, "segment_streams", lambda head, url, index, key: "segment")
    monkeypatch.setattr(
        section_basis,
        "choose_ts_frame_rate",
        lambda segments, declared: SimpleNamespace(rate=Fraction(30), source="declared"),
    )
    monkeypatch.setattr(
        section_basis,
        "ts_timeline",
        lambda playlist, at, fps: SimpleNamespace(duration=12.0, groups=("g",)),
    )
    item = SimpleNamespace(base_url=URL, vod_url=URL, resolution=144)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        probe_ts(item)

    lines = _lines(caplog)
    assert len(lines) == 1 and "[ts]" in lines[0]
    for stage in ("플레이리스트", "키", "첫 세그먼트", "끝 세그먼트"):
        assert stage in lines[0]
    for form in (SECRETS["key"], repr(key), key.hex().upper(), SECRETS["host"], SECRETS["token"]):
        assert form not in lines[0]


def test_mp4_lookup_logs_the_moov_size_and_length(caplog, monkeypatch):
    """인코딩 완료 VOD 조회는 moov 크기 · 프레임 수 · 영상 길이를 줄에 적어야 한다.

    moov가 파일의 32~340627바이트, 프레임 3개, 길이 1234.5초
    -> "[mp4]", "moov 340,596바이트", "프레임 3개", "영상 길이 1234.500초"
    """
    index = SimpleNamespace(
        fps=Fraction(60), duration=1234.5, moov_range=(32, 340627), frame_pts=(0.0, 0.1, 0.2)
    )
    monkeypatch.setattr(section_basis, "fetch_mp4_head", lambda url: SimpleNamespace(index=index))

    with caplog.at_level(logging.INFO, logger=LOGGER):
        probe_mp4(URL)

    line = _lines(caplog)[0]
    assert "[mp4]" in line and "moov 340,596바이트" in line
    assert "프레임 3개" in line and "영상 길이 1234.500초" in line
    assert SECRETS["host"] not in line and SECRETS["token"] not in line


def test_a_failed_lookup_still_logs_and_names_only_the_exception_type(caplog, monkeypatch):
    """조회가 실패해도 줄을 남기되 예외의 이름만 적고, 예외는 그대로 올려야 한다.

    moov 받기가 주소가 든 문구의 RuntimeError를 던짐
    -> RuntimeError가 그대로 올라온다, 줄에 "실패 RuntimeError", 예외 문구의 주소는 없다
    """

    def broken(url):
        raise RuntimeError(f"failed to fetch {url}")

    monkeypatch.setattr(section_basis, "fetch_mp4_head", broken)

    with caplog.at_level(logging.INFO, logger=LOGGER), pytest.raises(RuntimeError):
        probe_mp4(URL)

    line = _lines(caplog)[0]
    assert "실패 RuntimeError" in line
    assert SECRETS["host"] not in line and SECRETS["token"] not in line


def test_lookup_finishes_even_when_timing_itself_breaks(caplog, monkeypatch, item, fake_fmp4):
    """시간을 재는 일이 실패해도 조회는 같은 값을 돌려줘야 한다.

    시계(time.perf_counter)와 로그 내기가 모두 예외를 던지게 둠
    -> 조회 결과 == SectionBasis(60, 39922.345), 예외 없음
    """

    def broken(*args, **kwargs):
        raise OSError("시계 고장(대역)")

    monkeypatch.setattr(probe_timing.time, "perf_counter", broken)
    monkeypatch.setattr(probe_timing.logger, "info", broken)

    probe = probe_fmp4(item)

    assert probe.basis == SectionBasis(fps=Fraction(60), duration=39922.345)


def test_requests_of_other_threads_are_not_counted():
    """공유 세션의 훅은 조회를 돌리는 스레드의 요청만 세야 한다.

    조회 스레드에서 단계 하나를 연 채, 다른 스레드에서 훅을 부름
    -> 그 단계의 요청 0건
    """
    import threading

    timing = ProbeTiming("mp4")
    with timing.stage("단계"):
        worker = threading.Thread(target=lambda: timing._on_response(_response("100", 1.0)))
        worker.start()
        worker.join()
        timing._on_response(_response("200", 2.0))

    assert "요청 1건" in timing.line() and "2ms/200B" in timing.line()


# ================================================================ 훅의 적용 범위


def test_requests_after_the_lookup_are_not_counted_and_the_session_keeps_no_hook(
    caplog, item, fake_fmp4
):
    """조회가 끝난 뒤 같은 세션으로 나간 요청은 세지 않아야 하고, 세션에 훅이 남지 않아야 한다.

    조회 한 번(요청 5건)이 끝난 뒤 같은 세션의 훅 목록으로 응답 하나를 더 흘림
    -> 세션의 훅 목록이 비어 있다, 로그 줄은 하나뿐이고 "요청 5건" 그대로다
    """
    with caplog.at_level(logging.INFO, logger=LOGGER):
        probe_fmp4(item)
        for hook in list(fake_fmp4):  # 엔진이 같은 세션을 다시 쓰는 경우 — 남은 훅이 있으면 불린다
            hook(_response("999999", 1.0))

    assert fake_fmp4 == []
    lines = _lines(caplog)
    assert len(lines) == 1 and "요청 5건" in lines[0]


def test_a_lookup_that_ends_with_an_exception_leaves_no_hook(monkeypatch):
    """예외로 끝난 조회 뒤에도 세션에 훅이 남지 않아야 한다.

    moov 받기가 RuntimeError를 던지는 조회
    -> 예외가 그대로 올라오고, 조회가 도는 동안에는 훅이 하나 있었고, 끝난 뒤 훅 목록이 비어 있다
    """
    hooks: list = []
    during = []

    def broken(url):
        during.append(len(hooks))
        raise RuntimeError("조회 실패(대역)")

    session = SimpleNamespace(hooks={"response": hooks})
    monkeypatch.setattr(probe_timing, "get_thread_session", lambda: session)
    monkeypatch.setattr(section_basis, "fetch_mp4_head", broken)

    with pytest.raises(RuntimeError):
        probe_mp4(URL)

    assert during == [1], "전제: 조회가 도는 동안에는 훅이 걸려 있어야 한다"
    assert hooks == []


def test_the_shared_session_never_gets_a_hook(monkeypatch, item, fake_fmp4):
    """모든 스레드가 함께 쓰는 공유 세션에는 조회가 도는 동안에도 훅을 걸지 않아야 한다.

    조회의 단계(변형 해석 · 첫 세그먼트) 안에서 공유 세션의 훅 목록을 들여다봄
    -> 매번 비어 있다. 조회가 끝난 뒤에도 비어 있다
    """
    shared = session_module._session.hooks["response"]
    assert shared == [], "전제: 공유 세션에 다른 훅이 없어야 한다"
    seen = []
    inner = section_basis.segment_frames

    def watching_frames(head, url, index):
        seen.append(list(shared))
        return inner(head, url, index)

    monkeypatch.setattr(section_basis, "segment_frames", watching_frames)

    probe_fmp4(item)

    assert seen and all(hooks == [] for hooks in seen)
    assert shared == []


def test_two_lookups_running_at_once_do_not_mix_their_counts(caplog, monkeypatch):
    """조회 둘이 동시에 돌아도 요청의 셈이 서로 섞이지 않아야 한다.

    스레드 둘이 각자의 세션으로 mp4 조회를 동시에 돌린다(둘 다 moov 받기 안에 들어온 뒤 진행).
    한쪽은 요청 1건(100B), 다른 쪽은 요청 3건(각 200B)을 보낸다
    -> 로그 줄 둘: 하나는 "요청 1건 · 응답이 말한 크기 100바이트", 하나는 "요청 3건 · … 600바이트"
    -> 끝난 뒤 두 세션 모두 훅이 없다
    """
    sessions: dict[int, SimpleNamespace] = {}
    both_inside = threading.Barrier(2, timeout=5)

    def thread_session():
        return sessions.setdefault(threading.get_ident(), SimpleNamespace(hooks={"response": []}))

    def fetch(url):
        count, size = (1, "100") if url == "one" else (3, "200")
        both_inside.wait()  # 둘 다 조회 안에 들어온 뒤에 요청을 흘린다
        for _ in range(count):
            for hook in list(thread_session().hooks["response"]):
                hook(_response(size, 1.0))
        index = SimpleNamespace(fps=Fraction(60), duration=1.0, moov_range=(0, 9), frame_pts=(0.0,))
        return SimpleNamespace(index=index)

    monkeypatch.setattr(probe_timing, "get_thread_session", thread_session)
    monkeypatch.setattr(section_basis, "fetch_mp4_head", fetch)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        workers = [threading.Thread(target=probe_mp4, args=(name,)) for name in ("one", "three")]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(10)

    lines = sorted(_lines(caplog), key=lambda line: "요청 3건" in line)
    assert len(lines) == 2, lines
    assert "요청 1건" in lines[0] and "응답이 말한 크기 100바이트" in lines[0]
    assert "요청 3건" in lines[1] and "응답이 말한 크기 600바이트" in lines[1]
    assert len(sessions) == 2, "전제: 두 조회가 서로 다른 스레드에서 돌아야 한다"
    assert all(session.hooks["response"] == [] for session in sessions.values())
