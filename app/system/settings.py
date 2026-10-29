"""설정 저장 — 앱 데이터 폴더의 SQLite DB 하나, 그 안의 설정 테이블 (#475).

설정 키 · 기본값 · 검증은 키 표(`SCHEMA`)가 단일 출처다(#257) — 저장소만 config.json에서 DB로
바뀌었다. 테이블은 `settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)`이고 최상위 키 하나가
한 행, 값은 JSON 문자열이다. 안쪽 계층(`cookies`의 두 이름 · `window`의 다섯 값)은 펼치지 않는다 —
늘 한 덩어리로 읽고 쓴다. 읽을 때 JSON 해석 → 표 검증이고, 한 행이 깨지면 그 키만 기본값 + 경고다.
키를 더해도 테이블은 바뀌지 않는다. 기본값은 표에만 있다 — DB에는 사용자가 정한 키의 행만 두고,
행이 없으면 표의 기본값으로 읽는다. 저장은 바뀐 키의 행만 쓰고, 초기화는 행을 지운다.

DB 버전은 파일 머리의 `PRAGMA user_version`이다(새 DB는 0). 열 때마다 목표(`DATABASE_VERSION`)까지
단계를 차례로 돌린다 — 한 단계는 테이블 바꾸기 · 값 넣기 · `user_version` 쓰기를 한 트랜잭션으로
하므로, 중간에 꺼지면 그 단계 전 버전으로 남고 다음 실행에서 다시 돈다. 코드가 모르는 버전(옛 판이
새 판의 DB를 엶)이거나 DB 파일이 아닌 것이면 DB에 쓰지 않고 이번 실행 동안 메모리 안 기본값으로
돈다(경고 한 번). 2.13.0의 카드 · 이력도 이 DB에 단계를 더해 넣는다.

연결은 부를 때마다 열고 닫는다 — 쿠키는 다운로드 워커 스레드에서도 읽으므로(`load_cookies`)
연결을 스레드 사이에 나누지 않는다. 쓰기는 메인 스레드뿐이다(설정 창 · 메인 창).
이 모듈은 Qt를 모른다 — 창 기하의 `QRect` 변환은 호출부(app/views/mainWindow.py)가 한다.
"""

import contextlib
import copy
import json
import logging
import math
import os
import platform
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: 앱 데이터 폴더 이름 — OS의 사용자 데이터 위치 아래에 이 이름으로 둔다.
APP_NAME = "chzzk-vod-downloader-v2"


def _default_data_dir() -> str:
    """앱 데이터 폴더 — 설정 DB와 `logs/`를 두는 곳을 정하는 유일한 자리다.

    실행 형태(onefile · zip · `.app` · 소스 실행)와 상관없이 OS의 사용자 데이터 위치다.
    """
    if platform.system() == "Windows":
        return os.path.join(os.getenv("APPDATA"), APP_NAME)
    if platform.system() == "Darwin":
        return os.path.join(os.path.expanduser("~/Library/Application Support"), APP_NAME)
    return os.path.join(os.getenv("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), APP_NAME)


#: 앱 데이터 폴더 — 테스트는 이 값을 임시 폴더로 돌린다(tests/conftest.py). 경로를 쓰는 곳은
#: 아래 함수들을 거친다 — 부르는 시점에 이 값을 읽는다.
DATA_DIR = _default_data_dir()

#: DB 파일 이름 — 설정과 2.13.0의 카드 · 이력이 함께 드는 파일 하나.
DATABASE_NAME = "cvdv2.db"

#: 코드가 아는 DB 버전(`PRAGMA user_version`의 목표). 단계를 더하면 함께 올린다.
DATABASE_VERSION = 1

#: 다른 실행이 DB를 쓰는 중일 때 기다리는 최대 시간(초) — 설정 쓰기는 행 몇 개라 순간이다.
#: 넘기면 `sqlite3.OperationalError`가 호출부로 올라간다(config.json 저장 실패와 같은 자리).
BUSY_TIMEOUT = 5.0


def data_dir() -> str:
    """앱 데이터 폴더 경로."""
    return DATA_DIR


def database_path() -> str:
    """설정 DB 파일 경로 — 앱 데이터 폴더 안."""
    return os.path.join(DATA_DIR, DATABASE_NAME)


def logs_dir() -> str:
    """로그 폴더 경로 — 앱 데이터 폴더의 `logs/`. 만드는 것은 로그를 쓰는 쪽이다."""
    return os.path.join(DATA_DIR, "logs")


# ============ 설정 키 표 (#257) ============
#
# 설정 키의 **단일 출처**다. 기본값(`default_settings`) · 등재 · 검증(`normalize_settings`)이 전부
# 이 표에서 나온다 — 하드코딩된 기본값 dict를 표와 따로 두지 않는다. 두 출처가 되는 순간
# "등재를 빠뜨렸는데 조용하다"(#159·#253 전례 — 미등재 키를 정규화가 소리 없이 지웠다)가 다시
# 가능해진다. 새 키를 넣을 때 손댈 곳은 **표 한 줄 + 그 값을 쓰는 곳**뿐이다 — 테이블은 바뀌지 않는다.
# 게이트: tests/app/logic/test_config_schema.py — 표에 임시 줄을 붙이고 다른 코드를 안 건드린 채
# 등재·기본값·검증·보존이 전부 되는지 잰다.


class InvalidSetting(ValueError):
    """표의 검증기가 값을 거부할 때 던진다 — 정규화가 잡아 기본값으로 대체하고 경고를 남긴다."""


@dataclass(frozen=True)
class Setting:
    """설정 키 표의 한 줄 — 키 이름 · 기본값 · 검증기.

    `normalize`는 저장된 값을 받아 **정규화된 값**을 돌려주거나 `InvalidSetting`을 던진다
    (화이트리스트 — 원하는 형태가 아니면 거부한다. 예외 목록을 늘리는 블랙리스트는 다음 종류의
    깨진 값에 또 샌다, #180 전례). 기본값은 `fallback()`으로 꺼낸다 — 중첩 dict(cookies·window)라
    매번 깊은 복사본이어야 호출부의 수정이 표를 오염시키지 않는다(#255).
    """

    key: str
    default: object
    normalize: Callable[[object], object]

    def fallback(self) -> object:
        """기본값의 깊은 복사본."""
        return copy.deepcopy(self.default)


#: 32비트 부호 있는 정수 범위 — 창 기하 호출부(Qt `QRect`)가 C++ int라 기록값을 이 범위로
#: 제한한다. 범위 밖 정수는 QRect를 만들다 OverflowError가 난다(#254 CodeRabbit).
_GEOMETRY_INT_MIN, _GEOMETRY_INT_MAX = -(2**31), 2**31 - 1


def _integer(value: object) -> int:
    """bool이 아닌 정수 또는 정수값 float(JSON을 거치며 `2.0`이 된 정수)를 int로. 그 외 거부."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidSetting(type(value).__name__)
    if isinstance(value, float) and not (math.isfinite(value) and value.is_integer()):
        raise InvalidSetting("non-integral float")
    return int(value)


def _string(value: object) -> str:
    """문자열이면 그대로. 내용은 보지 않는다."""
    if not isinstance(value, str):
        raise InvalidSetting(type(value).__name__)
    return value


def _one_of(choices: tuple[str, ...]) -> Callable[[object], str]:
    """열거값 검증기 — `choices` 중 하나인 문자열만 통과한다."""

    def normalize(value: object) -> str:
        """`choices`에 없는 값은 거부한다."""
        if not isinstance(value, str):
            raise InvalidSetting(type(value).__name__)
        if value not in choices:
            raise InvalidSetting("not one of " + "/".join(choices))
        return value

    return normalize


#: 쿠키 이름 — 설정 창(app/views/dialog.py)이 이 두 값을 쓰고 `load_cookies`가 이 둘을 조립한다.
COOKIE_NAMES = ("NID_AUT", "NID_SES")


def _cookies(value: object) -> dict:
    """`cookies` — dict여야 한다. 안의 값은 **문자열이 아닐 때만** 빈 문자열로 대체한다.

    ⚠️ 문자열의 **내용은 검사하지 않는다**(오너 결정, #257). 쿠키 형식은 이 앱이 정하는 것이
    아니고, 잘못 대체하면 유저가 다시 로그인해야 한다. 두 이름 밖의 키는 건드리지 않고,
    비-dict일 때만 통째로 기본값이 된다. 경고에 값을 찍지 않는다 — 인증 값이 로그에 남으면
    안 된다.
    """
    if not isinstance(value, dict):
        raise InvalidSetting(type(value).__name__)
    cleaned = dict(value)
    for name in COOKIE_NAMES:
        if not isinstance(cleaned.get(name, ""), str):
            logger.warning(
                "Setting 'cookies.%s' is not a string (%s) — cleared; log in again",
                name,
                type(cleaned[name]).__name__,
            )
            cleaned[name] = ""
    return cleaned


def parse_saved_window(saved: object) -> tuple[tuple[int, int, int, int], bool] | None:
    """설정의 `window` 기록을 **화이트리스트**로 검증해 ((x, y, width, height), 최대화)로 돌려준다 (#253).

    저장된 값은 유저가 손으로 고칠 수 있으므로 잘못된 값은 예외가 아니라 정상 시나리오다 —
    원하는 형태가 아니면 `None`(= 기록 없음, 첫 실행)이다. 통과 조건:
    dict · x/y/width/height가 bool 아닌 정수(또는 정수값 float) · 유한(NaN·무한 아님) ·
    32비트 int 범위 안 · width/height 양수 · maximized는 없거나 bool.
    순수 파이썬 값만 돌려준다 — `QRect` 변환은 호출부(app/views/mainWindow.py)가 한다(#257).
    """
    if not isinstance(saved, dict):
        return None
    values: list[int] = []
    for key in ("x", "y", "width", "height"):
        try:
            number = _integer(saved.get(key))
        except InvalidSetting:
            return None
        if not _GEOMETRY_INT_MIN <= number <= _GEOMETRY_INT_MAX:
            return None
        values.append(number)
    x, y, width, height = values
    if width <= 0 or height <= 0:
        return None
    maximized = saved.get("maximized", False)
    if not isinstance(maximized, bool):
        return None
    return (x, y, width, height), maximized


def _window(value: object) -> dict:
    """`window` — 빈 dict(기록 없음) 또는 `parse_saved_window`를 넘는 기록만. 값은 그대로 둔다."""
    if value == {}:
        return {}
    if parse_saved_window(value) is None:
        raise InvalidSetting(type(value).__name__)
    return dict(value)


#: 설정 키 표 — 새 키는 여기 한 줄로 끝난다. DB 버전은 키가 아니라 `PRAGMA user_version`이다.
SCHEMA: tuple[Setting, ...] = (
    Setting("cookies", {name: "" for name in COOKIE_NAMES}, _cookies),
    # 다운로드 완료 후 동작 — 설정 창(app/views/dialog.py)의 세 항목
    Setting("afterDownload", "none", _one_of(("none", "sleep", "shutdown"))),
    # UI 언어 — 설정 창의 두 항목이자 translations/*.qm의 이름
    Setting("language", "en_US", _one_of(("en_US", "ko_KR"))),
    # 유저가 마지막으로 쓴 저장 경로 (#159). 빈 값 = 미설정 → 시스템 다운로드 폴더 폴백.
    # 실존 여부는 사용처(_default_download_path)가 본다 — 외장 드라이브 분리 대비.
    Setting("downloadPath", "", _string),
    # 마지막 창 크기·위치·최대화 상태 (#253). 빈 값 = 기록 없음(첫 실행) → 초기 크기 규칙
    # (app/views/mainWindow.py). 채워지면 {"x","y","width","height","maximized"}.
    Setting("window", {}, _window),
)


def _setting(key: str) -> Setting:
    """표에서 키의 줄을 찾는다 — 부르는 시점의 표를 본다(테스트가 표에 줄을 붙인다)."""
    return next(setting for setting in SCHEMA if setting.key == key)


def default_settings() -> dict:
    """표에서 도출한 기본 설정 — 호출마다 새 깊은 복사본이다(#255)."""
    return {setting.key: setting.fallback() for setting in SCHEMA}


def normalize_settings(raw: dict) -> dict:
    """설정 dict를 표로 정규화한다 — 알려진 키만, 누락은 기본값, 깨진 값은 기본값 + 경고.

    항상 새 dict를 돌려준다. 표에 없는 키는 버린다. 경고에 값 자체는 찍지 않는다(cookies가
    같은 경로를 탄다 — 인증 값이 로그에 남으면 안 된다).
    """
    normalized = {}
    for setting in SCHEMA:
        if setting.key not in raw:
            normalized[setting.key] = setting.fallback()
            continue
        try:
            normalized[setting.key] = setting.normalize(raw[setting.key])
        except InvalidSetting as reason:
            logger.warning(
                "Setting %r has an invalid value (%s) — falling back to the default",
                setting.key,
                reason,
            )
            normalized[setting.key] = setting.fallback()
    return normalized


# ============ DB ============

#: 메모리 모드의 설정 — None이면 DB를 쓴다. 코드가 모르는 버전 · DB가 아닌 파일을 만나면 기본값으로
#: 채워지고, 그 뒤 이번 실행의 읽기 · 쓰기는 전부 여기서 한다(DB는 열지 않는다).
_memory: dict | None = None


def _enter_memory_mode(reason: str) -> None:
    """이번 실행 동안 DB에 쓰지 않고 메모리 안 기본값으로 돈다 — 경고는 이때 한 번."""
    global _memory
    _memory = default_settings()
    logger.warning(
        "Settings database %s %s — settings are kept in memory only for this run",
        database_path(),
        reason,
    )


@contextlib.contextmanager
def _transaction(conn: sqlite3.Connection):
    """쓰기 잠금을 먼저 잡는 트랜잭션(`BEGIN IMMEDIATE`) — 예외면 되돌리고 그대로 올린다."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _user_version(conn: sqlite3.Connection) -> int:
    """DB 파일 머리의 `PRAGMA user_version`."""
    return conn.execute("PRAGMA user_version").fetchone()[0]


#: config.json에서 가져오는 키 — 쿠키 · 저장 위치 · 언어. 창 크기 · 다운로드 뒤 동작 · version은 버린다.
IMPORTED_KEYS = ("cookies", "downloadPath", "language")


def import_config_json(conn: sqlite3.Connection) -> list[str]:
    """옛 `config.json`의 세 키를 설정 테이블에 넣고, 커밋한 뒤 지울 파일 경로를 돌려준다 (#475).

    ⚠️ **2.14.0에서 이 함수와 그 테스트(tests/app/logic/test_settings_store.py의
    `TestImportConfigJson`)를 지운다.** 2.13.x까지만 둔다 — 그 뒤에 2.12.0보다 앞 판에서 바로
    올리면 설정이 기본값으로 시작한다. 다른 코드는 config.json을 모른다.

    0 → 1 단계의 트랜잭션 안에서 불린다 — 지우는 것은 커밋한 뒤 부르는 쪽이 한다(커밋 전에
    지우면 그 단계가 실패했을 때 쿠키가 사라진다). 가져오는 키는 `IMPORTED_KEYS` 셋이고 각 값은
    표로 검증한다 — 검증에 걸린 키는 넣지 않는다(기본값 + 경고). 못 읽는 파일(깨진 JSON ·
    인코딩 · 읽기 실패 · 최상위가 dict가 아님)이면 아무것도 넣지 않고 지울 목록에만 올린다.
    """
    path = os.path.join(DATA_DIR, "config.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as file:
            raw = json.load(file)
    except (OSError, ValueError) as error:
        logger.warning("Old config.json is unreadable (%s) — starting from defaults", error)
        return [path]
    if not isinstance(raw, dict):
        logger.warning("Old config.json is not an object — starting from defaults")
        return [path]
    imported = []
    for key in IMPORTED_KEYS:
        if key not in raw:
            continue
        try:
            value = _setting(key).normalize(raw[key])
        except InvalidSetting as reason:
            logger.warning("Old config.json key %r is invalid (%s) — not imported", key, reason)
            continue
        _write_rows(conn, {key: value})
        imported.append(key)
    logger.info("Imported settings from config.json: %s", ", ".join(imported) or "none")
    return [path]


def _create_settings_table(conn: sqlite3.Connection) -> list[str]:
    """0 → 1: 설정 테이블을 만들고 config.json을 가져온다. 커밋 뒤 지울 파일을 돌려준다."""
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    return import_config_json(conn)


#: DB 버전 올리기 단계 — 시작 버전 → 그 단계. 단계는 커밋한 뒤 지울 파일 목록을 돌려준다.
STEPS: dict[int, Callable[[sqlite3.Connection], list[str]]] = {
    0: _create_settings_table,
}


def _run_step(conn: sqlite3.Connection, version: int) -> int:
    """한 단계를 한 트랜잭션으로 돌리고 바뀐 버전을 돌려준다.

    `BEGIN IMMEDIATE`로 쓰기 잠금을 먼저 잡고 버전을 다시 읽는다 — 두 실행이 함께 떠서
    둘 다 0을 봤으면 뒤의 것은 앞의 것이 커밋할 때까지 기다린 뒤 이미 올라간 버전을 보고
    아무것도 하지 않는다. 예외가 나면 되돌리고 그대로 올린다 — 버전은 그 단계 전 그대로다.
    """
    with _transaction(conn):
        current = _user_version(conn)
        if current != version:
            return current
        remove_after_commit = STEPS[version](conn)
        conn.execute(f"PRAGMA user_version = {version + 1}")
    for path in remove_after_commit:
        try:
            os.remove(path)
        except OSError as error:
            logger.warning("Could not remove %s after importing it: %s", path, error)
    return version + 1


def _open() -> sqlite3.Connection | None:
    """DB를 열고 목표 버전까지 올린 연결을 돌려준다. 메모리 모드면 None.

    코드가 모르는 버전이거나 DB 파일이 아닌 것(깨진 파일 포함)이면 그 파일은 그대로 두고
    메모리 모드로 바꾼다. 잠금 대기 초과(`OperationalError`)는 메모리 모드로 바꾸지 않고 올린다 —
    순간의 경합으로 그 실행 내내 설정이 저장되지 않게 되면 안 된다.
    """
    if _memory is not None:
        return None
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(database_path(), timeout=BUSY_TIMEOUT, isolation_level=None)
    try:
        version = _user_version(conn)
        while version < DATABASE_VERSION:
            version = _run_step(conn, version)
    except sqlite3.OperationalError:
        conn.close()
        raise
    except sqlite3.DatabaseError as error:
        conn.close()
        _enter_memory_mode(f"cannot be read ({error})")
        return None
    except BaseException:
        conn.close()
        raise
    if version > DATABASE_VERSION:
        conn.close()
        _enter_memory_mode(f"has version {version}, newer than this app ({DATABASE_VERSION})")
        return None
    return conn


def _write_rows(conn: sqlite3.Connection, values: dict) -> None:
    """키마다 한 행(값은 JSON 문자열)을 넣거나 바꾼다 — 트랜잭션은 부르는 쪽이 연다."""
    conn.executemany(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        [(key, json.dumps(value, ensure_ascii=False)) for key, value in values.items()],
    )


def _stored(conn: sqlite3.Connection) -> dict[str, str]:
    """설정 테이블의 행 전부 — 키 → 저장된 JSON 문자열 그대로."""
    return dict(conn.execute("SELECT key, value FROM settings").fetchall())


def load_settings() -> dict:
    """설정을 읽어 **표로 검증한 새 dict**를 돌려준다.

    반환값은 늘 새 객체다 — 호출부가 반환 dict를 고쳐 저장하는 관례(`cfg["window"] = …`)라
    원본을 주면 기본값 표가 오염된다(#255). 행이 없는 키는 표의 기본값이고(DB에는 사용자가 정한
    키의 행만 있다), 깨진 행(JSON이 아님 · 검증에 걸림)은 그 키만 기본값 + 경고다. 읽기만으로는
    DB에 아무것도 쓰지 않는다(버전 올리기는 예외 — 열 때 돈다).
    """
    conn = _open()
    if conn is None:
        return copy.deepcopy(_memory)
    try:
        rows = _stored(conn)
    finally:
        conn.close()
    raw = {}
    for key, text in rows.items():
        try:
            raw[key] = json.loads(text)
        except ValueError:
            logger.warning("Setting %r is not valid JSON — falling back to the default", key)
    return normalize_settings(raw)


def save_settings(values: dict) -> None:
    """설정 dict 가운데 **저장된 값과 다른 키의 행만** 한 트랜잭션으로 쓴다.

    호출부는 `load_settings()` 결과를 고쳐 통째로 넘긴다 — 바뀌지 않은 키는 쓰지 않으므로 행이
    없던 키(기본값)는 계속 행이 없다. 견주는 대상은 잠금을 잡은 뒤 다시 읽은 행이다 — 행이 없으면
    표의 기본값, JSON이 아니면 다른 값으로 본다. ⚠️ 이 dict를 읽은 뒤 다른 실행이 바꾼 키는 DB와
    다르므로 이 dict의 옛 값으로 다시 쓰인다(두 판을 함께 띄웠을 때 — config.json 때와 같다).

    표에 없는 키가 있으면 `KeyError` — 미등재 키가 조용히 버려지는 일(#159 전례)을 막는다.
    메모리 모드면 메모리에만 쓴다. 잠금 대기 초과 등 DB 오류는 그대로 올라간다.
    """
    unknown = set(values) - {setting.key for setting in SCHEMA}
    if unknown:
        raise KeyError(f"not in the settings table: {sorted(unknown)}")
    conn = _open()
    if conn is None:
        _memory.update(copy.deepcopy(values))
        return
    try:
        with _transaction(conn):
            stored = _stored(conn)
            changed = {key: value for key, value in values.items() if _differs(key, value, stored)}
            _write_rows(conn, changed)
    finally:
        conn.close()


def _differs(key: str, value: object, stored: dict[str, str]) -> bool:
    """`value`가 저장된 값(행이 없으면 표의 기본값)과 다른가 — JSON이 아닌 행은 다르다고 본다."""
    if key not in stored:
        return value != _setting(key).default
    try:
        return value != json.loads(stored[key])
    except ValueError:
        return True


def open_settings() -> dict:
    """앱 시작 때 한 번 — DB를 열어 버전을 올리고, 깨진 행을 고친 뒤 설정을 돌려준다.

    깨진 행(JSON이 아님 · 검증에 걸림)은 지운다 — 그 키는 기본값으로 읽힌다(경고는 이때 한 번).
    검증이 값을 고친 행(쿠키 값 하나가 문자열이 아님 → 빈 문자열)은 고친 값으로 다시 쓴다. 나머지
    행은 쓰지 않고, 표에 없는 행도 지우지 않는다 — 키를 더해도 DB 버전이 오르지 않으므로 같은
    버전의 새 판이 더한 키일 수 있다.
    """
    conn = _open()
    if conn is not None:
        try:
            with _transaction(conn):
                _repair_rows(conn)
        finally:
            conn.close()
    return load_settings()


def _repair_rows(conn: sqlite3.Connection) -> None:
    """표에 있는 키의 행을 검증해 깨진 행은 지우고 검증이 고친 행은 다시 쓴다."""
    known = {setting.key for setting in SCHEMA}
    broken, fixed = [], {}
    for key, text in _stored(conn).items():
        if key not in known:
            continue
        try:
            value = json.loads(text)
        except ValueError:
            logger.warning("Setting %r is not valid JSON — removed, the default applies", key)
            broken.append(key)
            continue
        try:
            normalized = _setting(key).normalize(value)
        except InvalidSetting as reason:
            logger.warning(
                "Setting %r has an invalid value (%s) — removed, the default applies", key, reason
            )
            broken.append(key)
            continue
        if normalized != value:
            fixed[key] = normalized
    conn.executemany("DELETE FROM settings WHERE key = ?", [(key,) for key in broken])
    _write_rows(conn, fixed)


def reset_settings(key: str | None = None) -> None:
    """설정을 기본값으로 되돌린다 — 그 키의 행(키가 없으면 전부)을 지운다. 읽으면 기본값이 나온다.

    설정 파일을 지워 초기화하던 길이 없어져 둔 것이다. 메뉴 항목은 화면 개편 때 붙인다.
    표에 없는 키는 `KeyError`.
    """
    global _memory
    if key is not None:
        _setting_or_key_error(key)
    conn = _open()
    if conn is None:
        if key is None:
            _memory = default_settings()
        else:
            _memory[key] = _setting(key).fallback()
        return
    try:
        with _transaction(conn):
            if key is None:
                conn.execute("DELETE FROM settings")
            else:
                conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    finally:
        conn.close()


def _setting_or_key_error(key: str) -> Setting:
    """표에서 키의 줄 — 없으면 `KeyError`."""
    for setting in SCHEMA:
        if setting.key == key:
            return setting
    raise KeyError(f"not in the settings table: {key!r}")


def load_cookies() -> dict:
    """등록된 치지직 인증 쿠키를 {NID_AUT, NID_SES} 형태로 읽는다 (#170).

    조립의 단일 지점이다. 미설정 키는 빈 문자열이다. 다운로드 워커 스레드에서도 불린다.
    """
    data = load_settings()["cookies"]
    return {name: data.get(name, "") for name in COOKIE_NAMES}
