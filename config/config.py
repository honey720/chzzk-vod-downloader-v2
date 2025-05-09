import json
import os
import platform

# 설정 파일 경로 (AppData 디렉토리에 저장)
APP_NAME = "chzzk-vod-downloader-v2"

if platform.system() == "Windows":
    CONFIG_DIR = os.path.join(os.getenv("APPDATA"), APP_NAME)  # C:\Users\<User>\AppData\Roaming\chzzk-vod-downloader-v2

elif platform.system() == "Linux":
    CONFIG_DIR = config_dir = os.path.join(os.getenv("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), APP_NAME)

CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

# 초기 설정
DEFAULT_CONFIG = {
    "cookies": {
        "NID_AUT": "",
        "NID_SES": ""
    },
    "afterDownload": "none",
    "language": "en_US"
}

# 설정 로드 함수
def load_config():

    os.makedirs(CONFIG_DIR, exist_ok=True)  # 디렉토리 생성
    os.makedirs(os.path.join(CONFIG_DIR, "logs"), exist_ok=True)  # logs 디렉토리 생성

    if not os.path.exists(CONFIG_FILE):
        save_config(DEFAULT_CONFIG)  # 파일 생성
        
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Error reading config file: {e}")
        return DEFAULT_CONFIG  # 기본값을 반환하거나 예외 처리
        
    return config

# 설정 저장 함수
def save_config(config):
    with open(CONFIG_FILE, "w") as file:
        json.dump(config, file, indent=4)

def merge_config(default, current):
    """
    default: 기본 설정 딕셔너리 (DEFAULT_CONFIG)
    current: 기존 설정 딕셔너리 (config.json에서 로드한 값)
    누락된 key가 있으면 기본값을 추가하고, 변경되었다면 True를 반환.
    """
    updated = False
    for key, default_value in default.items():
        if key not in current:
            current[key] = default_value
            updated = True
        else:
            # 만약 값이 딕셔너리이면, 재귀적으로 병합합니다.
            if isinstance(default_value, dict) and isinstance(current[key], dict):
                if merge_config(default_value, current[key]):
                    updated = True
    return updated