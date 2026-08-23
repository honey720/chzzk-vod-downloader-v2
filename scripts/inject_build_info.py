"""빌드 시점의 커밋·릴리즈 마커를 config/config.py에 주입한다 (#195).

Nuitka 번들에는 .git도 pyproject.toml도 포함되지 않아(#116과 같은 문제),
빌드 실행 파일은 소스에 미리 박아 둔 상수만 읽을 수 있다. 이 스크립트는
Nuitka 빌드를 실행하기 **직전에** config/config.py의 BUILD_COMMIT·
IS_RELEASE_BUILD 두 상수를 실제 값으로 고쳐 쓴다.

- 커밋(BUILD_COMMIT)은 항상 채운다 — 로컬 빌드든 CI 빌드든 무관하다.
- 릴리즈 마커(IS_RELEASE_BUILD)는 --release 플래그를 줬을 때만 True로
  채운다 — release.yml만 이 플래그를 준다. 그래야 로컬 빌드가 커밋 정보가
  붙는다고 정식 릴리즈로 오인되지 않는다("층 1"과 "층 2"의 분리).

이 스크립트가 만든 변경은 빌드 작업공간에만 남고 커밋되지 않는다 — CI
체크아웃은 매 실행마다 새로 받으므로 자연히 원복된다. git이 없거나
실패해도 빌드 자체를 막지 않는다(커밋은 "unknown"으로 남을 뿐이다) —
릴리즈 마커가 이미 정식 여부를 확정하므로 커밋이 unknown이어도 진단이
막히지 않는다.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

# Windows 러너의 stdout은 파이프로 리다이렉트되면 로케일 기반 인코딩(cp1252 등)
# 으로 열려 한글 출력이 UnicodeEncodeError로 죽는다 — macOS·Linux는 stdout이
# 기본 UTF-8이라 안 보이는 문제였다. Python 3.13은 아직 Windows UTF-8 모드가
# 기본이 아니므로(PEP 686은 3.15부터) 여기서 명시적으로 고정한다 (#204).
sys.stdout.reconfigure(encoding="utf-8")

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "config.py"


def _git_describe(repo_root: Path) -> str:
    """git describe --dirty 결과를 얻는다. 실패하면 "unknown"."""
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    commit = result.stdout.strip()
    return commit or "unknown"


def inject(release: bool) -> str:
    """config.py의 두 상수를 고쳐 쓰고, 주입한 커밋 문자열을 반환한다."""
    commit = _git_describe(CONFIG_PATH.parent.parent)
    text = CONFIG_PATH.read_text(encoding="utf-8")

    text, n_commit = re.subn(
        r'^BUILD_COMMIT = ".*"$', f'BUILD_COMMIT = "{commit}"', text, count=1, flags=re.MULTILINE
    )
    text, n_release = re.subn(
        r"^IS_RELEASE_BUILD = \w+$",
        f"IS_RELEASE_BUILD = {release}",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if n_commit != 1 or n_release != 1:
        raise RuntimeError(
            "config/config.py에서 BUILD_COMMIT·IS_RELEASE_BUILD 상수를 찾지 못했다 "
            f"(commit 치환 {n_commit}회, release 치환 {n_release}회) — "
            "상수 이름·형식이 바뀌었는지 확인할 것"
        )

    CONFIG_PATH.write_text(text, encoding="utf-8")
    return commit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--release",
        action="store_true",
        help="정식 릴리즈 빌드 마커(IS_RELEASE_BUILD)를 True로 주입한다 (release.yml 전용)",
    )
    args = parser.parse_args()

    commit = inject(release=args.release)
    print(f"주입 완료: BUILD_COMMIT={commit!r}, IS_RELEASE_BUILD={args.release}")


if __name__ == "__main__":
    main()
