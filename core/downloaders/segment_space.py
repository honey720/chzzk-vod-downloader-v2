"""세그먼트 다운로드(m3u8 · 암호화 VOD)가 디스크에 쓸 양을 어림하고 여유 공간과 견준다 (#309).

세그먼트로 받는 다운로드는 받기 전에 전체 크기를 모른다. 그래서 두 번 어림한다.

1. **시작 전**: 플레이리스트의 비트레이트(BANDWIDTH) × 받을 세그먼트 길이의 합. 비트레이트를
   모르면 이 단계는 없다
2. **전송 중**: 처음 ``REESTIMATE_AFTER``개를 받은 뒤 실제 평균(바이트/초) × 남은 길이. 받은
   세그먼트는 실제 크기로 센다

필요한 여유는 (더 쓸 양) × ``MARGIN_RATIO`` + ``MARGIN_BYTES``다. 임시 폴더와 산출물 폴더가
같은 볼륨이면 한 번, 다르면 각각 본다.

디스크를 가장 많이 쓰는 순간은 후처리 중이다. 넘긴 세그먼트는 곧바로 지우므로
(``BaseDownloader._remux_streamed`` · ``_cut_sections``) 구간 없는 다운로드는 세그먼트의 합이
곧 최대다. 구간 다운로드는 구간을 하나 끝낼 때마다 (아직 남은 세그먼트 + 그때까지의 구간
파일)을 따라가 가장 큰 값을 쓴다 — 구간이 겹치지 않으면 세그먼트의 합에 가깝고, 전부 겹치면
세그먼트의 합 + 구간 파일의 합이다.

이 모듈은 어림과 비교만 한다. 언제 부를지와 모자랄 때 무엇을 할지는 엔진(base.py)이 정한다.
"""

import os
import shutil
import threading
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from core.utils.paths import same_volume

MARGIN_RATIO = 1.1  # 어림의 오차 몫 — 더 쓸 양의 10%를 더 본다
# 산출물의 머리(moov) · 경계 조각 · 오디오 임시 파일 몫으로 늘 더 보는 양 — 256MiB
MARGIN_BYTES = 256 * 1024 * 1024
REESTIMATE_AFTER = 20  # 세그먼트를 이만큼 받으면 실제 평균으로 다시 어림한다


@dataclass(frozen=True)
class SectionUse:
    """구간 하나가 쓰는 세그먼트와 그 구간의 길이 — 구간 파일의 크기를 어림하는 데 쓴다."""

    first: int  # 그 구간이 쓰는 첫 세그먼트의 인덱스
    last: int  # 마지막 세그먼트의 인덱스(포함)
    seconds: float  # 구간의 길이(초) — 구간 파일에 담기는 길이


@dataclass(frozen=True)
class SpaceNeed:
    """다운로드가 끝날 때까지 디스크에 놓이는 양의 최대(바이트) — 여유분을 더하기 전의 어림."""

    temp: float  # 임시 폴더 — 받는 세그먼트 전부
    output: float  # 산출물 폴더 — 산출물(구간 다운로드는 구간 파일의 합)
    together: float  # 두 폴더가 같은 볼륨일 때 — 세그먼트와 산출물의 합이 가장 큰 순간
    on_disk: int  # 그 가운데 이미 임시 폴더에 놓인 세그먼트(바이트) — 여유 공간에서 이미 빠져 있다


@dataclass(frozen=True)
class SpaceCheck:
    """볼륨 하나를 본 결과."""

    where: str  # 본 곳 — "both"(두 폴더가 같은 볼륨) · "temp"(임시 폴더) · "output"(산출물 폴더)
    needed: int  # 그 볼륨에 비어 있어야 하는 양(바이트) — 여유분을 더한 값
    free: int  # 그 볼륨의 여유 공간(바이트)

    @property
    def short(self) -> bool:
        """여유 공간이 모자란지."""
        return self.needed > self.free


def free_bytes(folder: str) -> int:
    """그 폴더가 놓인 볼륨의 여유 공간(바이트). 폴더가 아직 없으면 있는 윗폴더에서 잰다."""
    return shutil.disk_usage(_existing(folder)).free


def _existing(folder: str) -> str:
    """folder에서 위로 올라가며 처음 만나는, 실제로 있는 폴더."""
    path = os.path.abspath(folder)
    while not os.path.isdir(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path


def required_free(more: float) -> int:
    """더 쓸 양(바이트)에 여유분을 더한 값 — 볼륨에 이만큼은 비어 있어야 한다."""
    return int(max(more, 0.0) * MARGIN_RATIO) + MARGIN_BYTES


class SegmentSpace:
    """다운로드 하나가 받을 세그먼트의 크기를 어림하고 여유 공간과 견준다.

    엔진이 계획을 세울 때 만들고, 세그먼트를 다 받을 때마다 ``received``로 실제 크기를 알린다.
    ``received``는 받는 스레드 여럿에서 불리고, 나머지는 엔진의 실행 스레드에서 불린다.
    """

    def __init__(
        self,
        durations: Mapping[int, float],
        *,
        bandwidth: int | None = None,
        sections: Sequence[SectionUse] = (),
        on_disk: Mapping[int, int] | None = None,
    ):
        """
        Args:
            durations: 받을 세그먼트의 인덱스 → 길이(초, #EXTINF). 이 다운로드가 쓰는 세그먼트만
                담는다 — 이미 받아 둔 것도 넣는다
            bandwidth: 플레이리스트가 선언한 비트레이트(bit/s, BANDWIDTH). 모르면 None —
                시작 전 어림을 하지 않는다
            sections: 구간 다운로드면 이번 실행이 만들 구간들 — **만드는 순서대로**. 구간 없는
                다운로드는 빈 것
            on_disk: 이미 임시 폴더에 받아 둔 세그먼트의 인덱스 → 크기(바이트)
        """
        self._durations = dict(durations)
        self._bandwidth = bandwidth if bandwidth and bandwidth > 0 else None
        self._sections = tuple(sections)
        self._sizes: dict[int, int] = dict(on_disk or {})  # 실제 크기를 아는 세그먼트
        self._lock = threading.Lock()
        self._reestimated = False

    def received(self, index: int, size: int) -> None:
        """세그먼트 하나를 다 받았다 — 디스크에 놓인 크기(바이트)를 적는다."""
        with self._lock:
            self._sizes[index] = size

    def reestimate_due(self) -> bool:
        """실제 평균으로 다시 어림할 때가 됐는지 — 한 번만 참이다.

        크기를 아는 세그먼트가 ``REESTIMATE_AFTER``개에 이르렀고 아직 받을 것이 남았을 때다.
        받을 것이 남지 않았으면 다시 어림할 것이 없다.
        """
        with self._lock:
            if self._reestimated:
                return False
            known = sum(1 for index in self._sizes if index in self._durations)
            if known < REESTIMATE_AFTER or known >= len(self._durations):
                return False
            self._reestimated = True
            return True

    def need(self, measured: bool) -> SpaceNeed | None:
        """지금 아는 것으로 어림한 최대 사용량. 어림할 수 없으면 None이다.

        받은 세그먼트는 실제 크기로 센다. 받지 않은 것은 (바이트/초) × 그 세그먼트의 길이다.

        Args:
            measured: 참이면 바이트/초를 받은 세그먼트의 실제 평균으로 잡는다(전송 중의 어림).
                거짓이면 선언된 비트레이트 ÷ 8이다(시작 전의 어림) — 이미 받아 둔 세그먼트가
                있어도 그 몇 개로 평균을 내지 않는다. 그 값을 모르면(비트레이트 선언 없음 ·
                받은 세그먼트의 #EXTINF 없음) 어림하지 않는다
        """
        with self._lock:
            known = {i: size for i, size in self._sizes.items() if i in self._durations}
        rate = self._rate(known) if measured else self._declared_rate()
        if rate is None and len(known) < len(self._durations):
            return None
        sizes = {
            index: float(known[index]) if index in known else rate * seconds
            for index, seconds in self._durations.items()
        }
        segments = sum(sizes.values())
        on_disk = sum(known.values())
        if not self._sections:
            # 넘긴 세그먼트를 곧바로 지운다 — 산출물이 커지는 만큼 세그먼트가 준다
            return SpaceNeed(temp=segments, output=segments, together=segments, on_disk=on_disk)

        uses = Counter(
            index
            for section in self._sections
            for index in range(section.first, section.last + 1)
            if index in sizes
        )
        files = 0.0
        peak = segments  # 후처리를 시작할 때 — 세그먼트 전부, 구간 파일 없음
        for section in self._sections:
            indexes = [i for i in range(section.first, section.last + 1) if i in sizes]
            held = sum(sizes[i] for i in indexes)
            seconds = sum(self._durations[i] for i in indexes)
            # 구간 파일은 그 구간이 쓰는 세그먼트 가운데 구간 길이만큼의 몫이다
            files += held * min(section.seconds / seconds, 1.0) if seconds > 0 else held
            for index in indexes:
                uses[index] -= 1
            left = sum(sizes[index] for index, count in uses.items() if count > 0)
            peak = max(peak, left + files)
        return SpaceNeed(temp=segments, output=files, together=peak, on_disk=on_disk)

    def _rate(self, known: Mapping[int, int]) -> float | None:
        """받은 세그먼트의 실제 평균(바이트/초). 받은 것이 없거나 그 길이를 모르면 None."""
        seconds = sum(self._durations[index] for index in known)
        return sum(known.values()) / seconds if known and seconds > 0 else None

    def _declared_rate(self) -> float | None:
        """선언된 비트레이트로 본 바이트/초. 선언이 없으면 None."""
        return self._bandwidth / 8 if self._bandwidth is not None else None


def check_space(need: SpaceNeed, temp_dir: str, output_dir: str) -> tuple[SpaceCheck, ...]:
    """어림한 양을 볼륨마다 여유 공간과 견준다 — 본 볼륨마다 결과 하나.

    두 폴더가 같은 볼륨이면 한 번(세그먼트와 산출물의 합이 가장 큰 순간), 다르면 각각 본다.
    이미 받아 둔 세그먼트는 여유 공간에서 이미 빠져 있으므로 더 쓸 양에서 뺀다. 여유 공간을
    알 수 없는 볼륨은 결과에 넣지 않는다 — 확인하지 않는다.
    """
    if same_volume(_existing(temp_dir), _existing(output_dir)):
        wanted = [("both", need.together - need.on_disk, output_dir)]
    else:
        wanted = [
            ("temp", need.temp - need.on_disk, temp_dir),
            ("output", need.output, output_dir),
        ]
    checks = []
    for where, more, folder in wanted:
        try:
            free = free_bytes(folder)
        except OSError:
            continue
        checks.append(SpaceCheck(where, required_free(more), free))
    return tuple(checks)
