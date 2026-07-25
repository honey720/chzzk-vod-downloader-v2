"""하위 호환 re-export — DownloadData 정의는 core/models/download_data.py로 이주 (#75).

기존 호출부(download 모듈, 테스트)의 import 경로를 유지하기 위한 파일이다.
새 코드는 core.models.download_data에서 직접 import할 것.
"""

from core.models.download_data import DownloadData

__all__ = ["DownloadData"]
