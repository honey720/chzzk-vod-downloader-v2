"""core/의 "PySide6 import 금지" 불변 규칙 검증 (#50 리뷰 반영).

core/ 내 모든 .py 파일을 AST로 파싱해 PySide6 계열 import가 없음을 확인한다.
grep 대신 AST를 쓰는 이유: docstring·주석에 규칙 설명으로 등장하는 "PySide6"
문자열은 허용하고, 실제 import 구문만 위반으로 잡기 위함이다.
이후 core에 Qt 의존이 스며들면 CI가 이 테스트로 잡는다.

순회는 tests/unit/core/import_scan.py(방향 규칙 게이트와 공유)를 쓰고, 금지 판정은 여기서만 한다 —
"core → app 방향"은 다른 사실이라 tests/unit/core/test_layer_direction.py가 따로 잰다(#193).
"""

from tests.unit.core.import_scan import core_source_files, describe, iter_imports

# core에서 import가 금지된 최상위 패키지 (Qt 바인딩 및 그 런타임)
FORBIDDEN_PACKAGES = {"PySide6", "shiboken6"}


def test_core_has_no_qt_import():
    """core/ 내 모든 .py 파일에 PySide6·shiboken6 import가 없어야 한다."""
    py_files = core_source_files()
    assert py_files, "core/에서 .py 파일을 찾지 못했다 — 스캔 대상 경로를 확인할 것"

    violations = [
        describe(site)
        for py_file in py_files
        for site in iter_imports(py_file)
        if site.top_level in FORBIDDEN_PACKAGES
    ]

    assert not violations, "core/는 UI 프레임워크를 몰라야 한다. 금지 import 발견:\n" + "\n".join(
        violations
    )
