"""앱 계층의 **위치 정의** — 계층 게이트와 색 스캔이 같은 정의를 읽는다 (#259 A4).

뷰 계층은 실제로 셋이다: `app/views/`(화면) · `app/widgets/`(부품) · `app/theme`(색·토큰 정의 —
Qt를 아는 파일이므로 뷰 계층이다). 여기에 없는 `app/` 아래는 전부 뷰모델 계층이고 Qt 위젯을
보면 안 된다(`app/platform_adapter.py` 포함 — "Qt 없는 부품 창고").

경로를 **접두사**로 잰다. `app/theme.py`(파일)든 `app/theme/…`(패키지로 승격)든 import 경로
`app.theme`가 같으므로 같은 정의로 잡힌다 — 승격해도 게이트를 다시 고치지 않는다. 파일명을
박지 않는 이유다. 색 스캔의 theme 제외도 이 정의에서 읽는다 — 두 곳에 따로 적으면 파일이
옮겨질 때 한쪽만 따라가고 다른 쪽은 아무것도 안 재는 규칙으로 남는다(루트 `theme.py`용
`SCAN_EXCLUDED_FILES`가 그랬다).
"""

from pathlib import Path

import app

APP_DIR = Path(app.__file__).resolve().parent
ROOT_DIR = APP_DIR.parent

#: 색·토큰의 유일한 정의처 — 파일(`app/theme.py`)이든 패키지(`app/theme/`)든 이 접두사.
THEME = APP_DIR / "theme"

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
