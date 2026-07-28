"""산출물 경로 유틸(core/utils/paths.py) 단위 테스트 (#105).

핵심 계약:
- 같은 경로가 이미 있으면 절대 덮어쓰지 않고 " (n)"을 붙인 새 경로를 준다
- 제목의 Windows 금지 문자는 제거되고, 전체 경로 길이는 상한을 넘지 않는다
- 세그먼트 임시 폴더 이름은 산출물 파일명에서 파생돼 다운로드 간 구분된다
"""

import os

from core.utils.paths import (
    build_output_path,
    ensure_unique_path,
    sanitize_filename,
    temp_dir_for,
)


# ================================================================ ensure_unique_path


def test_ensure_unique_path_returns_original_when_free(tmp_path):
    """비어 있는 경로는 그대로 돌려준다 — 기본 파일명은 변하지 않는다."""
    path = str(tmp_path / "방송 1080p.mp4")
    assert ensure_unique_path(path) == path


def test_ensure_unique_path_appends_counter_when_taken(tmp_path):
    """이미 파일이 있으면 확장자 앞에 ' (1)'을 붙인다 — 덮어쓰기 금지 필수 조건."""
    (tmp_path / "방송 1080p.mp4").write_bytes(b"existing")
    result = ensure_unique_path(str(tmp_path / "방송 1080p.mp4"))
    assert result == str(tmp_path / "방송 1080p (1).mp4")


def test_ensure_unique_path_increments_until_free(tmp_path):
    """(1)도 차 있으면 (2), (3)… 비어 있는 번호까지 올린다."""
    (tmp_path / "방송 1080p.mp4").write_bytes(b"a")
    (tmp_path / "방송 1080p (1).mp4").write_bytes(b"b")
    result = ensure_unique_path(str(tmp_path / "방송 1080p.mp4"))
    assert result == str(tmp_path / "방송 1080p (2).mp4")


def test_ensure_unique_path_counts_directories_too(tmp_path):
    """같은 이름의 디렉토리가 있어도 충돌로 본다 (os.path.exists 기준)."""
    (tmp_path / "방송 1080p.mp4").mkdir()
    result = ensure_unique_path(str(tmp_path / "방송 1080p.mp4"))
    assert result == str(tmp_path / "방송 1080p (1).mp4")


# ================================================================ 연속 다운로드 시나리오


def test_sequential_same_title_downloads_keep_both_files(tmp_path):
    """완료 조건: 같은 제목 VOD를 연속으로 받아도 두 파일이 모두 남는다."""
    first = build_output_path(str(tmp_path), "즐거운 방송", 1080)
    with open(first, "wb") as f:
        f.write(b"episode-1")

    second = build_output_path(str(tmp_path), "즐거운 방송", 1080)
    with open(second, "wb") as f:
        f.write(b"episode-2")

    assert first != second
    with open(first, "rb") as f:
        assert f.read() == b"episode-1"  # 기존 파일이 보존된다
    with open(second, "rb") as f:
        assert f.read() == b"episode-2"


# ================================================================ 정제·길이 제한


def test_sanitize_filename_strips_windows_invalid_chars():
    """Windows 금지 문자와 개행이 제거된다 — content/network.py 정제와 동일 집합."""
    assert sanitize_filename('a\\b/c:d*e?f"g<h>i|j\nk') == "abcdefghijk"


def test_build_output_path_sanitizes_title(tmp_path):
    """완료 조건: 특수문자가 있는 제목으로도 정상 경로가 나온다."""
    result = build_output_path(str(tmp_path), '공지: "오늘 방송" <특집>?', 720)
    assert result == str(tmp_path / "공지 오늘 방송 특집 720p.mp4")


def test_build_output_path_falls_back_when_title_empty(tmp_path):
    """정제 후 제목이 비면 'video'로 대체한다 — ' 1080p.mp4' 같은 이름을 막는다."""
    result = build_output_path(str(tmp_path), "???", 1080)
    assert result == str(tmp_path / "video 1080p.mp4")


def test_build_output_path_truncates_long_title(tmp_path):
    """전체 경로가 상한(240자)을 넘으면 제목만 잘라 맞춘다 — 접미사·확장자 보존."""
    result = build_output_path(str(tmp_path), "가" * 300, 1080)
    assert len(result) <= 240
    assert result.endswith(" 1080p.mp4")
    assert os.path.dirname(result) == str(tmp_path)


def test_truncated_titles_with_same_prefix_stay_distinct(tmp_path):
    """추가 확인 3: 앞부분이 같은 긴 제목들은 절단 후에도 해시 표식으로 갈라진다.

    표식 없이는 절단본이 동일해져 " (n)"만으로 구분되고(식별성 저하),
    파일이 없는 시점엔 아예 같은 경로가 나온다.
    """
    prefix = "공통 앞부분 " * 30  # 절단 지점 너머까지 동일한 앞부분
    a = build_output_path(str(tmp_path), prefix + "1회차", 1080)
    b = build_output_path(str(tmp_path), prefix + "2회차", 1080)
    assert a != b  # 파일을 만들지 않아도 서로 다른 경로
    assert len(a) <= 240 and len(b) <= 240


def test_truncated_title_is_deterministic(tmp_path):
    """절단 표식은 원제목의 해시라 같은 제목이면 항상 같은 이름이다 (충돌 시에만 (n))."""
    title = "가" * 300
    assert build_output_path(str(tmp_path), title, 1080) == build_output_path(
        str(tmp_path), title, 1080
    )


# ================================================================ Windows 예약어·끝 점


def test_reserved_device_title_gets_prefixed(tmp_path):
    """추가 확인 1: 제목이 예약 장치명(그대로/점·공백 후속)이면 '_'를 앞에 붙인다.

    Windows 11 실측으로는 CON.mp4도 생성되지만 지원 대상인 Windows 10이
    여전히 예약하므로 보수적으로 막는다 (대소문자 무시).
    """
    assert os.path.basename(build_output_path(str(tmp_path), "CON", 1080)) == "_CON 1080p.mp4"
    assert os.path.basename(build_output_path(str(tmp_path), "con.", 720)) == "_con. 720p.mp4"
    assert os.path.basename(build_output_path(str(tmp_path), "lpt1", 480)) == "_lpt1 480p.mp4"


def test_reserved_lookalike_titles_are_untouched(tmp_path):
    """예약어로 시작하는 일반 단어(CONCERT 등)는 오탐하지 않는다."""
    assert (
        os.path.basename(build_output_path(str(tmp_path), "CONCERT 실황", 1080))
        == "CONCERT 실황 1080p.mp4"
    )
    assert (
        os.path.basename(build_output_path(str(tmp_path), "COM10 리뷰", 1080))
        == "COM10 리뷰 1080p.mp4"
    )


def test_trailing_dots_in_title_land_mid_filename(tmp_path):
    """추가 확인 2: 마침표로 끝나는 제목도 접미사가 항상 뒤에 붙어 이름 중간에 놓인다.

    Windows는 이름 '끝'의 점·공백만 조용히 잘라낸다(실측) — 생성된 이름은
    항상 .mp4로 끝나므로 잘리거나 변형되지 않는다.
    """
    result = build_output_path(str(tmp_path), "오늘도 방송합니다...", 1080)
    assert os.path.basename(result) == "오늘도 방송합니다... 1080p.mp4"
    with open(result, "wb") as f:
        f.write(b"x")
    assert os.path.basename(result) in os.listdir(tmp_path)  # 이름 그대로 저장된다


def test_trailing_spaces_in_title_are_stripped(tmp_path):
    """제목 양끝 공백은 정제 단계에서 제거된다 — ' 제목  ' 같은 입력도 안전."""
    result = build_output_path(str(tmp_path), "  공백 제목   ", 1080)
    assert os.path.basename(result) == "공백 제목 1080p.mp4"


# ================================================================ 임시 폴더 명명


def test_temp_dir_for_derives_from_output_stem(tmp_path):
    """임시 폴더는 산출물 파일명(확장자 제외)에서 파생된다."""
    output = str(tmp_path / "방송 1080p.mp4")
    assert temp_dir_for(output) == str(tmp_path / "CVDv2_temp_방송 1080p")


def test_temp_dir_for_distinct_across_uniquified_outputs(tmp_path):
    """중복 회피로 갈라진 산출물끼리는 임시 폴더도 겹치지 않는다 (#105 확인 항목 4)."""
    a = temp_dir_for(str(tmp_path / "방송 1080p.mp4"))
    b = temp_dir_for(str(tmp_path / "방송 1080p (1).mp4"))
    assert a != b


def test_same_video_queued_twice_gets_distinct_temp_dirs(tmp_path):
    """추가 확인 4: 같은 영상을 두 번 큐에 넣어도 임시 폴더가 갈라진다.

    앱은 다운로드를 순차 실행하고 경로는 각 건의 시작 직전에 배정되므로,
    두 번째 건은 첫 건의 산출물을 보고 " (1)"로 갈라지고 임시 폴더도
    산출물 이름에서 파생돼 함께 갈라진다. (동일 videoId 여부와 무관 —
    산출물 경로 기준이라 같은 영상끼리도 충돌하지 않는다)
    """
    first = build_output_path(str(tmp_path), "같은 영상", 1080)
    with open(first, "wb") as f:
        f.write(b"x")  # 첫 건 완료를 흉내 낸다
    second = build_output_path(str(tmp_path), "같은 영상", 1080)
    assert temp_dir_for(first) != temp_dir_for(second)
