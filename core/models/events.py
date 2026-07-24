"""app ↔ core 콜백 계약 — 진행 이벤트와 콜백 타입 별칭 (#72, SPEC §5).

Phase 3 이후 모든 core 컴포넌트는 이 모듈의 계약만으로 앱 계층에 통지한다.
core는 Qt 시그널·이벤트 버스를 두지 않는다 (SPEC §2·§10) — 통지는 콜백
등록 방식 하나로 단일화하고, Qt Signal 변환은 앱 계층 어댑터가 담당한다.

콜백 시그니처:
- 진행: ``ProgressCallback`` — ``ProgressEvent`` 하나를 받는다.
- 완료: ``FinishedCallback`` — 인자 없이 호출된다.
- 실패: ``FailedCallback`` — 발생한 예외 객체를 **그대로** 받는다.
  문자열로 변환해 넘기지 않는다 — 타입 분기·traceback 활용은 받는 쪽의 몫이다.

스레드 규칙 (SPEC §2 스레드 경계):
- 콜백은 작업 스레드에서 호출될 수 있다. 어댑터는 콜백 안에서 Qt Signal
  emit까지만 수행한다(emit은 스레드 세이프). 위젯 직접 접근·수정 금지.
- 콜백은 빠르게 반환해야 한다. 블로킹 작업을 콜백 안에 넣지 않는다.
- 같은 태스크의 콜백 호출 순서는 보장된다: 진행 N회 → 완료 또는 실패 1회.
"""

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class ProgressEvent:
    """다운로드 진행 상황 스냅샷 (표준 라이브러리 타입만 사용).

    total_size·speed·active_threads는 아직 알 수 없는 시점(매니페스트 파싱 전,
    속도 표본 수집 전 등)이 존재하므로 None을 허용한다.
    """

    downloaded_size: int
    total_size: int | None = None
    speed: float | None = None
    active_threads: int | None = None


ProgressCallback = Callable[[ProgressEvent], None]
FinishedCallback = Callable[[], None]
FailedCallback = Callable[[BaseException], None]
