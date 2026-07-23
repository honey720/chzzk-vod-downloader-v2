"""하위 호환 re-export — DownloadState 정의는 core/models/download_state.py로 이주 (#60).

기존 호출부(application, content, download 모듈)의 import 경로를 유지하기 위한 파일이다.
새 코드는 core.models.download_state에서 직접 import할 것.
"""

from core.models.download_state import DownloadState

__all__ = ["DownloadState"]
