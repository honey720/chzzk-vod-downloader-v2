"""core 안 패키지 사이의 import 방향 게이트 (#468).

규칙: core/의 패키지(`core/<패키지>/`)는 아래 표에 적힌 패키지만 import한다. 주된 방향은
`services → sources → engine · sections · media · models`와 `chzzk → media · models`이고, 나머지는
지금 쓰는 아래쪽 방향이다. 표 밖의 방향이 생기거나 표에 없는 패키지가 생기면 실패한다 — 새 방향이
필요하면 이 표를 고치는 것이 곧 그 결정이다. 양방향 쌍(서로 import하는 두 패키지)은 따로 잰다.

순회 대상은 tests/meta/import_scan.py의 파일 목록이고, import는 여기서 직접 읽는다(함수 안 지연
import 포함) — 상대 import(`from ..services import x`)는 파일의 패키지 경로로 풀고,
`from core import services`는 가져오는 이름까지 본다. 둘 다 건너뛰면 그 꼴의 import가 게이트를
조용히 지나간다. 같은 패키지 안의 import와 core/ 바로 아래 파일(`core/__init__.py`)은 재지 않는다.
"""

import ast
from collections import defaultdict
from pathlib import Path

from tests.meta.import_scan import core_source_files
from tests.support.layers import CORE_DIR

#: 패키지 → import해도 되는 다른 core 패키지. 패키지 셋이 고리를 이룬다(engine → models →
#: media → engine — 계약 모델 · 색인 모델 · 요청 세션). 양방향 쌍은 아니라 그대로 적는다.
ALLOWED: dict[str, frozenset[str]] = {
    "services": frozenset({"sources", "models", "utils"}),
    "sources": frozenset({"engine", "sections", "media", "models", "utils"}),
    "chzzk": frozenset({"media", "models"}),
    "sections": frozenset({"engine", "media", "models", "utils"}),
    "engine": frozenset({"models", "utils"}),
    "media": frozenset({"engine", "utils"}),
    "models": frozenset({"media"}),
    "utils": frozenset({"models"}),
}


def _core_packages() -> set[str]:
    """core/ 아래 .py가 있는 패키지 이름 전부 — import가 없는 패키지도 든다."""
    return {
        parts[0]
        for py_file in core_source_files()
        if len(parts := py_file.relative_to(CORE_DIR).parts) >= 2
    }


def _imported_modules(py_file: Path) -> list[tuple[int, str]]:
    """파일 하나의 import 대상(줄 번호, 절대 점 구분 이름) — 상대 import는 풀고, 꾸러미에서 가져온 이름은 붙인다."""
    package = ["core", *py_file.relative_to(CORE_DIR).parts[:-1]]
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if node.level - 1 >= len(package):
                    continue  # 저장소 밖으로 올라가는 상대 import — core 패키지가 아니다
                base = package[: len(package) - (node.level - 1)]
                base += node.module.split(".") if node.module else []
            else:
                base = (node.module or "").split(".")
            found.extend((node.lineno, ".".join([*base, alias.name])) for alias in node.names)
    return found


def _package_edges() -> dict[tuple[str, str], list[str]]:
    """(가져오는 패키지, 가져와지는 패키지) → 그 import 자리 목록. 같은 패키지 안은 뺀다."""
    edges: dict[tuple[str, str], list[str]] = defaultdict(list)
    for py_file in core_source_files():
        parts = py_file.relative_to(CORE_DIR).parts
        if len(parts) < 2:
            continue
        source = parts[0]
        where = py_file.relative_to(CORE_DIR.parent).as_posix()
        for lineno, name in _imported_modules(py_file):
            names = name.split(".")
            if names[0] != "core" or len(names) < 2 or names[1] == source:
                continue
            edges[(source, names[1])].append(f"{where}:{lineno}: {name}")
    return edges


def test_core_packages_import_only_the_packages_their_row_allows() -> None:
    """core 패키지가 표 밖의 패키지를 import하면 그 자리가 실패 글에 찍혀야 한다.

    core/<패키지>/의 .py 전부(지연 import 포함) · ALLOWED 표
    -> 표에 없는 패키지 0 · 표 밖의 방향 import 0
    """
    edges = _package_edges()
    assert edges, "core 패키지 사이 import를 하나도 찾지 못했다 — 순회 대상을 확인할 것"
    packages = _core_packages() | {target for _, target in edges}
    unknown = sorted(packages - ALLOWED.keys())
    outside = [
        f"{source} → {target}: {site}"
        for (source, target), sites in sorted(edges.items())
        if target not in ALLOWED.get(source, frozenset())
        for site in sites
    ]

    assert not unknown, f"표(ALLOWED)에 없는 core 패키지: {unknown}"
    assert not outside, "표(ALLOWED) 밖의 방향 import:\n" + "\n".join(outside)


def test_no_two_core_packages_import_each_other() -> None:
    """core 패키지 두 개가 서로 import하면 그 쌍이 실패 글에 찍혀야 한다.

    core/<패키지>/의 .py 전부(지연 import 포함), 그리고 ALLOWED 표 자체
    -> 실제 import의 양방향 쌍 0 · 표에 적힌 양방향 쌍 0
    """
    edges = _package_edges()
    both = sorted({tuple(sorted(pair)) for pair in edges if pair[::-1] in edges})
    allowed_both = sorted(
        tuple(sorted((source, target)))
        for source, targets in ALLOWED.items()
        for target in targets
        if source in ALLOWED.get(target, frozenset()) and source < target
    )

    assert not allowed_both, f"표(ALLOWED)가 양방향을 허용한다: {allowed_both}"
    assert not both, "서로 import하는 core 패키지:\n" + "\n".join(
        f"{a} ↔ {b}: {edges[(a, b)] + edges[(b, a)]}" for a, b in both
    )
