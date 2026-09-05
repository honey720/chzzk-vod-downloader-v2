import copy
import functools
import json
import logging
import math
import os
import platform
import subprocess
import tempfile
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# pyproject.toml [project].version의 미러 상수 (#116). **정본은 pyproject.toml이다.**
# Nuitka 번들에는 pyproject.toml도 배포 메타데이터(dist-info)도 포함되지 않아
# 빌드 실행 파일은 이 상수를 쓴다. 정본과의 불일치는 단위 테스트
# (tests/unit/test_download_log_phases.py)가 잡는다 — 버전을 올릴 때는
# pyproject.toml과 이 상수를 함께 갱신할 것.
APP_VERSION = "2.9.6"

# 빌드 시점의 커밋 스냅샷 (#195, 층 2 — 최선 노력). 정본이 없다 — 소스에
# 박아 둔 값을 scripts/inject_build_info.py가 Nuitka 빌드 직전에 실제
# 값으로 고쳐 쓴다. 모든 Nuitka 빌드(로컬·CI 무관)가 이 상수를 채운다 —
# 릴리즈 마커(IS_RELEASE_BUILD)와 분리된 것이 핵심이다.
BUILD_COMMIT = "unknown"

# 정식 릴리즈 빌드 여부 (#195, 층 1 — 필수·100% 판별). release.yml만
# scripts/inject_build_info.py --release로 이 값을 True로 주입한다.
# 로컬 Nuitka 빌드·소스 실행은 이 상수를 절대 건드리지 않으므로 기본값
# False가 이미 "주입이 없으면 비정식으로 본다"는 원칙 그 자체다.
IS_RELEASE_BUILD = False

# git archive(GitHub "Download ZIP" 등)가 export-subst로 커밋 해시를
# 심어 주는 파일 — .git이 없는 소스 zip에서도 층 2를 살리는 보조 수단.
_EXPORT_SUBST_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "core",
    "utils",
    "_git_archive_commit.txt",
)


def _git_describe(repo_root: str) -> str | None:
    """소스 실행에서 커밋 정보를 최선 노력으로 얻는다. 실패하면 None."""
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = result.stdout.strip()
    return commit or None


def _exported_commit() -> str | None:
    """export-subst가 실제로 치환한 경우에만 값을 돌려준다 (git 없는 소스 zip용).

    일반 clone에서는 플레이스홀더(``$Format:%H$``)가 치환되지 않은 채
    그대로 남는다 — 그 경우는 git이 있을 가능성이 높아 _git_describe가
    먼저 시도되고, 이 함수는 git이 없을 때만 의미를 갖는다.
    """
    try:
        with open(_EXPORT_SUBST_FILE, encoding="utf-8") as f:
            content = f.read().strip()
    except OSError:
        return None
    if not content or content.startswith("$Format:"):
        return None
    return content[:9]


def _source_commit(repo_root: str) -> str:
    """소스 실행의 커밋 정보 — git describe 우선, 안 되면 export-subst, 둘 다 없으면 unknown."""
    return _git_describe(repo_root) or _exported_commit() or "unknown"


def _strip_redundant_tag_prefix(base: str, commit: str) -> str:
    """git describe 결과에서 base 버전과 중복되는 태그 부분을 제거한다 (#195 후속).

    describe 기본 형태는 "가장 가까운 태그(v 접두 가능)-거리-g해시"라, 그
    태그가 base 버전과 같으면 표시에서 버전이 두 번 찍힌다
    (``2.9.5+dev.v2.9.5-6-g55d5f5b``). 접두사가 겹치면 떼고 나머지(거리
    -g해시, 더티면 -dirty까지)만 남긴다.

    태그를 못 찾아 ``--always``가 대신 내놓은 맨해시(``55d5f5b``)나 git
    자체가 없어 ``"unknown"``인 경우는 애초에 겹치는 접두사가 없으므로
    그대로 통과한다 — 이 경우 해시/unknown만 있으면 어느 버전대인지 알 수
    없으므로, 호출부(_dev_version)가 base를 앞에 붙여 기준선을 보존한다.
    """
    for prefix in (f"v{base}", base):
        if commit == prefix:
            return ""
        if commit.startswith(prefix + "-"):
            return commit[len(prefix) + 1 :]
    return commit


def _dev_version(base: str, commit: str) -> str:
    """base 버전에 커밋 상세를 붙여 표시 문자열을 만든다 (#195).

    정확히 태그 커밋이라 거리 0으로 상세가 빈 문자열이 되면(describe가
    태그명만 반환) ``+dev.``로 끝나는 빈 세그먼트를 남기지 않고 ``+dev``만
    붙인다.
    """
    detail = _strip_redundant_tag_prefix(base, commit)
    return f"{base}+dev.{detail}" if detail else f"{base}+dev"


@functools.lru_cache(maxsize=1)
def get_app_version() -> str:
    """앱 버전 문자열을 반환한다 (#110 — 다운로드 로그 시작 정보용).

    정본은 릴리즈 검증(CI)과 동일하게 pyproject.toml의 [project].version이다.
    소스 실행은 pyproject를 직접 읽어 정본 문자열 그대로 돌려주고, 파일이
    없는 빌드 실행(Nuitka onefile)은 미러 상수(APP_VERSION)를 쓴다 (#116).

    구 구현이 우선하던 importlib.metadata는 쓰지 않는다 — Nuitka 번들에
    dist-info가 없어 빌드에서 실패했고(#116의 원인 절반), 소스에서도 버전을
    정규화(2.9.0-rc1 → 2.9.0rc1)해 정본 문자열과 어긋났다.

    **개발 빌드와 정식 릴리즈 구분 (#195)**: 정식 릴리즈(IS_RELEASE_BUILD가
    release.yml에 의해 True로 주입된 빌드)만 깨끗한 버전 문자열을 돌려준다.
    그 외(소스 실행·로컬 빌드)는 전부 ``+dev.<커밋>`` 접미사가 붙는다 —
    소스 실행은 pyproject.toml이 발견된다는 사실 자체가 이미 "빌드 산출물이
    아니다"를 뜻하므로 무조건 dev 취급한다. 마커 주입이 없으면 곧 비정식으로
    보는 것이 원칙이라, 이 판정은 git 유무와 무관하게 항상 성립한다.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pyproject = os.path.join(repo_root, "pyproject.toml")
    try:
        with open(pyproject, "rb") as f:
            base = tomllib.load(f)["project"]["version"]
        return _dev_version(base, _source_commit(repo_root))
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        if IS_RELEASE_BUILD:
            return APP_VERSION
        return _dev_version(APP_VERSION, BUILD_COMMIT)

# 설정 파일 경로 (AppData 디렉토리에 저장)
APP_NAME = "chzzk-vod-downloader-v2"

if platform.system() == "Windows":
    CONFIG_DIR = os.path.join(os.getenv("APPDATA"), APP_NAME)  # C:\Users\<User>\AppData\Roaming\chzzk-vod-downloader-v2

elif platform.system() == "Darwin":
    CONFIG_DIR = os.path.join(os.path.expanduser("~/Library/Application Support"), APP_NAME)

elif platform.system() == "Linux":
    CONFIG_DIR = config_dir = os.path.join(os.getenv("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), APP_NAME)

CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
CONFIG_VERSION = 2


# ============ 설정 스키마 표 (#257) ============
#
# 설정 키의 **단일 출처**다. 기본값 표(`default_config`) · 정렬/등재 · 검증(`normalize_config`)이
# 전부 이 표에서 나온다 — 하드코딩된 기본값 dict를 표와 따로 두지 않는다. 두 출처가 되는
# 순간 "등재를 빠뜨렸는데 조용하다"(#159·#253 전례 — 미등재 키를 정규화가 소리 없이
# 지웠다)가 다시 가능해진다. 새 키를 넣을 때 손댈 곳은 **표 한 줄 + 그 값을 쓰는 곳**뿐이다.
# 게이트: tests/unit/test_config_schema.py — 표에 임시 줄을 붙이고 다른 코드를 안 건드린 채
# 등재·기본값·검증·보존이 전부 되는지 잰다.
#
# ⚠️ 저장 형식·위치·키 이름은 이 표가 바꾸지 않는다. 마이그레이션은 MIGRATIONS의 몫이고,
# 검증·정규화는 **마이그레이션 뒤**에 돈다(구 스키마 키가 새 검증에 걸려 버려지면 안 된다).
# 유일한 예외가 `version` — 마이그레이션 판정(`<`) 자체가 크래시하지 않도록 앞에서 정수로
# 강제한다(`update_config`).


class InvalidSetting(ValueError):
    """표의 검증기가 값을 거부할 때 던진다 — 정규화가 잡아 기본값으로 대체하고 경고를 남긴다."""


@dataclass(frozen=True)
class Setting:
    """설정 스키마 표의 한 줄 — 키 이름 · 기본값 · 검증기.

    `normalize`는 파일에서 읽은 값을 받아 **정규화된 값**을 돌려주거나 `InvalidSetting`을
    던진다(화이트리스트 — 원하는 형태가 아니면 거부한다. 예외 목록을 늘리는 블랙리스트는
    다음 종류의 깨진 값에 또 샌다, #180 전례). 기본값은 `fallback()`으로 꺼낸다 — 중첩
    dict(cookies·window)라 매번 깊은 복사본이어야 호출부의 수정이 표를 오염시키지 않는다(#255).
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


def _version(value: object) -> int:
    """`version` — 1 이상의 정수. 마이그레이션 단계(MIGRATIONS)가 정수 키라 정수여야 판정이 선다."""
    number = _integer(value)
    if number < 1:
        raise InvalidSetting("version below 1")
    return number


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


#: 쿠키 이름 — 설정 창(config/dialog.py)이 이 두 값을 쓰고 `load_cookies`가 이 둘을 조립한다.
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
                "Config key 'cookies.%s' is not a string (%s) — cleared; log in again",
                name,
                type(cleaned[name]).__name__,
            )
            cleaned[name] = ""
    return cleaned


def parse_saved_window(saved: object) -> tuple[tuple[int, int, int, int], bool] | None:
    """config의 `window` 기록을 **화이트리스트**로 검증해 ((x, y, width, height), 최대화)로 돌려준다 (#253).

    config.json은 유저가 손으로 고칠 수 있는 파일이라 잘못된 값은 예외가 아니라 정상
    시나리오다 — 원하는 형태가 아니면 `None`(= 기록 없음, 첫 실행)이다. 통과 조건:
    dict · x/y/width/height가 bool 아닌 정수(또는 정수값 float) · 유한(NaN·무한 아님) ·
    32비트 int 범위 안 · width/height 양수 · maximized는 없거나 bool.
    순수 파이썬 값만 돌려준다 — `config/`는 Qt를 보지 않는다. `QRect` 변환은 호출부
    (application/mainWindow.py)가 한다(#257).
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


#: 설정 스키마 표 — 순서가 곧 config.json의 키 순서다. 새 키는 여기 한 줄로 끝난다.
SCHEMA: tuple[Setting, ...] = (
    Setting("version", CONFIG_VERSION, _version),
    Setting("cookies", {name: "" for name in COOKIE_NAMES}, _cookies),
    # 다운로드 완료 후 동작 — 설정 창(config/dialog.py)의 세 항목
    Setting("afterDownload", "none", _one_of(("none", "sleep", "shutdown"))),
    # UI 언어 — 설정 창의 두 항목이자 translations/*.qm의 이름
    Setting("language", "en_US", _one_of(("en_US", "ko_KR"))),
    # 유저가 마지막으로 쓴 저장 경로 (#159). 빈 값 = 미설정 → 시스템 다운로드 폴더 폴백.
    # 실존 여부는 사용처(_default_download_path)가 본다 — 외장 드라이브 분리 대비.
    Setting("downloadPath", "", _string),
    # 마지막 창 크기·위치·최대화 상태 (#253). 빈 값 = 기록 없음(첫 실행) → 초기 크기 규칙
    # (application/mainWindow.py). 채워지면 {"x","y","width","height","maximized"}.
    Setting("window", {}, _window),
)


def default_config() -> dict:
    """표에서 도출한 기본 설정 — 호출마다 새 깊은 복사본이다(#255)."""
    return {setting.key: setting.fallback() for setting in SCHEMA}


def normalize_config(raw: object) -> dict:
    """설정 dict를 표로 정규화한다 — 알려진 키만 표 순서로, 누락은 기본값, 깨진 값은 기본값 + 경고.

    항상 새 dict를 돌려준다. 표에 없는 키는 버린다(구 `reorder_config`의 등재 규칙 그대로).
    ⚠️ 마이그레이션 **뒤**에 불러야 한다 — 구 스키마 키가 여기서 버려진다(`update_config`).
    경고에 값 자체는 찍지 않는다(cookies가 같은 경로를 탄다 — 인증 값이 로그에 남으면 안 된다).
    """
    if not isinstance(raw, dict):
        logger.warning("Config is not an object (%s) — starting from defaults", type(raw).__name__)
        raw = {}
    normalized = {}
    for setting in SCHEMA:
        if setting.key not in raw:
            normalized[setting.key] = setting.fallback()
            continue
        value = raw[setting.key]
        try:
            normalized[setting.key] = setting.normalize(value)
        except InvalidSetting as reason:
            logger.warning(
                "Config key %r has an invalid value (%s) — falling back to the default",
                setting.key,
                reason,
            )
            normalized[setting.key] = setting.fallback()
    return normalized


#: 깨진 config.json을 비켜 둘 이름 — 같은 디렉토리, 하나만 유지(#255).
BROKEN_SUFFIX = ".broken"

#: 디렉토리 fsync 가능 여부 — POSIX만. Windows는 디렉토리를 `os.open`할 수 없다(§8.4 분기).
#: 모듈 상수로 둔 이유: 테스트가 어느 러너에서든 두 갈래를 다 태우기 위해서다.
_DIRECTORY_FSYNC_SUPPORTED = os.name != "nt"


def _fsync_directory(path: str) -> None:
    """디렉토리 엔트리 변경(`os.replace`)을 디스크에 밀어 넣는다 — POSIX 전용, 실패는 로그만 (#255).

    파일 `fsync`는 파일 내용만 지속시킨다. rename으로 바뀐 디렉토리 엔트리는 디렉토리
    자체를 `fsync`해야 정전 뒤에도 남는다(POSIX). Windows는 디렉토리를 열 수 없으므로
    아무것도 하지 않는다. ⚠️ 여기서 실패해도 **저장은 이미 성공한 뒤**다 — 네트워크
    드라이브 등 일부 파일시스템은 디렉토리 fsync를 거부하므로 `OSError`는 로그만 남기고
    넘어간다. 저장의 성패를 좌우하지 않는다(`Exception` 전체로 넓히지 않는다).
    """
    if not _DIRECTORY_FSYNC_SUPPORTED:
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as e:
        logger.warning("Directory fsync skipped — cannot open %s: %s", path, e)
        return
    try:
        os.fsync(fd)
    except OSError as e:
        logger.warning("Directory fsync failed for %s: %s — the file itself is already in place", path, e)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def broken_config_path() -> str:
    """깨진 `config.json`을 옮겨 둘 경로 — `CONFIG_FILE` 옆의 `config.json.broken`."""
    return CONFIG_FILE + BROKEN_SUFFIX


def _read_config_file() -> object:
    """`config.json`을 **정규화 없이** 읽는다. 없으면 기본값으로 만들고, 깨져 있으면 비켜 둔다 (#255).

    파싱에 실패하면 **깨진 파일을 지우지도 덮어쓰지도 않는다** — 저장된 인증 정보(쿠키)를
    유저가 복구할 유일한 원본이다. 같은 디렉토리의 `config.json.broken`으로 옮겨 두고
    경로를 로그에 남긴 뒤 기본값을 돌려준다. 이전 `.broken`이 있으면 덮어쓴다(하나만
    유지 — 쌓이면 디스크에 남고, 두 번째 손상 시점의 파일은 첫 번째보다 최신이라
    복구 가치가 더 크다). 깨진 JSON에서 값을 건져내는 복구는 하지 않는다.

    반환값은 파일에 있는 그대로다(dict가 아닐 수도 있다) — 마이그레이션이 구 스키마 키를
    봐야 하므로 여기서는 정규화하지 않는다. 호출부는 `load_config`(정규화)와
    `update_config`(마이그레이션 → 정규화) 둘뿐이다.
    """
    os.makedirs(CONFIG_DIR, exist_ok=True)  # 디렉토리 생성
    os.makedirs(os.path.join(CONFIG_DIR, "logs"), exist_ok=True)  # logs 디렉토리 생성

    if not os.path.exists(CONFIG_FILE):
        save_config(default_config())  # 파일 생성

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        broken = broken_config_path()
        os.replace(CONFIG_FILE, broken)
        _fsync_directory(os.path.dirname(CONFIG_FILE) or ".")
        logger.error("Config file is corrupt (%s) — moved to %s and starting with defaults", e, broken)
        return default_config()


def load_config() -> dict:  # TODO: config.json에서 추출한 값들을 ENUM으로 변환하여 반환
    """`config.json`을 읽어 **표로 검증한 복사본**을 dict로 돌려준다 (#255·#257).

    파일이 없으면 기본값으로 만들고, 깨져 있으면 `.broken`으로 비켜 둔다(`_read_config_file`).
    반환값은 항상 **새 객체**다 — 기본값이 필요할 때도 표의 깊은 복사본을 준다. 호출부가
    반환 dict를 고쳐 저장하는 관례(`cfg["window"] = …`)라 원본을 주면 모듈의 기본값 표가
    프로세스 수명 내내 오염되고, `cookies`가 중첩 dict라 얕은 복사로는 그 안이 공유된다.
    누락 키는 기본값으로 채워져 있고 깨진 값은 기본값으로 대체돼 있다 — 호출부는 형태를
    다시 검사하지 않아도 된다. 마이그레이션은 하지 않는다(시작 시 `update_config` 한 번).
    """
    return normalize_config(_read_config_file())


#: `os.replace` 재시도 — 순간 잠금(바이러스 검사 등)을 넘기는 정도. 횟수 × 간격 = 최대 0.5초.
_REPLACE_ATTEMPTS, _REPLACE_INTERVAL = 10, 0.05


def _replace_with_retry(src: str, dst: str) -> None:
    """`os.replace(src, dst)` — `PermissionError`만 짧게 재시도한다(Windows 잠금 대비, #255)."""
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_INTERVAL)


def save_config(config) -> None:
    """`config.json`을 **원자적으로** 저장한다 (#255).

    같은 디렉토리의 임시 파일에 전부 쓴 뒤 `os.replace()`로 갈아끼운다 — 쓰기 도중
    종료·정전이 나도 대상 파일은 옛 내용 전체 아니면 새 내용 전체다. 임시 파일은
    반드시 같은 디렉토리에 만든다(다른 볼륨이면 `os.replace`가 복사+삭제가 되어 원자성이
    성립하지 않는다). 인코딩은 명시한다(§8.4 — OS 기본 로케일에 의존하지 않는다).
    Windows에서는 대상 파일을 다른 핸들이 열어 둔 동안 `os.replace`가 `PermissionError`를
    낸다(실측 — 읽기 핸들도 막는다). 바이러스 검사처럼 순간적인 잠금은 짧게 재시도하고,
    그래도 실패하면 임시 파일을 치우고 예외를 그대로 올린다 — 대상 파일은 옛 내용 그대로다.
    갈아끼운 뒤 디렉토리도 `fsync`한다(POSIX — rename의 엔트리 변경 지속). 그 실패는
    로그만 남긴다(`_fsync_directory`).
    """
    directory = os.path.dirname(CONFIG_FILE) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(config, file, indent=4)
            file.flush()
            os.fsync(file.fileno())
        _replace_with_retry(tmp_path, CONFIG_FILE)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    _fsync_directory(directory)


def load_cookies() -> dict:
    """등록된 치지직 인증 쿠키를 {NID_AUT, NID_SES} 형태로 읽는다 (#170).

    조립의 단일 지점 — 구 mainWindow.onFetch와 download/resolvers.py에
    같은 조립이 중복돼 있던 것을 수렴했다. 미설정 키는 빈 문자열이다.
    """
    data = load_config().get("cookies", {})
    return {
        "NID_AUT": data.get("NID_AUT", ""),
        "NID_SES": data.get("NID_SES", ""),
    }


def update_config() -> dict:
    """config.json을 최신 상태로 유지한다 — 시작 시 한 번: 마이그레이션 → 표 검증·정규화 → 저장.

    순서가 불변식이다(#257):
    ① `version`만 마이그레이션 **앞**에서 정수로 강제한다 — 문자열·1.5 같은 값은 `<` 판정이나
       MIGRATIONS 조회에서 크래시했다. 표의 `version` 줄(같은 검증기)을 쓴다. 키가 없거나
       검증에 걸리면 1로 본다(버전 키가 생기기 전 파일과 같은 취급 — 마이그레이션이 전부
       돌아 구 키를 살린다). 최종 버전은 마이그레이션이 CONFIG_VERSION으로 올린다.
    ② 마이그레이션(MIGRATIONS)을 현재 버전까지 돌린다.
    ③ 그 **뒤**에 표로 검증·정규화한다 — 구 스키마 키(`afterDownloadComplete` 등)는 ②가
       옮긴 뒤에야 버려야 한다. 앞에서 정규화하면 옮기기 전에 지워진다.
    """
    raw = _read_config_file()
    if not isinstance(raw, dict):
        logger.warning("Config is not an object (%s) — starting from defaults", type(raw).__name__)
        raw = {}

    version_setting = next(setting for setting in SCHEMA if setting.key == "version")
    if "version" not in raw:
        current_version = 1
    else:
        try:
            current_version = version_setting.normalize(raw["version"])
        except InvalidSetting as reason:
            # 값을 믿을 수 없으면 가장 오래된 버전으로 본다 — 마이그레이션을 전부 돌려 구 스키마
            # 키가 정규화에서 버려지기 전에 이관되게 한다. 이미 v2인 파일에 v1→v2가 다시 돌아도
            # 구 키가 없어 값은 바뀌지 않는다(CodeRabbit #258). 최신 버전(표 기본값)으로 보면
            # `{"version": "1", "afterDownloadComplete": …}`의 설정이 이관 없이 사라진다.
            current_version = 1
            logger.warning(
                "Config key 'version' has an invalid value (%s) — assuming %s", reason, current_version
            )
    raw["version"] = current_version

    if current_version < CONFIG_VERSION:
        logger.info(f"Migrating config from version {current_version} to {CONFIG_VERSION}...")
        while current_version < CONFIG_VERSION:
            migrate_func = MIGRATIONS.get(current_version)
            if not migrate_func:
                raise Exception(f"No migration function for version {current_version}")
            raw = migrate_func(raw)
            current_version = raw["version"]

        logger.info("Configuration file has been updated to the latest version.")
    else:
        logger.info("Configuration file is up to date.")
    config = normalize_config(raw)  # 마이그레이션 뒤 — 알려진 키만 표 순서로, 누락은 기본값
    save_config(config)
    return config

def migrate_v1_to_v2(config):
    # 예: 기존 "afterDownloadComplete"를 "afterDownload"로 이관
    if "afterDownloadComplete" in config:
        config["afterDownload"] = config.pop("afterDownloadComplete")
    if "threads" in config:
        del config["threads"]

    config["version"] = 2
    return config

# 마이그레이션 맵
MIGRATIONS = {
    1: migrate_v1_to_v2,
}