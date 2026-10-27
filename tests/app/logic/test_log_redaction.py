"""로그에 요청 주소의 서명 · 토큰 값이 남지 않는다 (#328).

앱 로그 · 콘솔(루트 로거의 핸들러)과 다운로드 로그(DownloadLogger의 파일 핸들러)가 실제로 쓴 글을
읽어 잰다 — caplog는 자기 포매터로 받으므로 핸들러의 가리기를 재지 못한다. 주소는 모두 쓰지 않는
호스트(.invalid)이고 연결은 하지 않는다.
"""

import io
import logging
import re
import subprocess
import sys
import threading
import types

import pytest
import requests
import urllib3

import core.downloaders.base as base_module
import core.downloaders.m3u8_downloader as m3u8_module
import core.utils.ffmpeg as ffmpeg_module
import core.utils.logging_setup as logging_setup
from app.download_logger import DownloadLogger
from core.downloaders.m3u8_downloader import M3U8Downloader
from core.models.download_data import DownloadData
from tests.core.downloaders.test_m3u8_downloader_run import (
    CHUNK,
    INIT_CONTENT,
    SEGMENT_COUNT,
    _passthrough_remux_stream,
    _segment_content,
)

# 가려야 하는 값 — 로그 어디에도 이 글자가 남으면 안 된다
PATH_TOKEN = "PATHSECRET"  # 경로에 든 인증 토큰(CDN의 hdntl=…)
QUERY_TOKEN = "QUERYSECRET"  # 플레이리스트 주소의 쿼리 서명
SEGMENT_TOKEN = "SEGSECRET"  # 플레이리스트가 적은 세그먼트 주소의 쿼리
SECRETS = (PATH_TOKEN, QUERY_TOKEN, SEGMENT_TOKEN)

BASE_URL = f"https://cdn.invalid/video/hdntl=exp~{PATH_TOKEN}/chunklist.m3u8?sig={QUERY_TOKEN}"
SEGMENT_URL = f"https://cdn.invalid/video/hdntl=exp~{PATH_TOKEN}/seg_0.m4v?token={SEGMENT_TOKEN}"
# 가린 뒤에 남아야 하는 모양 — 호스트와 경로는 읽힌다
SEGMENT_URL_REDACTED = "https://cdn.invalid/video/hdntl=***/seg_0.m4v?token=***"


@pytest.fixture
def app_log(monkeypatch, tmp_path):
    """앱의 setup_logging으로 루트 로거에 파일 · 콘솔 핸들러를 걸고, 끝나면 되돌린다.

    콘솔 핸들러의 스트림만 메모리로 바꾼다 — 핸들러와 포매터는 앱이 만든 그대로다. pytest의
    출력 캡처는 단계가 끝나면 닫히므로 그 스트림을 붙잡게 두지 않는다.

    Returns:
        (앱 로그 파일의 글, 콘솔에 나간 글)을 돌려주는 함수
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    monkeypatch.setattr(logging_setup, "_configured", False)
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    log_dir = tmp_path / "logs"
    logging_setup.setup_logging(str(log_dir))
    console = io.StringIO()
    (console_handler,) = [
        handler
        for handler in root.handlers
        if isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.FileHandler)
    ]
    console_handler.setStream(console)

    def read() -> tuple[str, str]:
        for handler in logging.getLogger().handlers:
            handler.flush()
        written = (log_dir / logging_setup.LOG_FILE_NAME).read_text(encoding="utf-8")
        return written, console.getvalue()

    yield read

    for handler in root.handlers[:]:
        handler.close()
        root.removeHandler(handler)
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)


def _http_error(url: str) -> requests.HTTPError:
    """requests가 403 응답에서 내는 HTTPError — 글은 requests가 만든다(주소가 쿼리까지 들어간다)."""
    response = requests.Response()
    response.status_code = 403
    response.reason = "Forbidden"
    response.url = url
    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        return error
    raise AssertionError("raise_for_status가 예외를 내지 않았다")


def _connection_error(url: str) -> requests.ConnectionError:
    """연결 실패 때 requests가 내는 예외 — 글은 urllib3가 만든다(스킴 없이 "url: /경로?쿼리")."""
    path = url.split("cdn.invalid", 1)[1]
    pool = urllib3.HTTPSConnectionPool("cdn.invalid", 443)  # 만들기만 한다 — 연결하지 않는다
    reason = urllib3.exceptions.NewConnectionError(None, "Failed to establish a new connection")
    return requests.ConnectionError(urllib3.exceptions.MaxRetryError(pool, path, reason))


def _assert_no_secret(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text


# ================================================================ 앱 로그 · 콘솔


def test_app_log_and_console_hide_signatures_in_messages_exceptions_and_tracebacks(app_log):
    """앱 로그 파일과 콘솔에는 주소의 서명 · 토큰 값이 남지 않고 호스트 · 경로는 남아야 한다.

    모듈 로거로 주소를 적은 줄 · requests의 HTTPError 글 · 연결 실패 글 · HTTPError의 트레이스백을 남김
    -> 앱 로그와 콘솔 어디에도 세 토큰이 없다, 가린 세그먼트 주소("…/hdntl=***/seg_0.m4v?token=***")와
       연결 실패의 경로("url: /video/hdntl=***/seg_0.m4v?token=***")가 있다
    """
    log = logging.getLogger("test.redaction")

    log.info("요청: %s", SEGMENT_URL)
    log.error("실패: %s", _http_error(SEGMENT_URL))
    log.error("실패: %s", _connection_error(SEGMENT_URL))
    try:
        raise _http_error(SEGMENT_URL)
    except requests.HTTPError:
        log.exception("트레이스백")

    for text in app_log():
        _assert_no_secret(text)
        assert text.count(SEGMENT_URL_REDACTED) == 3  # 요청 줄 · HTTPError 줄 · 트레이스백
        assert "url: /video/hdntl=***/seg_0.m4v?token=***" in text
        assert "Traceback (most recent call last)" in text


# ================================================================ 다운로드 로그 — 엔진 끝까지


class _Response:
    """가짜 응답 — 본문을 조각으로 흘리고, 상태가 오류면 raise_for_status가 requests의 예외를 낸다."""

    def __init__(self, url: str, body: bytes = b"", text: str = "", status: int = 200):
        self.url = url
        self.content = body
        self.text = text
        self._status = status
        self.headers = {}

    def raise_for_status(self):
        if self._status >= 400:
            raise _http_error(self.url)

    def iter_content(self, chunk_size=CHUNK):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i : i + chunk_size]


class _Session:
    """플레이리스트 · 초기화 · 세그먼트 요청을 주소로 가르는 가짜 세션.

    플레이리스트의 세그먼트 · 초기화 주소에는 쿼리 토큰이 붙어 있다. fail_playlist면 플레이리스트가
    403이다. 그 밖에는 첫 세그먼트의 첫 요청만 403이고 다시 받으면 온다.
    """

    def __init__(self, fail_playlist: bool = False):
        self._fail_playlist = fail_playlist
        self._first_segment_failed = False
        self._lock = threading.Lock()

    def get(self, url, **kwargs):
        if "chunklist.m3u8" in url:
            lines = [
                "#EXTM3U",
                "#EXT-X-VERSION:7",
                f'#EXT-X-MAP:URI="init.m4s?token={SEGMENT_TOKEN}"',
            ]
            for index in range(SEGMENT_COUNT):
                lines += ["#EXTINF:2.000,", f"seg_{index}.m4v?token={SEGMENT_TOKEN}"]
            lines.append("#EXT-X-ENDLIST")
            return _Response(url, text="\n".join(lines), status=403 if self._fail_playlist else 200)
        if "init.m4s" in url:
            return _Response(url, body=INIT_CONTENT)
        index = int(re.search(r"seg_(\d+)\.m4v", url).group(1))
        with self._lock:
            fail = index == 0 and not self._first_segment_failed
            self._first_segment_failed = self._first_segment_failed or index == 0
        if fail:
            return _Response(url, status=403)
        return _Response(url, body=_segment_content(index, chunks_per_segment=3))


def _run_engine(tmp_path, monkeypatch, session: _Session) -> tuple[str, list]:
    """DEBUG 레벨의 DownloadLogger로 m3u8 엔진을 끝까지 돌리고 다운로드 로그 파일의 글을 돌려준다."""
    monkeypatch.setattr(m3u8_module, "get_thread_session", lambda: session)
    monkeypatch.setattr(base_module, "remux_stream", _passthrough_remux_stream)
    data = DownloadData(
        base_url=BASE_URL,
        vod_url="https://chzzk.naver.com/video/1",
        output_path=str(tmp_path / "out.mp4"),
        resolution=1080,
        content_type="m3u8",
    )
    logger = DownloadLogger(logging.DEBUG)
    log_file = logger.log_file
    failures: list = []
    finished = threading.Event()

    def on_finished():
        data.model.finish()
        finished.set()

    engine = M3U8Downloader(data, logger, on_finished=on_finished, on_failed=failures.append)
    data.model.start()
    engine.run()
    with open(log_file, encoding="utf-8") as f:
        return f.read(), failures


def test_download_log_hides_signatures_on_a_retried_segment_and_on_success(
    app_log, tmp_path, monkeypatch
):
    """세그먼트가 403으로 한 번 실패해도 다운로드 로그 · 앱 로그에 서명 · 토큰 값이 남지 않아야 한다.

    플레이리스트 주소: 경로 토큰 · 쿼리 서명, 세그먼트 · 초기화 주소: 쿼리 토큰, 첫 세그먼트의 첫 요청만 403,
    DownloadLogger는 DEBUG(세그먼트마다 주소 줄을 적는다)
    -> 다운로드가 끝난다, 두 로그 어디에도 세 토큰이 없다, 실패 줄에 가린 세그먼트 주소가 있다,
       세그먼트 주소 줄은 "Segment URL: seg_N.m4v?token=***", 요약 스크립트가 읽는 완료 줄은 그대로다
    """
    written, failures = _run_engine(tmp_path, monkeypatch, _Session())

    assert failures == []
    for text in (written, *app_log()):
        _assert_no_secret(text)
    assert (
        f"download failed (segment 1) - Exception: 403 Client Error: Forbidden for url: {SEGMENT_URL_REDACTED}"
        in written
    )
    assert "Segment URL: seg_1.m4v?token=***" in written
    assert re.search(r"Download completed in [\d.]+ seconds", written)
    assert re.search(r"Transfer completed in [\d.]+ seconds - Bytes: \d+ - Retries: \d+", written)


def test_download_log_hides_signatures_in_the_traceback_of_a_failed_download(
    app_log, tmp_path, monkeypatch
):
    """플레이리스트 요청이 403으로 실패하면 남는 트레이스백에도 서명 · 토큰 값이 없어야 한다.

    플레이리스트 403 → 엔진이 "Download failed"를 트레이스백과 함께 적음
    -> 실패 콜백 1회, 두 로그 어디에도 세 토큰이 없다, 트레이스백 안의 주소는 호스트 · 경로가 남은 채 가려져 있다
    """
    written, failures = _run_engine(tmp_path, monkeypatch, _Session(fail_playlist=True))

    assert len(failures) == 1
    for text in (written, *app_log()):
        _assert_no_secret(text)
    assert "Traceback (most recent call last)" in written
    assert "for url: https://cdn.invalid/video/hdntl=***/chunklist.m3u8?sig=***" in written


# ================================================================ 홈 폴더


WINDOWS_HOME = r"C:\Users\Tester"  # 가짜 Windows 홈 — 사용자 이름 "Tester"가 로그에 남으면 안 된다
# 홈 아래의 저장 경로 — 폴더 이름에 U+00A0(공백 비슷한 글자)이 있다. repr은 이를 \xa0로 적는다 (#148)
SAVED = WINDOWS_HOME + "\\Videos\\영상\u00a0폴더\\제목 1080p.mp4"


def test_app_log_and_console_replace_the_windows_home_with_a_tilde(app_log, monkeypatch):
    r"""앱 로그와 콘솔은 Windows 홈 폴더 경로를 ~로 바꾸고 그 뒤의 경로 repr은 그대로 둬야 한다.

    홈 C:\Users\Tester. 경로 repr 한 줄 · 소문자와 / 구분자로 적은 경로 한 줄 · 다른 사용자(Tester2)의
    경로 한 줄 · 홈 아래 파일의 FileNotFoundError 트레이스백을 남김
    -> 홈 경로가 남지 않는다, repr은 '~\\Videos\\영상\xa0폴더\\제목 1080p.mp4',
       / 경로는 ~/Downloads/a.mp4, Tester2의 경로는 그대로, 트레이스백의 경로도 ~로 시작한다
    """
    monkeypatch.setattr(logging_setup, "_home_dir", lambda: WINDOWS_HOME)
    log = logging.getLogger("test.redaction")

    log.info("저장 경로: %r", SAVED)
    log.info("소문자 경로: %s", "c:/users/tester/Downloads/a.mp4")
    log.info("다른 사용자: %s", r"C:\Users\Tester2\a.mp4")
    try:
        raise FileNotFoundError(2, "No such file or directory", WINDOWS_HOME + r"\Downloads\b.mp4")
    except FileNotFoundError:
        log.exception("파일 없음")

    for text in app_log():
        assert r"Tester\\" not in text and "Tester'" not in text and "tester/" not in text
        assert r"저장 경로: '~\\Videos\\영상\xa0폴더\\제목 1080p.mp4'" in text
        assert "소문자 경로: ~/Downloads/a.mp4" in text
        assert r"다른 사용자: C:\Users\Tester2\a.mp4" in text
        assert r"'~\\Downloads\\b.mp4'" in text  # 트레이스백 끝줄의 예외 글(경로는 repr)


def test_posix_home_is_replaced_case_sensitively_and_only_as_a_whole_folder(app_log, monkeypatch):
    """macOS · Linux 홈은 대소문자를 가려 바꾸고, 이름이 이어지는 다른 폴더는 바꾸지 않아야 한다.

    홈 /Users/tester. /Users/tester/Movies/a.mp4 · /Users/tester2/x · /Users/TESTER/Movies를 적음
    -> ~/Movies/a.mp4, /Users/tester2/x와 /Users/TESTER/Movies는 그대로
    """
    monkeypatch.setattr(logging_setup, "_home_dir", lambda: "/Users/tester")

    logging.getLogger("test.redaction").info(
        "경로: %s %s %s", "/Users/tester/Movies/a.mp4", "/Users/tester2/x", "/Users/TESTER/Movies"
    )

    for text in app_log():
        assert "경로: ~/Movies/a.mp4 /Users/tester2/x /Users/TESTER/Movies" in text


def test_download_log_header_keeps_title_and_channel_and_hides_the_home(app_log, monkeypatch):
    r"""다운로드 로그의 시작 정보는 제목 · 채널 줄을 그대로 두고 경로 줄의 홈 폴더만 ~로 바꿔야 한다.

    홈 C:\Users\Tester, 제목 "제목" · 채널 "채널", 산출물 SAVED, 저장 폴더 홈\Videos
    -> "title: 제목" · "channel_name: 채널"이 있다, output_path · download_path 줄은 ~로 시작하고 repr 그대로,
       다운로드 로그와 앱 로그 어디에도 "Tester"가 없다
    """
    monkeypatch.setattr(logging_setup, "_home_dir", lambda: WINDOWS_HOME)
    logger = DownloadLogger()
    item = types.SimpleNamespace(
        content_type="video",
        title="제목",
        channel_name="채널",
        live_open_date="",
        duration=60,
        resolution=1080,
        total_size=0,
        output_path=SAVED,
        download_path=WINDOWS_HOME + r"\Videos",
    )

    logger.log_download_info(item)
    with open(logger.log_file, encoding="utf-8") as f:
        written = f.read()
    logger.save_and_close()

    assert "title: 제목" in written and "channel_name: 채널" in written
    assert r"output_path: '~\\Videos\\영상\xa0폴더\\제목 1080p.mp4'" in written
    assert r"download_path: '~\\Videos'" in written
    for text in (written, *app_log()):
        assert "Tester" not in text


# ================================================================ 홈 경로 조각 (#397)


@pytest.mark.parametrize(
    ("home", "text", "expected"),
    [
        # 드라이브 문자가 잘린 조각
        (WINDOWS_HOME, r":\Users\Tester\Videos\a.mp4", r"~\Videos\a.mp4"),
        (WINDOWS_HOME, r"\Users\Tester\Videos\a.mp4", r"~\Videos\a.mp4"),
        (WINDOWS_HOME, r"Users\Tester\Videos\a.mp4", r"~\Videos\a.mp4"),
        # 구분자 방향 · repr의 이스케이프 · 대소문자
        (WINDOWS_HOME, ":/Users/Tester/a.mp4", "~/a.mp4"),
        (WINDOWS_HOME, r"\Users\Tester\a.mp4", r"~\a.mp4"),
        (WINDOWS_HOME, "users/tester/a.mp4", "~/a.mp4"),
        # 다른 드라이브
        (WINDOWS_HOME, r"D:\Users\Tester\a.mp4", r"~\a.mp4"),
        # macOS · Linux — 앞 폴더가 붙은 조각 · 앞의 /가 빠진 조각
        ("/home/tester", "x/home/tester/a.mp4", "x/~/a.mp4"),
        ("/home/tester", "home/tester/a.mp4", "~/a.mp4"),
        ("/Users/tester", "Users/tester/a.mp4", "~/a.mp4"),
    ],
)
def test_home_fragment_without_the_drive_or_the_leading_folders_is_replaced(home, text, expected):
    r"""홈의 끝 두 조각(Users\<이름> · home/<이름>)이 경로 조각으로 나오면 앞에 무엇이 있든 ~로 바뀌어야 한다 (#397).

    홈과 글 -> 바꾼 글 == expected
    """
    assert logging_setup.redact_home(text, home) == expected


@pytest.mark.parametrize(
    ("home", "text"),
    [
        (WINDOWS_HOME, r"\Users\Tester2\a.mp4"),  # 다른 사용자(이름이 이어진다)
        (WINDOWS_HOME, r"MyUsers\Tester\a.mp4"),  # 앞 조각이 Users가 아니다
        (WINDOWS_HOME, r"\Users\Testers"),  # 다른 사용자(끝에서 이어진다)
        (WINDOWS_HOME, "Users Tester"),  # 경로가 아닌 곳의 같은 글자
        (WINDOWS_HOME, "Tester"),  # 이름만
        ("/home/tester", "/home/TESTER/a.mp4"),  # POSIX는 대소문자를 가린다
        ("/home/tester", "home tester"),  # 경로가 아닌 곳의 같은 글자
        ("/home/tester", "/home/tester2/a.mp4"),  # 다른 사용자
    ],
)
def test_same_letters_outside_a_home_fragment_are_kept(home, text):
    """홈 조각이 아닌 곳의 같은 글자 · 다른 사용자의 경로는 그대로여야 한다 (#397).

    홈과 글 -> 바꾼 글 == 원래 글
    """
    assert logging_setup.redact_home(text, home) == text


@pytest.mark.parametrize(
    ("home", "text", "expected"),
    [
        # faulthandler의 파일 이름 줄 — 이름의 비ASCII 글자가 \uNNNN으로 적힌다
        (
            "C:\\Users\\이동",
            r'  File "C:\Users\\uc774\ub3d9\crash.py", line 4',
            r'  File "~\crash.py", line 4',
        ),
        ("C:\\Users\\이동", r"D:\Users\\uc774\ub3d9\a.py", r"~\a.py"),  # 끝 두 조각
        ("/home/테스터", r'  File "/home/\ud14c\uc2a4\ud130/a.py"', r'  File "~/a.py"'),
        ("/home/josé", r'  File "/home/jos\xe9/a.py"', r'  File "~/a.py"'),  # \xNN
        ("/home/a😀", r'  File "/home/a\U0001f600/a.py"', r'  File "~/a.py"'),  # \UNNNNNNNN
    ],
)
def test_home_written_with_faulthandler_escapes_is_replaced(home, text, expected):
    r"""비ASCII 홈 이름이 faulthandler의 이스케이프(\xNN · \uNNNN · \UNNNNNNNN)로 적혀 있어도 ~로 바뀌어야 한다 (#427).

    홈과 글 -> 바꾼 글 == expected
    """
    assert logging_setup.redact_home(text, home) == expected


@pytest.mark.parametrize(
    ("home", "text"),
    [
        ("/home/테스터", r"/home/\ud14c\uc2a4/a.py"),  # 다른 사용자(이름의 앞부분만 같다)
        ("/home/테스터", r"/home/\ud14c\uc2a4\ud130x/a.py"),  # 다른 사용자(이름이 이어진다)
    ],
)
def test_escaped_name_of_another_user_is_kept(home, text):
    """이스케이프로 적힌 다른 사용자의 홈은 그대로여야 한다 (#427).

    홈과 글 -> 바꾼 글 == 원래 글
    """
    assert logging_setup.redact_home(text, home) == text


_FAKE_FFMPEG_FAILING = (  # 표준 입력을 다 읽고, 파일의 글을 표준 오류에 쓰고 1로 끝나는 ffmpeg 대역
    "import sys\n"
    "sys.stdin.buffer.read()\n"
    "sys.stderr.buffer.write(open(sys.argv[1], 'rb').read())\n"
    "sys.exit(1)\n"
)


@pytest.mark.parametrize("cut_at", range(len(WINDOWS_HOME) + 1))
def test_ffmpeg_error_whose_tail_starts_inside_the_home_leaves_no_part_of_the_name_in_the_log(
    app_log, tmp_path, monkeypatch, cut_at
):
    r"""ffmpeg 오류 글의 끝 500글자가 홈 경로의 어느 글자에서 시작해도 로그에 이름의 일부가 남지 않아야 한다 (#397).

    홈 C:\Users\Tester. remux_stream의 ffmpeg 대역이 3줄을 쓰고 1로 끝남 — 가운데 줄
    "Error opening output file C:\Users\Tester\Videos\…mp4"의 cut_at번째 글자에서 끝 500글자가 시작한다.
    그 RemuxError를 앱 로그에 적음
    -> 적힌 오류 글에 "~\Videos\"가 있고, "Tester"의 끝 세 글자 이상인 조각이 없다
    """
    monkeypatch.setattr(logging_setup, "_home_dir", lambda: WINDOWS_HOME)
    after = r"\Videos" + "\\"
    last = "\nConversion failed!"
    filler = 500 - len(WINDOWS_HOME[cut_at:]) - len(after) - len(".mp4") - len(last)
    stderr = (
        "[mp4 @ 0x1] header\n"
        f"Error opening output file {WINDOWS_HOME}{after}{'a' * filler}.mp4{last}\n"
    )
    stderr_file = tmp_path / "stderr.txt"
    stderr_file.write_bytes(stderr.encode())
    real = subprocess.Popen

    def fake(command, **kwargs):
        return real([sys.executable, "-c", _FAKE_FFMPEG_FAILING, str(stderr_file)], **kwargs)

    monkeypatch.setattr(ffmpeg_module, "get_ffmpeg_exe", lambda: "ffmpeg")  # 찾기도 대역을 띄운다
    monkeypatch.setattr(ffmpeg_module.subprocess, "Popen", fake)
    with pytest.raises(ffmpeg_module.RemuxError) as raised:
        ffmpeg_module.remux_stream(iter([b"x"]), str(tmp_path / "out.mp4"))
    assert stderr.strip()[-500:].startswith(WINDOWS_HOME[cut_at:])  # 글자 수 자리가 맞다

    logging.getLogger("test.redaction").error("BEGIN397 %s END397", raised.value)

    for text in app_log():
        written = text[text.index("BEGIN397") : text.index("END397")]
        assert "~" + after in written
        assert not any("Tester"[i:] in written for i in range(len("Tester") - 2))


_LONG_LINE_PATH = WINDOWS_HOME + r"\Videos\clip.mp4"  # 한 줄짜리 긴 오류 글에 든 경로
_LONG_LINE = "w " * 600 + _LONG_LINE_PATH + " end"  # 줄 머리가 꼬리의 두 배보다 먼 한 줄


@pytest.mark.parametrize("cut_at", range(len(_LONG_LINE_PATH)))
def test_long_one_line_error_whose_tail_starts_inside_the_path_leaves_no_part_of_the_name(
    app_log, monkeypatch, cut_at
):
    r"""한 줄이 매우 긴 오류 글에서 끝 글자 수 꼬리가 경로의 어느 글자에서 시작해도 로그에 이름의 일부가 남지 않아야 한다 (#397).

    홈 C:\Users\Tester. "w " × 600 + "C:\Users\Tester\Videos\clip.mp4 end"(줄 바꿈 없음)를 error_tail로
    자름 — 글자 수 꼬리가 경로의 cut_at번째 글자에서 시작한다. 그 꼬리를 앱 로그에 적음
    -> 적힌 글에 "Tester"의 앞 · 끝 세 글자 이상인 조각이 없다, 꼬리는 원래 글의 끝부분이다
    """
    monkeypatch.setattr(logging_setup, "_home_dir", lambda: WINDOWS_HOME)
    limit = len(_LONG_LINE) - _LONG_LINE.index(_LONG_LINE_PATH) - cut_at
    tail = ffmpeg_module.error_tail(_LONG_LINE, limit)
    assert _LONG_LINE[-limit:].startswith(_LONG_LINE_PATH[cut_at:])  # 글자 수 자리가 맞다

    logging.getLogger("test.redaction").error("BEGIN397 %s END397", tail)

    assert _LONG_LINE.endswith(tail)
    for text in app_log():
        written = text[text.index("BEGIN397") : text.index("END397")]
        pieces = ["Tester"[i:] for i in range(len("Tester") - 2)]
        pieces += ["Tester"[:i] for i in range(3, len("Tester"))]
        assert not any(piece in written for piece in pieces)


_DECODE_ERROR_LINE = (
    r"[h264 @ 0x1] error while decoding C:\Users\Tester\Videos\a.mp4 at 1"  # 홈 경로가 든 오류 줄
)
_HOME_AT = _DECODE_ERROR_LINE.index("C:")  # 줄에서 홈 경로가 시작하는 자리


@pytest.mark.parametrize("cut_at", range(_HOME_AT + 1, _HOME_AT + len(WINDOWS_HOME) + 1))
def test_error_line_cut_from_the_front_inside_the_home_leaves_no_part_of_the_name_in_the_log(
    app_log, monkeypatch, cut_at
):
    r"""오류 줄의 앞을 남기는 자리가 홈 경로의 어느 글자여도 로그에 이름의 앞부분이 남지 않아야 한다 (#397).

    홈 C:\Users\Tester. 오류 줄 "[h264 @ 0x1] error while decoding C:\Users\Tester\Videos\a.mp4 at 1"을
    error_head로 cut_at글자 남기고 앱 로그에 적음 (cut_at은 홈 경로의 둘째 글자 ~ 끝 다음 글자)
    -> 적힌 글에 "Tester"의 앞 세 글자 이상인 조각이 없다, 남긴 글은 원래 줄의 앞부분이다
    """
    monkeypatch.setattr(logging_setup, "_home_dir", lambda: WINDOWS_HOME)
    head = ffmpeg_module.error_head(_DECODE_ERROR_LINE, cut_at)

    logging.getLogger("test.redaction").error("BEGIN397 %s END397", head)

    assert _DECODE_ERROR_LINE.startswith(head)
    for text in app_log():
        written = text[text.index("BEGIN397") : text.index("END397")]
        assert not any("Tester"[:i] in written for i in range(3, len("Tester") + 1))


@pytest.mark.parametrize(
    ("line", "cut_at", "expected"),
    [
        # 다른 사용자 — 글자 수 자리는 "…\Tes"
        (r"error in C:\Users\Tester2\a.mp4", 21, r"error in C:\Users\Tester2"),
        # 경로가 아닌 곳의 같은 글자 — 글자 수 자리는 "… Test"
        ("error in Tester at 1", 13, "error in Tester"),
    ],
)
def test_error_line_cut_from_the_front_keeps_what_is_not_this_home(
    app_log, monkeypatch, line, cut_at, expected
):
    r"""홈이 아닌 경로 · 경로가 아닌 같은 글자는 조각 안에서 앞을 잘라도 그 조각 끝까지 그대로 적혀야 한다 (#397).

    홈 C:\Users\Tester. 오류 줄을 error_head로 cut_at글자(조각 안) 남겨 앱 로그에 적음
    -> 적힌 글 == expected (그 조각의 끝까지, 가리지 않음)
    """
    monkeypatch.setattr(logging_setup, "_home_dir", lambda: WINDOWS_HOME)

    logging.getLogger("test.redaction").error(
        "BEGIN397 %s END397", ffmpeg_module.error_head(line, cut_at)
    )

    for text in app_log():
        assert f"BEGIN397 {expected} END397" in text


SPACED_HOME = r"C:\Users\Jo Doe"  # 가짜 홈 — 사용자 이름에 공백이 있다
_SPACED_PATH = SPACED_HOME + r"\Videos\a.mp4"  # 그 홈 아래의 파일
_SPACED_NAME_PIECES = ("Jo", "Doe", "oe")  # 로그에 남으면 안 되는 이름 조각


def _written(app_log) -> list[str]:
    """앱 로그 · 콘솔에 BEGIN397 ~ END397 사이로 적힌 글."""
    return [text[text.index("BEGIN397") : text.index("END397")] for text in app_log()]


@pytest.mark.parametrize("cut_at", range(len(_SPACED_PATH)))
@pytest.mark.parametrize(
    "shape",
    [
        "\nError opening {path}\nConversion failed!",  # 줄 머리가 꼬리의 limit 안팎
        "w " * 600 + "{path} end",  # 줄 바꿈 없는 긴 한 줄
    ],
    ids=["lines", "one-long-line"],
)
def test_error_tail_never_starts_inside_a_home_whose_name_has_a_space(
    app_log, monkeypatch, shape, cut_at
):
    r"""사용자 이름에 공백이 있는 홈이어도 꼬리가 경로의 어느 글자에서 시작하든 로그에 이름 조각이 남지 않아야 한다 (#397).

    홈 C:\Users\Jo Doe. 오류 글(여러 줄 · 긴 한 줄)에 "C:\Users\Jo Doe\Videos\a.mp4"가 있고, 글자 수
    꼬리가 경로의 cut_at번째 글자에서 시작하게 error_tail로 자름. 그 꼬리를 앱 로그에 적음
    -> 적힌 글에 "Jo" · "Doe" · "oe"가 없다, 꼬리는 원래 글의 끝부분이다
    """
    monkeypatch.setattr(logging_setup, "_home_dir", lambda: SPACED_HOME)
    text = ("first" + shape).format(path=_SPACED_PATH)
    limit = len(text) - text.index(_SPACED_PATH) - cut_at
    tail = ffmpeg_module.error_tail(text, limit)

    logging.getLogger("test.redaction").error("BEGIN397 %s END397", tail)

    assert text.endswith(tail)
    for written in _written(app_log):
        assert not any(piece in written for piece in _SPACED_NAME_PIECES)


@pytest.mark.parametrize("cut_at", range(1, len(SPACED_HOME) + 1))
def test_error_head_never_ends_inside_a_home_whose_name_has_a_space(app_log, monkeypatch, cut_at):
    r"""사용자 이름에 공백이 있는 홈이어도 머리가 홈 경로의 어느 글자에서 끝나든 로그에 이름 조각이 남지 않아야 한다 (#397).

    홈 C:\Users\Jo Doe. 오류 줄 "error while decoding C:\Users\Jo Doe\Videos\a.mp4 at 1"을 홈 경로의
    cut_at번째 글자에서 끝나게 error_head로 자름. 그 머리를 앱 로그에 적음
    -> 적힌 글에 "Jo" · "Doe" · "oe"가 없다, 머리는 원래 줄의 앞부분이다
    """
    monkeypatch.setattr(logging_setup, "_home_dir", lambda: SPACED_HOME)
    line = f"error while decoding {_SPACED_PATH} at 1"
    head = ffmpeg_module.error_head(line, line.index(SPACED_HOME) + cut_at)

    logging.getLogger("test.redaction").error("BEGIN397 %s END397", head)

    assert line.startswith(head)
    for written in _written(app_log):
        assert not any(piece in written for piece in _SPACED_NAME_PIECES)


def test_cut_inside_another_users_path_with_a_space_is_not_moved(monkeypatch):
    r"""홈이 아닌 경로(이름이 이어지는 다른 사용자) 안에서는 자르는 자리가 홈 때문에 옮겨지지 않아야 한다 (#397).

    홈 C:\Users\Jo Doe. "error in C:\Users\Jo Doe2\a.mp4"를 "Jo" 다음(공백)에서 끝나게 error_head로 자름
    -> "error in C:\Users\Jo" (공백 경계 그대로)
    """
    monkeypatch.setattr(logging_setup, "_home_dir", lambda: SPACED_HOME)
    line = r"error in C:\Users\Jo Doe2\a.mp4"

    assert ffmpeg_module.error_head(line, line.index("Jo") + 2) == r"error in C:\Users\Jo"
