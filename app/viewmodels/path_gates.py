"""저장 경로 관문 판정의 단일 지점 (#169 — #146 ⓑ1).

네 관문(조회·보존 #165·다운로드 게이트 #137·카드 편집 #148)의 '판정'이
이 모듈에 모인다. 실패 문구·팝업·로그는 호출자(UI 계층)에 남는다 —
문구 재설계는 UI 무변화 원칙상 셸 단계(2.10.0)의 몫이다.

판정 기준이 관문마다 다른 것(exists / isdir / 쓰기 프로브)은 현행 동작
보존을 위해 의도적으로 유지한다. 기준 통일(예: 조회 관문이 파일 경로를
통과시키는 문제)은 동작 변경이므로 셸 단계에서 문구와 함께 재검토한다.
"""

import os


def check_fetch_path(path: str) -> bool:
    """조회 관문: 존재만 본다 — 파일이어도 통과, 쓰기 여부는 다운로드 게이트가 판정."""
    return os.path.exists(path)


def check_remember_path(path: str) -> bool:
    """보존 관문 (#165): 실존 폴더만 — 커밋된 미완성·오타 경로가 초기값을 오염시키지 않게."""
    return bool(path) and os.path.isdir(path)


def check_card_edit_path(path: str) -> bool:
    """카드 편집 관문 (#148): 존재만 본다."""
    return bool(path) and os.path.exists(path)


def check_download_path(path: str, probe) -> tuple[bool, str]:
    """다운로드 게이트 (#137): 존재+쓰기 프로브.

    probe는 content.manager.probe_writable을 주입받는다 — 무응답 마운트
    대비 제물 스레드 방식(#136)은 그쪽이 정본이고, 여기는 판정 배치만
    담당한다. 반환은 (쓰기 가능 여부, "" | "missing" | "denied" | "timeout").
    """
    return probe(path)
