import sys
import os
import logging
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QIcon
from PySide6.QtCore import QTranslator, QLocale

from application.mainWindow import VodDownloader
import config.config as config
from config.log_setup import setup_logging

logger = logging.getLogger(__name__)

def resource_path(relative_path: str) -> str:
    """소스 실행과 Nuitka onefile 빌드 양쪽에서 동작하는 리소스 절대 경로를 반환한다.

    Nuitka onefile은 리소스를 임시 해제 경로에 풀고 __file__도 그 안을 가리키므로,
    CWD가 아니라 이 파일의 위치를 기준으로 해석해야 한다 (#43).
    """
    base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)

def set_language(app_config, translator):
    
    # 1. 설정 파일에서 언어 가져오기
    language = app_config.get('language')
    is_language_in_config = language is not None
    logger.debug("config_language : %s", language)
    logger.debug("is_language_in_config : %s", is_language_in_config)

    if not is_language_in_config:
        # 설정 파일에 언어가 없는 경우 시스템 언어 사용
        language = QLocale.system().name()
        logger.debug("local_language : %s", language)

    # 2. 번역 파일 로드 시도
    translation_file = resource_path(f"translations/{language}.qm")
    logger.debug(f"translation_file path: {translation_file}")
    logger.debug("translation_file : %s", os.path.exists(translation_file))

    if os.path.exists(translation_file) and translator.load(translation_file):
        logger.info("translation file load success")
        # 번역 파일 로드 성공   
        app.installTranslator(translator)
        
        # 설정 파일에 언어가 없었던 경우에만 저장
        if not is_language_in_config:
            app_config['language'] = language
            config.save_config(app_config)
    else:
        logger.warning("translation file load failed")
        # 번역 파일 로드 실패 -> 기본 언어(en_US) 사용
        language = "en_US"
        if translator.load(resource_path(f"translations/{language}.qm")):
            app.installTranslator(translator)
            # 기본 언어로 설정 저장
            app_config['language'] = language
            config.save_config(app_config)


if __name__ == '__main__':
    # 공통 로깅 설정 (콘솔 + logs/ 회전 파일)
    setup_logging()

    app = QApplication(sys.argv)

    # 설정 파일 로드
    app_config = config.update_config()
    
    # 번역 시스템 초기화
    translator = QTranslator()
    
    set_language(app_config, translator)

    # 아이콘도 번역과 같은 기준(__file__)으로 해석한다 (#43)
    icon_path = resource_path('resources/chzzk.ico')
    app.setWindowIcon(QIcon(icon_path))
    # 메인 UI 실행
    ex = VodDownloader()
    sys.exit(app.exec())
