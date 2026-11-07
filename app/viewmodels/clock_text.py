"""초를 「시:분:초」 글로 적는다 — 남은 시간 · 걸린 시간 · 영상 길이가 함께 쓰는 한 곳 (#511).

`strftime("%H:%M:%S", gmtime(초))`는 날짜를 버려 시가 0 ~ 23에서 돈다 — 30시간이 「06:00:00」이었다.
여기서는 시를 24로 나누지 않는다(30시간 = 「30:00:00」, 100시간이면 세 자리). 화면의 짧은 표기
(「3:12」 · 「1:02:03」)는 이 글을 받아 `app/widgets/widget.py`의 `short_time`이 줄인다.
"""

from time import gmtime, strftime


def clock_text(seconds: float | None) -> str:
    """초를 "HH:MM:SS"로 적는다 — 시는 24를 넘어도 그대로 센다. 초 아래는 버린다.

    음수와 None은 바꾸기 전 식(`strftime("%H:%M:%S", gmtime(…))`)에 그대로 넘긴다 — 그 동작을
    바꾸지 않는다(#511 범위 밖). None은 지금 시각을, 음수는 Windows에서 OSError를 낸다.
    """
    if seconds is None or seconds < 0:
        return strftime("%H:%M:%S", gmtime(seconds))
    whole = int(seconds)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"
