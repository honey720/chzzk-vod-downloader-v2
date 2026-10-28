"""core 안 패키지 사이의 import 방향 게이트 (#468).

규칙: core/의 패키지(`core/<패키지>/`)는 아래 표에 적힌 패키지만 import한다. 주된 방향은
`services → sources → engine · sections · media · models`와 `chzzk → media · models`이고, 나머지는
지금 쓰는 아래쪽 방향이다. 표 밖의 방향이 생기거나 표에 없는 패키지가 생기면 실패한다 — 새 방향이
필요하면 이 표를 고치는 것이 곧 그 결정이다. 양방향 쌍(서로 import하는 두 패키지)은 따로 잰다.

순회는 tests/meta/import_scan.py(함수 안 지연 import 포함). 같은 패키지 안의 import와
core/ 바로 아래 파일(`core/__init__.py`)은 재지 않는다.
"""

from collections import defaultdict

from tests.meta.import_scan import core_source_files, describe, iter_imports
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


def _package_edges() -> dict[tuple[str, str], list[str]]:
    """(가져오는 패키지, 가져와지는 패키지) → 그 import 자리 목록. 같은 패키지 안은 뺀다."""
    edges: dict[tuple[str, str], list[str]] = defaultdict(list)
    for py_file in core_source_files():
        parts = py_file.relative_to(CORE_DIR).parts
        if len(parts) < 2:
            continue
        source = parts[0]
        for site in iter_imports(py_file):
            names = site.name.split(".")
            if site.relative or names[0] != "core" or len(names) < 2:
                continue
            if names[1] != source:
                edges[(source, names[1])].append(describe(site))
    return edges


def test_core_packages_import_only_the_packages_their_row_allows():
    """core 패키지가 표 밖의 패키지를 import하면 그 자리가 실패 글에 찍혀야 한다.

    core/<패키지>/의 .py 전부(지연 import 포함) · ALLOWED 표
    -> 표에 없는 패키지 0 · 표 밖의 방향 import 0
    """
    edges = _package_edges()
    assert edges, "core 패키지 사이 import를 하나도 찾지 못했다 — 순회 대상을 확인할 것"
    packages = {source for source, _ in edges} | {target for _, target in edges}
    unknown = sorted(packages - ALLOWED.keys())
    outside = [
        f"{source} → {target}: {site}"
        for (source, target), sites in sorted(edges.items())
        if target not in ALLOWED.get(source, frozenset())
        for site in sites
    ]

    assert not unknown, f"표(ALLOWED)에 없는 core 패키지: {unknown}"
    assert not outside, "표(ALLOWED) 밖의 방향 import:\n" + "\n".join(outside)


def test_no_two_core_packages_import_each_other():
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
