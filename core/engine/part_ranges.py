"""다운로드 파트 크기 결정·구간 분할 유틸.

download/download.py에서 이동한 순수 함수 (#50).
"""


def decide_part_size(content_type: str, resolution: int) -> int:
    """
    해상도에 따라 파트 크기 가중치를 달리 부여한다.

    테스트를 위해 DownloadThread._decide_part_size에서 분리한 순수 함수 (#27).
    """
    base_part_size = 1024 * 1024  # 1MB
    if content_type == 'clips':
        return base_part_size * 1
    elif resolution == 144:
        return base_part_size * 1
    elif resolution in [360, 480]:
        return base_part_size * 2
    elif resolution == 720:
        return base_part_size * 5
    else:
        return base_part_size * 10


def split_ranges(total_size: int, part_size: int) -> list[tuple[int, int]]:
    """
    total_size 바이트를 part_size 단위의 (start, end) 구간 목록으로 분할한다.

    테스트를 위해 DownloadThread.run의 인라인 계산에서 분리한 순수 함수 (#27).
    """
    return [
        (i * part_size, min((i + 1) * part_size - 1, total_size - 1))
        for i in range((total_size + part_size - 1) // part_size)
    ]


def split_span(first: int, last: int, part_size: int) -> list[tuple[int, int]]:
    """바이트 범위 first~last(양 끝 포함)를 part_size 단위의 (start, end) 목록으로 나눈다 (#309).

    ``split_ranges``가 0부터 시작하는 범위만 나누는 것과 달리 파일 중간의 범위를 나눈다.
    """
    return [
        (start, min(start + part_size - 1, last)) for start in range(first, last + 1, part_size)
    ]
