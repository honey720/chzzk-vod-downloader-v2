"""core/ 게이트들이 함께 쓰는 import 순회 — AST 기반, 함수 안 지연 import까지.

두 게이트(tests/unit/core/test_no_qt_import.py의 Qt 금지, test_layer_direction.py의 방향 금지)가
같은 순회를 쓰되 **금지 판정은 각자** 한다 — 둘은 서로 다른 사실을 잰다. grep 대신 AST를 쓰는
이유: docstring·주석에 규칙 설명으로 등장하는 이름은 허용하고 실제 import 구문만 잡기 위함이다.
`ast.walk`는 함수·클래스 본문까지 내려가므로 지연 import도 잡힌다.
"""

import ast
from dataclasses import dataclass
from pathlib import Path

from tests.unit.layers import CORE_DIR


@dataclass(frozen=True)
class ImportSite:
    """import 구문 하나 — 파일 · 줄 · 점 구분 이름 · 상대 import 여부."""

    file: Path
    lineno: int
    name: str
    relative: bool

    @property
    def top_level(self) -> str:
        """최상위 이름(`a.b.c` → `a`). 상대 import는 빈 문자열."""
        return "" if self.relative else self.name.split(".")[0]


def iter_imports(py_file: Path) -> list[ImportSite]:
    """파일 하나의 import 구문 전부(모듈 상단 + 함수·클래스 안 지연 import)."""
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    sites: list[ImportSite] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            sites.extend(
                ImportSite(py_file, node.lineno, alias.name, False) for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # 상대 import(from . import x) — 같은 패키지 안이므로 어느 게이트도 잡지 않는다
                sites.append(ImportSite(py_file, node.lineno, node.module or "", True))
            elif node.module:
                sites.append(ImportSite(py_file, node.lineno, node.module, False))
    return sites


def core_source_files() -> list[Path]:
    """검사 대상 — `core/` 아래 제품 `.py` 전부(바이트코드 제외). tests/는 `core/` 밖이다."""
    return sorted(p for p in CORE_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def describe(site: ImportSite) -> str:
    """위반 보고용 한 줄 — `core/x.py:12: app.viewmodels.data`."""
    return (
        f"{site.file.resolve().relative_to(CORE_DIR.parent).as_posix()}:{site.lineno}: {site.name}"
    )
