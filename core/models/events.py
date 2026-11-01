"""app ↔ core 콜백 계약 — 진행 이벤트와 콜백 타입 별칭 (#72, SPEC §5).

Phase 3 이후 모든 core 컴포넌트는 이 모듈의 계약만으로 앱 계층에 통지한다.
core는 Qt 시그널·이벤트 버스를 두지 않는다 (SPEC §2·§10) — 통지는 콜백
등록 방식 하나로 단일화하고, Qt Signal 변환은 앱 계층 어댑터가 담당한다.

콜백 시그니처:
- 진행: ``ProgressCallback`` — ``ProgressEvent`` 하나를 받는다.
- 완료: ``FinishedCallback`` — 인자 없이 호출된다.
- 실패: ``FailedCallback`` — 발생한 예외 객체를 **그대로** 받는다.
  문자열로 변환해 넘기지 않는다 — 타입 분기·traceback 활용은 받는 쪽의 몫이다.
- 실패(키): ``FailedKeyCallback`` — 엔진 쪽(서비스 핸들)이 예외에서 고른 안내 키와 실패하던
  때의 값 묶음을 받는다(#484). 위의 예외 콜백과 함께 더해진 것이다 — 헤드리스 · core는 예외 콜백을 그대로 쓰고,
  **뷰모델(화면 쪽)은 키 콜백만 읽는다.** 예외 객체가 화면 쪽으로 넘어가지 않는다.
- 완료(값 묶음): ``FinishedCountersCallback`` — 완료 콜백과 함께, 완료하던 때의 값 묶음을
  받는다(#484). 뷰모델은 이것으로 완료를 받는다.
- 진행의 값 묶음: ``ProgressEvent.counters`` — 서비스 핸들이 통지할 때의 엔진 공유
  데이터를 떠 싣는다(#484). 뷰모델은 엔진 공유 데이터를 직접 읽지 않고 이것만 읽는다.

스레드 규칙 (SPEC §2 스레드 경계):
- 콜백은 작업 스레드에서 호출될 수 있다. 어댑터는 콜백 안에서 Qt Signal
  emit까지만 수행한다(emit은 스레드 세이프). 위젯 직접 접근·수정 금지.
- 콜백은 빠르게 반환해야 한다. 블로킹 작업을 콜백 안에 넣지 않는다.
- 같은 태스크의 콜백 호출 순서는 보장된다: 진행 N회 → 완료 또는 실패 1회.
"""

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class DownloadCounters:
    """진행 통지에 실리는 엔진 카운터의 값 묶음 — 통지하던 때의 엔진 공유 데이터 (#484).

    엔진이 계속 바꾸는 공유 데이터를 화면 쪽이 직접 읽지 않게, 서비스 핸들이 통지마다 떠서
    싣는다. 화면 쪽이 진행률 · 남은 시간 · 구간 수 · 크기를 내는 데 쓰는 값만 담는다.
    """

    total_size: int | None = None  # 받을 크기(바이트) — 모르면 None
    max_threads: int = 0  # 받을 항목 수(세그먼트 기반은 세그먼트 수)
    completed_threads: int = 0  # 끝낸 항목 수(다시 쓴 항목 포함)
    merged_segments: int = 0  # 병합한 세그먼트 수
    reused_threads: int = 0  # 받지 않고 다시 쓴 항목 수 (#376)
    reusable_threads: int = 0  # 이번 실행이 다시 쓸 항목 수 (#390)
    sections_total: int = 0  # 구간 수
    sections_done: int = 0  # 잘라서 파일로 만든 구간 수
    sections_failed: int = 0  # 자르지 못한 구간 수
    sections_resumed: int = 0  # 이전 실행이 끝내 이어받은 구간 수
    cut_progress: float | None = None  # 컷 단계의 진행(0~1) — 적힌 값이 없으면 None
    cut_preparing: bool = False  # 컷 준비(원본 받기) 중인지
    sections_output_size: int | None = None  # 만든 구간 파일 크기의 합(바이트)
    output_size: int | None = None  # 저장된 파일의 크기(바이트)
    paused_seconds: float = 0.0  # 지금까지 일시정지해 있던 시간의 합(초)
    merging: bool = False  # 후처리(병합 · 컷) 단계에 들어갔는지 — 병합 시작 통지 뒤 참


@dataclass(frozen=True)
class ProgressEvent:
    """다운로드 진행 상황 스냅샷 (표준 라이브러리 타입만 사용).

    total_size·speed·active_threads는 아직 알 수 없는 시점(매니페스트 파싱 전,
    속도 표본 수집 전 등)이 존재하므로 None을 허용한다. counters는 엔진이 만들 때는
    비어 있고, 서비스 핸들이 중계하며 채운다 (#484).
    """

    downloaded_size: int
    total_size: int | None = None
    speed: float | None = None
    active_threads: int | None = None
    # 비교(==)에는 넣지 않는다 — 같은 진행인지는 엔진이 낸 값으로 가린다. 값 묶음은 중계하는
    # 순간에 뜬 곁가지라, 넣으면 같은 통지도 뜬 때에 따라 달라진다
    counters: DownloadCounters | None = field(default=None, compare=False)


ProgressCallback = Callable[[ProgressEvent], None]
FinishedCallback = Callable[[], None]
FailedCallback = Callable[[BaseException], None]
# 실패의 (안내 키, 실패하던 때의 값 묶음) — 고를 키가 없는 실패는 키가 None. 실패 앞에는 마지막 진행
# 통지가 없어, 실패 때의 구간 수 · 크기를 값 묶음으로 함께 싣는다
FailedKeyCallback = Callable[[str | None, DownloadCounters], None]
# 완료의 값 묶음 — 완료하던 때의 카운터(만든 파일 크기 등). 완료 콜백과 함께 부른다
FinishedCountersCallback = Callable[[DownloadCounters], None]
