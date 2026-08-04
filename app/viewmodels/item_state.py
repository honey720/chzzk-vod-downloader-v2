"""앱 계층 항목 상태 — core 태스크 생명주기에 없는 표시 전용 상태 (#167).

LOADING(메타데이터 조회 중, #124)은 다운로드 태스크가 만들어지기 전의
카드 표시 상태다. core 전이 규칙(download_task)에 등장하지 않는데도
core DownloadState에 얹혀 있던 것을 여기로 분리했다.

ContentItem.downloadState에는 core DownloadState와 ItemState가 병존한다.
Enum 멤버는 클래스가 다르면 절대 같지 않으므로(==/in 비교 안전) 기존
비교식은 타입이 섞여도 오동작하지 않는다 — 이 성질은
tests/unit/test_item_state.py가 고정한다.
"""

from enum import Enum


class ItemState(Enum):
    """다운로드 태스크 이전의 카드 표시 상태."""

    LOADING = "loading"  # 메타데이터 조회 중 — findItem이 건너뛴다 (#124)
