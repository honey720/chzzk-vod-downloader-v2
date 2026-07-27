"""ffmpeg 실행 파일 탐색·remux 유틸 (#88, SPEC §6.5).

세그먼트 병합 산출물은 바이트 연결이라 편집 프로그램이 읽지 못한다 —
라이브 원본 타임라인(시작 오프셋≠0)을 그대로 보유하고, 전역 인덱스(moov)가
없기 때문이다. ffmpeg 스트림 복사(remux)로 재포장해 이를 해소한다.

**실행 파일 경로 탐색은 이 모듈 하나에 격리한다** (SPEC §6.5 설계 요건).
현재 배포 방식은 pip 패키지 imageio-ffmpeg(휠 안에 정적 바이너리 동봉,
Nuitka가 표준 패키지 설정으로 번들링)이며, 커스텀 빌드 등으로 바뀌어도
``get_ffmpeg_exe()``만 고치면 된다. 못 찾으면 명확한 예외를 던진다 —
무음 실패 금지. remux 실패 시의 폴백(바이트 연결 유지)은 호출자
(다운로더 postprocess)의 책임이다.
"""

import os
import subprocess
import sys

# GUI 앱의 서브프로세스가 콘솔 창을 띄우지 않게 한다 (Windows 전용 플래그)
_CREATE_NO_WINDOW = 0x08000000


class FFmpegError(Exception):
    """ffmpeg 관련 실패의 공통 상위 예외 — 폴백 판단은 이것 하나로 잡는다."""


class FFmpegNotFoundError(FFmpegError):
    """ffmpeg 실행 파일을 찾지 못했다 — 배포(의존성 설치·번들링) 문제 신호."""


class RemuxError(FFmpegError):
    """remux 실행이 실패했다 — 입력 손상·미지원 컨테이너·디스크 부족 등."""


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


def remux(src_path: str, dst_path: str) -> None:
    """src를 재인코딩 없이(스트림 복사) dst 컨테이너로 재포장한다.

    옵션 근거 (#88 실측 — 치지직 실스트림으로 확인):
    - ``-c copy``: 재인코딩 금지(이슈 요건). 화질 무손실·수 초 내 완료
    - 타임스탬프 0 정규화는 ffmpeg 기본 동작이다(-copyts를 주지 않는 한
      입력 시작 오프셋을 빼고 기록) — 별도 옵션 불필요함을 실측으로 확인
    - ``-movflags +faststart``: 전역 인덱스(moov)를 파일 선두로 이동.
      기본값이면 moov가 꼬리에 남아 순차 읽기 도구가 인덱스를 늦게 만난다
    - 출력 포맷은 dst 확장자로 결정된다. 확장자를 인식하지 못하면 실패가
      명확히 반환되고 호출자가 폴백한다

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
        src_path,
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        dst_path,
    ]
    creationflags = _CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            creationflags=creationflags,
        )
    except OSError as e:
        raise RemuxError(f"ffmpeg 실행 실패: {e}") from e

    if proc.returncode != 0:
        # 실패한 부분 산출물을 남기지 않는다 — 폴백이 같은 경로에 쓴다
        if os.path.exists(dst_path):
            os.remove(dst_path)
        stderr_tail = proc.stderr.strip()[-500:]
        raise RemuxError(f"ffmpeg remux 실패 (exit {proc.returncode}): {stderr_tail}")
