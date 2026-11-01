"""서비스 대역이 엔진 쪽 중계(서비스 핸들)를 거쳐 뷰모델에 통지하게 하는 도우미 (#484).

뷰모델은 진행의 값 묶음(``ProgressEvent.counters``)과 실패의 안내 키(``on_failed_key``)만 읽는다.
그 둘은 엔진이 아니라 서비스 핸들(``DownloadHandle``)이 붙인다. 서비스를 대역으로 갈아 끼우는
시험은 엔진 흉내(공유 데이터에 쓰고 콜백을 부름)를 그대로 두고, 그 콜백 입구만 실제 핸들의
중계로 바꾼다 — 제품 서비스와 같은 길로 값 묶음 · 키가 실린다.
"""

from core.services.download_service import DownloadHandle


def engine_side(content, kwargs: dict) -> dict:
    """제출 인자에서, 대역이 엔진 대신 부를 콜백 입구를 실제 서비스 핸들의 중계로 바꾼 사본을 낸다.

    뷰모델이 넘긴 콜백은 핸들에 그대로 건다. 돌려준 사본의 ``on_progress`` · ``on_finished`` ·
    ``on_failed``(예외를 받는다) · ``on_merge_start``는 그 핸들의 중계라, 진행에는 그 순간의
    공유 데이터가 값 묶음으로, 실패에는 예외에서 고른 안내 키가 실린다. 나머지 인자는 그대로다.
    """
    handle = DownloadHandle(
        content,
        kwargs["data"],
        kwargs.get("task_logger"),
        on_progress=kwargs.get("on_progress"),
        on_finished=kwargs.get("on_finished"),
        on_failed=kwargs.get("on_failed"),
        on_merge_start=kwargs.get("on_merge_start"),
        on_failed_key=kwargs.get("on_failed_key"),
        on_finished_counters=kwargs.get("on_finished_counters"),
    )
    return {
        "content": content,
        **kwargs,
        "on_progress": handle._relay_progress,
        "on_finished": handle._relay_finished,
        "on_failed": handle._relay_failed,
        "on_merge_start": handle._relay_merge_start,
    }
