"""커밋 · PR · 이슈 · 코멘트 글이 저장소 규칙을 지켰는지 가린다.

표준 라이브러리만 쓴다. 같은 함수를 git hook(commit-msg) · Claude Code hook(claude_gh_guard) ·
GitHub Actions(pr-rules · issue-rules)가 함께 부른다 — 검사 정본은 이 파일 하나다.
결과는 (등급, 규칙 id, 설명) 목록이다. 등급은 "block"(막기) · "warn"(알리기).

등급은 소유자가 정한 구성을 따른다.
- 제목 길이 · 명사형 끝 · 커밋 한 줄 · 제목 타입 ≠ 브랜치 타입은 언제나 알리기
- 외부 기여자 PR은 모든 항목을 알리기로 낮춘다
- `agent` 라벨은 에이전트 명령(Claude hook)에서만 막고, Actions는 누가 냈는지 몰라 알리기
- 브랜치 이름은 푸시(pre-push)에서 막고, PR 단계에서는 알리기

규칙 출처: CLAUDE.md, .claude/rules/{workflow,branch,privacy}.md, .github 템플릿, 소유자 규칙.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass

TYPES = ("feat", "fix", "refactor", "test", "docs", "chore")  # workflow.md 커밋 타입
ISSUE_PREFIX = {"bug": "[Bug]", "feature": "[Feat]", "improvement": "[Improve]"}  # 이슈 폼 title
TITLE_RE = re.compile(r"^(?P<type>%s)(\((?P<scope>[^)]+)\))?: (?P<summary>\S.*)$" % "|".join(TYPES))
ISSUE_TITLE_RE = re.compile(r"^\[(Bug|Feat|Improve)\] (?P<summary>\S.*)$")
BRANCH_RE = re.compile(r"^(?P<type>%s)/(?P<issue>\d+)-[a-z0-9.-]+$" % "|".join(TYPES))
TAIL_REF_RE = re.compile(r"\s*\((?:(?:Refs|Closes) )?#\d+\)\s*$")  # 제목 끝 번호 — 길이에서 뺀다
SUMMARY_MIN, SUMMARY_MAX = 30, 40  # workflow.md 제목 길이(타입 접두 · 번호 제외)
# 문장으로 끝난 제목 — 명사형 끝 규칙의 반례만 잡는다(「~ 수정」 「~ 쓰기」는 통과)
SENTENCE_END_RE = re.compile(r"(다|요|니다|함\.|음\.|\.)$")
ATTRIBUTION_RE = re.compile(r"^(Claude-Session:|Co-Authored-By:.*anthropic)", re.I)
PR_SECTIONS = ("관련 이슈", "변경 내용", "검증", "아키텍처 체크", "리뷰어에게")  # PR 템플릿
ISSUE_SECTIONS = {  # 이슈 폼이 본문에 남기는 ### 머리
    "bug": ("기대 동작", "실제 동작", "재현 절차", "환경"),
    "feature": ("목적", "요구사항", "작업 범위", "완료 조건"),
    "improvement": ("현재 상태와 문제", "제안 내용", "리스크와 영향 범위", "착수 조건"),
}
LINK_RE = re.compile(r"\b(Closes|Refs) #(\d+)\b")
CLOSE_WORD_RE = re.compile(r"\b(close[sd]?|fix(e[sd])?|resolve[sd]?) #\d+", re.I)
COMMENT_HEAD = "🤖 **Claude** —"  # privacy.md 코멘트 첫 줄
# privacy.md · 오너 규칙 — 오탐이 적은 꼴만. 실제 화면 px · 머신 구성 같은 것은 기계로 못 가린다
PRIVACY = [
    (
        "local-path",
        re.compile(r"\b[A-Za-z]:[\\/]+Users[\\/]|(?<![\w.])/(Users|home)/[^/\s]+/"),
        "로컬 절대 경로",
    ),
    ("cookie", re.compile(r"\bNID_(AUT|SES)\s*[=:]\s*\S{8,}"), "쿠키 값"),
    (
        "token",
        re.compile(
            r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-ant-[\w-]{10,})"
        ),
        "토큰",
    ),
    (
        "video-url",
        re.compile(r"chzzk\.naver\.com/(video|clips?|live)/\w+"),
        "영상 · 클립 · 방송 주소",
    ),
    ("channel-id", re.compile(r"\b[0-9a-f]{32}\b"), "채널 ID 꼴(32자리 16진수)"),
    (
        "cdn-host",
        re.compile(r"https?://[\w.-]*(pstatic|akamaized|navercdn|cdn)[\w.-]*/\S+"),
        "CDN 주소",
    ),
    (
        "resolution",
        re.compile(r"\b(1280|1366|1440|1536|1600|1920|2560|3440|3840)\s*[x×]\s*\d{3,4}\b"),
        "화면 해상도 꼴",
    ),
]


@dataclass(frozen=True)
class Finding:
    """검사 결과 하나."""

    level: str  # "block" | "warn"
    rule: str  # 규칙 id — 보고 표의 id와 같다
    message: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.rule}: {self.message}"


def _summary_len(summary: str) -> int:
    return len(TAIL_REF_RE.sub("", summary).strip())


def check_title(kind: str, title: str) -> list[Finding]:
    """커밋 · PR · 이슈 제목의 꼴 · 길이 · 맺음을 검사한다.

    Args:
        kind: "commit" · "pr" · "issue"
    """
    out: list[Finding] = []
    title = title.strip()
    if kind == "commit" and (
        title.startswith(('Revert "', "Merge ")) or title.startswith("fixup!")
    ):
        return out  # git이 만드는 제목
    if kind in ("commit", "pr"):
        m = TITLE_RE.match(title)
        if not m:
            out.append(
                Finding("block", "title-type", f"`<타입>: <요약>` 꼴이 아님 — {title[:60]!r}")
            )
            return out
    else:
        m = ISSUE_TITLE_RE.match(title)
        if not m:
            out.append(
                Finding(
                    "block",
                    "issue-prefix",
                    f"[Bug] · [Feat] · [Improve] 접두 없음 — {title[:60]!r}",
                )
            )
            return out
    summary = m.group("summary")
    n = _summary_len(summary)
    if not SUMMARY_MIN <= n <= SUMMARY_MAX:
        out.append(
            Finding(
                "warn",
                "title-length",
                f"요약 {n}자(기준 {SUMMARY_MIN}~{SUMMARY_MAX}, 넘칠 때만 예외)",
            )
        )
    if SENTENCE_END_RE.search(TAIL_REF_RE.sub("", summary).strip()):
        out.append(
            Finding("warn", "title-noun-end", "명사형으로 끝나지 않음(「~ 수정」 「~ 추가」 꼴)")
        )
    return out


def strip_attribution(message: str) -> str:
    """커밋 메시지에서 세션 꼬리표 · anthropic 공동저자 줄을 지운다(저장소 commit-msg 훅과 같은 일)."""
    return "\n".join(line for line in message.splitlines() if not ATTRIBUTION_RE.match(line))


def check_commit_message(message: str) -> list[Finding]:
    """커밋 메시지 전체를 검사한다. `#` 주석 줄과 첨부 줄은 지운 뒤 본다."""
    lines = [ln for ln in strip_attribution(message).splitlines() if not ln.startswith("#")]
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return [Finding("block", "commit-empty", "빈 메시지")]
    out = check_title("commit", lines[0])
    if any(ln.strip() for ln in lines[1:]) and not lines[0].startswith('Revert "'):
        out.append(Finding("warn", "commit-one-line", "본문이 있음(오너 규칙: 한 줄 · 본문 없음)"))
    out += check_privacy(message)
    return out


def _sections(body: str, level: str) -> list[str]:
    return [h.strip() for h in re.findall(rf"^{level} (.+)$", body, re.M)]


def check_pr(
    title: str,
    body: str,
    *,
    head: str,
    base: str,
    labels: list[str],
    milestone: str | None,
    issue_labels: list[str] | None = None,
    owner_pr: bool = True,
    by_agent: bool = False,
) -> list[Finding]:
    """PR 제목 · 본문 · 브랜치 · 라벨 · 마일스톤을 검사한다.

    범프 PR(`chore/version-*` → `dev-*`)과 릴리즈 PR(`dev-*` → `main`)은 워크플로가 만든다.
    제목 · 템플릿 검사에서 빼고, 마일스톤이 없어도 알리기만 한다 — 막으면 릴리즈가 멈춘다.

    Args:
        owner_pr: 저장소 안 브랜치에서 낸 PR이면 True. 외부 기여자(포크) PR은 모두 알리기로 낮춘다
        issue_labels: 대응 이슈의 라벨(모르면 None — 라벨 일치 검사를 건너뛴다)
        by_agent: 에이전트가 내는 명령이면 True(Claude hook). `agent` 라벨이 없으면 막는다
    """
    out: list[Finding] = []
    body = body or ""
    labels_set = set(labels)
    release_pr = head.startswith(("dev-", "chore/version-"))  # 워크플로가 만든 PR
    if not release_pr:
        out += check_title("pr", title)
    m_title = TITLE_RE.match(title.strip())
    m_branch = BRANCH_RE.match(head)
    if not release_pr and not head.startswith("chore/"):
        if not m_branch:
            # 원격에 올리는 것은 pre-push가 막는다 — PR 단계에서는 알리기
            out.append(
                Finding(
                    "warn",
                    "branch-name",
                    f"브랜치 이름이 `<타입>/<이슈번호>-<설명>`이 아님 — {head}",
                )
            )
        elif m_title and m_title.group("type") != m_branch.group("type"):
            out.append(
                Finding(
                    "warn",
                    "title-branch-type",
                    f"제목 타입 {m_title.group('type')} ≠ 브랜치 타입 {m_branch.group('type')}",
                )
            )
    if base == "main" and not head.startswith("dev-"):
        out.append(Finding("block", "base-main", "main을 베이스로 한 PR(dev-* → main만 허용)"))
    target = base if base.startswith("dev-") else head if head.startswith("dev-") else ""
    if target:
        want = "v" + target[len("dev-") :]
        if milestone != want:
            level = "warn" if release_pr else "block"
            out.append(Finding(level, "milestone", f"마일스톤 {milestone!r} — {want!r}여야 함"))
    if not release_pr and not head.startswith("chore/"):
        heads = _sections(body, "##")
        missing = [s for s in PR_SECTIONS if s not in heads]
        if missing:
            out.append(Finding("block", "pr-sections", "템플릿 절 없음: " + " · ".join(missing)))
        links = LINK_RE.findall(body)
        if not links:
            out.append(
                Finding("block", "pr-link", "`Closes #N` · `Refs #N` 없음(빈 `Closes #` 포함)")
            )
        elif m_branch and m_branch.group("issue") not in {n for _, n in links}:
            out.append(
                Finding(
                    "warn",
                    "pr-link-branch",
                    f"브랜치의 이슈 #{m_branch.group('issue')}가 Closes/Refs에 없음",
                )
            )
        first = body.lstrip().split("\n## ", 1)[0] if not body.lstrip().startswith("## ") else ""
        if not first.strip():
            out.append(Finding("warn", "pr-summary", "본문 최상단 요약(3~5줄) 없음"))
        if by_agent and "agent" not in labels_set:
            out.append(Finding("block", "agent-label", "에이전트 PR에 `agent` 라벨 없음"))
        elif owner_pr and "agent" not in labels_set:
            out.append(
                Finding("warn", "agent-label", "`agent` 라벨 없음(에이전트가 낸 PR이면 필수)")
            )
        if issue_labels is not None:
            lacking = set(issue_labels) - labels_set - {"approved"}
            if lacking:
                out.append(
                    Finding(
                        "block",
                        "label-match",
                        "대응 이슈 라벨이 PR에 없음: " + ", ".join(sorted(lacking)),
                    )
                )
        if (
            m_branch
            and m_branch.group("type") in ("test", "refactor", "docs")
            and "skip-release-notes" not in labels_set
        ):
            out.append(
                Finding(
                    "warn",
                    "skip-notes",
                    f"{m_branch.group('type')}/ PR인데 skip-release-notes 없음",
                )
            )
    out += check_privacy(title + "\n" + body)
    if not owner_pr:
        out = only_warn(out)
    return out


def check_issue(
    title: str, body: str, *, labels: list[str], milestone: str | None, by_agent: bool
) -> list[Finding]:
    """이슈 제목 · 폼 절 · 라벨 · 마일스톤을 검사한다.

    GitHub는 이슈 만들기를 막을 수 없어 막는 자리는 에이전트 명령(guard) 하나뿐이다.
    에이전트 이슈의 마일스톤 없음은 막는다 — `gh issue create --milestone`으로 바로 달 수 있다.
    Actions(issue_rules.py)는 결과를 only_warn으로 낮춰 로그로만 남긴다.
    """
    out = check_title("issue", title)
    kind = next((k for k in ISSUE_SECTIONS if k in labels), None)
    if kind is None:
        out.append(Finding("block", "issue-kind-label", "bug · feature · improvement 라벨 없음"))
    else:
        if not title.startswith(ISSUE_PREFIX[kind]):
            out.append(
                Finding(
                    "block",
                    "issue-prefix-label",
                    f"{kind} 라벨인데 제목 접두가 {ISSUE_PREFIX[kind]}이 아님",
                )
            )
        missing = [s for s in ISSUE_SECTIONS[kind] if s not in _sections(body or "", "###")]
        if missing:
            out.append(Finding("block", "issue-sections", "폼 절 없음: " + " · ".join(missing)))
    if by_agent and "agent" not in labels:
        out.append(Finding("block", "agent-label", "에이전트 이슈에 `agent` 라벨 없음"))
    if by_agent and not milestone:
        out.append(Finding("block", "milestone", "에이전트 이슈에 마일스톤 없음(대상 버전)"))
    out += check_privacy(title + "\n" + (body or ""))
    return out


def check_comment(body: str) -> list[Finding]:
    """에이전트 코멘트 · 리뷰 답글의 머리와 공개 금지 항목을 검사한다."""
    out: list[Finding] = []
    if not body.lstrip().startswith(COMMENT_HEAD):
        out.append(Finding("block", "comment-head", f"첫 줄이 「{COMMENT_HEAD}」로 시작하지 않음"))
    return out + check_privacy(body)


def check_privacy(text: str) -> list[Finding]:
    """공개 글에 쓰면 안 되는 꼴을 찾는다. 걸린 값 자체는 결과에 옮기지 않는다."""
    out: list[Finding] = []
    for rule, rx, name in PRIVACY:
        n = len(rx.findall(text))
        if n:
            level = "warn" if rule in ("channel-id", "resolution", "cdn-host") else "block"
            out.append(Finding(level, f"privacy-{rule}", f"{name} {n}곳"))
    return out


def only_warn(found: list[Finding]) -> list[Finding]:
    """모든 결과를 알리기로 낮춘다 — 이슈 검사(로그로만 알림)와 외부 기여자 PR이 쓴다."""
    return [Finding("warn", f.rule, f.message) for f in found]


def main(argv: list[str]) -> int:
    """`commit-msg <파일>` 하나만 CLI로 연다 — git commit-msg 훅이 부른다."""
    if len(argv) == 3 and argv[1] == "commit-msg":
        with open(argv[2], encoding="utf-8") as f:
            found = check_commit_message(f.read())
        for x in found:
            print(x, file=sys.stderr)
        return 1 if any(x.level == "block" for x in found) else 0
    print("사용: rule_checks.py commit-msg <파일>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
