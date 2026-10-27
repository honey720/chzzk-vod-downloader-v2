"""앱 계층 항목 상태 분리 검증 (#167 — Phase 5).

LOADING은 core 전이 규칙(download_task)에 등장하지 않는 표시 전용
상태라(#124) 앱 계층 ItemState로 분리됐다. core enum이 태스크 생명주기
5개만 갖는 것과, 병존 비교의 안전성(Enum 멤버는 클래스가 다르면 절대
같지 않다)을 고정한다. LOADING 게이트의 행동 자체는 기존
tests/unit/test_content_manager.py가 무수정으로 계속 검증한다.
"""

from app.viewmodels.item_state import ItemState
from core.models.download_state import DownloadState


def test_core_enum_has_only_task_lifecycle_states():
    """core DownloadState는 태스크 생명주기 5개만 갖는다 — LOADING 재유입 방지."""
    assert {m.name for m in DownloadState} == {
        "WAITING",
        "RUNNING",
        "PAUSED",
        "FINISHED",
        "FAILED",
    }


def test_value_five_is_not_reused():
    """값 5(구 LOADING 자리)는 재사용하지 않는다 — 로그·직렬화 혼동 방지."""
    assert all(m.value != 5 for m in DownloadState)


def test_item_state_never_equals_core_states():
    """병존 비교의 안전성: ItemState 멤버는 어떤 core 상태와도 같지 않다.

    ContentItem.downloadState에 두 타입이 섞여 들어가도 기존 ==/in
    비교식이 오동작하지 않는 근거다.
    """
    assert all(ItemState.LOADING != m for m in DownloadState)
    assert ItemState.LOADING not in list(DownloadState)
