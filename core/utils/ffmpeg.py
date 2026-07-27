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
"""

import os
import subprocess
import sys
import threading
from collections.abc import Iterable, Iterator

# GUI 앱의 서브프로세스가 콘솔 창을 띄우지 않게 한다 (Windows 전용 플래그)
_CREATE_NO_WINDOW = 0x08000000


class FFmpegError(Exception):
    """ffmpeg 관련 실패의 공통 상위 예외 — 실패 처리 판단은 이것 하나로 잡는다."""


class FFmpegNotFoundError(FFmpegError):
    """ffmpeg 실행 파일을 찾지 못했다 — 배포(의존성 설치·번들링) 문제 신호."""


class RemuxError(FFmpegError):
    """remux 실행이 실패했다 — 입력 손상·디스크 부족 등."""


def get_ffmpeg_exe() -> str:
    """ffmpeg 실행 파일 경로를 반환한다.

    Raises:
        FFmpegNotFoundError: 패키지 미설치·바이너리 미동봉 등으로 찾지 못한 경우
    """
    try:
        import imageio_ffmpeg
    except ImportError as e:
        raise FFmpegNotFoundError(
            "imageio-ffmpeg 패키지가 설치되어 있지 않다 — 의존성 설치(uv sync)를 확인하라"
        ) from e
    try:
        # imageio_ffmpeg은 휠에 동봉된 바이너리를 찾지 못하면 RuntimeError를 던진다
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        raise FFmpegNotFoundError(f"ffmpeg 실행 파일을 찾지 못했다: {e}") from e


def remux_stream(chunks: Iterable[bytes], dst_path: str) -> None:
    """바이트 스트림을 stdin으로 받아 재인코딩 없이(스트림 복사) mp4로 재포장한다.

    옵션 근거 (#88·#92 실측 — 치지직 실스트림으로 확인):
    - ``-i pipe:0``: 세그먼트 바이트 연결 스트림을 그대로 공급받는다(중간 파일
      없음). fMP4·TS는 스트리밍 컨테이너라 파이프 입력으로 정상 판별된다
    - ``-c copy``: 재인코딩 금지. 화질 무손실
    - 타임스탬프 0 정규화는 ffmpeg 기본 동작(-copyts 미지정) — 별도 옵션 불필요
    - ``-movflags +faststart``: 전역 인덱스(moov)를 파일 선두로 이동
    - ``-f mp4``: 출력 컨테이너를 명시한다(#92) — 산출물 확장자가 .mp4가
      아니어도(유저가 저장 파일명을 바꾼 경우) 확장자 추론에 기대지 않는다

    실패 시 불완전한 산출물을 삭제하고 예외를 던진다 — 폴백은 없다(#92).
    공급 이터레이터가 예외를 던지면(다운로드 중단 등) 프로세스를 종료하고
    산출물을 지운 뒤 그 예외를 그대로 전파한다.

    Raises:
        FFmpegNotFoundError: ffmpeg 실행 파일을 찾지 못한 경우
        RemuxError: ffmpeg가 0이 아닌 코드로 종료했거나 실행 자체가 실패한 경우
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
        "-c",
        "copy",
        "-movflags",
        "+faststart",
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
        )
    except OSError as e:
        raise RemuxError(f"ffmpeg 실행 실패: {e}") from e

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
        _remove_quietly(dst_path)
        raise

    try:
        proc.stdin.close()
    except OSError:
        pass
    returncode = proc.wait()
    reader.join(timeout=5)

    if returncode != 0:
        # 실패한 부분 산출물을 남기지 않는다 (#92 — 정상 파일로 오인 방지)
        _remove_quietly(dst_path)
        stderr_tail = b"".join(stderr_parts).decode(errors="replace").strip()[-500:]
        raise RemuxError(f"ffmpeg remux 실패 (exit {returncode}): {stderr_tail}")


def read_in_chunks(path: str, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    """파일을 청크 단위로 읽는 이터레이터 — remux_stream 공급용."""
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                return
            yield chunk


def _remove_quietly(path: str) -> None:
    """존재하면 삭제한다 (실패 정리 경로 전용 — 없어도 오류가 아니다)."""
    if os.path.exists(path):
        os.remove(path)
