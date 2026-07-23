"""다운로드 상태 정의 — UI와 무관한 순수 도메인 열거형 (#60, SPEC §4.2).

기존 download/state.py에서 이주했다. 값과 의미는 그대로 유지한다.
기존 호출부는 download/state.py의 re-export를 통해 무변경으로 동작한다.
"""

from enum import Enum


class DownloadState(Enum):
    """다운로드 태스크의 생명주기 상태."""

    WAITING = 0  # 다운로드 대기
    RUNNING = 1  # 다운로드 실행
    PAUSED = 2  # 다운로드 중지(일시정지)
    FINISHED = 3  # 다운로드 완료
    FAILED = 4  # 다운로드 실패
