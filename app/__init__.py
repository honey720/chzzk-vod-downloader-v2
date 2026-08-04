"""app 패키지 — UI와 core 사이의 앱 계층 (Phase 5, #167).

viewmodel·앱 전용 상태가 여기에 놓인다. core의 불변 규칙(PySide6 금지)은
app에 적용되지 않지만, app은 ui/ 위젯을 직접 알지 않는다.
"""
