"""다운로드 계획 모델 — prepare()의 반환 계약 (#83, SPEC §5).

"무엇을 받을지"를 다운로더 타입과 무관한 계획 객체 하나로 감싼다.
prepare()의 반환 형태가 다운로더마다 다르면(바이트 범위 목록 vs 세그먼트
목록) 구간 선택·부분 재개·AES 키 같은 계획 정보가 추가될 때마다 시그니처가
깨진다 — 계획 객체에 필드를 더하는 방식이면 기존 호출부가 흔들리지 않는다.

items의 표현은 다운로더별 튜플을 그대로 담는다:
- file: ``(start, end)`` 바이트 범위 (양 끝 포함)
- m3u8: ``(index, 세그먼트 상대 경로)``

공통 아이템 타입으로 묶지 않는 이유: 실행 규칙 박제 테스트
(test_file_downloader_rules / test_m3u8_downloader_rules)가 재큐잉 표현을
이 튜플 형태로 고정하고 있고, 베이스 엔진은 items를 순회해 하위 다운로더의
_download_item에 되돌려줄 뿐 내용을 해석하지 않는다. 아이템 타입화는
구간 해석이 실제로 필요해지는 시점에 계획 필드 추가로 얹는다.

selections는 처음부터 튜플(리스트 형태)로 모델링한다 — 단일 구간으로
두면 다중 구간 지원 시 데이터 모델부터 소비자까지 다시 뜯어야 하지만,
튜플이면 다중 구간이 자연히 따라온다. 빈 튜플 = 전체 다운로드가 기본
케이스다. 구간 해석(비어 있지 않은 selections)은 아직 구현하지 않으며,
베이스 엔진이 명시적 미지원 예외(NotImplementedError)로 거부한다 (#83).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TimeRange:
    """영상 내 시간 구간(초). start ≤ end를 전제한다."""

    start: float
    end: float


@dataclass(frozen=True)
class DownloadPlan:
    """다운로드 한 건의 실행 계획 — prepare()가 만들고 베이스 엔진이 소비한다.

    계획 단계에서 알 수 있는 정보(전송 단위 목록, 총 크기, 후처리 필요 여부)를
    모두 담아, 실행 중에 추측이 필요 없게 한다.
    """

    # 전송 단위 목록 — file: (start, end) 바이트 범위, m3u8: (index, 세그먼트)
    items: tuple
    # 총 바이트 크기. 계획 시점에 알 수 없으면 None (m3u8)
    total_size: int | None = None
    # 다운로드 완료 후 후처리(postprocess — m3u8 병합)가 필요한지
    requires_postprocess: bool = False
    # 선택 다운로드 구간 목록. 빈 튜플 = 전체 다운로드 (#83에서는 빈 값만 지원)
    selections: tuple[TimeRange, ...] = ()

    @property
    def part_count(self) -> int:
        """전송 단위 수 — 워커 상한·진행 배열 크기의 기준."""
        return len(self.items)
