"""pywebview 셸 진입점 (#209, Phase A1) — 최소 골격.

기존 main.py(PySide6)는 무변경으로 병행 유지한다. 5탭 UI·엔진 연결은
Phase C 이후 항목이며, 이 파일은 "pywebview + Nuitka 빌드 파이프라인이
실제로 동작하는가"만 증명하는 셸이다.

리소스 경로 해석은 main.py의 resource_path와 동일한 패턴을 쓴다 —
Nuitka onefile은 리소스를 임시 해제 경로에 풀고 __file__도 그 안을
가리키므로, CWD가 아니라 이 파일의 위치를 기준으로 해석해야 한다 (#43).
"""
import os

import webview

from config.log_setup import setup_logging

_PLACEHOLDER_HTML = """
<html>
<head>
<style>
  body { margin: 0; font-family: sans-serif; background: #1e1e1e; color: #eee;
         display: flex; align-items: center; justify-content: center; height: 100vh; }
</style>
</head>
<body>
  <h1>CVDv2 — pywebview shell skeleton (#209)</h1>
</body>
</html>
"""


def resource_path(relative_path: str) -> str:
    """소스 실행과 Nuitka onefile/standalone 빌드 양쪽에서 동작하는 리소스 절대 경로 (#43)."""
    base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


def main():
    setup_logging()

    webview.create_window(
        "CVDv2",
        html=_PLACEHOLDER_HTML,
        width=1000,
        height=700,
    )
    webview.start()


if __name__ == "__main__":
    main()
