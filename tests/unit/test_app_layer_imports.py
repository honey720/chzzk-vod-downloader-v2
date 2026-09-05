"""app 계층의 "뷰모델은 뷰를 모른다" 불변 규칙 검증 (#171 — Phase 5, #259 C0 재범위).

규칙은 **계층 관계**로 판정한다 — 모듈 이름을 문자열로 들지 않는다(#259 C0).

- **뷰 계층**은 두 가지로 정한다. ① `app/views/`·`app/widgets/` 아래 있는 모듈(위치 —
  아직 없어도 규칙은 서 있고 생기는 순간 적용된다). ② 프로젝트 import 그래프에서
  `PySide6.QtWidgets`·`PySide6.QtGui`에 **닿는** 모듈(내용 — 직접이든 다른 프로젝트 모듈을
  거쳐서든). `content.view`·`app.widgets.widget`·`ui/*`·`theme`는 이름이 아니라 ②로 잡히므로
  파일을 옮기거나 이름을 바꿔도 계속 잡힌다.
- **뷰모델 계층**은 `app/` 아래에서 뷰 계층 디렉토리 밖에 있는 모든 `.py`다
  (`app/viewmodels/`·`app/platform_adapter.py` 등). 이 계층은 Qt 위젯 모듈을 직접 import해도,
  뷰 계층 모듈을 import해도 안 된다. QtCore(QObject·Signal)는 쓸 수 있다.

구 게이트가 `app/` 전체에 `content.view`·`content.widget` 이름을 금지하던 것은 이동 뒤
그 이름이 사라지면 아무것도 막지 않았고, `app/views/`가 생기는 순간 실패했다. core의
"PySide6 금지"(tests/unit/core/test_no_qt_import.py)와 같은 AST 방식이다.

⚠️ 메타 게이트 — 각 단언은 **실제로 검사한 파일 수**를 함께 단언한다. 대상이 비면 통과가
아니라 실패다(#259 C0).
"""

import ast
from functools import lru_cache
from pathlib import Path

import app

APP_DIR = Path(app.__file__).resolve().parent
ROOT_DIR = APP_DIR.parent

#: Qt 위젯 모듈 — 여기에 닿는 프로젝트 모듈이 곧 뷰 계층이다.
QT_VIEW_MODULES = {"PySide6.QtWidgets", "PySide6.QtGui"}

#: 뷰 계층 디렉토리(SPEC §3.3 목표 구조) — 존재하지 않아도 규칙은 선다.
VIEW_LAYER_DIRS = (APP_DIR / "views", APP_DIR / "widgets")

#: 프로젝트 모듈 색인에서 빼는 디렉토리(어느 깊이든) — 테스트·숨김·바이트코드·빌드 산출물(test_theme의 스캔과 같은 이유).
_INDEX_EXCLUDED_DIRS = {"tests"}
_BUILD_ARTIFACT_SUFFIXES = (".build", ".dist", ".onefile-build")

#: 프로젝트 모듈 색인의 바닥 수 — 색인이 비면 "뷰에 닿는 모듈"이 없어 규칙이 헛돈다.
#: 이동은 이 수를 바꾸지 않는다. 흡수·삭제로 내려가면 색인 규칙부터 의심한 뒤 값을 내린다.
#: 등록 시점(2026-09-05) 실측 64.
MIN_INDEXED_MODULES = 50

#: 뷰모델 계층 검사 파일 수의 바닥 — 등록 시점 실측 7(app/__init__ · platform_adapter · viewmodels 5).
#: 이동으로 늘면 늘지 줄지 않는다. 줄면 검사 범위(`viewmodel_layer_files`)부터 의심한다.
MIN_VIEWMODEL_FILES = 5


def _is_skipped_dir(name: str) -> bool:
    """걷지 않는 디렉토리 — 숨김 · `__pycache__` · 빌드 산출물."""
    return (
        name.startswith(".")
        or name == "__pycache__"
        or name in ("build", "dist", "venv")
        or name.endswith(_BUILD_ARTIFACT_SUFFIXES)
    )


def _module_name(py_file: Path) -> str:
    """루트 기준 경로를 점 구분 모듈 이름으로(`a/b.py` → `a.b`, `a/__init__.py` → `a`)."""
    parts = list(py_file.relative_to(ROOT_DIR).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


@lru_cache(maxsize=1)
def project_modules() -> dict[str, Path]:
    """루트 아래 모든 `.py`를 모듈 이름 → 경로로 색인한다(tests·숨김·빌드 산출물 제외)."""
    index: dict[str, Path] = {}

    def walk(directory: Path) -> None:
        """하위를 재귀로 걷는다."""
        for entry in sorted(directory.iterdir()):
            if entry.is_dir():
                if entry.name not in _INDEX_EXCLUDED_DIRS and not _is_skipped_dir(entry.name):
                    walk(entry)
            elif entry.suffix == ".py":
                index[_module_name(entry)] = entry

    walk(ROOT_DIR)
    return index


def _absolute_base(py_file: Path, level: int, module: str | None) -> str | None:
    """상대 import(`from ..x import y`, level ≥ 1)의 기준을 절대 모듈 이름으로 푼다.

    `a/b/c.py`의 패키지는 `a.b`, `a/b/__init__.py`의 패키지는 `a.b`다. `level`만큼 위로 올라가
    `module`을 붙인다. 루트 위로 올라가면(잘못된 상대 import) None — 프로젝트 모듈이 아니다.
    상대 import를 건너뛰면 `from ..views import x`가 게이트를 조용히 지나간다(CodeRabbit #260).
    """
    package = _module_name(py_file).split(".")
    if py_file.name != "__init__.py":
        package = package[:-1]
    if level - 1 > len(package):
        return None
    base = package[: len(package) - (level - 1)]
    if module:
        base = base + module.split(".")
    return ".".join(base)


def _imported_names(py_file: Path) -> list[tuple[int, str]]:
    """파일 하나의 import 대상(줄 번호, 절대 점 구분 이름).

    `from a import b`는 `a.b`가 프로젝트 모듈(또는 Qt 위젯 모듈)이면 그것으로, 아니면 `a`로.
    상대 import는 파일의 패키지 경로를 기준으로 절대 이름으로 푼다.
    """
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    names: list[tuple[int, str]] = []
    index = project_modules()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = (
                node.module if node.level == 0 else _absolute_base(py_file, node.level, node.module)
            )
            if not base:
                continue
            for alias in node.names:
                candidate = f"{base}.{alias.name}"  # `from PySide6 import QtWidgets` · `from content import view` · `from . import x`
                if candidate in index or candidate in QT_VIEW_MODULES:
                    names.append((node.lineno, candidate))
                else:
                    names.append((node.lineno, base))
    return names


def _resolve_project_module(name: str) -> str | None:
    """import 이름을 프로젝트 모듈로 — 정확히 있거나, 그 상위 패키지 모듈이 있으면 그것."""
    index = project_modules()
    parts = name.split(".")
    while parts:
        candidate = ".".join(parts)
        if candidate in index:
            return candidate
        parts.pop()
    return None


def _is_under_view_dir(py_file: Path) -> bool:
    """`app/views/`·`app/widgets/` 아래인가(위치로 정한 뷰 계층)."""
    return any(py_file.resolve().is_relative_to(view_dir) for view_dir in VIEW_LAYER_DIRS)


@lru_cache(maxsize=None)
def qt_widget_chain(module: str) -> tuple[str, ...] | None:
    """모듈이 Qt 위젯 모듈에 닿는 경로 — 닿지 않으면 None. 순환은 방문 표시로 끊는다."""
    return _chain(module, frozenset())


def _chain(module: str, visiting: frozenset) -> tuple[str, ...] | None:
    """`qt_widget_chain`의 재귀 본체 — `visiting`은 현재 탐색 경로(순환 방지)."""
    index = project_modules()
    if module not in index or module in visiting:
        return None
    visiting = visiting | {module}
    for _, name in _imported_names(index[module]):
        if name in QT_VIEW_MODULES:
            return (module, name)
        target = _resolve_project_module(name)
        if target is None or target == module:
            continue
        tail = _chain(target, visiting)
        if tail is not None:
            return (module, *tail)
    return None


def is_view_module(module: str) -> bool:
    """뷰 계층인가 — 위치(`app/views`·`app/widgets`) 또는 내용(Qt 위젯에 닿음)."""
    path = project_modules().get(module)
    if path is not None and _is_under_view_dir(path):
        return True
    return qt_widget_chain(module) is not None


def viewmodel_layer_files() -> list[Path]:
    """검사 대상 — `app/` 아래에서 뷰 계층 디렉토리 밖의 모든 `.py`."""
    return sorted(
        p
        for p in APP_DIR.rglob("*.py")
        if not _is_under_view_dir(p) and "__pycache__" not in p.parts
    )


def _violations(py_file: Path) -> list[str]:
    """뷰모델 계층 파일 하나의 위반 — Qt 위젯 직접 import, 또는 뷰 계층 모듈 import(닿는 경로 표시)."""
    found = []
    where = py_file.relative_to(ROOT_DIR).as_posix()
    for lineno, name in _imported_names(py_file):
        if name in QT_VIEW_MODULES:
            found.append(f"{where}:{lineno}: {name}")
            continue
        target = _resolve_project_module(name)
        if target is None:
            continue
        if is_view_module(target):
            chain = qt_widget_chain(target)
            via = " -> ".join(chain) if chain else f"{target} (app/views·widgets 아래)"
            found.append(f"{where}:{lineno}: {name}  [{via}]")
    return found


def test_the_module_index_actually_sees_the_tree():
    """메타 게이트 — 프로젝트 모듈 색인이 비어 있지 않고, 뷰에 닿는 모듈이 실제로 존재한다.

    뷰 계층 모듈이 하나도 안 잡히면 "뷰를 import하지 않는다"가 공허하게 통과한다.
    """
    index = project_modules()
    assert len(index) >= MIN_INDEXED_MODULES, (
        f"모듈 색인이 {len(index)}개뿐이다 — 제외 규칙이 잘못 넓어졌다"
    )
    views = sorted(m for m in index if is_view_module(m))
    assert views, "Qt 위젯에 닿는 모듈이 하나도 없다 — 분류가 헛돌고 있다"
    assert all(_resolve_project_module(m) == m for m in index)


def test_viewmodel_layer_does_not_reach_qt_widgets_or_view_modules():
    """뷰모델 계층(`app/` 중 views·widgets 밖)의 모든 `.py`는 Qt 위젯도 뷰 계층 모듈도 import하지 않는다."""
    files = viewmodel_layer_files()
    assert len(files) >= MIN_VIEWMODEL_FILES, (
        f"뷰모델 계층 검사 대상이 {len(files)}개뿐이다(바닥 {MIN_VIEWMODEL_FILES}) — 스캔 대상 경로를 확인할 것"
    )

    violations = []
    for py_file in files:
        violations.extend(_violations(py_file))

    assert not violations, "뷰모델 계층은 뷰를 몰라야 한다. 위반 import 발견:\n" + "\n".join(
        violations
    )


def test_view_layer_directories_are_exempt_by_position_not_by_name():
    """`app/views/`·`app/widgets/`는 아직 없어도 규칙에 들어 있다 — 존재 여부와 무관하게 위치로 판정한다."""
    for view_dir in VIEW_LAYER_DIRS:
        assert view_dir.parent == APP_DIR
        assert _is_under_view_dir(view_dir / "anything.py")
    assert not _is_under_view_dir(APP_DIR / "viewmodels" / "anything.py")


def test_relative_imports_are_resolved_against_the_package_path():
    """상대 import도 절대 이름으로 풀려 규칙에 걸린다 — 건너뛰면 `from ..views import x`가 조용히 통과한다."""
    vm = APP_DIR / "viewmodels" / "some_viewmodel.py"
    assert _absolute_base(vm, 1, None) == "app.viewmodels"  # from . import x
    assert (
        _absolute_base(vm, 1, "item_state") == "app.viewmodels.item_state"
    )  # from .item_state import x
    assert _absolute_base(vm, 2, "views") == "app.views"  # from ..views import x
    assert _absolute_base(vm, 3, "content.view") == "content.view"  # from ...content.view import x
    assert (
        _absolute_base(APP_DIR / "viewmodels" / "__init__.py", 1, "item_state")
        == "app.viewmodels.item_state"
    )
    assert _absolute_base(vm, 4, "x") is None  # 루트 위 — 프로젝트 모듈이 아니다
