"""저장소 계층 정의 — 위치(app/ 안의 뷰 계층)와 소유(저장소가 소유한 최상위 이름). 게이트들이 같은 정의를 읽는다.

**위치**(#259 A4): 뷰 계층은 실제로 셋이다: `app/views/`(화면) · `app/widgets/`(부품) · `app/theme`(색·토큰
정의 — Qt를 아는 파일이므로 뷰 계층이다). 여기에 없는 `app/` 아래는 전부 뷰모델 계층이고 Qt 위젯을
보면 안 된다(`app/platform_adapter.py` 포함 — "Qt 없는 부품 창고").

경로를 **접두사**로 잰다. `app/theme.py`(파일)든 `app/theme/…`(패키지로 승격)든 import 경로
`app.theme`가 같으므로 같은 정의로 잡힌다 — 승격해도 게이트를 다시 고치지 않는다. 파일명을
박지 않는 이유다. 색 스캔의 theme 제외도 이 정의에서 읽는다 — 두 곳에 따로 적으면 파일이
옮겨질 때 한쪽만 따라가고 다른 쪽은 아무것도 안 재는 규칙으로 남는다(루트 `theme.py`용
`SCAN_EXCLUDED_FILES`가 그랬다).

**소유**(#193): "저장소가 소유한 최상위 이름"은 목록으로 적지 않고 저장소 루트에서 **유도**한다 —
`.py`를 가진 최상위 디렉토리(`tests/` 포함)와 루트의 `.py` 모듈. `core/`는 이 중 `core` 이외의 것을
import하지 않는다(방향 규칙, tests/unit/core/test_layer_direction.py). 목록이면 새 디렉토리가 생겨도
안 막고, 디렉토리가 사라지면(B 단계의 content·download) 목록만 조용히 빈다. 유도의 독립 대조는
git 추적 파일(같은 테스트의 메타 게이트).
"""

from pathlib import Path

import app

APP_DIR = Path(app.__file__).resolve().parent
ROOT_DIR = APP_DIR.parent

#: 색·토큰의 유일한 정의처 — 파일(`app/theme.py`)이든 패키지(`app/theme/`)든 이 접두사.
THEME = APP_DIR / "theme"

#: core 계층 — UI도 앱 계층도 모르는 엔진(SPEC §4). 방향 규칙의 피검사자.
CORE_DIR = ROOT_DIR / "core"

#: 걷지 않는 디렉토리 — 숨김(.venv·.git) · 바이트코드 · Nuitka/빌드 산출물(.gitignore와 같은 이름).
_BUILD_ARTIFACT_SUFFIXES = (".build", ".dist", ".onefile-build")

#: 뷰 계층 접두사(SPEC §3.3 + theme). 존재하지 않아도 규칙은 선다.
VIEW_LAYER_PREFIXES = (APP_DIR / "views", APP_DIR / "widgets", THEME)


def _under(path: Path, prefix: Path) -> bool:
    """`path`가 `prefix` 자체(확장자 무관)이거나 그 아래인가."""
    path = path.resolve()
    return path.with_suffix("") == prefix or path.is_relative_to(prefix)


def is_theme(path: Path) -> bool:
    """색·토큰 정의처인가 — 색 스캔이 제외하는 유일한 소스."""
    return _under(path, THEME)


def is_view_layer(path: Path) -> bool:
    """뷰 계층(views · widgets · theme)인가 — 계층 게이트가 Qt 위젯 사용을 허용하는 위치."""
    return any(_under(path, prefix) for prefix in VIEW_LAYER_PREFIXES)


def is_skipped_dir(name: str) -> bool:
    """소스 트리의 일부가 아니라 걷지 않는 디렉토리 — 숨김 · `__pycache__` · 빌드 산출물."""
    return (
        name.startswith(".")
        or name == "__pycache__"
        or name in ("build", "dist", "venv")
        or name.endswith(_BUILD_ARTIFACT_SUFFIXES)
    )


def _contains_py(directory: Path) -> bool:
    """디렉토리(걷지 않는 하위 제외) 어딘가에 `.py`가 하나라도 있는가."""
    for entry in directory.iterdir():
        if entry.is_dir():
            if not is_skipped_dir(entry.name) and _contains_py(entry):
                return True
        elif entry.suffix == ".py":
            return True
    return False


def owned_top_level_names() -> frozenset[str]:
    """저장소가 소유한 최상위 import 이름 — 루트에서 유도한다(목록 아님, `tests` 포함).

    `.py`를 가진 최상위 디렉토리는 패키지(또는 네임스페이스 패키지) 이름이고, 루트의 `.py`는
    모듈 이름이다. `resources/`·`translations/`처럼 `.py`가 없는 디렉토리는 import할 수 없으므로
    소유 이름이 아니다. 모듈 색인(test_app_layer_imports)을 재활용하지 않는다 — 그 색인은
    `tests/`를 빼며, 방향 규칙에서는 core가 `tests`도 import하면 안 된다.
    """
    names: set[str] = set()
    for entry in sorted(ROOT_DIR.iterdir()):
        if entry.is_dir():
            if not is_skipped_dir(entry.name) and _contains_py(entry):
                names.add(entry.name)
        elif entry.suffix == ".py":
            names.add(entry.stem)
    return frozenset(names)
