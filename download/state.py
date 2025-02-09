from enum import Enum

class DownloadState(Enum):
    WAITING = 0     # 다운로드 대기
    RUNNING = 1     # 다운로드 실행
    PAUSED = 2      # 다운로드 중지
    FINISHED = 3    # 다운로드 완료
    FAILED = 4      # 다운로드 실패