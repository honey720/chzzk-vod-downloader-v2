import sys
import os
from PySide6.QtWidgets import QApplication, QMainWindow
from PySide6.QtGui import QIcon
from PySide6.QtCore import QTranslator, QLocale

from downloader.main_window import VodDownloader
import config.config as config

class main(QMainWindow):
    def __init__(self):
        super().__init__()

def resource_path(relative_path):
    """Get absolute path to resource, works for dev and for PyInstaller"""
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    
    return os.path.join(base_path, relative_path)

if __name__ == '__main__':
    app = QApplication(sys.argv)

    # 설정 파일 로드
    app_config = config.load_config()
    
    # 번역 시스템 초기화
    translator = QTranslator()
    
    # 1. 설정 파일에서 언어 가져오기
    language = app_config.get('language')
    is_language_in_config = language is not None
    print("config_language :", language)
    print("is_language_in_config :", is_language_in_config)
    
    if not is_language_in_config:
        # 설정 파일에 언어가 없는 경우 시스템 언어 사용
        language = QLocale.system().name()
        print("local_language :", language)
    
    # 2. 번역 파일 로드 시도
    translation_file = resource_path(f"translations/{language}.qm")
    print(f"translation_file path: {translation_file}")
    print("translation_file :", os.path.exists(translation_file))

    if os.path.exists(translation_file) and translator.load(translation_file):
        print("translation file load success")
        # 번역 파일 로드 성공   
        app.installTranslator(translator)
        
        # 설정 파일에 언어가 없었던 경우에만 저장
        if not is_language_in_config:
            app_config['language'] = language
            config.save_config(app_config)
    else:
        print("translation file load failed")
        # 번역 파일 로드 실패 -> 기본 언어(en_US) 사용
        language = "en_US"
        if translator.load(f"translations/{language}.qm"):
            app.installTranslator(translator)
            # 기본 언어로 설정 저장
            app_config['language'] = language
            config.save_config(app_config)

    icon_path = os.path.join(os.path.dirname(__file__), 'resources', 'chzzk.ico')
    app.setWindowIcon(QIcon(icon_path))
    # 메인 UI 실행
    ex = VodDownloader()
    ex.setWindowIcon(QIcon(icon_path))

    sys.exit(app.exec())
