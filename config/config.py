import functools
import json
import logging
import os
import platform
import tomllib
from collections import OrderedDict

logger = logging.getLogger(__name__)

# pyproject.toml [project].version의 미러 상수 (#116). **정본은 pyproject.toml이다.**
# Nuitka 번들에는 pyproject.toml도 배포 메타데이터(dist-info)도 포함되지 않아
# 빌드 실행 파일은 이 상수를 쓴다. 정본과의 불일치는 단위 테스트
# (tests/unit/test_download_log_phases.py)가 잡는다 — 버전을 올릴 때는
# pyproject.toml과 이 상수를 함께 갱신할 것.
APP_VERSION = "2.9.3"


@functools.lru_cache(maxsize=1)
def get_app_version() -> str:
    """앱 버전 문자열을 반환한다 (#110 — 다운로드 로그 시작 정보용).

    정본은 릴리즈 검증(CI)과 동일하게 pyproject.toml의 [project].version이다.
    소스 실행은 pyproject를 직접 읽어 정본 문자열 그대로 돌려주고, 파일이
    없는 빌드 실행(Nuitka onefile)은 미러 상수(APP_VERSION)를 쓴다 (#116).

    구 구현이 우선하던 importlib.metadata는 쓰지 않는다 — Nuitka 번들에
    dist-info가 없어 빌드에서 실패했고(#116의 원인 절반), 소스에서도 버전을
    정규화(2.9.0-rc1 → 2.9.0rc1)해 정본 문자열과 어긋났다.
    """
    pyproject = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pyproject.toml"
    )
    try:
        with open(pyproject, "rb") as f:
            return tomllib.load(f)["project"]["version"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return APP_VERSION

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
    "downloadPath": ""
}

# 설정 로드 함수
def load_config(): # TODO: config.json에서 추출한 값들을 ENUM으로 변환하여 반환

    os.makedirs(CONFIG_DIR, exist_ok=True)  # 디렉토리 생성
    os.makedirs(os.path.join(CONFIG_DIR, "logs"), exist_ok=True)  # logs 디렉토리 생성

    if not os.path.exists(CONFIG_FILE):
        save_config(DEFAULT_CONFIG)  # 파일 생성
        
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        logger.error(f"Error reading config file: {e}")
        return DEFAULT_CONFIG  # 기본값을 반환하거나 예외 처리
        
    return config

# 설정 저장 함수
def save_config(config):
    with open(CONFIG_FILE, "w") as file:
        json.dump(config, file, indent=4)


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