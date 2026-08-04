"""app/(viewmodel 계층)의 "뷰 무의존" 불변 규칙 검증 (#171 — Phase 5).

app/ 내 모든 .py 파일을 AST로 파싱해 위젯 계층 import가 없음을 확인한다.
core의 "PySide6 금지"(tests/unit/core/test_no_qt_import.py)와 같은 방식이다 —
app은 QtCore(QObject·Signal)는 쓸 수 있지만, 위젯(QtWidgets·QtGui)과
뷰 모듈(ui, content.view, content.widget)을 알면 viewmodel 분리가 무너진다.
content/download → ui 방향 위반을 잡는 자동 게이트가 없던 공백(#146 감사)을
app 계층에 한해 메운다.
"""

import ast
from pathlib import Path

import app

APP_DIR = Path(app.__file__).resolve().parent

# app에서 import가 금지된 모듈 (최상위 또는 정확한 모듈 경로)
FORBIDDEN_TOP_LEVEL = {"ui"}
FORBIDDEN_MODULES = {"PySide6.QtWidgets", "PySide6.QtGui", "content.view", "content.widget"}


def _iter_import_violations(py_file: Path) -> list[str]:
    """파일 하나를 파싱해 금지 모듈 import 라인 목록을 반환한다."""
    py_file = py_file.resolve()
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module] if node.module else []
        else:
            continue
        for name in names:
            if name.split(".")[0] in FORBIDDEN_TOP_LEVEL or any(
                name == m or name.startswith(m + ".") for m in FORBIDDEN_MODULES
            ):
                violations.append(f"{py_file.relative_to(APP_DIR.parent)}:{node.lineno}: {name}")
    return violations


def test_app_layer_has_no_view_import():
    """app/ 내 모든 .py 파일에 위젯·뷰 모듈 import가 없어야 한다."""
    py_files = sorted(APP_DIR.rglob("*.py"))
    assert py_files, "app/에서 .py 파일을 찾지 못했다 — 스캔 대상 경로를 확인할 것"

    violations = []
    for py_file in py_files:
        violations.extend(_iter_import_violations(py_file))

    assert not violations, "app(viewmodel)은 뷰를 몰라야 한다. 금지 import 발견:\n" + "\n".join(
        violations
    )
