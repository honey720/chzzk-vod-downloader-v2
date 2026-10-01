"""헤드리스 스크립트의 구간 옵션(scripts/headless_download.py) 단위 테스트 (#309).

옵션 해석만 본다 — 조회·다운로드는 부르지 않는다.
"""

from fractions import Fraction

import pytest

from core.utils.timecode import TIMECODE_FRAME_OUT_OF_RANGE, TIMECODE_INVALID_FORMAT, TimecodeError
from scripts.headless_download import _parse_args, _parse_sections


def test_section_option_can_be_given_several_times():
    """--section을 여러 번 주면 준 순서대로 모아야 한다.

    URL 하나와 --section 둘
    -> args.section == [첫째, 둘째]
    """
    args = _parse_args(
        ["https://chzzk.naver.com/video/1", "--section", "00:10:05:00-00:10:16:00"]
        + ["--section", "00:42:00:12-00:42:31:00"]
    )

    assert args.section == ["00:10:05:00-00:10:16:00", "00:42:00:12-00:42:31:00"]


def test_section_option_defaults_to_whole_download():
    """--section을 주지 않으면 구간이 없어야 한다(전체 다운로드).

    URL 하나
    -> args.section == []
    """
    assert _parse_args(["https://chzzk.naver.com/video/1"]).section == []


def test_parse_sections_turns_timecodes_into_seconds():
    """_parse_sections는 `시작-끝` 타임코드를 그 프레임률의 초 쌍으로 바꿔야 한다.

    "00:00:10:15-00:01:00:00", 30fps (15프레임 = 0.5초)
    -> [(10.5, 60.0)]
    """
    assert _parse_sections(["00:00:10:15-00:01:00:00"], Fraction(30)) == [(10.5, 60.0)]


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("00:00:10:00", TIMECODE_INVALID_FORMAT),  # 끝이 없다
        ("00:00:10:00-abc", TIMECODE_INVALID_FORMAT),  # 끝이 타임코드가 아니다
        ("00:00:10:30-00:00:20:00", TIMECODE_FRAME_OUT_OF_RANGE),  # 30fps에서 프레임 칸 30
    ],
)
def test_parse_sections_rejects_malformed_section(text, key):
    """_parse_sections는 형식에 맞지 않는 구간을 타임코드 키로 거부해야 한다.

    주석의 경우마다 구간 문자열, 30fps
    -> TimecodeError(message_key == 그 키)
    """
    with pytest.raises(TimecodeError) as info:
        _parse_sections([text], Fraction(30))

    assert info.value.message_key == key
