"""산출물 경로 유틸 — 파일명 조립·중복 회피·임시 폴더 명명 (#105).

산출물 파일명(`{제목} {해상도}p.mp4`) 조립이 GUI(content/manager.py)와
헤드리스 스크립트(scripts/headless_download.py)에 같은 식으로 중복돼
있었고, 어느 쪽도 기존 파일 존재를 확인하지 않아 같은 제목의 VOD를
받으면 이전 파일이 경고 없이 덮어써졌다. 이 모듈이 조립·중복 회피의
단일 지점이다 — 호출부는 build_output_path 하나만 부른다.

중복 회피 시점: 경로는 다운로드 시작 직전(output_path 배정 시점)에
확정된다. 앱은 다운로드를 한 건씩 순차 실행하므로(동시 실행 기본 1)
배정 시점의 존재 확인으로 충분하다. 같은 폴더에 같은 제목을 동시에
받는 별도 프로세스(GUI+헤드리스 병행 등)까지는 보장하지 않는다 —
존재 확인과 파일 생성 사이의 원자성이 없기 때문이며, 알려진 한계로
문서화한다.
"""

import os
import re

# Windows에서 파일명에 쓸 수 없는 문자 + 개행 (content/network.py의 제목 정제와 동일 집합).
# 제목은 조회 단계에서 이미 정제되지만, core를 직접 쓰는 경로(헤드리스·다른 UI)가
# 정제를 빠뜨려도 안전하도록 여기서도 방어한다.
_INVALID_FILENAME_CHARS = re.compile(r'[\\/:\*\?"<>|\n]')

# 산출물 전체 경로 길이 상한 — Windows 기본 MAX_PATH(260)에서 중복 회피
# 접미사 " (n)"과 임시 폴더 접두사("CVDv2_temp_") 여유분을 뺀 값
_MAX_FULLPATH = 240


def sanitize_filename(name: str) -> str:
    """파일명에 쓸 수 없는 문자를 제거하고 양끝 공백을 정리한다."""
    return _INVALID_FILENAME_CHARS.sub("", name).strip()


def ensure_unique_path(path: str) -> str:
    """같은 경로가 이미 있으면 확장자 앞에 " (n)"을 붙인 새 경로를 반환한다.

    기존 파일을 절대 덮어쓰지 않기 위한 장치다 (#105 필수 조건).
    비어 있는 이름이 나올 때까지 n을 1부터 올린다.
    """
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 1
    while True:
        candidate = f"{stem} ({n}){ext}"
        if not os.path.exists(candidate):
            return candidate
        n += 1


def build_output_path(directory: str, title: str, resolution: int) -> str:
    """산출물 전체 경로를 조립한다 — 정제·길이 제한·중복 회피 포함.

    파일명 형식은 기존과 동일한 `{제목} {해상도}p.mp4`이고, 같은 이름이
    이미 있을 때만 " (n)"이 붙는다. 전체 경로가 상한을 넘으면 제목
    부분만 잘라 맞춘다 (해상도 접미사·확장자는 보존).
    """
    safe_title = sanitize_filename(str(title)) or "video"
    suffix = f" {resolution}p.mp4"
    candidate = os.path.join(directory, safe_title + suffix)
    overflow = len(candidate) - _MAX_FULLPATH
    if overflow > 0:
        keep = max(1, len(safe_title) - overflow)
        candidate = os.path.join(directory, safe_title[:keep].rstrip() + suffix)
    return ensure_unique_path(candidate)


def temp_dir_for(output_path: str) -> str:
    """세그먼트 임시 폴더 경로를 산출물 파일명에서 파생한다.

    구 코드는 고정 이름("CVDv2_temp") 하나를 모든 다운로드가 공유해,
    같은 폴더로 향하는 두 다운로드가 겹치면 서로의 세그먼트 폴더를
    삭제·재생성할 수 있었다 (#105 확인 항목 4). 산출물 파일명(중복
    회피 후)에서 파생하면 다운로드마다 폴더가 구분된다.
    """
    directory = os.path.dirname(output_path)
    stem = os.path.splitext(os.path.basename(output_path))[0]
    return os.path.join(directory, f"CVDv2_temp_{stem}")
