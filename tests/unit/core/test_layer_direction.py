"""core/의 "앱 계층을 모른다" 방향 규칙 게이트 (#193 — SPEC §4 app → core 단방향).

규칙: **core/는 저장소가 소유한 최상위 이름 중 `core` 이외의 것을 import하지 않는다.** 표준
라이브러리와 서드파티는 무관하다. 금지 목록(`{"app"}` 등)을 쓰지 않는 이유 둘 —
① 목록에 없는 새 최상위 디렉토리가 생기면 안 막는다 ② 디렉토리가 사라지면(B 단계의
content·download) 목록만 조용히 빈다. 소유 이름은 tests/unit/layers.py가 저장소 루트에서
**유도**한다(`tests` 포함). 유도가 무언가를 놓치면 이 파일의 메타 게이트가 git 추적 파일과
대조해 시끄럽게 실패한다 — git이 없는 환경에서는 그 대조만 사유가 보이게 건너뛴다.

순회는 tests/unit/core/import_scan.py(Qt 금지 게이트와 공유, 함수 안 지연 import 포함).
금지 판정은 여기서만 한다 — Qt 금지와는 다른 사실을 잰다.
"""

import subprocess
from pathlib import Path

import pytest

from tests.unit.core.import_scan import core_source_files, describe, iter_imports
from tests.unit.layers import ROOT_DIR, owned_top_level_names

#: 소유 이름 수의 바닥 — 유도가 빈 집합이나 한두 개로 무너지면 규칙이 헛돈다. 실측 10(2026-09-06).
MIN_OWNED_NAMES = 5


def forbidden_for_core() -> frozenset[str]:
    """core가 import하면 안 되는 최상위 이름 — 소유 이름 전부에서 `core` 자신만 뺀 것."""
    return owned_top_level_names() - {"core"}


def _git_tracked_top_level_names() -> frozenset[str] | None:
    """git이 아는 `.py`에서 같은 방식으로 뽑은 최상위 이름 — 독립 대조 출처. git이 없으면 None."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--", "*.py"],
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    names: set[str] = set()
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        path = Path(line.strip())
        names.add(path.parts[0] if len(path.parts) > 1 else path.stem)
    return frozenset(names)


def test_core_imports_nothing_the_repository_owns_except_core():
    """core/의 모든 .py(지연 import 포함)가 저장소의 다른 최상위 이름을 import하지 않는다."""
    py_files = core_source_files()
    assert py_files, "core/에서 .py 파일을 찾지 못했다 — 스캔 대상 경로를 확인할 것"
    forbidden = forbidden_for_core()
    assert len(forbidden) >= MIN_OWNED_NAMES - 1, (
        f"소유 이름 유도가 무너졌다(core 제외 {len(forbidden)}개) — layers.owned_top_level_names 확인"
    )

    violations = [
        describe(site)
        for py_file in py_files
        for site in iter_imports(py_file)
        if site.top_level in forbidden
    ]

    assert not violations, (
        "core/는 앱 계층을 몰라야 한다(SPEC §4 app → core 단방향). 저장소의 다른 이름을 import:\n"
        + "\n".join(violations)
    )


def test_owned_names_are_derived_and_cover_every_git_tracked_top_level():
    """메타 게이트 — 유도된 소유 이름이 git 추적 `.py`의 최상위 이름을 전부 포함한다.

    유도가 `tests/`나 새 디렉토리를 놓치면 여기서 이름이 찍혀 실패한다. 빈 집합도 여기서 잡힌다.
    """
    owned = owned_top_level_names()
    assert "core" in owned, "유도가 core 자신도 못 봤다 — 루트 걷기가 잘못됐다"
    assert len(owned) >= MIN_OWNED_NAMES, f"소유 이름이 {len(owned)}개뿐이다: {sorted(owned)}"
    tracked = _git_tracked_top_level_names()
    if tracked is None:
        pytest.skip(
            "git을 쓸 수 없어 소유 이름의 git 대조를 건너뛴다(바닥 수 단언은 위에서 돌았다)"
        )
    assert tracked, "git이 .py를 하나도 돌려주지 않았다 — 대조 전제가 깨졌다"
    missing = sorted(tracked - owned)
    assert not missing, (
        f"git이 아는 최상위 이름이 유도에서 빠졌다(규칙이 그만큼 안 막는다): {missing}"
    )


def test_scanner_sees_imports_inside_functions(tmp_path):
    """순회가 함수 안 지연 import를 본다 — 모듈 상단만 보면 `def f(): import app`이 새어 나간다."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import os\n\ndef f():\n    from app.viewmodels import data\n    import tests\n",
        encoding="utf-8",
    )
    tops = sorted(site.top_level for site in iter_imports(probe))
    assert tops == ["app", "os", "tests"], tops
