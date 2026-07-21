"""
Claude Code PostToolUse hook - 파일 변경 이력 자동 기록
.claude/settings.local.json 의 hooks 설정으로 실행됨

기록 대상: 2.8.0-dev/app, 2.8.0-dev/core 하위 .py 파일 Edit/Write
출력 위치: 2.8.0-dev/docs/activity.log
"""
import sys
import json
from datetime import datetime
from pathlib import Path

DOCS_DIR = Path(__file__).parent.parent / "docs"
LOG_FILE = DOCS_DIR / "activity.log"
WATCH_DIRS = ("2.8.0-dev/app", "2.8.0-dev/core", "2.8.0-dev/scripts")


def main():
    raw = sys.stdin.read().strip()
    if not raw:
        return

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return

    tool = data.get("tool_name", "")
    if tool not in ("Edit", "Write"):
        return

    file_path = data.get("tool_input", {}).get("file_path", "").replace("\\", "/")
    if not file_path.endswith(".py"):
        return

    # 관련 경로만 기록
    if not any(d in file_path for d in WATCH_DIRS):
        return

    # 상대 경로 추출
    marker = "2.8.0-dev/"
    rel = file_path.split(marker)[-1] if marker in file_path else file_path
    action = "CREATE" if tool == "Write" else "EDIT  "
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{timestamp} | {action} | {rel}\n")


if __name__ == "__main__":
    main()
