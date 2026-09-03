"""core.api.representations — 같은 해상도 트랙 합치기 규칙 (#244 3행 정리).

규칙: 해상도당 하나. **비트레이트가 높은 쪽**, 같거나 모르면(0) **먼저 온 쪽**.
반환은 파서들의 기존 형식(`[해상도, base_url]` 오름차순)과 같다.
"""

from core.api.representations import dedupe_by_resolution


class TestDedupeByResolution:
    def test_keeps_one_entry_per_resolution(self):
        reps = dedupe_by_resolution([(1080, "a", 5_000_000), (720, "b", 3_000_000), (1080, "c", 6_000_000)])
        assert [r for r, _ in reps] == [720, 1080]

    def test_higher_bandwidth_wins(self):
        reps = dedupe_by_resolution([(1080, "low", 5_000_000), (1080, "high", 6_000_000)])
        assert reps == [[1080, "high"]]
        # 순서를 뒤집어도 같은 답 — 등장 순서가 아니라 비트레이트가 결정한다
        reps = dedupe_by_resolution([(1080, "high", 6_000_000), (1080, "low", 5_000_000)])
        assert reps == [[1080, "high"]]

    def test_tie_or_unknown_bandwidth_keeps_the_first(self):
        assert dedupe_by_resolution([(1080, "first", 0), (1080, "second", 0)]) == [[1080, "first"]]
        assert dedupe_by_resolution([(720, "first", 4_000), (720, "second", 4_000)]) == [[720, "first"]]

    def test_result_is_ascending_and_keeps_the_list_shape(self):
        reps = dedupe_by_resolution([(1080, "u1080", 1), (144, "u144", 1), (720, "u720", 1)])
        assert reps == [[144, "u144"], [720, "u720"], [1080, "u1080"]]
        assert all(isinstance(r, list) for r in reps), "파서 반환 형식(list) 유지 — 위젯이 뒤에 크기 텍스트를 append한다"

    def test_empty_input(self):
        assert dedupe_by_resolution([]) == []
