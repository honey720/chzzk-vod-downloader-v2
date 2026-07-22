"""core.downloaders.ranges의 파트 크기 결정·구간 분할 박제 테스트.

목적은 "올바른 동작" 검증이 아니라 현재 동작의 보존이다 (#27).
구현이 download/download.py에서 core/downloaders/ranges.py로 이동해 import 경로만 갱신 (#50).
"""

import pytest

from core.downloaders.ranges import decide_part_size, split_ranges

MB = 1024 * 1024


class TestDecidePartSize:
    """decide_part_size의 content_type·해상도별 가중치 박제."""

    @pytest.mark.parametrize(
        ("content_type", "resolution", "expected"),
        [
            # clips는 해상도와 무관하게 항상 1MB
            ("clips", 1080, 1 * MB),
            ("clips", 144, 1 * MB),
            # video는 해상도별 가중치
            ("video", 144, 1 * MB),
            ("video", 360, 2 * MB),
            ("video", 480, 2 * MB),
            ("video", 720, 5 * MB),
            ("video", 1080, 10 * MB),
            ("video", 1440, 10 * MB),
            # 경계 케이스: 목록에 없는 해상도는 전부 else 분기(10MB)로 떨어진다
            ("video", 0, 10 * MB),
            ("video", 719, 10 * MB),
        ],
    )
    def test_decide_part_size(self, content_type: str, resolution: int, expected: int):
        """조합별 part_size를 현재 동작 그대로 고정한다."""
        assert decide_part_size(content_type, resolution) == expected


class TestSplitRanges:
    """split_ranges의 (start, end) 구간 분할 결과 박제."""

    @pytest.mark.parametrize(
        ("total_size", "part_size", "expected"),
        [
            # 정확히 나누어떨어지는 경우
            (20, 10, [(0, 9), (10, 19)]),
            # 나머지가 있는 경우: 마지막 구간은 total_size - 1에서 끝난다
            (25, 10, [(0, 9), (10, 19), (20, 24)]),
            # total이 part보다 작으면 구간 1개
            (5, 10, [(0, 4)]),
            # total == part
            (10, 10, [(0, 9)]),
            # 경계 케이스: 1바이트 파일
            (1, 10, [(0, 0)]),
            # 경계 케이스: 0바이트 파일이면 구간 없음
            (0, 10, []),
        ],
    )
    def test_split_ranges(self, total_size: int, part_size: int, expected: list):
        """total_size·part_size 조합별 분할 결과를 현재 동작 그대로 고정한다."""
        assert split_ranges(total_size, part_size) == expected

    def test_realistic_720p_video(self):
        """실제와 유사한 조합: 10.5MB 파일을 720p part_size(5MB)로 분할."""
        total_size = 10 * MB + 512 * 1024  # 10.5MB
        part_size = decide_part_size("video", 720)  # 5MB

        ranges = split_ranges(total_size, part_size)

        assert ranges == [
            (0, 5 * MB - 1),
            (5 * MB, 10 * MB - 1),
            (10 * MB, total_size - 1),
        ]

    @pytest.mark.parametrize(
        ("total_size", "part_size"),
        [(1, 1), (99, 10), (100, 10), (101, 10), (10 * MB + 3, 2 * MB)],
    )
    def test_ranges_cover_whole_file_without_gap(self, total_size: int, part_size: int):
        """분할 구간이 빈틈·겹침 없이 파일 전체 [0, total_size-1]을 덮는 불변식을 고정한다."""
        ranges = split_ranges(total_size, part_size)

        assert ranges[0][0] == 0
        assert ranges[-1][1] == total_size - 1
        for (_, prev_end), (next_start, _) in zip(ranges, ranges[1:]):
            assert next_start == prev_end + 1
