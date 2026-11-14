"""pr-rules.yml이 부르는 진입점 — 이벤트의 PR을 rule_checks.check_pr로 검사한다.

PR 정보는 이벤트 파일(GITHUB_EVENT_PATH)의 pull_request에서 읽는다 — 셸을 거치지 않는다.
대응 이슈 라벨은 브랜치 이름의 이슈 번호로 gh api에서 읽는다(읽기 권한만).
block이 있으면 종료 코드 1, warn은 ::warning::으로 남기고 통과한다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import rule_checks as rc  # noqa: E402


def issue_labels(repo: str, number: str) -> list[str] | None:
    """이슈 라벨을 읽는다. 읽지 못하면(없는 번호 · 네트워크) None — 라벨 일치 검사를 건너뛴다."""
    r = subprocess.run(
        ["gh", "api", f"repos/{repo}/issues/{number}", "--jq", "[.labels[].name]"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(r.stdout) if r.returncode == 0 else None


def main() -> int:
    """이벤트의 PR을 검사하고 결과를 Actions 주석으로 남긴다."""
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    pr = event["pull_request"]
    repo = os.environ["REPO"]
    head, base = pr["head"]["ref"], pr["base"]["ref"]
    # 저장소 안 브랜치에서 낸 PR만 소유자 쪽 PR로 본다. 포크(외부 기여자)는 head 저장소가 다르다
    own = bool(pr["head"]["repo"]) and pr["head"]["repo"]["full_name"] == repo
    m = rc.BRANCH_RE.match(head)
    il = issue_labels(repo, m.group("issue")) if m and own else None
    found = rc.check_pr(
        pr["title"],
        pr.get("body") or "",
        head=head,
        base=base,
        labels=[x["name"] for x in pr.get("labels", [])],
        milestone=(pr.get("milestone") or {}).get("title"),
        issue_labels=il,
        owner_pr=own,
    )
    if not own:
        print("::notice title=external::외부 기여자 PR — 모든 항목을 알리기로만 남긴다")
    for f in found:
        kind = "error" if f.level == "block" else "warning"
        print(f"::{kind} title={f.rule}::{f.message}")
    if not found:
        print("규칙 검사 통과")
    return 1 if any(f.level == "block" for f in found) else 0


if __name__ == "__main__":
    sys.exit(main())
