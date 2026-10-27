"""ffmpeg 실행 파일 탐색·스트림 remux 유틸 (#88·#92, SPEC §6.5).

세그먼트 병합 산출물은 바이트 연결이라 편집 프로그램이 읽지 못한다 —
라이브 원본 타임라인(시작 오프셋≠0)을 그대로 보유하고, 전역 인덱스(moov)가
없기 때문이다. ffmpeg 스트림 복사(remux)로 재포장해 이를 해소한다.

#92에서 단일 패스로 개편했다: 세그먼트 바이트를 **ffmpeg stdin으로 직접
흘려** 중간 병합 파일 없이 최종 mp4를 만든다. fMP4·MPEG-TS 모두 바이트
연결이 곧 유효한 스트림이라 파이프 공급이 컨테이너 의미를 정확히 보존한다.
concat demuxer(-f concat)는 채택하지 않는다 — TS는 파일별 길이 추정으로
duration이 부풀려지고, fMP4는 조각에 trex/tfhd 문맥이 없어 실패하면서도
exit 0에 쓰레기 산출물을 내놓아 실패 판정이 불가능하다(#92 조사 실측).

**실행 파일 경로 탐색은 이 모듈 하나에 격리한다** (SPEC §6.5 설계 요건).
현재 배포 방식은 pip 패키지 imageio-ffmpeg(휠 안에 정적 바이너리 동봉,
Nuitka가 표준 패키지 설정으로 번들링)이며, 커스텀 빌드 등으로 바뀌어도
``get_ffmpeg_exe()``만 고치면 된다. 못 찾으면 명확한 예외를 던진다 —
무음 실패 금지. remux 실패 처리 방침(명시적 실패, #92에서 폴백 제거)은
호출자(다운로더 postprocess)의 책임이다.

**리눅스는 시스템 ffmpeg를 우선 탐색한다 (#94).** 동봉본(johnvansickle
정적 빌드 계열)은 mpegts demux(SDT 파싱)에서 SIGSEGV라 AES(TS) 경로를
처리하지 못한다 — 4.2.2(2019)·7.0.2·git master 전 세대 공통, 같은 환경의
distro·BtbN 빌드는 정상임을 CI 교차 실측으로 확인했다. pip 방식은 기반으로
유지하고, 리눅스에서만 시스템 설치본이 있으면 그것을 쓴다.

**동봉본을 리눅스에서 쓸 때는 GCONV_PATH 가드를 얹는다 (#97).** 크래시의
근본 원인이 "정적 glibc가 호스트 gconv 모듈을 dlopen → 세대 비호환"으로
규명되어(#94 4차 진단), 그 로드를 차단하면 동봉본만으로도 TS remux가
동작한다. 상세는 _subprocess_env 참조.
"""

import ctypes
import functools
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence

from core.utils.logging_setup import home_spans

logger = logging.getLogger(__name__)

# GUI 앱의 서브프로세스가 콘솔 창을 띄우지 않게 한다 (Windows 전용 플래그)
_CREATE_NO_WINDOW = 0x08000000

# 리눅스 여부 — 시스템 ffmpeg 우선 탐색(#94)·GCONV_PATH 가드(#97)의 스위치.
# 테스트가 대체한다
_IS_LINUX = sys.platform.startswith("linux")


class FFmpegError(Exception):
    """ffmpeg 관련 실패의 공통 상위 예외 — 실패 처리 판단은 이것 하나로 잡는다."""


class FFmpegNotFoundError(FFmpegError):
    """ffmpeg 실행 파일을 찾지 못했다 — 배포(의존성 설치·번들링) 문제 신호."""


class RemuxError(FFmpegError):
    """remux 실행이 실패했다 — 입력 손상·디스크 부족 등."""


class FFmpegTimeoutError(FFmpegError):
    """ffmpeg가 제한 시간 안에 끝나지 않아 종료시켰다."""


class FFmpegCancelledError(Exception):
    """부르는 쪽이 멈추라고 해(``should_stop``) ffmpeg를 끝냈다 — 실패가 아니다 (#309).

    ``FFmpegError``를 잇지 않는다 — ffmpeg의 실패를 한꺼번에 잡는 자리에서 실패로 오인되지 않게.
    """


# 멈추라는 요청 · 일시정지를 확인하는 간격(초) — 중단을 누른 뒤 ffmpeg가 끝나기까지,
# 일시정지를 누른 뒤 ffmpeg가 서기까지의 상한이다. 앱은 워커가 끝나기를 2초까지 기다리므로
# (그동안 GUI가 선다) 그보다 훨씬 짧아야 한다
_STOP_POLL_SECONDS = 0.05


@functools.lru_cache(maxsize=1)
def _windows_process_api() -> tuple:
    """Windows의 (NtSuspendProcess, NtResumeProcess) — 한 번만 만든다. Windows에서만 부른다.

    두 함수 모두 프로세스 핸들 하나를 받고 NTSTATUS를 돌려준다. 핸들은 포인터 크기라
    ``c_void_p``로, NTSTATUS는 32비트 부호 있는 정수라 ``c_long``으로 적는다 — 적지 않으면
    ctypes가 인자를 C int로 넘겨 64비트 핸들의 윗부분이 잘릴 수 있다.

    ⚠️ 부를 때마다 ``ctypes.WinDLL``을 새로 만들지 않는다. 그렇게 만들었더니 멈춤을 몇 번 거친
    프로세스가 한참 뒤 Qt의 이벤트 처리 안에서 접근 위반으로 죽었다(#309). 재현으로 가른 것:
    부를 때마다 만들면 죽고(핸들을 따로 열고 닫지 않아도 죽는다), 한 번만 만들면 안 죽는다.
    """
    ntdll = ctypes.WinDLL("ntdll")
    for function in (ntdll.NtSuspendProcess, ntdll.NtResumeProcess):
        function.argtypes = (ctypes.c_void_p,)
        function.restype = ctypes.c_long
    return ntdll.NtSuspendProcess, ntdll.NtResumeProcess


_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000  # 잡의 마지막 핸들이 닫히면 잡 안의 프로세스를 끝낸다
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9  # SetInformationJobObject의 정보 종류 번호


@functools.lru_cache(maxsize=1)
def _windows_child_job() -> tuple | None:
    """Windows의 (잡 핸들, AssignProcessToJobObject) — 한 번만 만든다. 만들지 못하면 None.

    잡(Job Object)은 프로세스 묶음이다. "마지막 핸들이 닫히면 안의 프로세스를 끝낸다"로 설정해
    두고 이 프로세스만 그 핸들을 쥔다 — 이 프로세스가 **어떻게 끝나든**(정상 종료 · 강제 종료 ·
    충돌) OS가 핸들을 닫으므로 잡에 넣은 ffmpeg가 함께 끝난다. 일시정지로 멈춰 있는 것도 끝난다.

    핸들은 닫지 않는다 — 닫는 순간이 곧 자식을 끝내는 순간이다. 자식에게 물려주지도 않는다
    (기본 보안 속성은 상속 불가다).

    ⚠️ DLL · 함수 객체는 여기서 한 번만 만들고 인자 · 돌려주는 값의 형식을 적는다
    (``_windows_process_api``의 주의와 같다).
    """

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ("r", "w", "o", "rb", "wb", "ob")]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32")
    create = kernel32.CreateJobObjectW
    create.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
    create.restype = ctypes.c_void_p  # HANDLE — 포인터 크기
    configure = kernel32.SetInformationJobObject
    configure.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
    configure.restype = ctypes.c_int  # BOOL
    assign = kernel32.AssignProcessToJobObject
    assign.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    assign.restype = ctypes.c_int  # BOOL

    job = create(None, None)
    if not job:
        return None
    limits = _ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    configured = configure(
        job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)
    )
    return (job, assign) if configured else None


def _end_with_parent(process: subprocess.Popen) -> bool:
    """우리가 띄운 프로세스가 이 프로세스와 함께 끝나게 한다 (#309). 걸었으면 True.

    컷 도중 일시정지한 채 앱이 정상 종료가 아닌 길로 끝나면(작업 관리자로 끝냄 · 충돌) 멈춘
    ffmpeg가 멈춘 채 남았다. 정상 종료는 중단을 거쳐 끝내지만 그 길을 타지 못하는 종료가 있다.

    - Windows: 프로세스를 잡에 넣는다(``_windows_child_job``)
    - 그 밖: 아직 하지 않는다 — False를 돌려준다

    걸지 못해도(잡을 만들지 못했거나 이미 끝난 프로세스) 실행은 그대로 간다. 띄운 직후에
    부른다 — 띄우기와 넣기 사이의 짧은 틈에 이 프로세스가 죽으면 그 자식은 남는다.
    """
    if sys.platform != "win32":
        return False
    handle = getattr(process, "_handle", None)  # Popen이 띄울 때 받아 쥔 핸들
    job = _windows_child_job()
    if handle is None or job is None:
        return False
    job_handle, assign = job
    return bool(assign(job_handle, int(handle)))


def _set_suspended(process: subprocess.Popen, suspended: bool) -> bool:
    """우리가 띄운 프로세스를 OS 수준에서 멈추거나(True) 다시 돌린다(False) (#309). 됐으면 True.

    멈춘 프로세스는 CPU를 쓰지 않고 파일도 쓰지 않는다 — 다시 돌리면 멈춘 자리에서 이어 간다.
    일시정지를 ffmpeg에 그대로 옮기는 데 쓴다. 새 의존성 없이 OS가 주는 것만 쓴다:

    - Windows: ``NtSuspendProcess`` · ``NtResumeProcess``(ntdll)
    - 그 밖(리눅스 · macOS): ``SIGSTOP`` · ``SIGCONT``

    멈춘 프로세스도 끝낼 수 있다(``Popen.kill`` — Windows의 TerminateProcess · POSIX의 SIGKILL은
    멈춘 프로세스에도 듣는다). 이미 끝난 프로세스에는 아무것도 하지 않는다.

    프로세스는 **그 ``Popen`` 객체가 쥔 것**으로만 가리킨다. Windows에서는 ``Popen``이 프로세스를
    띄울 때 받은 핸들을 그대로 쓴다 — PID로 다시 열지 않으므로 다른 프로세스를 잡을 일이 없고
    따로 닫을 핸들도 없다. 그 밖에서는 ``Popen``이 아직 거두지 않은 자식의 PID에 신호를 보낸다.

    결과는 부를 때마다 debug 로그로 남긴다 — Windows는 NTSTATUS, 그 밖은 신호를 보냈는지 ·
    실패한 까닭. 일시정지 뒤 끝나지 않는 실행에서 멈추기 · 다시 시작이 들었는지 가르려고 둔다 (#458).
    """
    action = "멈추기" if suspended else "다시 시작"
    if process.poll() is not None:
        logger.debug("ffmpeg %s pid=%s — 이미 끝난 프로세스라 하지 않았다", action, process.pid)
        return False
    if sys.platform == "win32":
        # Popen이 프로세스를 띄울 때 받아 쥐고 있는 핸들(모든 권한) — Popen이 살아 있는 동안 유효하다
        handle = getattr(process, "_handle", None)
        if handle is None:
            logger.debug("ffmpeg %s pid=%s — 핸들이 없어 하지 않았다", action, process.pid)
            return False
        suspend, resume = _windows_process_api()
        status = (suspend if suspended else resume)(int(handle))
        # NTSTATUS — 0이 성공이다. 음수로 오는 실패 코드는 익숙한 0xC... 꼴로 적는다
        logger.debug("ffmpeg %s pid=%s NTSTATUS=0x%08X", action, process.pid, status & 0xFFFFFFFF)
        return status == 0
    try:
        os.kill(process.pid, signal.SIGSTOP if suspended else signal.SIGCONT)
    except OSError as e:
        logger.debug("ffmpeg %s pid=%s — 신호 실패: %s", action, process.pid, e)
        return False
    logger.debug("ffmpeg %s pid=%s — 신호를 보냈다", action, process.pid)
    return True


class _Control:
    """도는 ffmpeg 하나에 멈춤 · 일시정지 · 제한 시간을 건다 — ``_STOP_POLL_SECONDS``마다 ``poll``을 부른다.

    일시정지 중에는 프로세스가 서 있으므로 그 시간을 제한 시간에 세지 않는다 — 일시정지가
    길었다고 컷이 시간 초과로 실패하지 않는다.
    """

    CANCELLED = "cancelled"
    TIMED_OUT = "timed out"

    def __init__(
        self,
        process: subprocess.Popen,
        timeout: float,
        should_stop: Callable[[], bool] | None,
        should_pause: Callable[[], bool] | None,
    ):
        self._process = process
        self._should_stop = should_stop
        self._should_pause = should_pause
        self._deadline = time.monotonic() + timeout
        self._suspended_at: float | None = None  # 멈춘 시각. 돌고 있으면 None

    def poll(self) -> str | None:
        """요청을 확인해 프로세스에 옮긴다. 끝내야 하면 그 까닭(``CANCELLED`` · ``TIMED_OUT``)을 돌려준다.

        끝내는 것은 부르는 쪽이 한다(``Popen.kill``) — 멈춰 있는 프로세스도 그대로 끝난다.
        """
        now = time.monotonic()
        if self._should_stop is not None and self._should_stop():
            return self.CANCELLED
        paused = self._should_pause is not None and self._should_pause()
        if paused and self._suspended_at is None:
            # 멈추지 못하면 다음 poll(_STOP_POLL_SECONDS 뒤)에 다시 부른다. 그동안 프로세스는 돌고
            # 제한 시간도 센다 — 계속 실패해도 제한 시간에서 끝난다(#458에서 그대로 두기로 했다)
            if _set_suspended(self._process, True):
                self._suspended_at = now
        elif not paused and self._suspended_at is not None:
            if not _set_suspended(self._process, False) and self._process.poll() is None:
                # 살아 있는데 다시 돌리지 못했다 — 멈춘 채 남아 제한 시간까지 기다리게 된다 (#458)
                logger.warning(
                    "ffmpeg를 다시 돌리지 못했다(pid=%s) — 멈춘 채 남았을 수 있다",
                    self._process.pid,
                )
            self._deadline += now - self._suspended_at  # 서 있던 시간은 제한 시간에 세지 않는다
            self._suspended_at = None
        if self._suspended_at is None and now >= self._deadline:
            return self.TIMED_OUT
        return None


def _hold_while_paused(
    should_pause: Callable[[], bool] | None, should_stop: Callable[[], bool] | None
) -> None:
    """일시정지 중이면 풀릴 때까지 기다린다 — 일시정지 중에는 새 ffmpeg를 띄우지 않는다.

    Raises:
        FFmpegCancelledError: 기다리는 동안 멈추라는 요청이 온 경우
    """
    while should_pause is not None and should_pause():
        if should_stop is not None and should_stop():
            raise FFmpegCancelledError("일시정지 중에 멈추라는 요청이 왔다")
        time.sleep(_STOP_POLL_SECONDS)


def get_ffmpeg_exe() -> str:
    """ffmpeg 실행 파일 경로를 반환한다.

    탐색 순서:
    1. ``IMAGEIO_FFMPEG_EXE`` 환경 변수 — 명시적 지정은 항상 최우선
       (imageio-ffmpeg 자체 규약과 동일하게 존중한다)
    2. (리눅스) 시스템 ffmpeg — 동봉본의 mpegts demux SIGSEGV 회피 (#94)
    3. imageio-ffmpeg 동봉본 — 그 외 플랫폼의 기본이자 리눅스의 폴백
       (리눅스 동봉본도 fMP4(m3u8) 경로는 정상이다)

    Raises:
        FFmpegNotFoundError: 어느 경로로도 찾지 못한 경우 — 메시지에 설치
            안내를 담는다 (무음 실패 금지)
    """
    explicit = os.getenv("IMAGEIO_FFMPEG_EXE")
    if explicit:
        return explicit

    if _IS_LINUX:
        system_exe = shutil.which("ffmpeg")
        if system_exe:
            return system_exe

    try:
        import imageio_ffmpeg
    except ImportError as e:
        raise FFmpegNotFoundError(_not_found_message("imageio-ffmpeg 패키지 미설치")) from e
    try:
        # imageio_ffmpeg은 휠에 동봉된 바이너리를 찾지 못하면 RuntimeError를 던진다
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        _forget_failed_lookup(imageio_ffmpeg)
        raise FFmpegNotFoundError(_not_found_message(str(e))) from e


def _forget_failed_lookup(imageio_ffmpeg) -> None:
    """imageio-ffmpeg이 기억한 "못 찾음"을 지운다 — 다음 조회가 처음부터 다시 찾는다 (#423).

    imageio-ffmpeg 0.6.0의 ``_get_ffmpeg_exe``는 ``lru_cache``라 못 찾은 결과(None)도
    기억한다. 그대로 두면 ffmpeg를 고친 뒤에도 앱을 다시 켤 때까지 못 찾는다. 지우는 것은
    못 찾았을 때뿐이다 — 찾은 경로는 그 캐시가 계속 기억해 매번 찾지 않는다.

    비공개 이름이라 라이브러리 버전이 바뀌어 없으면 아무것도 하지 않는다 — 그때는
    ``tests/ffmpeg/process/test_ffmpeg_utils.py``가 실패해 알린다.
    """
    lookup = getattr(getattr(imageio_ffmpeg, "_utils", None), "_get_ffmpeg_exe", None)
    clear = getattr(lookup, "cache_clear", None)
    if clear is not None:
        clear()


def _not_found_message(reason: str) -> str:
    """ffmpeg 미발견 예외 메시지 — 원인과 설치 유도 안내를 함께 담는다."""
    guide = "의존성 설치(uv sync)를 확인하라"
    if _IS_LINUX:
        guide += (
            ". 시스템 ffmpeg 설치(예: sudo apt install ffmpeg) 또는 "
            "IMAGEIO_FFMPEG_EXE 환경 변수로 실행 파일을 지정할 수도 있다"
        )
    return f"ffmpeg 실행 파일을 찾지 못했다 ({reason}) — {guide}"


def _is_bundled_exe(exe: str) -> bool:
    """exe가 imageio-ffmpeg 동봉 바이너리인지 판정한다 (#97 가드 적용 조건)."""
    try:
        import imageio_ffmpeg
    except ImportError:
        return False
    binaries_dir = os.path.realpath(
        os.path.join(os.path.dirname(imageio_ffmpeg.__file__), "binaries")
    )
    return os.path.realpath(exe).startswith(binaries_dir + os.sep)


def _subprocess_env(exe: str) -> dict[str, str] | None:
    """ffmpeg 서브프로세스에 줄 환경을 반환한다. None이면 부모 환경 상속.

    **GCONV_PATH 가드 (#94·#97)**: 리눅스 동봉본(johnvansickle 정적 빌드)은
    정적 링크된 구세대 glibc가 mpegts SDT 서비스명의 문자셋 변환(iconv)
    시점에 **호스트의 gconv 공유 모듈을 dlopen**한다. 신형 glibc(우분투
    24.04/2.39대) 호스트에서는 모듈 세대가 맞지 않아 SIGSEGV — GCONV_PATH를
    빈 디렉토리로 돌려 그 로드를 차단하면 크래시가 사라진다(#94 4차 실측).
    부작용은 SDT 서비스명 문자셋 변환 생략뿐이며(A/V 무관), 산출물 바이트
    동일성은 테스트로 입증한다.

    시스템 ffmpeg(동적 glibc — 자기 배포판의 gconv와 호환)와 Windows·macOS
    에는 적용하지 않는다. 프로세스 전역(os.environ)은 건드리지 않는다.
    imageio-ffmpeg가 동봉 빌드 계열을 교체(musl 등)하면 이 가드는 무해한
    잉여가 되므로 그때 제거해도 된다.
    """
    if not _IS_LINUX or not _is_bundled_exe(exe):
        return None
    env = os.environ.copy()
    env["GCONV_PATH"] = _empty_gconv_dir()
    return env


def _empty_gconv_dir() -> str:
    """GCONV_PATH용 빈 디렉토리를 보장하고 경로를 반환한다.

    존재하지 않는 경로 대신 빈 디렉토리를 쓴다 — "유효한 경로에 모듈이
    없음"으로 귀결되어 glibc 버전별 경로 오류 처리 차이를 타지 않는다.
    """
    path = os.path.join(tempfile.gettempdir(), "cvdv2-empty-gconv")
    os.makedirs(path, exist_ok=True)
    return path


def remux_stream(
    chunks: Iterable[bytes],
    dst_path: str,
    *,
    timeout: float | None = None,
    should_stop: Callable[[], bool] | None = None,
    should_pause: Callable[[], bool] | None = None,
) -> None:
    """바이트 스트림을 stdin으로 받아 재인코딩 없이(스트림 복사) mp4로 재포장한다.

    옵션 근거 (#88·#92 실측 — 치지직 실스트림으로 확인):
    - ``-i pipe:0``: 세그먼트 바이트 연결 스트림을 그대로 공급받는다(중간 파일
      없음). fMP4·TS는 스트리밍 컨테이너라 파이프 입력으로 정상 판별된다
    - ``-c copy``: 재인코딩 금지. 화질 무손실
    - 타임스탬프 0 정규화는 ffmpeg 기본 동작(-copyts 미지정) — 별도 옵션 불필요
    - ``+faststart``는 쓰지 않는다(#108) — moov를 선두로 옮기는 2차 패스가
      산출물 전체를 다시 읽고 다시 쓴다(제거로 후처리 I/O 5N→3N). 편집
      프로그램 인식에 필요한 것은 moov의 **존재**(#88)이지 위치가 아니며,
      로컬 재생·편집은 랜덤 액세스라 moov가 파일 끝(ffmpeg 기본)이어도
      duration·시작 0·탐색·디코드가 동일함을 실측으로 확인했다(#108 비교표)
    - ``-f mp4``: 출력 컨테이너를 명시한다(#92) — 산출물 확장자가 .mp4가
      아니어도(유저가 저장 파일명을 바꾼 경우) 확장자 추론에 기대지 않는다

    실패 시 불완전한 산출물을 삭제하고 예외를 던진다 — 폴백은 없다(#92).
    공급 이터레이터가 예외를 던지면(다운로드 중단 등) 프로세스를 종료하고
    산출물을 지운 뒤 그 예외를 그대로 전파한다.

    Args:
        timeout: 바이트를 다 넣은 뒤 ffmpeg가 끝나기를 기다리는 제한 시간(초). 일시정지로 서
            있던 시간은 세지 않는다. 주지 않으면 끝날 때까지 기다린다 (#309)
        should_stop: 주면 그 기다림 동안 짧은 간격으로 부른다. 참이면 ffmpeg를 끝내고 산출물을
            지운 뒤 ``FFmpegCancelledError``를 낸다. 넣는 동안의 멈춤은 공급 이터레이터가 맡는다
        should_pause: 주면 같은 간격으로 부른다. 참인 동안 ffmpeg를 멈춰 둔다

    Raises:
        FFmpegNotFoundError: ffmpeg 실행 파일을 찾지 못한 경우
        RemuxError: ffmpeg가 0이 아닌 코드로 종료했거나 실행 자체가 실패한 경우
        FFmpegTimeoutError: 바이트를 다 넣은 뒤 timeout 안에 끝나지 않은 경우
        FFmpegCancelledError: 그 기다림 동안 should_stop이 참을 돌려줘 끝낸 경우
    """
    _copy_piped(chunks, dst_path, (), timeout, should_stop, should_pause)


def extract_audio_stream(
    chunks: Iterable[bytes],
    dst_path: str,
    *,
    timeout: float | None = None,
    should_stop: Callable[[], bool] | None = None,
    should_pause: Callable[[], bool] | None = None,
) -> None:
    """바이트 스트림을 stdin으로 받아 오디오만 재인코딩 없이 mp4로 뽑는다 (#309).

    세그먼트 방식의 구간 다운로드가 쓴다 — 구간의 세그먼트를 한 번 흘려 오디오를 작은 파일로
    뽑아 두고, 구간 파일을 쓸 때 영상과 합친다. 공급 · 실패 처리와 timeout · should_stop ·
    should_pause는 ``remux_stream``과 같다.

    Raises:
        FFmpegNotFoundError: ffmpeg 실행 파일을 찾지 못한 경우
        RemuxError: ffmpeg가 0이 아닌 코드로 종료했거나 실행 자체가 실패한 경우
        FFmpegTimeoutError · FFmpegCancelledError: ``remux_stream``과 같다
    """
    _copy_piped(chunks, dst_path, ("-vn",), timeout, should_stop, should_pause)


def _await_end(
    processes: Sequence[subprocess.Popen],
    timeout: float | None,
    should_stop: Callable[[], bool] | None,
    should_pause: Callable[[], bool] | None,
) -> Exception | None:
    """입력을 다 넣은 ffmpeg들이 끝나기를 기다린다 — 멈춤 · 일시정지 · 제한 시간을 본다 (#309).

    파이프로 바이트를 넣는 실행(``remux_stream`` · ``remux_through``)은 넣는 동안의 멈춤 ·
    일시정지를 공급 이터레이터가 맡는다. 다 넣은 뒤에는 이터레이터가 불리지 않으므로 여기서
    본다 — ``run_ffmpeg``와 같은 규칙이다. 셋 다 주지 않으면 바로 돌아간다(부르는 쪽이 기다린다).

    일시정지 동안은 프로세스들을 함께 멈춰 두고, 서 있던 시간은 제한 시간에 세지 않는다. 먼저
    끝난 프로세스가 있어도 시간은 한 곳에서 센다.

    Returns:
        모두 끝났으면 None. 끝내야 하면 낼 예외 — 멈추라는 요청이면 ``FFmpegCancelledError``,
        제한 시간을 넘겼으면 ``FFmpegTimeoutError``. 프로세스를 끝내고 산출물을 지우는 것은
        부르는 쪽이 한다(멈춰 있는 프로세스도 ``Popen.kill``로 끝난다)
    """
    if timeout is None and should_stop is None and should_pause is None:
        return None
    deadline = time.monotonic() + (timeout if timeout is not None else float("inf"))
    suspended_at: float | None = None  # 멈춰 둔 시각. 돌고 있으면 None
    while any(process.poll() is None for process in processes):
        now = time.monotonic()
        stopped = should_stop is not None and should_stop()
        paused = not stopped and should_pause is not None and should_pause()
        if paused and suspended_at is None:
            for process in processes:
                _set_suspended(process, True)  # 이미 끝난 쪽에는 아무 일도 없다
            suspended_at = now
        elif not paused and suspended_at is not None:
            for process in processes:
                _set_suspended(process, False)
            deadline += now - suspended_at  # 서 있던 시간은 제한 시간에 세지 않는다
            suspended_at = None
        if stopped:
            return FFmpegCancelledError("멈추라는 요청으로 ffmpeg를 끝냈다")
        if suspended_at is None and now >= deadline:
            return FFmpegTimeoutError(f"ffmpeg가 {timeout:g}초 안에 끝나지 않았다")
        time.sleep(_STOP_POLL_SECONDS)
    return None


def _copy_piped(
    chunks: Iterable[bytes],
    dst_path: str,
    selection: Sequence[str],
    timeout: float | None = None,
    should_stop: Callable[[], bool] | None = None,
    should_pause: Callable[[], bool] | None = None,
) -> None:
    """stdin의 바이트 스트림을 스트림 복사로 dst_path에 mp4로 쓴다. selection은 스트림을 고르는 옵션이다.

    timeout · should_stop · should_pause는 바이트를 다 넣은 뒤의 기다림에 건다(``_await_end``).
    """
    exe = get_ffmpeg_exe()
    cmd = [
        exe,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        "pipe:0",
        *selection,
        "-c",
        "copy",
        "-f",
        "mp4",
        dst_path,
    ]
    creationflags = _CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
            env=_subprocess_env(exe),
        )
    except OSError as e:
        raise RemuxError(f"ffmpeg 실행 실패: {e}") from e
    _end_with_parent(proc)

    # stderr는 별도 스레드로 계속 비운다 — 파이프 버퍼가 차면 ffmpeg가 멈춘다
    stderr_parts: list[bytes] = []
    reader = threading.Thread(target=lambda: stderr_parts.append(proc.stderr.read()), daemon=True)
    reader.start()

    try:
        for chunk in chunks:
            try:
                proc.stdin.write(chunk)
            except OSError:
                # ffmpeg가 먼저 종료해 파이프가 닫힘 — 공급을 멈추고
                # 아래에서 종료 코드·stderr로 실패를 보고한다
                break
    except BaseException:
        # 공급측 예외(다운로드 중단 등) — 프로세스를 끝내고 산출물을 남기지
        # 않은 채 원래 예외를 그대로 전파한다 (실패로 오인하지 않도록)
        proc.kill()
        proc.wait()
        reader.join(timeout=5)
        _close_quietly(proc.stdin, proc.stderr)
        _remove_quietly(dst_path)
        raise
    finally:
        _close_feed(chunks)

    _close_quietly(proc.stdin)
    # 넣을 것은 다 넣었다 — ffmpeg가 남은 것(mp4의 moov)을 쓰고 끝나기를 기다린다
    outcome = _await_end((proc,), timeout, should_stop, should_pause)
    if outcome is not None:
        proc.kill()  # 멈춰 있어도 끝난다
        proc.wait()
        reader.join(timeout=5)
        _close_quietly(proc.stderr)
        _remove_quietly(dst_path)
        raise outcome
    returncode = proc.wait()
    reader.join(timeout=5)
    _close_quietly(proc.stderr)

    if returncode != 0:
        # 실패한 부분 산출물을 남기지 않는다 (#92 — 정상 파일로 오인 방지)
        _remove_quietly(dst_path)
        stderr_tail = error_tail(b"".join(stderr_parts).decode(errors="replace"), 500)
        raise RemuxError(f"ffmpeg remux 실패 (exit {returncode}): {stderr_tail}")


def remux_through(
    chunks: Iterable[bytes],
    convert_args: Sequence[str],
    join_args: Sequence[str],
    dst_path: str,
    *,
    cwd: str | None = None,
    timeout: float | None = None,
    should_stop: Callable[[], bool] | None = None,
    should_pause: Callable[[], bool] | None = None,
) -> str:
    """바이트 스트림을 변환 ffmpeg에 넣고, 그 출력을 잇는 ffmpeg의 표준 입력으로 이어 dst_path를 만든다 (#309).

    세그먼트 방식의 구간 다운로드가 쓴다 — 가운데 세그먼트의 원본 바이트를 그대로 잇는
    ffmpeg(concat의 ``pipe:0``)에 넣으면 시각이 어긋나므로, 변환 ffmpeg가 먼저 조각난 mp4로
    바꾼다. 두 프로세스는 파이프로 바로 이어져 파이썬을 거치지 않는다.

    공급 · 실패 처리는 ``remux_stream``과 같다: 공급 이터레이터가 예외를 던지면 두 프로세스를
    끝내고 산출물을 지운 뒤 그 예외를 그대로 올린다. 어느 한쪽이라도 0이 아닌 코드로 끝나면
    산출물을 지우고 ``RemuxError``를 낸다 — 변환 쪽이 죽었는데 잇는 쪽이 받은 데까지만으로
    정상 종료한 경우도 실패다.

    Args:
        chunks: 변환 ffmpeg의 표준 입력으로 넣을 바이트
        convert_args: 변환 ffmpeg의 인자 — 입력은 ``pipe:0``, 출력은 ``pipe:1``이어야 한다
        join_args: 잇는 ffmpeg의 인자 — 출력 경로까지 든 것. ``-nostdin``은 붙여 준다(없으면
            ffmpeg가 표준 입력에서 키 입력을 읽으며 파이프의 바이트를 집어 간다)
        dst_path: 잇는 ffmpeg가 쓰는 산출물 — 실패하면 지운다
        cwd: 잇는 ffmpeg의 작업 디렉토리(concat 목록의 상대 경로 기준)
        timeout: 바이트를 다 넣은 뒤 두 ffmpeg가 끝나기를 기다리는 제한 시간(초). 일시정지로
            서 있던 시간은 세지 않는다. 주지 않으면 끝날 때까지 기다린다
        should_stop: 주면 그 기다림 동안 짧은 간격으로 부른다. 참을 돌려주면 두 프로세스를
            끝내고 산출물을 지운 뒤 ``FFmpegCancelledError``를 낸다. 바이트를 넣는 동안의 멈춤은
            공급 이터레이터가 맡는다(예외를 던진다)
        should_pause: 주면 같은 간격으로 부른다. 참인 동안 두 프로세스를 멈춰 둔다

    Returns:
        잇는 ffmpeg가 낸 경고(stderr) — 조각을 잇는 곳의 시각이 어긋났는지 부르는 쪽이 본다

    Raises:
        FFmpegNotFoundError: ffmpeg 실행 파일을 찾지 못한 경우
        RemuxError: 두 ffmpeg 가운데 하나가 0이 아닌 코드로 끝났거나 실행 자체가 실패한 경우
        FFmpegTimeoutError: 바이트를 다 넣은 뒤 timeout 안에 끝나지 않은 경우
        FFmpegCancelledError: 그 기다림 동안 should_stop이 참을 돌려줘 끝낸 경우
    """
    exe = get_ffmpeg_exe()
    creationflags = _CREATE_NO_WINDOW if sys.platform == "win32" else 0
    head = [exe, "-hide_banner", "-loglevel", "error"]
    try:
        join = subprocess.Popen(
            # 잇는 쪽은 경고까지 받는다 — DTS가 뒤로 가도 ffmpeg는 0으로 끝난다
            [exe, "-hide_banner", "-loglevel", "warning", "-nostdin", "-y", *join_args],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            cwd=cwd,
            creationflags=creationflags,
            env=_subprocess_env(exe),
        )
    except OSError as e:
        raise RemuxError(f"ffmpeg 실행 실패: {e}") from e
    _end_with_parent(join)
    try:
        convert = subprocess.Popen(
            [*head, *convert_args],
            stdin=subprocess.PIPE,
            stdout=join.stdin,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
            env=_subprocess_env(exe),
        )
    except OSError as e:
        join.kill()
        join.wait()
        _close_quietly(join.stdin, join.stderr)
        raise RemuxError(f"ffmpeg 실행 실패: {e}") from e
    _end_with_parent(convert)
    # 잇는 쪽의 입력은 이제 변환 쪽만 쥔다 — 이쪽에서 닫아야 변환이 끝날 때 잇는 쪽이 끝을 본다
    _close_quietly(join.stdin)

    # stderr는 별도 스레드로 계속 비운다 — 파이프 버퍼가 차면 ffmpeg가 멈춘다
    errors: dict[str, bytes] = {}
    readers = [
        threading.Thread(
            target=lambda n=name, p=process: errors.update({n: p.stderr.read()}), daemon=True
        )
        for name, process in (("convert", convert), ("join", join))
    ]
    for reader in readers:
        reader.start()

    def end_both() -> None:
        for process in (convert, join):
            process.kill()
        for process in (convert, join):
            process.wait()
        for reader in readers:
            reader.join(timeout=5)
        _close_quietly(convert.stdin, convert.stderr, join.stderr)

    try:
        for chunk in chunks:
            try:
                convert.stdin.write(chunk)
            except OSError:
                break  # 변환 쪽이 먼저 끝났다 — 아래에서 종료 코드로 실패를 알린다
    except BaseException:
        end_both()
        _remove_quietly(dst_path)
        raise
    finally:
        _close_feed(chunks)

    _close_quietly(convert.stdin)
    # 넣을 것은 다 넣었다 — 두 ffmpeg가 남은 것을 쓰고 끝나기를 기다린다. 이때부터는 공급
    # 이터레이터가 불리지 않으므로 멈춤 · 일시정지 · 제한 시간을 여기서 본다(run_ffmpeg와 같은 규칙)
    outcome = _await_end((convert, join), timeout, should_stop, should_pause)
    if outcome is not None:
        end_both()  # 멈춰 있어도 끝난다
        _remove_quietly(dst_path)
        raise outcome
    codes = (convert.wait(), join.wait())
    for reader in readers:
        reader.join(timeout=5)
    _close_quietly(convert.stderr, join.stderr)
    if any(codes):
        _remove_quietly(dst_path)
        tail = " | ".join(
            error_tail(errors.get(name, b"").decode(errors="replace"), 250)
            for name in ("convert", "join")
        )
        raise RemuxError(f"ffmpeg remux 실패 (변환 exit {codes[0]} · 잇기 exit {codes[1]}): {tail}")
    return errors.get("join", b"").decode(errors="replace")


def run_ffmpeg(
    args: Sequence[str],
    *,
    timeout: float,
    cwd: str | None = None,
    on_out_time: Callable[[float], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    should_pause: Callable[[], bool] | None = None,
) -> subprocess.CompletedProcess[str]:
    """ffmpeg를 끝날 때까지 실행하고 종료 코드와 출력을 돌려준다 (#309).

    실행 파일 경로·콘솔 창 억제·리눅스 동봉본 가드(#97)를 한 곳에서 붙인다 —
    ffmpeg를 직접 부르는 코드가 이것들을 빠뜨리지 않게 한다. 종료 코드가 0이
    아니어도 예외를 내지 않는다. 판정은 호출자가 한다.

    Args:
        args: ffmpeg 인자(실행 파일 이름 제외). ``-hide_banner``는 붙여 준다
        timeout: 제한 시간(초). 넘으면 프로세스를 종료한다
        cwd: 작업 디렉토리. 상대 경로 인자의 기준이다
        on_out_time: 주면 ffmpeg가 진행을 알릴 때마다(``-progress``) 그때까지 쓴 출력의
            시각(초)으로 부른다 — 부르는 스레드에서 돈다. 이때 돌려주는 값의 stdout은
            비어 있다(진행 글자가 그 자리로 나온다)
        should_stop: 주면 도는 동안 ``_STOP_POLL_SECONDS``마다 부른다. 참을 돌려주면
            프로세스를 바로 끝내고 ``FFmpegCancelledError``를 낸다 — 다운로드를 중단했을 때
            도는 ffmpeg가 끝까지 돌지 않게 한다. 다른 스레드에서 불릴 수 있다
        should_pause: 주면 같은 간격으로 부른다. 참인 동안 프로세스를 OS 수준에서 멈춰 두고
            (``_set_suspended``), 거짓이 되면 멈춘 자리에서 다시 돌린다. 시작할 때 참이면
            프로세스를 띄우지 않고 기다린다. 멈춰 있던 시간은 제한 시간에 세지 않는다.
            다른 스레드에서 불릴 수 있다

    Raises:
        FFmpegNotFoundError: ffmpeg 실행 파일을 찾지 못한 경우
        FFmpegTimeoutError: 제한 시간을 넘긴 경우
        FFmpegCancelledError: should_stop이 참을 돌려줘 끝낸 경우
        FFmpegError: 프로세스를 시작하지 못한 경우
    """
    exe = get_ffmpeg_exe()
    _hold_while_paused(should_pause, should_stop)
    if on_out_time is not None:
        return _run_reporting(exe, args, timeout, cwd, on_out_time, should_stop, should_pause)
    if should_stop is not None or should_pause is not None:
        return _run_controlled(exe, args, timeout, cwd, should_stop, should_pause)
    command = [exe, "-hide_banner", *args]
    try:
        # subprocess.run과 같은 일을 한다 — 띄운 프로세스를 쥐어야 앱과 함께 끝나게 걸 수 있다
        with subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            cwd=cwd,
            creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            env=_subprocess_env(exe),
        ) as process:
            _end_with_parent(process)
            try:
                output, errors = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()  # 끝난 프로세스의 파이프를 비우고 거둔다
                raise
            except BaseException:
                process.kill()  # 기다리는 도중의 예외(인터럽트 등) — 프로세스를 남기지 않는다
                raise
        return subprocess.CompletedProcess(command, process.returncode, output, errors)
    except subprocess.TimeoutExpired as e:
        raise FFmpegTimeoutError(f"ffmpeg가 {timeout:g}초 안에 끝나지 않았다") from e
    except OSError as e:
        raise FFmpegError(f"ffmpeg 실행 실패: {e}") from e


def parse_out_time(line: str) -> float | None:
    """``-progress``가 낸 한 줄에서 출력 시각(초)을 읽는다. 그 줄이 아니거나 값이 없으면 None.

    ``out_time_us=<마이크로초>`` 줄만 읽는다. 첫 패킷을 쓰기 전에는 값이 ``N/A``다.
    """
    key, _, value = line.strip().partition("=")
    if key != "out_time_us":
        return None
    try:
        return int(value) / 1_000_000
    except ValueError:
        return None


def error_tail(text: str, limit: int) -> str:
    """오류 글(ffmpeg의 stderr)의 끝 ``limit`` 글자쯤을 줄 경계에서 잘라 돌려준다 (#397).

    글자 수로만 자르면 경로 한가운데(홈 폴더의 사용자 이름 안 등)에서 시작해, 로그가 홈 경로를
    가리는 규칙(``redact_home``)을 빠져나간다. 자른 자리가 줄 중간이면 그 줄의 머리까지 늘린다 —
    줄을 버리지 않으므로 글자 수로 자를 때 들어 있던 글(공간 부족 판정이 찾는 "File too large" 등)은
    모두 남는다.

    줄 머리가 ``limit``보다 더 멀면(한 줄이 매우 길다) 자른 자리가 든 낱말(공백 사이 — 경로 하나)을
    가르지 않는다: 낱말 머리가 ``limit`` 안이면 거기까지 늘리고, 아니면 그 낱말의 남은 부분을 버린다.
    경로 구분자에서 자르지 않는 것은 홈 뒤의 폴더에서 자를 때 그 앞 조각(사용자 이름)이 맨 앞에
    드러나기 때문이다. 버릴 수 없으면(끝까지 한 낱말) 글자 수로 자른다.

    마지막으로 홈 경로(``home_spans``)는 가르지 않는다 — 시작이 그 안이면 그 머리로 옮긴다. 이름에
    공백이 있으면(``John Doe``) 위의 경계만으로는 이름 한가운데서 시작할 수 있다.

    Args:
        limit: 남길 글자 수(줄 · 낱말 머리까지 늘리면 최대 그 두 배, 홈 경로를 품으면 그만큼 더)
    """
    text = text.strip()
    head = _tail_head(text, limit)
    for span_start, span_end in home_spans(text):
        if span_start < head < span_end:
            head = span_start
    return text[head:]


def _tail_head(text: str, limit: int) -> int:
    """``error_tail``이 홈 경로를 보기 전에 정한 꼬리의 시작 자리."""
    start = len(text) - limit
    if start <= 0:
        return 0
    line_head = max(text.rfind("\n", 0, start), text.rfind("\r", 0, start)) + 1
    if start - line_head <= limit:
        return line_head
    word_head = max(
        (space.end() for space in _SPACE.finditer(text, line_head, start)), default=line_head
    )
    if start - word_head <= limit:
        return word_head
    word_end = _SPACE.search(text, start)
    if word_end is None:
        return start
    return len(text) - len(text[word_end.end() :].lstrip())


_SPACE = re.compile(r"\s")  # 낱말의 경계
_PIECE_BREAK = re.compile(r"[\s\\/]")  # 경로 조각의 경계 — 공백 · 경로 구분자


def error_head(text: str, limit: int) -> str:
    """오류 글의 앞 ``limit`` 글자쯤을 경로 조각의 경계에서 잘라 돌려준다 (#397).

    ``error_tail``의 앞쪽판이다. 글자 수로만 자르면 경로 조각(홈 폴더의 사용자 이름 등) 한가운데서
    끝나, 이름의 앞부분이 로그의 홈 가리기(``redact_home``)를 빠져나간다. 자른 자리가 조각 안이면 그
    조각의 끝(다음 공백 · 경로 구분자)까지 늘린다. 늘릴 길이가 ``limit``보다 길면 그 조각 앞에서
    자른다 — 조각 앞이 없으면(글 전체가 한 조각) 글자 수로 자른다.

    마지막으로 홈 경로(``home_spans``)는 가르지 않는다 — 끝이 그 안이면 그 끝으로 옮긴다.

    Args:
        limit: 남길 글자 수(조각 끝까지 늘리면 최대 그 두 배, 홈 경로를 품으면 그만큼 더)
    """
    text = text.strip()
    end = _head_end(text, limit)
    for span_start, span_end in home_spans(text):
        if span_start < end < span_end:
            end = span_end
    return text[:end]


def _head_end(text: str, limit: int) -> int:
    """``error_head``가 홈 경로를 보기 전에 정한 머리의 끝 자리."""
    # 자른 자리의 앞이나 뒤가 경계면 조각을 가르지 않는다
    if len(text) <= limit or _PIECE_BREAK.match(text, limit - 1) or _PIECE_BREAK.match(text, limit):
        return min(limit, len(text))
    piece_end = _PIECE_BREAK.search(text, limit)
    end = piece_end.start() if piece_end else len(text)
    if end - limit <= limit:
        return end
    piece_head = max((match.start() for match in _PIECE_BREAK.finditer(text, 0, limit)), default=0)
    return piece_head if piece_head > 0 else limit


def _run_controlled(
    exe: str,
    args: Sequence[str],
    timeout: float,
    cwd: str | None,
    should_stop: Callable[[], bool] | None,
    should_pause: Callable[[], bool] | None,
) -> subprocess.CompletedProcess[str]:
    """ffmpeg를 실행해 출력을 모으되, 멈춤 · 일시정지 요청을 따른다. ``run_ffmpeg``가 부른다.

    출력을 모으는 일(``communicate``)을 짧은 간격으로 끊어 그 사이에 요청을 확인한다
    (``_Control``). 돌려주는 값은 그런 확인이 없는 경로(``subprocess.run``)와 같다.
    """
    command = [exe, "-hide_banner", *args]
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            env=_subprocess_env(exe),
        )
    except OSError as e:
        raise FFmpegError(f"ffmpeg 실행 실패: {e}") from e
    _end_with_parent(process)
    control = _Control(process, timeout, should_stop, should_pause)
    # 띄우자마자 한 번 확인한다 — 띄우는 사이에 온 일시정지 · 중단이 첫 간격만큼 늦지 않게
    outcome = control.poll()
    while True:
        try:
            if outcome is None:
                output, errors = process.communicate(timeout=_STOP_POLL_SECONDS)
                return subprocess.CompletedProcess(command, process.returncode, output, errors)
        except subprocess.TimeoutExpired:
            outcome = control.poll()
        if outcome is not None:
            process.kill()  # 멈춰 있는 프로세스도 그대로 끝난다
            process.communicate()  # 파이프를 비우고 닫는다 — 프로세스가 끝난 것까지 기다린다
            if outcome == _Control.CANCELLED:
                raise FFmpegCancelledError("멈추라는 요청으로 ffmpeg를 끝냈다")
            raise FFmpegTimeoutError(f"ffmpeg가 {timeout:g}초 안에 끝나지 않았다")


def _run_reporting(
    exe: str,
    args: Sequence[str],
    timeout: float,
    cwd: str | None,
    on_out_time: Callable[[float], None],
    should_stop: Callable[[], bool] | None = None,
    should_pause: Callable[[], bool] | None = None,
) -> subprocess.CompletedProcess[str]:
    """ffmpeg를 실행하면서 진행(출력 시각)을 on_out_time에 알린다. ``run_ffmpeg``가 부른다.

    진행은 stdout으로 받는다(``-progress pipe:1``). stderr는 따로 읽어 둔다 — 한쪽 파이프만
    읽으면 다른 쪽이 차서 ffmpeg가 멈춘다. 제한 시간은 타이머가 프로세스를 끝내는 것으로
    지킨다. on_out_time이 예외를 내면 프로세스를 끝내고 그 예외를 그대로 올린다. 멈춤 · 일시정지
    요청은 지켜보는 스레드가 짧은 간격으로 확인한다(``_Control``) — 진행 줄을 기다리는 동안에도
    듣는다. 그때는 제한 시간도 그 스레드가 지킨다(멈춰 있던 시간을 빼고 센다). 프로세스가 멈춰
    있는 동안 진행 줄을 읽는 쪽은 다음 줄을 기다리며 서 있을 뿐이다.
    """
    command = [exe, "-hide_banner", "-nostats", "-progress", "pipe:1", *args]
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            env=_subprocess_env(exe),
        )
    except OSError as e:
        raise FFmpegError(f"ffmpeg 실행 실패: {e}") from e
    _end_with_parent(process)
    errors: list[str] = []
    reader = threading.Thread(target=lambda: errors.append(process.stderr.read()), daemon=True)
    reader.start()
    timed_out = threading.Event()

    def expire() -> None:
        timed_out.set()
        process.kill()

    finished, cancelled = threading.Event(), threading.Event()
    controlled = should_stop is not None or should_pause is not None

    def watch() -> None:
        control = _Control(process, timeout, should_stop, should_pause)
        while True:
            # 띄우자마자 한 번, 그 뒤로는 간격마다 확인한다
            outcome = control.poll()
            if outcome is not None:
                (cancelled if outcome == _Control.CANCELLED else timed_out).set()
                process.kill()  # 멈춰 있어도 끝난다. stdout이 닫혀 아래의 읽기가 끝난다
                return
            if finished.wait(_STOP_POLL_SECONDS):
                return

    watcher = threading.Thread(target=watch, daemon=True) if controlled else None
    # 지켜보는 스레드가 있으면 제한 시간도 그쪽이 지킨다 — 멈춰 있던 시간을 빼야 한다
    timer = threading.Timer(timeout, expire)
    if watcher is None:
        timer.start()
    else:
        watcher.start()
    try:
        for line in process.stdout:
            seconds = parse_out_time(line)
            if seconds is not None:
                on_out_time(seconds)
        returncode = process.wait()
    finally:
        timer.cancel()
        finished.set()
        if watcher is not None:
            watcher.join()
        if process.poll() is None:
            process.kill()
            process.wait()
        reader.join()
        process.stdout.close()
        process.stderr.close()
    if cancelled.is_set():
        raise FFmpegCancelledError("멈추라는 요청으로 ffmpeg를 끝냈다")
    if timed_out.is_set():
        raise FFmpegTimeoutError(f"ffmpeg가 {timeout:g}초 안에 끝나지 않았다")
    return subprocess.CompletedProcess(command, returncode, "", "".join(errors))


def read_in_chunks(path: str, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    """파일을 청크 단위로 읽는 이터레이터 — remux_stream 공급용."""
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                return
            yield chunk


def _remove_quietly(path: str) -> None:
    """존재하면 삭제한다 (실패 정리 경로 전용 — 없어도, 지우지 못해도 오류가 아니다).

    지우지 못하면(``OSError``) 경고 로그만 남긴다 (#389). 이 함수는 실패를 알리는 길에서만
    불린다 — 여기서 예외를 내면 그 예외가 알리려던 실패(``RemuxError`` 등)를 대신해, 구간의 컷
    실패가 다운로드 전체의 실패가 됐다.
    """
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as e:
        logger.warning("실패한 산출물을 지우지 못했다: %s", e)


def _close_feed(chunks: Iterable[bytes]) -> None:
    """공급 이터레이터가 제너레이터면 닫는다 — 읽던 파일을 놓게 한다 (#389).

    ffmpeg가 먼저 끝나 공급 루프가 중간에 빠지면 제너레이터는 파일을 연 채 멈춰 있다. 그것을
    가리키는 프레임을 예외의 트레이스백이 붙잡아, 실패 뒤 정리가 임시 폴더를 지울 때 그 파일이
    열려 있었다(Windows에서는 지우지 못한다). 끝까지 돈 제너레이터 · 리스트에는 할 일이 없다.
    """
    close = getattr(chunks, "close", None)
    if close is not None:
        close()


def _close_quietly(*streams) -> None:
    """프로세스 파이프를 닫는다 (이미 닫혔거나 깨진 파이프여도 오류가 아니다)."""
    for stream in streams:
        try:
            stream.close()
        except OSError:
            pass
