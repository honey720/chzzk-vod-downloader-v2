"""issue-rules.yml이 부르는 진입점 — 소유자 이슈를 rule_checks.check_issue로 검사한다.

알림은 Actions 실행 로그(::warning::)로만 남긴다 — 라벨 · 코멘트를 달지 않는다.
이슈 만들기는 막을 수 없으므로 언제나 종료 코드 0이다.
작성자가 저장소 소유자가 아니면 검사하지 않는다(사용자 이슈에는 아무것도 남기지 않는다).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import rule_checks as rc  # noqa: E402


def main() -> int:
    """이벤트의 이슈를 검사하고 결과를 로그로 남긴다."""
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    issue = event["issue"]
    if issue["user"]["login"] != os.environ["OWNER"]:
        print("소유자 이슈가 아니라 검사하지 않는다")
        return 0
    labels = [x["name"] for x in issue.get("labels", [])]
    # 에이전트 이슈는 `agent` 라벨로만 알아본다 — 소유자 계정으로 만들어 작성자로는 못 가린다
    found = rc.only_warn(
        rc.check_issue(
            issue["title"],
            issue.get("body") or "",
            labels=labels,
            milestone=(issue.get("milestone") or {}).get("title"),
            by_agent="agent" in labels,
        )
    )
    for f in found:
        print(f"::warning title={f.rule}::#{issue['number']} {f.message}")
    if not found:
        print(f"#{issue['number']} 규칙 검사 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
