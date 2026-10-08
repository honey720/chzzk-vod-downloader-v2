"""mp4 샘플 표에서 뽑은 색인 모델 (#178).

인코딩 완료 VOD는 mp4 파일 하나로 내려온다. 그 파일의 moov에는 샘플마다
시각·크기·파일 위치가 적혀 있어, 받기 전에 프레임 시각과 받을 바이트 범위를
정확히 계산할 수 있다. 이 모듈은 그 계산의 입력과 출력을 담는 불변 모델이다 —
해석은 ``core.api.mp4``, 구간 → 바이트 범위 계산은 ``core.utils.mp4_ranges``가 한다.

시각의 기준: 편집 목록(elst)을 적용한 표시 시각이고, 영상·오디오를 통틀어 가장
먼저 표시되는 샘플의 시각이 0이다.
"""

import copy
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from fractions import Fraction


@dataclass(frozen=True)
class Mp4Track:
    """트랙 하나의 샘플 표를 담는다.

    샘플별 표는 길이가 같고 **디코드 순서**(파일에 적힌 샘플 순서)다. 해석기(``core.api.mp4``)는
    표를 읽기 전용 연속 배열(``core.models.sample_column.SampleColumn``)로 채운다 — 긴 영상은
    샘플이 수백만 개라 값을 하나씩 파이썬 객체로 들면 색인이 1GB를 넘는다 (#309). 튜플처럼
    읽는다(인덱스 · 조각 · 길이 · 순회 · 값이 같은 튜플과 ``==``).
    """

    timescale: int  # 이 트랙의 초당 틱 수 (mdhd)
    times: Sequence[float]  # 샘플별 표시 시각(초). 편집 목록이 가린 샘플은 음수다
    decode_times: Sequence[float]  # 샘플별 DTS(초). times와 같은 기준(VOD 시작 = 0)이다
    durations: Sequence[float]  # 샘플별 길이(초)
    offsets: Sequence[int]  # 샘플별 파일 안 시작 위치(바이트)
    sizes: Sequence[int]  # 샘플별 크기(바이트)
    # 청크마다의 첫 샘플 인덱스(오름차순). 청크는 파일 안에 이어 붙어 놓인 샘플 묶음이고
    # stco/co64는 청크의 위치만 적는다 — 받을 범위를 청크 단위로 맞출 때 쓴다
    chunk_starts: Sequence[int]
    sync_samples: Sequence[int]  # 단독으로 디코드를 시작할 수 있는 샘플의 인덱스(오름차순)
    # 오디오 샘플 엔트리가 선언한 비트레이트(bit/s) — esds·btrt. 영상 트랙이거나 적혀 있지
    # 않으면 None이다 (#309)
    declared_bitrate: int | None = None


@dataclass(frozen=True)
class Mp4Index:
    """mp4 한 파일의 프레임 시각·키프레임·길이와 트랙별 샘플 표를 담는다.

    ``frame_pts``는 오름차순이라 ``core.utils.timecode.snap_to_frame``에 그대로 넣는다.
    프레임별 표도 트랙의 표처럼 읽기 전용 연속 배열이다(``Mp4Track`` 참고).
    """

    frame_pts: Sequence[float]  # 표시되는 영상 프레임의 PTS(초), 표시 순서
    frame_samples: Sequence[int]  # frame_pts와 같은 순서로, 그 프레임의 video 샘플 인덱스
    keyframes: Sequence[int]  # 키프레임인 프레임의 번호(frame_pts의 인덱스, 오름차순)
    duration: float  # 영상 길이(초) — 마지막으로 표시되는 영상 프레임이 끝나는 시각
    fps: Fraction  # 샘플 표가 선언한 프레임률 — timescale ÷ 가장 많은 샘플 길이
    video: Mp4Track  # 영상 트랙
    audio: Mp4Track | None  # 오디오 트랙. 없으면 None
    # 파일 안에서 moov가 놓인 (시작, 끝) 바이트 — 양 끝 포함. 파일에서 찾아 읽었을 때만
    # 채운다(read_mp4_index · fetch_mp4_index). moov bytes만 해석했으면 None이다
    moov_range: tuple[int, int] | None = None


@dataclass(frozen=True)
class Mp4Head:
    """파일에서 읽은 moov의 색인과, 그때 받은 파일 앞부분의 바이트를 담는다 (#309).

    구간 다운로드는 받은 범위만 든 부분 mp4를 만들 때 moov가 다시 필요하다. 색인을
    만들면서 받은 바이트를 들고 있다가 그대로 쓴다 — 같은 바이트를 두 번 받지 않는다.
    """

    index: Mp4Index  # moov를 해석한 색인
    # 파일의 0부터 moov의 마지막 바이트까지. moov가 첫 읽기 안에서 시작하지 않았으면
    # (mdat 뒤의 moov 등) 앞부분을 받지 않았으므로 None이다
    data: bytes | None
    # moov를 해석하는 데 걸린 시간(초) — 받는 시간과 나눠 보려고 적는 진단값이다. 재지
    # 않았으면 None. 같은지 견줄 때와 repr에는 들지 않는다
    parse_seconds: float | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class Mp4Raw:
    """파일에서 받은 moov의 바이트를 담는다 — 아직 해석하지 않은 것 (#309).

    긴 영상의 moov는 수십 MB이고, 샘플마다 펼친 색인(``Mp4Index``)을 만드는 데 몇 초가 걸린다.
    프레임률과 길이만 필요한 쪽(구간 편집 창)은 이것에서 가볍게 읽고
    (``core.api.mp4.summarize_mp4``), 색인이 필요한 쪽은 필요할 때 해석한다
    (``core.api.mp4.index_mp4``). 같은 바이트를 두 번 받지 않는다.
    """

    moov: bytes = field(repr=False)  # moov 상자 전체(머리 포함)
    moov_range: tuple[int, int]  # 파일 안에서 moov가 놓인 (시작, 끝) 바이트 — 양 끝 포함
    # 파일의 0부터 moov가 시작하기 전까지의 바이트(ftyp 등). moov가 첫 읽기 안에서 시작하지
    # 않았으면(mdat 뒤의 moov 등) 앞부분을 받지 않았으므로 None이다
    prefix: bytes | None = field(default=None, repr=False)


class PendingMp4Head:
    """받아 둔 moov를 필요할 때 **한 번만** 해석해 여럿이 함께 쓰게 한다 (#309).

    긴 영상의 moov는 해석에 몇 초가 걸리고 그동안 수백 MB를 쓴다. 받을 크기를 세는 쪽과
    다운로드 엔진이 같은 moov를 거의 같은 때에 필요로 할 수 있다 — 각자 해석하면 그 몇 초
    동안 해석이 둘 돈다. 이 객체를 함께 쥐면 먼저 ``get()``을 부른 쪽이 해석하고, 다른 쪽은
    끝나기를 기다렸다가 같은 색인을 받는다. 도는 해석을 멈출 방법이 없어, 멈추는 대신 기다린다.

    해석이 끝나면 바이트를 놓는다 — 색인(``Mp4Head.data``)이 같은 바이트를 들고 있다.

    실패는 **다시 해도 같을 것만** 기억한다(``permanent`` — moov 자체가 틀린 경우). 그런 실패는
    바이트를 놓고, 그 뒤로 청하는 쪽마다 같은 예외를 낸다(다시 해석하지 않는다). 그 밖의 예외
    (메모리 부족처럼 그때의 사정으로 난 것)는 기억하지 않는다 — 그 청에만 그대로 내고 바이트를
    놓지 않아, 다음에 청한 쪽(기다리던 쪽 포함)이 해석을 다시 돌린다.

    기억하는 예외에는 traceback을 두지 않는다 — traceback은 해석 중의 지역 변수(moov 바이트 ·
    만들던 표)를 붙잡아, 이 묶음이 살아 있는 동안 그것들이 풀리지 않는다. 처음 실패한 청에는
    원래의 예외를 그대로 낸다(해석 자리까지의 traceback이 그쪽 로그에 남는다).
    """

    def __init__(
        self,
        raw: Mp4Raw,
        build: Callable[[Mp4Raw], Mp4Head],
        permanent: tuple[type[Exception], ...] = (),
    ):
        """
        Args:
            raw: 받아 둔 moov
            build: 그것을 해석하는 함수 — ``core.api.mp4.index_mp4``
              (``core.api.mp4.pending_mp4_head``로 만든다)
            permanent: 다시 해석해도 같은 결과일 실패의 예외 종류 — 이것만 기억한다.
              주지 않으면 어떤 실패도 기억하지 않는다
        """
        self._raw: Mp4Raw | None = raw
        self._build = build
        self._permanent = permanent
        self._head: Mp4Head | None = None
        self._error: Exception | None = None  # 기억한 실패 — traceback이 없다
        self._lock = threading.Lock()  # 해석을 한 번만 돌게 한다 — 기다리는 쪽이 여기서 선다

    @property
    def raw(self) -> Mp4Raw | None:
        """받아 둔 바이트. 해석이 끝났으면 None이다."""
        return self._raw

    def peek(self) -> Mp4Head | None:
        """이미 해석한 색인. 아직이면 None — 기다리지 않는다."""
        return self._head

    def get(self) -> Mp4Head:
        """색인을 돌려준다. 아직이면 해석하고, 다른 쪽이 해석하는 중이면 끝나기를 기다린다.

        Raises:
            Exception: 해석이 던진 예외. 기억하는 종류(``permanent``)면 그 뒤로 부를 때마다
                같은 종류 · 같은 글의 예외를 던진다
        """
        with self._lock:
            if self._error is not None:
                # 앞의 청이 남긴 traceback(그 청의 호출 자리들)을 떼고 던진다
                raise self._error.with_traceback(None)
            if self._head is None:
                try:
                    self._head = self._build(self._raw)
                except self._permanent as e:
                    self._error = _without_traceback(e)
                    self._raw = None
                    raise  # 처음 실패한 청 — 원래의 예외를 해석 자리까지의 traceback과 함께 낸다
                self._raw = None
            return self._head


def _without_traceback(error: Exception) -> Exception:
    """같은 종류 · 같은 글의 예외를 traceback과 원인 사슬 없이 새로 만든다 — 기억해 두는 용도다."""
    remembered = copy.copy(error)
    remembered.__traceback__ = remembered.__cause__ = remembered.__context__ = None
    return remembered


@dataclass(frozen=True)
class Mp4Summary:
    """moov에서 가볍게 읽은 프레임률 · 길이 — 색인(``Mp4Index``)의 같은 이름의 값과 비트까지 같다."""

    fps: Fraction  # 샘플 표가 선언한 프레임률 — timescale ÷ 가장 많은 샘플 길이
    duration: float  # 영상 길이(초) — 마지막으로 표시되는 영상 프레임이 끝나는 시각
    frames: int  # 영상 트랙의 샘플 수


@dataclass(frozen=True)
class SelectionBytes:
    """구간 하나를 받는 데 필요한 파일 바이트 범위를 담는다."""

    # 받을 바이트 범위 목록 — (시작, 끝) 양 끝 포함. file 다운로더의 items와 같은 표현이다
    ranges: tuple[tuple[int, int], ...]
    total_size: int  # ranges의 바이트 수 합
    keyframe: int  # 범위가 시작하는 키프레임의 프레임 번호
    first_frame: int  # 구간의 첫 프레임 번호
    last_frame: int  # 구간의 끝 프레임 번호 (포함)
