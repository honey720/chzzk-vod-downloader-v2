import json
import os

# 설정 파일 경로 (AppData 디렉토리에 저장)
APP_NAME = "chzzk-vod-downloader-v2"
CONFIG_DIR = os.path.join(os.getenv("APPDATA"), APP_NAME)  # C:\Users\<User>\AppData\Roaming\chzzk-vod-downloader
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

# 초기 설정
DEFAULT_CONFIG = {
    "cookies": {
        "NID_AUT": "",
        "NID_SES": ""
    }
}

# 모듈 전역 변수
NID_AUT = ""
NID_SES = ""

# 설정 로드 함수
def load_config():
    global NID_AUT, NID_SES
    
    if not os.path.exists(CONFIG_DIR):
        os.makedirs(CONFIG_DIR)  # 디렉토리 생성

    if not os.path.exists(CONFIG_FILE):
        save_config(DEFAULT_CONFIG)  # 파일 생성

    with open(CONFIG_FILE, "r") as file:
        data = json.load(file)

    # cookies 섹션에서 값 로드
    cookies = data.get("cookies", {})
    NID_AUT = cookies.get("NID_AUT", "")
    NID_SES = cookies.get("NID_SES", "")

    return data

# 설정 저장 함수
def save_config(config):
    if not os.path.exists(CONFIG_DIR):
        os.makedirs(CONFIG_DIR)  # 디렉토리 생성

    with open(CONFIG_FILE, "w") as file:
        json.dump(config, file, indent=4)

# 쿠키 가져오기 함수
def get_cookies():
    config = load_config()
    return config.get('cookies', {})

# 쿠키 설정 함수
def set_cookies(nidaut, nidses):
    global NID_AUT, NID_SES
    NID_AUT = nidaut
    NID_SES = nidses

    config = load_config()
    config['cookies'] = {"NID_AUT": nidaut, "NID_SES": nidses}
    save_config(config)