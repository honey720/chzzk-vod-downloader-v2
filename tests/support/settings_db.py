"""설정 DB를 제품 함수 없이 직접 읽고 쓴다 (#475) — 씨앗과 판정이 제품과 출처를 나누게.

테이블 모양(`settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)`, 값은 JSON 문자열)과
`PRAGMA user_version`을 여기서 따로 적는다. 제품이 같은 모양을 쓰지 않으면 이 도구로 만든
씨앗을 제품이 못 읽어 테스트가 실패한다. 경로만 제품의 `database_path()`를 쓴다 — 격리가
돌린 임시 폴더를 가리키는지는 tests/meta/test_config_isolation.py가 잰다.
"""

import json
import os
import sqlite3

import app.system.settings as settings_module

#: 이 도구가 만드는 DB의 버전 — 설정 테이블이 생긴 버전.
SETTINGS_TABLE_VERSION = 1


def _connect() -> sqlite3.Connection:
    """격리된 DB 파일에 연결한다(폴더가 없으면 만든다)."""
    os.makedirs(os.path.dirname(settings_module.database_path()), exist_ok=True)
    return sqlite3.connect(settings_module.database_path())


def write_rows(rows: dict[str, str], user_version: int = SETTINGS_TABLE_VERSION) -> None:
    """설정 테이블에 행(키 → 값 문자열 그대로)을 쓰고 `user_version`을 정한다 — 깨진 JSON도 쓸 수 있다."""
    with _connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.executemany("INSERT OR REPLACE INTO settings VALUES (?, ?)", list(rows.items()))
        conn.execute(f"PRAGMA user_version = {user_version}")
    conn.close()


def write_settings(values: dict, user_version: int = SETTINGS_TABLE_VERSION) -> None:
    """설정 테이블에 키마다 JSON 값을 쓴다."""
    write_rows({key: json.dumps(value) for key, value in values.items()}, user_version)


def read_rows() -> dict[str, str]:
    """설정 테이블의 행을 키 → 저장된 문자열 그대로 읽는다 — 행이 다시 쓰였는지 견줄 때."""
    conn = _connect()
    try:
        return dict(conn.execute("SELECT key, value FROM settings").fetchall())
    finally:
        conn.close()


def read_settings() -> dict:
    """설정 테이블의 행을 키 → JSON 해석한 값으로 읽는다."""
    conn = _connect()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    finally:
        conn.close()
    return {key: json.loads(value) for key, value in rows}


def user_version() -> int:
    """DB 파일의 `PRAGMA user_version` — 파일이 없으면 0(새 DB와 같다)."""
    if not os.path.exists(settings_module.database_path()):
        return 0
    conn = _connect()
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def has_settings_table() -> bool:
    """설정 테이블이 있는가 — 파일이 없으면 False."""
    if not os.path.exists(settings_module.database_path()):
        return False
    conn = _connect()
    try:
        found = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'settings'"
        ).fetchone()
    finally:
        conn.close()
    return found is not None


def write_config_json(value: object | str) -> str:
    """옛 config.json을 앱 데이터 폴더에 쓴다 — 문자열이면 그대로, 아니면 JSON으로. 경로를 돌려준다."""
    path = os.path.join(settings_module.data_dir(), "config.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = value if isinstance(value, str) else json.dumps(value)
    with open(path, "w", encoding="utf-8") as file:
        file.write(text)
    return path
