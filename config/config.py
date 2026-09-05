import copy
import functools
import json
import logging
import os
import platform
import subprocess
import tempfile
import time
import tomllib
from collections import OrderedDict

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

# 초기 설정
DEFAULT_CONFIG = {
    "version": CONFIG_VERSION,
    "cookies": {
        "NID_AUT": "",
        "NID_SES": ""
    },
    "afterDownload": "none",
    "language": "en_US",
    # 유저가 마지막으로 쓴 저장 경로 (#159). 빈 값 = 미설정 → 시스템 다운로드
    # 폴더 폴백. reorder_config가 여기 없는 키를 삭제하므로 등재가 보존의 전제다
    "downloadPath": "",
    # 마지막 창 크기·위치·최대화 상태 (#253). 빈 값 = 기록 없음(첫 실행) → 초기 크기
    # 규칙(application/mainWindow.py)으로 뜬다. 여기 등재돼야 reorder_config가 안 지운다.
    # 채워지면 {"x","y","width","height","maximized"} — 복원 시 현재 화면으로 클램프한다.
    "window": {}
}

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


def load_config() -> dict:  # TODO: config.json에서 추출한 값들을 ENUM으로 변환하여 반환
    """`config.json`을 읽어 dict로 돌려준다. 없으면 기본값으로 만들고, 깨져 있으면 비켜 둔다 (#255).

    파싱에 실패하면 **깨진 파일을 지우지도 덮어쓰지도 않는다** — 저장된 인증 정보(쿠키)를
    유저가 복구할 유일한 원본이다. 같은 디렉토리의 `config.json.broken`으로 옮겨 두고
    경로를 로그에 남긴 뒤 기본값을 돌려준다. 이전 `.broken`이 있으면 덮어쓴다(하나만
    유지 — 쌓이면 디스크에 남고, 두 번째 손상 시점의 파일은 첫 번째보다 최신이라
    복구 가치가 더 크다). 깨진 JSON에서 값을 건져내는 복구는 하지 않는다.

    반환값은 항상 **새 객체**다 — 기본값이 필요할 때도 `DEFAULT_CONFIG`의 깊은 복사본을
    준다. 호출부가 반환 dict를 고쳐 저장하는 관례(`cfg["window"] = …`)라 원본을 주면
    모듈의 기본값 표가 프로세스 수명 내내 오염되고, `cookies`가 중첩 dict라 얕은 복사로는
    그 안이 공유된다.
    """
    os.makedirs(CONFIG_DIR, exist_ok=True)  # 디렉토리 생성
    os.makedirs(os.path.join(CONFIG_DIR, "logs"), exist_ok=True)  # logs 디렉토리 생성

    if not os.path.exists(CONFIG_FILE):
        save_config(DEFAULT_CONFIG)  # 파일 생성

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        broken = broken_config_path()
        os.replace(CONFIG_FILE, broken)
        _fsync_directory(os.path.dirname(CONFIG_FILE) or ".")
        logger.error("Config file is corrupt (%s) — moved to %s and starting with defaults", e, broken)
        return copy.deepcopy(DEFAULT_CONFIG)

    return config


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


def update_config():
    """
    config.json을 최신 상태로 유지하며,
    버전별 마이그레이션과 값 검증을 수행한다.
    """
    config = load_config()
    current_version = config.get("version", 1)

    if current_version < CONFIG_VERSION:
        logger.info(f"Migrating config from version {current_version} to {CONFIG_VERSION}...")
        while current_version < CONFIG_VERSION:
            migrate_func = MIGRATIONS.get(current_version)
            if not migrate_func:
                raise Exception(f"No migration function for version {current_version}")
            config = migrate_func(config)
            current_version = config["version"]

        logger.info("Configuration file has been updated to the latest version.")
    else:
        logger.info("Configuration file is up to date.")
    config = reorder_config(config) # 순서 변경이 필요한 경우에만 정렬
    save_config(config)
    return config

def reorder_config(config):
    ordered = OrderedDict()
    # 기본 설정에 있는 키들만 ordered에 추가
    for key in DEFAULT_CONFIG:
        if key in config:
            ordered[key] = config[key]
    
    # 기본 설정에 없는 키를 삭제
    config = {key: value for key, value in config.items() if key in DEFAULT_CONFIG}
    
    # 수정된 config 반환
    return ordered

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