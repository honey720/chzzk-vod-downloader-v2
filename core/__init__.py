"""core 패키지 — UI와 독립적인 백엔드 로직 (docs/SPEC.md §3.2).

불변 규칙: core 전체에 PySide6 import 금지.
core는 UI 프레임워크를 몰라야 하며, 언제든 CLI·웹·다른 UI로 교체 가능해야 한다.
app ↔ core 통신은 콜백, 이벤트, 반환값으로만 한다.
"""
