"""릴리즈 초안에 넣을 AI 릴리즈 노트를 만든다(release.yml의 release 잡이 부른다).

직전 정식 태그 뒤 머지된 PR 가운데 노트 대상(제외 라벨이 없는 것)을 모아 Anthropic Messages API를
한 번 부르고, 결과를 notes.md에 쓴다. 실패해도 종료 코드는 0이다 — 노트 없이 초안을 만들고 경고를
notes.md 맨 위에 적는다(초안은 소유자가 읽고 고친 뒤 공개한다).

입력: 환경 변수 ANTHROPIC_API_KEY(시크릿) · ANTHROPIC_MODEL(변수) · GH_TOKEN, gh CLI, 태그까지의 git 이력.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

EXCLUDED_LABELS = {
    "chore",
    "documentation",
    "skip-release-notes",
}  # .github/release.yml의 exclude와 같다
EXCLUDED_AUTHORS = {"github-actions[bot]", "app/github-actions"}  # 같은 파일의 exclude.authors
SUMMARY_LIMIT = 1500  # PR당 넣는 본문 요약의 최대 글자 수
API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
MAX_TOKENS = 4096  # 노트 한 벌에 넉넉한 출력 상한
TIMEOUT_SECONDS = 120  # API 한 번의 제한 시간
PROMPT_FILE = Path(__file__).resolve().parents[1] / ".github" / "release-notes" / "prompt.md"
PR_SQUASH = re.compile(r"\(#(\d+)\)\s*$")  # squash 머지 커밋 제목 끝의 (#번호)
PR_MERGE = re.compile(r"^Merge pull request #(\d+) ")  # merge commit 제목


def run(*command: str) -> str:
    """명령을 돌려 표준 출력을 돌려준다. 실패하면 CalledProcessError."""
    return subprocess.run(
        command, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout


def previous_release_tag(tag: str) -> str | None:
    """이 태그 앞의 정식 태그(`-`가 없는 것) 가운데 가장 높은 판."""
    tags = run("git", "tag", "-l", "v*", "--sort=-v:refname").split()
    official = [t for t in tags if "-" not in t and t != tag]
    base = tag.split("-")[0]
    older = [t for t in official if _key(t) < _key(base)]
    return older[0] if older else None


def _key(tag: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", tag.split("-")[0]))


def merged_pr_numbers(prev: str | None, tag: str) -> list[int]:
    """prev..tag 사이 커밋 제목에서 PR 번호를 순서대로(중복 없이)뽑는다."""
    span = f"{prev}..{tag}" if prev else tag
    numbers: list[int] = []
    for subject in run("git", "log", "--format=%s", span).splitlines():
        match = PR_MERGE.match(subject) or PR_SQUASH.search(subject)
        if match and int(match.group(1)) not in numbers:
            numbers.append(int(match.group(1)))
    return sorted(numbers)


def summary_of(body: str) -> str:
    """본문 맨 위 요약 — '## 요약' 절, 없으면 첫 '## ' 제목 앞까지. 최대 SUMMARY_LIMIT자."""
    body = (body or "").replace("\r\n", "\n")
    match = re.search(r"^## 요약\s*\n(.*?)(?=^## |\Z)", body, re.S | re.M)
    if match:
        text = match.group(1)
    else:
        text = re.split(r"^## ", body, maxsplit=1, flags=re.M)[0] or body
    return text.strip()[:SUMMARY_LIMIT]


def collect(prev: str | None, tag: str, repo: str) -> tuple[list[dict], list[int]]:
    """노트 대상 PR과 뺀 PR 번호를 돌려준다."""
    included, skipped = [], []
    for number in merged_pr_numbers(prev, tag):
        pr = json.loads(
            run(
                "gh",
                "pr",
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                "number,title,labels,body,author,mergedAt,headRefName",
            )
        )
        labels = [label["name"] for label in pr["labels"]]
        # dev-* → main(릴리즈 머지) PR은 라벨이 없어도 뺀다 — v2.11.1의 #451이 라벨 없이 머지됐다
        if (
            not pr["mergedAt"]
            or EXCLUDED_LABELS & set(labels)
            or pr["author"]["login"] in EXCLUDED_AUTHORS
            or pr["headRefName"].startswith("dev-")
        ):
            skipped.append(number)
            continue
        included.append(
            {
                "number": number,
                "title": pr["title"],
                "labels": labels,
                "summary": summary_of(pr["body"]),
            }
        )
    return included, skipped


def ask_model(prs: list[dict], tag: str) -> str:
    """Messages API를 한 번 부른다(도구 없음). 실패하면 RuntimeError."""
    key, model = os.environ.get("ANTHROPIC_API_KEY"), os.environ.get("ANTHROPIC_MODEL")
    if not key or not model:
        raise RuntimeError("ANTHROPIC_API_KEY 또는 ANTHROPIC_MODEL이 없다")
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(
            {
                "model": model,
                "max_tokens": MAX_TOKENS,
                "system": PROMPT_FILE.read_text(encoding="utf-8"),
                "messages": [
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"version": tag, "pull_requests": prs}, ensure_ascii=False
                        ),
                    }
                ],
            }
        ).encode("utf-8"),
        headers={
            "x-api-key": key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            reply = json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise RuntimeError(f"API 호출 실패: {error}") from error
    text = "".join(
        block.get("text", "") for block in reply.get("content", []) if block.get("type") == "text"
    ).strip()
    if not text:
        raise RuntimeError(f"빈 응답(stop_reason={reply.get('stop_reason')})")
    return text


def referenced(notes: str) -> set[int]:
    """노트에 적힌 PR 번호 — (#1, #2) 묶음도 센다."""
    return {int(n) for n in re.findall(r"#(\d+)", notes)}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", required=True)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--out", default="notes.md")
    parser.add_argument("--dry-run", action="store_true", help="API를 부르지 않고 입력만 남긴다")
    args = parser.parse_args()

    prev = previous_release_tag(args.tag)
    prs, skipped = collect(prev, args.tag, args.repo)
    Path(args.out).with_suffix(".inputs.json").write_text(
        json.dumps(
            {"prev": prev, "tag": args.tag, "included": prs, "skipped": skipped},
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"직전 정식 태그 {prev} · 노트 대상 {len(prs)}건 · 뺀 것 {len(skipped)}건")

    warnings, notes = [], ""
    if args.dry_run:
        warnings.append("dry-run — API를 부르지 않았다")
    elif not prs:
        warnings.append("노트 대상 PR이 없다")
    else:
        try:
            notes = ask_model(prs, args.tag)
        except RuntimeError as error:
            warnings.append(f"AI 노트를 만들지 못했다 — {error}")
    if notes:
        expected, got = {pr["number"] for pr in prs}, referenced(notes)
        if expected != got:
            missing = ", ".join(f"#{n}" for n in sorted(expected - got)) or "없음"
            extra = ", ".join(f"#{n}" for n in sorted(got - expected)) or "없음"
            warnings.append(
                f"노트의 PR 번호가 입력과 다르다 — 빠진 것: {missing} · 입력에 없는 것: {extra}"
            )

    head = "".join(f"> ⚠️ **초안 확인 필요**: {w}\n" for w in warnings)
    Path(args.out).write_text((head + "\n" if head else "") + notes + "\n", encoding="utf-8")
    for warning in warnings:
        print(f"::warning::{warning}")


if __name__ == "__main__":
    main()
