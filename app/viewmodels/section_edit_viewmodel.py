"""구간 편집 창의 뷰모델 — 기준값 조회 · 행 해석 · 검증 · 카드에 쓰기 (#309).

구간 편집 창(``app/views/section_dialog.py``)은 이 뷰모델만 본다. 타임코드 해석과 구간
검증은 core의 기존 계약(``core/utils/timecode.py`` · ``core/utils/selections.py``)을 그대로
부른다 — 규칙을 여기서 다시 구현하지 않는다. 엔진이 다운로드를 시작할 때 같은 함수로 다시
검증하므로, 여기서 통과한 구간은 같은 프레임률 · 같은 길이에서 엔진도 통과시킨다.

행은 ``TimeRange``가 아니라 글자 둘(시작 · 끝)로 든다. ``TimeRange``는 시작 < 끝이 아니면
만들 수 없어 틀린 입력을 들고 있을 수 없다. 확인할 때만 ``TimeRange``로 바꾼다.

**전체 구간 하나 = 구간 없음.** 창은 구간이 없는 카드를 "처음 ~ 끝" 한 행으로 보여 주고,
확인할 때 행이 그 한 행뿐이면 카드에 빈 튜플을 쓴다 — 전체 다운로드는 구간 다운로드
경로(받은 뒤 자르기)가 아니라 기존 경로로 간다.
"""

import logging
import weakref
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from types import SimpleNamespace

from PySide6.QtCore import QObject, QThreadPool, Signal

import app.section_basis as section_basis
from app.section_basis import SectionBasis
from app.viewmodels.data import (
    SECTION_CHECK_PENDING,
    SECTION_CHECK_UNVERIFIED,
    ContentItem,
)
from core.models.download_state import DownloadState
from core.models.plan import TimeRange
from core.utils.selections import (
    MAX_SELECTIONS,
    SELECTION_DUPLICATE,
    SELECTION_ORDER,
    SELECTION_OUT_OF_RANGE,
    SELECTION_TOO_MANY,
    SELECTION_TOO_SHORT,
    reaches_end,
    validate_selections,
)
from core.utils.timecode import (
    TIMECODE_FIELD_OUT_OF_RANGE,
    TIMECODE_FRAME_OUT_OF_RANGE,
    TIMECODE_INVALID_FORMAT,
    TimecodeError,
    format_milliseconds,
    format_timecode,
    frame_index,
    frame_rate,
    parse_timecode,
)

logger = logging.getLogger(__name__)

STATE_LOADING = "loading"  # 기준값을 조회하는 중 — 입력을 받지 않는다
STATE_READY = "ready"  # 조회가 끝났다 — 편집할 수 있다
STATE_FAILED = "failed"  # 조회가 실패했다 — 편집할 수 없다

START, END = 0, 1  # 행의 칸 번호

SHOW_NOW = "now"  # 치는 도중에도 바로 띄운다
SHOW_ON_LEAVE = "leave"  # 칸을 떠나거나 Enter를 칠 때 띄운다

# 치고 있는 칸의 오류를 언제 띄우는가 — 오류 키 → (시작 칸을 칠 때, 끝 칸을 칠 때).
#
# 숫자는 오른쪽부터 채워져 칠수록 값이 커지기만 한다(프레임률 10 이상). 그래서 "너무 크다"는
# 오류는 더 쳐도 풀리지 않아 바로 띄우고, "아직 작다"와 "자리 값이 넘쳤다"는 더 치면 풀릴 수
# 있어 칸을 떠날 때 띄운다.
#
# 이 표는 **치고 있는 행**에만 쓴다. 치고 있지 않은 행의 오류(다른 행의 값 때문에 생긴 중복
# 등)와 창을 열 때의 오류는 언제나 바로 띄운다. 오류가 사라지는 것도 언제나 바로다. 띄우지
# 않은 오류도 확인 버튼은 막는다(``canCommit``).
ERROR_TIMING: dict[str, tuple[str, str]] = {
    SELECTION_OUT_OF_RANGE: (SHOW_NOW, SHOW_NOW),  # 영상 길이 초과 — 더 쳐도 커지기만 한다
    SELECTION_ORDER: (SHOW_NOW, SHOW_ON_LEAVE),  # 시작이 끝을 넘음 / 끝이 아직 시작에 못 미침
    SELECTION_TOO_SHORT: (SHOW_NOW, SHOW_ON_LEAVE),
    SELECTION_DUPLICATE: (SHOW_ON_LEAVE, SHOW_ON_LEAVE),  # 치는 도중 다른 행과 잠깐 같아진다
    SELECTION_TOO_MANY: (SHOW_NOW, SHOW_NOW),
    TIMECODE_FIELD_OUT_OF_RANGE: (SHOW_ON_LEAVE, SHOW_ON_LEAVE),  # 초 · 분 60 이상
    TIMECODE_FRAME_OUT_OF_RANGE: (SHOW_ON_LEAVE, SHOW_ON_LEAVE),  # 프레임 ≥ 프레임률
    TIMECODE_INVALID_FORMAT: (SHOW_ON_LEAVE, SHOW_ON_LEAVE),
}

# 창이 받는 타임코드의 칸 수 — HH:MM:SS:FF. parse_timecode는 짧은 형태(MM:SS 등)도 받지만
# 창은 네 칸만 받는다(오너 결정)
_TIMECODE_FIELDS = 4

# 두 프레임률을 같은 것으로 보는 상대 오차의 상한 — 1%. 목록의 선언값(60.0)과 조회로 정한
# 값(60000/1001)의 차이는 0.1%이고, 30과 60 같은 실제 변경은 이보다 훨씬 크다
_SAME_RATE_TOLERANCE = Fraction(1, 100)


# 받은 moov를 들고 있는 카드 — 앱 전체에서 하나뿐이다. 약한 참조라 카드가 사라지면 함께 사라진다
_head_owner: weakref.ref | None = None


def keep_section_head(item: ContentItem, base_url: str, head) -> None:
    """구간을 정하며 받은 moov를 카드에 둔다 — 다운로드를 시작할 때 엔진에 넘긴다 (#309).

    **한 번에 한 카드의 것만 든다.** 긴 영상의 해석된 색인은 메모리를 크게 쓴다(8시간 60fps에서
    약 770MB — 샘플마다의 시각 · 위치 · 크기). 다른 카드가 들고 있던 것은 여기서 버린다.
    head가 None이면 이 카드의 것을 버린다.

    Args:
        base_url: 그 moov를 받은 주소 — 다운로드를 시작할 때의 주소와 같을 때만 넘긴다
    """
    global _head_owner
    owner = _head_owner() if _head_owner is not None else None
    if owner is not None and owner is not item:
        owner.section_head = None
    item.section_head = (base_url, head) if head is not None else None
    _head_owner = weakref.ref(item) if head is not None else None


def take_section_head(item: ContentItem):
    """카드에 둔 moov를 꺼낸다 — 카드에서는 비운다. 넘길 수 없으면 None.

    그 moov를 받은 주소가 지금의 주소와 같을 때만 돌려준다 — 해상도가 바뀌었으면 다른 파일의
    moov다. 꺼낸 뒤에는 엔진이 들고, 다운로드가 끝나면 엔진과 함께 사라진다.
    """
    kept = getattr(item, "section_head", None)
    item.section_head = None
    if kept is None:
        return None
    base_url, head = kept
    return head if base_url == item.base_url else None


def declared_frame_rate(rep) -> Fraction | None:
    """해상도 목록의 한 항목이 든 선언 프레임률. 없으면 None.

    항목의 값은 조회 때 응답에서 읽은 소수다(마스터 플레이리스트의 FRAME-RATE ·
    매니페스트의 frameRate, #318). 분모 1001까지의 가까운 비로 옮긴다.
    """
    declared = getattr(rep, "frame_rate", None)
    if not declared or declared <= 0:
        return None
    return Fraction(declared).limit_denominator(1001)


def same_frame_rate(first: Fraction, second: Fraction) -> bool:
    """두 프레임률이 같은 프레임 격자를 뜻하는지 — 상대 오차 1% 안이면 같다."""
    return abs(first - second) <= max(first, second) * _SAME_RATE_TOLERANCE


def refit_selections(selections: Sequence[TimeRange], fps: Fraction) -> tuple[TimeRange, ...]:
    """구간을 시각 기준으로 새 프레임률의 프레임에 다시 맞춘다.

    시작 · 끝을 각각 가장 가까운 프레임의 시각으로 옮긴다. 옮긴 뒤 시작과 끝이 같은
    프레임이 되면(60fps의 한 프레임짜리 구간을 30fps로 옮길 때) 끝을 한 프레임 뒤로 둔다 —
    구간의 최소 길이는 한 프레임이다.
    """
    rate = frame_rate(fps)
    refit = []
    for selection in selections:
        first = max(frame_index(selection.start, rate), 0)
        last = max(frame_index(selection.end, rate), first + 1)
        refit.append(TimeRange(float(Fraction(first) / rate), float(Fraction(last) / rate)))
    return tuple(refit)


@dataclass(frozen=True)
class RefitResult:
    """구간을 새 기준값(프레임률 · 길이)에 다시 맞춘 결과를 담는다."""

    selections: tuple[TimeRange, ...]  # 다시 맞춘 구간. 바뀐 것이 없으면 받은 튜플 그대로다
    regridded: bool  # 프레임률이 달라 구간을 새 프레임 경계로 옮겼다
    end_pulled: bool  # 새 길이를 넘는 구간의 끝을 새 영상의 끝으로 당겼다
    end_extended: bool  # 영상 끝에 닿아 있던 구간의 끝을 더 긴 새 끝으로 늘렸다
    unfit: frozenset[int]  # 당길 수 없는 구간의 번호(0부터) — 새 영상의 끝 이후에서 시작한다


def refit_to_basis(
    selections: tuple[TimeRange, ...], old: SectionBasis, new: SectionBasis
) -> RefitResult:
    """조회한 기준값으로 구간을 다시 맞춘다 — 프레임은 시각 기준으로, 끝은 새 길이에.

    - 프레임률이 다르면 시작 · 끝을 새 프레임률의 가장 가까운 프레임 경계로 옮긴다
      (``refit_selections``)
    - **끝은 새 영상의 끝을 넘지 않는다.** 새 길이를 넘는 구간은 끝을 새 영상의 끝으로
      당긴다 — 새 해상도에 없는 부분만 잘린다
    - **옛 영상의 끝에 닿아 있던 구간**(``reaches_end``)은 새 영상의 끝을 따라간다. 새 영상이
      더 짧으면 당겨지고, 더 길면 늘어난다. 끝에 닿아 있지 않던 구간은 늘어나지 않는다
    - **당길 수 없는 구간** — 새 영상의 끝 이후에서 시작해 한 프레임도 남지 않는 구간 — 은
      고치지 않고 번호만 돌려준다. 지우지 않는다

    Args:
        selections: 옛 기준값으로 확인된 구간
        old: 그 구간을 확인한 기준값
        new: 새 해상도에서 조회한 기준값
    """
    old_rate, new_rate = frame_rate(old.fps), frame_rate(new.fps)
    regridded = old_rate != new_rate
    moved = refit_selections(selections, new_rate) if regridded else selections
    new_end = last_frame_seconds(new.duration, new_rate)
    last_frame = frame_index(new_end, new_rate)
    refit: list[TimeRange] = []
    unfit: set[int] = set()
    end_pulled = end_extended = False
    for number, (before, after) in enumerate(zip(selections, moved)):
        if _violations_of((after.start, new_end), new.duration, new_rate):
            unfit.add(number)  # 시작이 새 영상의 끝 이후다 — 끝을 당겨도 한 프레임이 안 남는다
            refit.append(after)
            continue
        end_frame = frame_index(after.end, new_rate)
        follows_end = reaches_end(before.end, old.duration, old_rate)
        if end_frame > last_frame:
            end_pulled = True
        elif follows_end and end_frame < last_frame:
            end_extended = True
        else:
            refit.append(after)
            continue
        refit.append(TimeRange(after.start, new_end))
    if not (regridded or end_pulled or end_extended):
        return RefitResult(selections, False, False, False, frozenset(unfit))
    return RefitResult(tuple(refit), regridded, end_pulled, end_extended, frozenset(unfit))


def last_frame_seconds(duration: float, fps: Fraction) -> float:
    """구간의 끝으로 적을 수 있는 가장 늦은 시각 — 영상 길이를 넘지 않는 마지막 프레임 경계.

    영상 길이는 프레임 경계에 놓이지 않을 수 있어, 길이를 그대로 타임코드로 적으면 반올림으로
    길이를 넘을 수 있다. 검증(``validate_selections``)이 받는 가장 큰 프레임 번호를 고른다.
    """
    rate = frame_rate(fps)
    nearest = frame_index(duration, rate)
    for frames in (nearest, nearest - 1):
        end = float(Fraction(frames) / rate)
        if end > 0 and SELECTION_OUT_OF_RANGE not in _violations_of((0.0, end), duration, rate):
            return end
    return duration


def _violations_of(pair: tuple[float, float], duration: float, fps: Fraction) -> tuple[str, ...]:
    """구간 하나의 위반 키들."""
    return validate_selections([pair], duration, fps).get(0, ())


def format_fps(rate: Fraction) -> str:
    """프레임률을 표시 문자열로 — 정수면 "60", 아니면 소수 둘째 자리까지("29.97")."""
    if rate.denominator == 1:
        return str(rate.numerator)
    return f"{float(rate):.2f}".rstrip("0").rstrip(".")


class SectionBasisJob(QObject):
    """기준값 조회 한 건 — 풀 스레드에서 ``run()``하고 결과를 Signal로 메인에 넘긴다.

    emit은 풀 스레드에서 일어나고, 메인 스레드에 사는 뷰모델의 바운드 메서드가 큐로 받는다.
    뷰모델이 결과가 올 때까지 이 객체의 참조를 든다 — 참조가 없으면 ``run()``이 끝난 직후
    파괴되어 큐에 남은 전달이 유실된다(#124).
    """

    finished = Signal(object)  # SectionBasis
    failed = Signal()

    def __init__(self, item: ContentItem):
        super().__init__()
        self._item = item

    def run(self) -> None:
        """기준값을 조회해 finished(성공) 또는 failed(실패)를 emit한다."""
        try:
            # 모듈 전역을 호출 시점에 조회한다 — 테스트의 monkeypatch 지점
            basis = section_basis.probe_section_basis(self._item)
        except Exception:
            # 원시 예외 문자열에는 주소가 섞여 있어 화면에 올리지 않는다 — 상세는 로그로만
            logger.exception("구간 기준값 조회 실패: %s", self._item.vod_url)
            self.failed.emit()
            return
        self.finished.emit(basis)


class SectionEditViewModel(QObject):
    """구간 편집 창 한 번의 상태 — 조회 상태, 행, 검증 결과.

    ``start()``로 조회를 시작하고, ``commit()``으로 카드에 쓴다. 창을 닫으면 버린다.
    """

    stateChanged = Signal()  # 조회 상태가 바뀌었다(loading → ready/failed)
    rowsReset = Signal()  # 행의 수 · 순서가 바뀌었다 — 창이 행을 다시 만든다
    validated = Signal()  # 행의 글자 · 검증 결과가 바뀌었다 — 창이 표시만 고친다

    def __init__(
        self,
        item: ContentItem,
        notify: Callable[[ContentItem], None] | None = None,
        parent: QObject | None = None,
    ):
        """
        Args:
            item: 편집할 카드의 데이터
            notify: 카드에 쓴 뒤 부르는 함수 — 목록 모델의 ``notifyChanged``
        """
        super().__init__(parent)
        self.item = item
        self.state = STATE_LOADING
        self.fps: Fraction | None = None
        self.duration = 0.0
        self.rows: list[list[str]] = []  # 행마다 [시작 글자, 끝 글자]
        self._notify = notify
        self._job: SectionBasisJob | None = None
        self._head = None  # 조회하며 받은 moov(인코딩 완료 VOD) — 없으면 None
        self._released = False  # 창이 닫혔다 — 늦게 온 조회 결과의 moov를 받아 두지 않는다
        self._errors: dict[int, str] = {}  # 행 번호 → 오류 키
        self._allErrors: dict[int, tuple[str, ...]] = {}  # 행 번호 → 그 행의 오류 키 전부
        self._pairs: dict[int, tuple[float, float]] = {}  # 행 번호 → 해석한 (시작, 끝) 초

    # ---- 조회 ----

    def start(self, pool: QThreadPool | None = None) -> None:
        """기준값 조회를 풀 스레드에서 시작한다. 한 번만 부른다."""
        job = SectionBasisJob(self.item)
        job.finished.connect(self._onBasis)
        job.failed.connect(self._onBasisFailed)
        self._job = job
        (pool or QThreadPool.globalInstance()).start(lambda: job.run())

    def release(self) -> None:
        """조회하며 받은 moov를 놓는다 — 편집 창이 닫힐 때 부른다 (#309).

        이 객체는 ``deleteLater`` 뒤에도 한동안 남을 수 있고, 긴 영상의 해석된 색인은 수백 MB다.
        확인했으면 카드가 이미 넘겨받았고, 취소했으면 쓸 곳이 없다.
        """
        self._released = True
        self._head = None

    def _onBasis(self, basis) -> None:
        self._job = None
        self.fps = frame_rate(basis.fps)
        self.duration = basis.duration
        if not self._released:
            self._head = getattr(basis, "mp4_head", None)  # 확인하면 카드에 둔다
        self.rows = [
            [format_timecode(selection.start, self.fps), format_timecode(selection.end, self.fps)]
            for selection in self.item.selections
        ] or [self._wholeRow()]
        self.state = STATE_READY
        self._evaluate()
        self.stateChanged.emit()
        self.rowsReset.emit()

    def _onBasisFailed(self) -> None:
        self._job = None
        self.state = STATE_FAILED
        self.stateChanged.emit()

    def failureText(self) -> str:
        """조회 실패 때 창에 보이는 문구."""
        return self.tr(
            "Could not read the video information.\n"
            "Check your connection and cookies, then open this window again."
        )

    def loadingText(self) -> str:
        """조회 중에 창에 보이는 문구."""
        return self.tr("Reading the video information...")

    # ---- 행 ----

    def _wholeRow(self) -> list[str]:
        """영상 전체를 가리키는 행 — 처음 ~ 영상의 끝 타임코드."""
        end = last_frame_seconds(self.duration, self.fps)  # endSeconds()와 같은 값이다
        return [format_timecode(0.0, self.fps), format_timecode(end, self.fps)]

    def canAdd(self) -> bool:
        """행을 더 넣을 수 있는지 — 구간은 ``MAX_SELECTIONS``개까지다."""
        return self.state == STATE_READY and len(self.rows) < MAX_SELECTIONS

    def addRow(self) -> None:
        """영상 전체를 가리키는 행을 끝에 넣는다."""
        if not self.canAdd():
            return
        self.rows.append(self._wholeRow())
        self._evaluate()
        self.rowsReset.emit()

    def removeRow(self, row: int) -> None:
        """행을 지운다. 마지막 남은 행을 지우면 영상 전체를 가리키는 행으로 돌아간다."""
        if self.state != STATE_READY or not 0 <= row < len(self.rows):
            return
        del self.rows[row]
        if not self.rows:
            self.rows.append(self._wholeRow())
        self._evaluate()
        self.rowsReset.emit()

    def moveRow(self, row: int, step: int) -> None:
        """행을 위(step=-1) · 아래(step=1)로 옮긴다. 행의 순서가 구간 번호이고 파일 이름의 번호다."""
        target = row + step
        if self.state != STATE_READY or not (
            0 <= row < len(self.rows) and 0 <= target < len(self.rows)
        ):
            return
        self.rows[row], self.rows[target] = self.rows[target], self.rows[row]
        self._evaluate()
        self.rowsReset.emit()

    def setText(self, row: int, column: int, text: str, normalize: bool = True) -> None:
        """칸의 글자를 받아 다시 검증한다.

        Args:
            normalize: 해석되는 글자를 ``HH:MM:SS:FF`` 표기로 고쳐 들지 여부. 입력하는 도중에는
                False로 준다 — 치고 있는 글자를 고쳐 쓰지 않는다
        """
        if self.state != STATE_READY or not 0 <= row < len(self.rows):
            return
        try:
            if normalize:
                text = format_timecode(self._parse(text), self.fps)
        except TimecodeError:
            pass  # 틀린 글자는 그대로 들고 오류로 보인다
        self.rows[row][column] = text
        self._evaluate()
        self.validated.emit()

    # ---- 검증 ----

    def _parse(self, text: str) -> float:
        """칸의 글자를 명목 시각(초)으로 바꾼다 — 네 칸(HH:MM:SS:FF)만 받는다.

        Raises:
            TimecodeError: 네 칸이 아니거나 ``parse_timecode``가 거부한 경우
        """
        if len(text.strip().split(":")) != _TIMECODE_FIELDS:
            raise TimecodeError(TIMECODE_INVALID_FORMAT, text)
        return parse_timecode(text, self.fps)

    def _evaluate(self) -> None:
        """모든 행을 해석하고 검증해 행마다의 오류 키를 정한다.

        해석된 행만 모아 ``validate_selections``에 넣는다 — 중복 · 개수는 행 사이의 규칙이라
        한꺼번에 봐야 한다. 행의 오류는 그 행의 첫 위반 키다(키의 순서는 core가 정한다).
        """
        self._errors, self._pairs, self._allErrors = {}, {}, {}
        for row, (start, end) in enumerate(self.rows):
            try:
                self._pairs[row] = (self._parse(start), self._parse(end))
            except TimecodeError as e:
                self._errors[row] = e.message_key
                self._allErrors[row] = (e.message_key,)
        parsed = sorted(self._pairs)
        violations = validate_selections(
            [self._pairs[row] for row in parsed], self.duration, self.fps
        )
        for position, keys in violations.items():
            self._errors[parsed[position]] = keys[0]
            self._allErrors[parsed[position]] = tuple(keys)

    def shownErrorKey(self, row: int, typing: tuple[int, int] | None = None) -> str:
        """행에 지금 띄울 오류 키. 띄울 것이 없으면 빈 문자열.

        치고 있는 행이 아니면 그 행의 첫 오류다. 치고 있는 행이면 ``ERROR_TIMING``이 바로
        띄우라고 한 오류 가운데 첫 것이다 — 떠날 때 띄울 오류만 있으면 아직 띄우지 않는다.

        Args:
            typing: 숫자를 치고 있는 (행, 칸). 없으면 None — 모든 오류를 바로 띄운다
        """
        keys = self._allErrors.get(row, ())
        if typing is None or typing[0] != row:
            return keys[0] if keys else ""
        column = typing[1]
        for key in keys:
            if ERROR_TIMING.get(key, (SHOW_NOW, SHOW_NOW))[column] == SHOW_NOW:
                return key
        return ""

    def shownErrorText(self, row: int, typing: tuple[int, int] | None = None) -> str:
        """행에 지금 띄울 오류 문구(번역된 것). 띄울 것이 없으면 빈 문자열."""
        key = self.shownErrorKey(row, typing)
        return self._translate(key) if key else ""

    def errorKey(self, row: int) -> str:
        """행의 오류 키(번역하지 않은 원문). 오류가 없으면 빈 문자열."""
        return self._errors.get(row, "")

    def errorText(self, row: int) -> str:
        """행의 오류 문구(번역된 것). 오류가 없으면 빈 문자열."""
        key = self.errorKey(row)
        return self._translate(key) if key else ""

    def lengthText(self, row: int) -> str:
        """행의 구간 길이 — ``HH:MM:SS.mmm``. 오류가 있는 행은 빈 문자열."""
        if row in self._errors or row not in self._pairs:
            return ""
        start, end = self._pairs[row]
        return format_milliseconds(end - start)

    def millisecondsText(self, row: int, column: int) -> str:
        """칸의 시각을 밀리초 표기로 — 표시만 한다. 해석되지 않는 칸은 빈 문자열."""
        try:
            return format_milliseconds(self._parse(self.rows[row][column]))
        except TimecodeError:
            return ""

    def headerText(self) -> str:
        """머리줄 — 구간 수 · 프레임률 · 영상의 끝 타임코드. 조회가 끝나기 전에는 빈 문자열."""
        if self.state != STATE_READY:
            return ""
        return self.tr("Sections {0} / {1} · {2}fps · video ends at {3}").format(
            len(self.rows), MAX_SELECTIONS, format_fps(self.fps), self.endTimecodeText()
        )

    def endSeconds(self) -> float | None:
        """구간의 끝으로 적을 수 있는 가장 늦은 시각(초). 조회가 끝나기 전에는 None.

        조회한 프레임률 · 길이로 정한다 — 영상 길이를 넘지 않는 마지막 프레임 경계다. 끝 칸에
        이 값을 넣은 구간은 영상의 마지막 프레임까지 받는다(``reaches_end``).
        """
        if self.state != STATE_READY:
            return None
        return last_frame_seconds(self.duration, self.fps)

    def endTimecodeText(self) -> str:
        """영상의 끝 타임코드 ``HH:MM:SS:FF`` — 끝 칸에 그대로 치면 "영상 끝까지"가 되는 값.

        조회가 끝나기 전 · 실패한 뒤에는 빈 문자열이다 — 모르는 값을 보이지 않는다.
        """
        end = self.endSeconds()
        return "" if end is None else format_timecode(end, self.fps)

    def endMillisecondsText(self) -> str:
        """영상의 끝을 밀리초 표기로 — 표시만 한다. 조회가 끝나기 전에는 빈 문자열."""
        end = self.endSeconds()
        return "" if end is None else format_milliseconds(end)

    def canCommit(self) -> bool:
        """확인할 수 있는지 — 조회가 끝났고 오류가 없다."""
        return self.state == STATE_READY and not self._errors

    def selections(self) -> tuple[TimeRange, ...]:
        """지금의 행을 카드에 쓸 구간 목록으로 바꾼다. 오류가 없을 때만 부른다.

        행이 영상 전체를 가리키는 한 행뿐이면 빈 튜플(전체 다운로드)이다.
        """
        pairs = [self._pairs[row] for row in range(len(self.rows))]
        if len(pairs) == 1:
            start, end = pairs[0]
            if frame_index(start, self.fps) == 0 and reaches_end(end, self.duration, self.fps):
                return ()
        return tuple(TimeRange(start, end) for start, end in pairs)

    def commit(self) -> bool:
        """구간을 카드에 쓴다. 썼으면 True.

        카드가 대기 상태가 아니면 쓰지 않는다 — 창이 열린 사이 받기 시작한 카드는 그때의
        구간으로 이미 돌고 있다.
        """
        if not self.canCommit():
            return False
        if self.item.downloadState != DownloadState.WAITING:
            logger.info("구간 편집 무시 — 창이 열린 사이 상태가 %s로 바뀜", self.item.downloadState)
            return False
        selections = self.selections()
        basis = SectionBasis(fps=self.fps, duration=self.duration)
        self.item.selections = selections
        self.item.section_frame_rate = self.fps if selections else None
        # 조회한 값으로 확인한 구간이다 — 해상도를 바꾸면 이것을 다시 맞춘다. 돌고 있던
        # 다시 맞추기 조회의 결과는 버려진다(section_check가 조회 중이 아니게 된다)
        self.item.section_verified = (selections, basis) if selections else None
        self.item.section_check = ""
        self.item.section_refit_fps = None
        self.item.section_end_pulled = self.item.section_end_extended = False
        self.item.section_unfit = frozenset()
        # 조회하며 받은 moov를 카드에 둔다 — 구간이 있을 때만(전체 다운로드는 moov를 쓰지 않는다)
        keep_section_head(self.item, self.item.base_url, self._head if selections else None)
        logger.info("구간 편집: %d개 (%sfps)", len(selections), format_fps(self.fps))
        if self._notify is not None:
            self._notify(self.item)
        return True

    def _translate(self, key: str) -> str:
        """오류 키를 현재 언어로 번역한다.

        키는 core가 내는 원문이다. lupdate가 추출하도록 리터럴로 tr()을 부른다 — 키 목록은
        ``core/utils/timecode.py``의 ``TIMECODE_*``와 ``core/utils/selections.py``의
        ``SELECTION_*`` 가운데 입력 검증이 내는 것 전부다.
        """
        translated = {
            TIMECODE_INVALID_FORMAT: self.tr("Invalid timecode format"),
            TIMECODE_FIELD_OUT_OF_RANGE: self.tr("Minutes and seconds must be below 60"),
            TIMECODE_FRAME_OUT_OF_RANGE: self.tr("Frame number must be below the frame rate"),
            SELECTION_ORDER: self.tr("Start must be before end"),
            SELECTION_OUT_OF_RANGE: self.tr("Selection is outside the video"),
            SELECTION_TOO_SHORT: self.tr("Selection is shorter than one frame"),
            SELECTION_DUPLICATE: self.tr("Duplicate selection"),
            SELECTION_TOO_MANY: self.tr("Too many selections"),
        }
        return translated.get(key, key)


class SectionRefitJob(QObject):
    """해상도를 바꾼 뒤의 기준값 조회 한 건 — 풀 스레드에서 돌고 결과를 Signal로 메인에 넘긴다.

    조회는 요청한 순간의 값(``snapshot``)으로 한다 — 카드의 값은 그사이 또 바뀔 수 있다.
    ``token``은 요청을 가리키는 값이고 결과와 함께 돌려준다. 받는 쪽이 그것으로 늦게 온
    결과를 가려낸다.
    """

    done = Signal(object, object)  # (token, SectionBasis 또는 None — 조회 실패)

    def __init__(self, snapshot, token):
        super().__init__()
        self._snapshot = snapshot
        self._token = token

    def run(self) -> None:
        """기준값을 조회해 done을 emit한다. 실패하면 값 자리에 None을 싣는다."""
        try:
            # 모듈 전역을 호출 시점에 조회한다 — 테스트의 monkeypatch 지점
            basis = section_basis.probe_section_basis(self._snapshot)
        except Exception:
            logger.exception("해상도 변경 뒤 구간 기준값 조회 실패: %s", self._snapshot.vod_url)
            basis = None
        self.done.emit(self._token, basis)


class SectionRefitter(QObject):
    """구간이 있는 카드의 해상도가 바뀌면 구간을 새 해상도에 다시 맞춘다 (#309).

    두 단계다.

    1. **곧바로**: 목록 항목의 선언 프레임률로 맞춘다 — 네트워크를 타지 않는다. 카드에는
       길이를 확인하는 중이라고 표시된다(``section_check``)
    2. **조회가 끝나면**: 편집 창과 같은 조회(``probe_section_basis``)로 얻은 프레임률 · 길이로
       다시 맞춘다(``refit_to_basis``). 선언값과 조회값이 다르면 조회값이 이긴다. 조회가
       실패하면 1의 결과를 두고 길이를 확인하지 못했다고 표시한다

    두 단계 모두 **마지막으로 확인된 구간**(``ContentItem.section_verified``)에서 출발한다 —
    선언값으로 맞춘 것을 조회값으로 또 맞추면 반올림이 두 번 쌓인다.

    늦게 온 결과는 버린다: 그사이 해상도를 또 바꿨거나(요청 번호가 다르다), 카드가 지워졌거나,
    대기 상태가 아니게 됐거나(받기 시작했다), 구간을 다시 편집한 경우다.

    조회가 도는 동안(``section_check``가 조회 중) 그 카드는 다운로드 대상에서 빠진다
    (``ContentViewModel.findItem``). 조회가 끝나면 ``settled``로 알린다 — 그 카드를 기다리던
    배치가 이어 간다.
    """

    settled = Signal(object)  # 카드의 조회가 끝났다(성공 · 실패) — 더는 조회 중이 아니다

    def __init__(self, model, pool: QThreadPool, parent: QObject | None = None):
        """
        Args:
            model: 목록 모델 — ``getRow`` · ``notifyChanged``를 쓴다
            pool: 조회를 돌릴 스레드 풀
        """
        super().__init__(parent)
        self._model = model
        self._pool = pool
        self._generation: dict[ContentItem, int] = {}  # 카드마다의 마지막 요청 번호
        # 결과를 기다리는 조회 — (카드, 요청 번호) → 조회 객체. 결과가 올 때까지 참조를 든다(#124)
        self._jobs: dict[tuple[ContentItem, int], SectionRefitJob] = {}

    def request(self, item: ContentItem) -> None:
        """카드의 해상도가 바뀌었다 — 구간을 선언값으로 맞추고 새 해상도의 조회를 시작한다.

        구간이 없는 카드, 대기가 아닌 카드, 확인된 구간이 없는 카드는 아무것도 하지 않는다 —
        조회도 돌리지 않는다.
        """
        verified = item.section_verified
        if item.downloadState != DownloadState.WAITING or not item.selections or verified is None:
            return
        selections, basis = verified
        declared = declared_frame_rate(SimpleNamespace(frame_rate=item.selected_frame_rate))
        if declared is not None and not same_frame_rate(basis.fps, declared):
            item.selections = refit_selections(selections, declared)
            item.section_frame_rate = item.section_refit_fps = declared
        else:
            item.selections = selections
            item.section_frame_rate, item.section_refit_fps = basis.fps, None
        item.section_end_pulled = item.section_end_extended = False
        item.section_unfit = frozenset()
        item.section_check = SECTION_CHECK_PENDING
        item.section_head = None  # 앞 해상도의 moov다 — 새 해상도의 조회가 끝나면 새로 둔다

        generation = self._generation.get(item, 0) + 1
        self._generation[item] = generation
        token = (item, generation)
        snapshot = SimpleNamespace(
            content_type=item.content_type,
            base_url=item.base_url,
            vod_url=item.vod_url,
            resolution=item.resolution,
            stream=item.stream,
        )
        job = SectionRefitJob(snapshot, token)
        job.done.connect(self._onDone)
        self._jobs[token] = job
        self._pool.start(lambda: job.run())
        self._model.notifyChanged(item)

    def pendingCount(self) -> int:
        """결과를 기다리는 조회의 수."""
        return len(self._jobs)

    def _onDone(self, token, basis) -> None:
        """조회 결과를 받는다 — 아직 유효한 요청이면 구간을 조회값으로 다시 맞춘다."""
        self._jobs.pop(token, None)
        item, generation = token
        if self._model.getRow(item) is None:
            self._generation.pop(item, None)  # 카드가 지워졌다
            return
        if self._generation.get(item) != generation:
            return  # 그사이 해상도를 또 바꿨다 — 새 요청의 결과를 기다린다
        if (
            item.downloadState != DownloadState.WAITING
            or item.section_check != SECTION_CHECK_PENDING
            or item.section_verified is None
        ):
            return  # 받기 시작했거나 구간을 다시 편집했다
        if basis is None:
            item.section_check = SECTION_CHECK_UNVERIFIED
            self._model.notifyChanged(item)
            self.settled.emit(item)
            return
        selections, old = item.section_verified
        result = refit_to_basis(selections, old, basis)
        rate = frame_rate(basis.fps)
        item.selections = result.selections
        item.section_frame_rate = rate
        item.section_verified = (result.selections, SectionBasis(fps=rate, duration=basis.duration))
        item.section_check = ""
        item.section_refit_fps = rate if result.regridded else None
        item.section_end_pulled = result.end_pulled
        item.section_end_extended = result.end_extended
        item.section_unfit = result.unfit
        # 새 해상도를 조회하며 받은 moov — 요청이 아직 유효하므로 지금의 주소에서 받은 것이다
        keep_section_head(item, item.base_url, getattr(basis, "mp4_head", None))
        logger.info(
            "해상도 변경으로 구간을 다시 맞춤: %sfps%s%s%s",
            format_fps(rate),
            " · 끝을 당김" if result.end_pulled else "",
            " · 끝을 늘림" if result.end_extended else "",
            f" · 당길 수 없는 구간 {len(result.unfit)}개" if result.unfit else "",
        )
        self._model.notifyChanged(item)
        self.settled.emit(item)
