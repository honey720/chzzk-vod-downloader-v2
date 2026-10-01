"""타임코드 해석·표기와 실제 프레임 맞추기 (#178).

구간의 시작·끝은 내부에서 시각(초)으로 다루고, 입력과 표시 때만 타임코드
``HH:MM:SS:FF``로 바꾼다. 이 모듈은 그 변환과, 타임코드가 가리키는 시각을
영상의 실제 프레임에 맞추는 계산을 모은 순수 함수 묶음이다.

타임코드의 시각은 **명목 시각**이다::

    명목 시각 = HH×3600 + MM×60 + SS + FF ÷ fps

FF는 "그 초 안에서 몇 번째 실제 프레임인가"가 아니다. 실제 프레임은 간격이
고르지 않거나 빠져 있을 수 있으므로, 명목 시각을 구한 뒤 ``snap_to_frame``이
PTS가 가장 가까운 프레임을 고른다.

fps는 추정하지 않고 원본이 말한 값 그대로 쓴다(``FrameRate``). mp4는 샘플 표에서
구한 정확한 비(``Mp4Index.fps``)를 넘기고, 선언값만 있으면 그 글자 그대로의
유리수로 계산한다 — 근거는 ``frame_rate``의 docstring에 있다.

실패는 ``TimecodeError``로 던진다. message_key는 번역하지 않은 i18n 키 원문이며
번역은 앱 계층이 한다(``MetadataError``와 같은 방식).
"""

import math
from bisect import bisect_left
from collections.abc import Sequence
from fractions import Fraction
from typing import Literal

# 타임코드 해석 실패 키 — 번역하지 않은 i18n 키 원문
TIMECODE_INVALID_FORMAT = "Invalid timecode format"  # 칸 수·숫자가 아닌 글자·빈 칸
TIMECODE_FIELD_OUT_OF_RANGE = "Minutes and seconds must be below 60"  # MM·SS가 60 이상
TIMECODE_FRAME_OUT_OF_RANGE = "Frame number must be below the frame rate"  # FF가 fps 이상

# 프레임률로 받는 값. Fraction은 정확한 비(예: mp4 샘플 표의 timescale ÷ 샘플 길이),
# 나머지는 선언값이다 — 문자열은 "29.97" · "60.00" · "30000/1001" 모양을 받는다
type FrameRate = int | float | str | Fraction

# 반 프레임 판정·동률 판정의 여유(프레임 길이 대비 비율) — float 오차가 경계를 넘지 않게 한다
_HALF_FRAME_SLACK = 1e-6


class TimecodeError(ValueError):
    """타임코드 문자열을 해석하지 못했다.

    message_key는 번역하지 않은 i18n 키 원문이다(이 모듈의 ``TIMECODE_*`` 상수).
    """

    def __init__(self, message_key: str, text: str):
        super().__init__(f"{message_key}: {text!r}")
        self.message_key = message_key
        self.text = text


def frame_rate(fps: FrameRate) -> Fraction:
    """프레임률을 계산에 쓸 유리수로 바꾼다. 값을 추정하거나 다른 값으로 바꾸지 않는다.

    - ``Fraction``은 그대로 돌려준다
    - 선언값(정수·소수·문자열)은 그 글자 그대로의 유리수로 바꾼다
      (29.97 → 2997/100, "60.00" → 60, "30000/1001" → 30000/1001)

    29.97을 30000/1001로 바꾸지 않는 이유: 29.97fps로 표시되는 영상에는 프레임
    간격이 100/2997초인 것과 1001/30000초인 것이 둘 다 있고, 선언값만으로는 구분할
    수 없다. 실제로 확인한 인코딩 완료 VOD는 간격이 400/11988초(정확히 1/29.97)였다.
    두 값은 100만 분의 1만큼 달라, 틀린 쪽으로 계산하면 약 4시간 38분(50만 프레임)
    부터 시각을 프레임 번호로 바꿀 때 한 칸 어긋난다. 그래서 정확한 비를 아는
    쪽(mp4의 ``Mp4Index.fps``)이 ``Fraction``으로 넘기고, 선언값은 선언한 그대로 쓴다.

    Raises:
        ValueError: fps가 0 이하이거나, 유한한 수가 아니거나, 수로 읽을 수 없는 문자열인 경우
    """
    if isinstance(fps, Fraction):
        rate = fps
    else:
        if isinstance(fps, float) and not math.isfinite(fps):
            raise ValueError(f"fps는 0보다 큰 유한한 수여야 한다: {fps}")
        try:
            # float는 2진 근삿값이 아니라 표기(29.97)를 유리수로 읽는다
            rate = Fraction(str(fps) if isinstance(fps, float) else fps)
        except (ValueError, ZeroDivisionError) as e:
            raise ValueError(f"fps를 수로 읽을 수 없다: {fps!r}") from e
    if rate <= 0:
        raise ValueError(f"fps는 0보다 큰 유한한 수여야 한다: {fps}")
    return rate


def frames_per_second(fps: FrameRate) -> int:
    """타임코드 FF 칸이 가질 수 있는 값의 개수를 반환한다.

    FF는 0부터 이 값 미만이다. 60fps는 60(00~59), 29.97fps는 30(00~29)이다.
    """
    return math.ceil(frame_rate(fps))


def frame_index(seconds: float, fps: FrameRate) -> int:
    """시각(초)을 가장 가까운 프레임 번호로 바꾼다.

    프레임 번호 n의 시각은 n ÷ fps다. float 시각을 서로 비교할 때 프레임 단위로
    양자화하는 데 쓴다 — 같은 프레임을 가리키는 두 시각은 같은 번호가 된다.
    """
    return round(Fraction(seconds) * frame_rate(fps))


def parse_timecode(text: str, fps: FrameRate) -> float:
    """타임코드 문자열을 명목 시각(초)으로 바꾼다.

    받는 형태는 셋이다. 밀리초 입력(``.``)은 받지 않는다.

    - ``HH:MM:SS:FF``
    - ``HH:MM:SS`` — FF는 0 (``1:05:03`` → 01:05:03:00)
    - ``MM:SS`` — HH·FF는 0 (``5:03`` → 00:05:03:00)

    각 칸은 숫자만 받고 자릿수는 따지지 않는다. MM·SS는 60 미만, FF는
    ``frames_per_second(fps)`` 미만이어야 한다.

    Raises:
        TimecodeError: 형태가 맞지 않거나 칸의 값이 범위를 벗어난 경우
    """
    fields = text.strip().split(":")
    if not 2 <= len(fields) <= 4 or not all(f.isascii() and f.isdigit() for f in fields):
        raise TimecodeError(TIMECODE_INVALID_FORMAT, text)
    values = [int(f) for f in fields]
    frames = values.pop() if len(values) == 4 else 0
    hours = values.pop(0) if len(values) == 3 else 0
    minutes, seconds = values
    if minutes >= 60 or seconds >= 60:
        raise TimecodeError(TIMECODE_FIELD_OUT_OF_RANGE, text)
    if frames >= frames_per_second(fps):
        raise TimecodeError(TIMECODE_FRAME_OUT_OF_RANGE, text)
    return float(hours * 3600 + minutes * 60 + seconds + Fraction(frames) / frame_rate(fps))


def format_timecode(seconds: float, fps: FrameRate) -> str:
    """시각(초)을 ``HH:MM:SS:FF``로 표기한다.

    ``parse_timecode``의 역이다 — 초 아래 부분을 가장 가까운 FF로 반올림하고,
    FF가 칸의 범위를 넘으면 다음 초로 올린다. 시는 99를 넘으면 자릿수가 늘어난다.

    Raises:
        ValueError: seconds가 음수이거나 유한한 수가 아닌 경우
    """
    whole, fraction = _split_seconds(seconds)
    frames = round(fraction * frame_rate(fps))
    if frames >= frames_per_second(fps):
        whole, frames = whole + 1, 0
    return f"{_format_hms(whole)}:{frames:02d}"


def format_milliseconds(seconds: float) -> str:
    """시각(초)을 ``HH:MM:SS.mmm``으로 표기한다.

    밀리초는 가장 가까운 값으로 반올림하고 1000이 되면 다음 초로 올린다.

    Raises:
        ValueError: seconds가 음수이거나 유한한 수가 아닌 경우
    """
    whole, fraction = _split_seconds(seconds)
    millis = round(fraction * 1000)
    if millis >= 1000:
        whole, millis = whole + 1, 0
    return f"{_format_hms(whole)}.{millis:03d}"


def snap_to_frame(
    seconds: float, frame_pts: Sequence[float], fps: FrameRate, edge: Literal["start", "end"]
) -> int:
    """명목 시각에 맞는 실제 프레임의 번호(frame_pts의 인덱스)를 고른다.

    PTS가 가장 가까운 프레임을 고른다. 가장 가까운 프레임이 반 프레임(1 ÷ fps의
    절반)보다 멀면 그 자리의 프레임이 빠진 것이다 — 이때 시작은 앞 프레임, 끝은
    뒤 프레임을 고른다. 고른 장면이 잘리지 않고 구간이 바깥으로 넓어진다.
    두 프레임과의 거리가 같을 때도 같은 방향으로 고른다.

    그 방향에 프레임이 없으면(첫 프레임보다 앞의 시작, 마지막 프레임보다 뒤의 끝)
    남은 쪽 프레임을 고른다.

    Args:
        seconds: 명목 시각(초)
        frame_pts: 프레임 PTS 목록(초). 오름차순이어야 한다 — 검사하지 않는다
        fps: 프레임률. 반 프레임의 길이를 정한다
        edge: 구간의 시작("start")인지 끝("end")인지

    Raises:
        ValueError: frame_pts가 비어 있는 경우
    """
    if not frame_pts:
        raise ValueError("frame_pts가 비어 있다")
    frame_length = float(1 / frame_rate(fps))
    slack = frame_length * _HALF_FRAME_SLACK
    after = bisect_left(frame_pts, seconds)
    before = after - 1
    if before < 0:
        return after
    if after == len(frame_pts):
        return before
    gap_before = seconds - frame_pts[before]
    gap_after = frame_pts[after] - seconds
    if (
        min(gap_before, gap_after) > frame_length / 2 + slack
        or abs(gap_before - gap_after) <= slack
    ):
        return before if edge == "start" else after
    return before if gap_before < gap_after else after


def _split_seconds(seconds: float) -> tuple[int, Fraction]:
    """시각을 (정수 초, 1 미만의 나머지)로 나눈다."""
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(f"시각은 0 이상의 유한한 수여야 한다: {seconds}")
    exact = Fraction(seconds)
    whole = math.floor(exact)
    return whole, exact - whole


def _format_hms(whole_seconds: int) -> str:
    """정수 초를 ``HH:MM:SS``로 표기한다."""
    minutes, seconds = divmod(whole_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
