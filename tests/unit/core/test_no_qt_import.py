"""core/의 "PySide6 import 금지" 불변 규칙 검증 (#50 리뷰 반영).

core/ 내 모든 .py 파일을 AST로 파싱해 PySide6 계열 import가 없음을 확인한다.
grep 대신 AST를 쓰는 이유: docstring·주석에 규칙 설명으로 등장하는 "PySide6"
문자열은 허용하고, 실제 import 구문만 위반으로 잡기 위함이다.
이후 core에 Qt 의존이 스며들면 CI가 이 테스트로 잡는다.
"""

import ast
from pathlib import Path

import core

CORE_DIR = Path(core.__file__).resolve().parent

# core에서 import가 금지된 최상위 패키지 (Qt 바인딩 및 그 런타임)
FORBIDDEN_PACKAGES = {"PySide6", "shiboken6"}


def _iter_import_violations(py_file: Path) -> list[str]:
    """파일 하나를 파싱해 금지 패키지 import 라인 목록을 반환한다."""
    py_file = py_file.resolve()
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            # 상대 import(from . import x)는 module이 None일 수 있다 — core 내부이므로 허용
            names = [node.module] if node.module else []
        else:
            continue
        for name in names:
            if name.split(".")[0] in FORBIDDEN_PACKAGES:
                violations.append(f"{py_file.relative_to(CORE_DIR.parent)}:{node.lineno}: {name}")
    return violations


def test_core_has_no_qt_import():
    """core/ 내 모든 .py 파일에 PySide6·shiboken6 import가 없어야 한다."""
    py_files = sorted(CORE_DIR.rglob("*.py"))
    assert py_files, "core/에서 .py 파일을 찾지 못했다 — 스캔 대상 경로를 확인할 것"

    violations = []
    for py_file in py_files:
        violations.extend(_iter_import_violations(py_file))

    assert not violations, "core/는 UI 프레임워크를 몰라야 한다. 금지 import 발견:\n" + "\n".join(
        violations
    )
