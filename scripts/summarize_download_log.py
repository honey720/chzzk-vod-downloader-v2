"""다운로드 로그에서 구조적 지표를 추출·요약하는 보조 스크립트 (#73 착수 조건 나).

이주 전(기준선)과 이주 후 로그를 같은 기준으로 비교하기 위한 도구다.
판정 기준은 절대 속도가 아니라 구조적 지표다 — 최대 동시 스레드 수,
스레드 증가 궤적, 느린 파트 재시도 건수, 파트 실패·복구 여부.

파싱 규칙: 스레드 이름([MonitorThread] 등)이나 로그 프리픽스 형식에 의존하지
않고 **메시지 본문 패턴만** 사용한다 — 엔진 이주로 스레드 이름이 바뀌어도
기준선 로그와 이주 후 로그를 동일하게 처리할 수 있어야 하기 때문이다.

사용법:
    uv run python scripts/summarize_download_log.py <download_로그파일> [...]
    uv run python scripts/summarize_download_log.py --json <download_로그파일>

로그 파일이 여러 개면 순서대로 각각 요약한다 (기준선/이주 후 나란히 비교용).
"""

import argparse
import json
import re
import sys
from pathlib import Path

# 메시지 본문 패턴 (download/logger.py의 메시지 형식과 1:1).
# 줄 프리픽스(시각·레벨·스레드 이름)는 의도적으로 매칭하지 않는다.
_PATTERNS = {
    "total_size": re.compile(r"Download started - Total size: (\d+) bytes"),
    "part_size": re.compile(r"Part size: (\d+) bytes"),
    "segments": re.compile(r"Segments: (\d+)"),
    "initial_threads": re.compile(r"Initial threads: (\d+)"),
    # 스레드 조정 이벤트(조정 시 INFO로 남는 메시지). 뒤에 "- Avg speed:"가
    # 붙는 주기 측정 디버그 메시지와 구분하기 위해 문장 끝을 고정한다.
    "adjust": re.compile(r"Active threads: (\d+) - Download speed: [\d.]+ MB/s$"),
    # 주기 측정 디버그 메시지 — 활성 스레드 수 관측용
    "tick": re.compile(
        r"Active threads: (\d+) - Download speed: [\d.]+ MB/s - Avg speed: [\d.]+ MB/s"
    ),
    "part_start": re.compile(r"Thread (\d+) started - Range: (\d+)-(\d+)"),
    "part_complete": re.compile(r"Thread (\d+) completed - Downloaded: (\d+) bytes"),
    "slow_retry": re.compile(r"Part (\d+) stopped due to slow speed, will retry"),
    "part_failed": re.compile(r"Part (\d+) download failed"),
    "completed": re.compile(r"Download completed in ([\d.]+) seconds"),
}


def summarize(log_path: Path) -> dict:
    """로그 파일 하나를 파싱해 구조적 지표 dict를 반환한다."""
    summary: dict = {
        "file": str(log_path),
        "total_size": None,
        "part_size": None,
        "segments": None,
        "initial_threads": None,
        # 스레드 조정 이벤트의 시간순 목록 — 증가 궤적 비교의 핵심 지표
        "thread_trajectory": [],
        # 주기 측정에서 관측된 활성 스레드 수의 최댓값
        "max_active_threads": 0,
        "part_starts": 0,
        "part_completes": 0,
        "slow_retries": 0,
        "part_failures": 0,
        "completed_in_seconds": None,
    }

    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        # 문장 끝 고정($) 패턴을 위해 줄 끝 공백을 제거한다
        line = line.rstrip()
        if m := _PATTERNS["adjust"].search(line):
            summary["thread_trajectory"].append(int(m.group(1)))
        elif m := _PATTERNS["tick"].search(line):
            summary["max_active_threads"] = max(summary["max_active_threads"], int(m.group(1)))
        elif _PATTERNS["part_start"].search(line):
            summary["part_starts"] += 1
        elif _PATTERNS["part_complete"].search(line):
            summary["part_completes"] += 1
        elif _PATTERNS["slow_retry"].search(line):
            summary["slow_retries"] += 1
        elif _PATTERNS["part_failed"].search(line):
            summary["part_failures"] += 1
        elif m := _PATTERNS["total_size"].search(line):
            summary["total_size"] = int(m.group(1))
        elif m := _PATTERNS["part_size"].search(line):
            summary["part_size"] = int(m.group(1))
        elif m := _PATTERNS["segments"].search(line):
            summary["segments"] = int(m.group(1))
        elif m := _PATTERNS["initial_threads"].search(line):
            summary["initial_threads"] = int(m.group(1))
        elif m := _PATTERNS["completed"].search(line):
            summary["completed_in_seconds"] = float(m.group(1))

    # 파생 지표: 재시도·실패로 인한 재큐잉 합계, 재큐잉이 있었어도 완주했는지 여부.
    # 파트 시작/완료는 DEBUG 로그라 기본 레벨(INFO)에서는 0으로 남는다 — 완주 판정은
    # 완료 메시지(INFO) 기준으로 하고, 파트 수 검증은 DEBUG 로그가 있을 때만 수행한다.
    summary["requeued_parts"] = summary["slow_retries"] + summary["part_failures"]
    completed = summary["completed_in_seconds"] is not None
    if summary["part_completes"] > 0 and summary["segments"] is not None:
        completed = completed and summary["part_completes"] >= summary["segments"]
    summary["recovered"] = completed
    return summary


def _format_bytes(size: int | None) -> str:
    """바이트 수를 사람이 읽는 단위로 표기한다."""
    if size is None:
        return "?"
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{size / 1024:.1f} KB"


def print_summary(summary: dict) -> None:
    """지표 요약을 사람이 읽는 형식으로 출력한다."""
    print(f"=== {summary['file']}")
    print(f"  전체 크기        : {_format_bytes(summary['total_size'])}")
    print(f"  파트 크기        : {_format_bytes(summary['part_size'])}")
    print(f"  세그먼트 수      : {summary['segments']}")
    print(f"  초기 스레드      : {summary['initial_threads']}")
    trajectory = summary["thread_trajectory"]
    trajectory_str = " -> ".join(map(str, trajectory)) if trajectory else "(조정 없음)"
    print(f"  스레드 조정 궤적 : {trajectory_str}")
    print(f"  최대 활성 스레드 : {summary['max_active_threads']}")
    if summary["part_starts"] or summary["part_completes"]:
        print(f"  파트 시작/완료   : {summary['part_starts']} / {summary['part_completes']}")
    else:
        print("  파트 시작/완료   : (DEBUG 로그 없음)")
    print(f"  느린 파트 재시도 : {summary['slow_retries']}")
    print(f"  파트 실패        : {summary['part_failures']}")
    print(f"  재큐잉 합계      : {summary['requeued_parts']}")
    completed = summary["completed_in_seconds"]
    print(
        f"  완료 시간        : {f'{completed:.2f}s' if completed is not None else '(완료 기록 없음)'}"
    )
    print(f"  완주(복구 포함)  : {'예' if summary['recovered'] else '아니오'}")


def main(argv: list[str]) -> int:
    """인자로 받은 로그 파일들을 각각 요약한다."""
    parser = argparse.ArgumentParser(description="다운로드 로그 구조적 지표 요약 (#73)")
    parser.add_argument("logs", nargs="+", help="download_*.log 파일 경로 (여러 개 가능)")
    parser.add_argument("--json", action="store_true", help="JSON으로 출력 (기계 비교용)")
    args = parser.parse_args(argv)

    summaries = []
    for log in args.logs:
        path = Path(log)
        if not path.is_file():
            print(f"오류: 파일을 찾을 수 없다 — {path}", file=sys.stderr)
            return 2
        summaries.append(summarize(path))

    if args.json:
        print(json.dumps(summaries, ensure_ascii=False, indent=2))
    else:
        for summary in summaries:
            print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
