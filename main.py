import sys
import os
import logging
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QIcon
from PySide6.QtCore import QTranslator, QLocale

from application.mainWindow import VodDownloader
import config.config as config
import theme
from config.log_setup import setup_logging

logger = logging.getLogger(__name__)

def resource_path(relative_path: str) -> str:
    """소스 실행과 Nuitka onefile 빌드 양쪽에서 동작하는 리소스 절대 경로를 반환한다.

    Nuitka onefile은 리소스를 임시 해제 경로에 풀고 __file__도 그 안을 가리키므로,
    CWD가 아니라 이 파일의 위치를 기준으로 해석해야 한다 (#43).
    """
    base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)

def apply_theme(app):
    """OS 라이트/다크 설정을 감지해 앱 전역 팔레트·스타일시트를 적용한다 (#227).

    실패해도 앱은 뜬다 — 스타일이 없으면 Qt 기본 외형으로 보일 뿐이고,
    번들에서 리소스가 빠진 채로 나가는 사고(#216)가 다운로드 기능까지
    막을 이유는 없다. 대신 조용히 넘어가지 않고 로그로 남긴다.

    Fusion 고정은 유지한다 — 이 함수가 감지 결과로 채운 팔레트를 그대로
    따르게 하려는 것이 목적인데, 네이티브 스타일(Windows Vista·macOS)은
    팔레트를 상당 부분 무시해 카드 목록 뷰포트 배경만 시스템 기본 밝은
    회색으로 남는 문제가 있었다(#227 실측). Fusion + 감지된 팔레트 조합은
    그 문제를 피하면서도 OS 테마를 따라간다 — 다크로 고정하지만 않으면 된다.
    """
    app.setStyle("Fusion")
    theme.set_color_scheme(theme.detect_color_scheme(app))
    # 팔레트 먼저 — QSS가 안 덮는 부분(스크롤 영역 뷰포트·컨텍스트 메뉴 등)을 담당한다
    app.setPalette(theme.build_palette())

    qss_path = resource_path(theme.QSS_RELATIVE_PATH)
    try:
        app.setStyleSheet(theme.load_stylesheet(qss_path))
        logger.info("stylesheet applied: %s", qss_path)
    except (OSError, KeyError) as e:
        logger.warning("stylesheet load failed (%s): %s", qss_path, e)


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
        # 번역 파일 로드 실패 -> 이번 실행에 한해 기본 언어(en_US) 사용.
        # 실패는 일시적일 수 있으므로 유저가 저장한 language 설정은 덮어쓰지 않는다
        language = "en_US"
        if translator.load(resource_path(f"translations/{language}.qm")):
            app.installTranslator(translator)


if __name__ == '__main__':
    # 공통 로깅 설정 (콘솔 + logs/ 회전 파일)
    setup_logging()

    app = QApplication(sys.argv)

    # 전역 스타일시트 — 위젯이 만들어지기 전에 적용해야 첫 렌더부터 반영된다 (#227)
    #
    # 실행 중 OS 테마 전환 추종은 일부러 배선하지 않는다 — 시도했다가 뺐다.
    # colorSchemeChanged로 다시 칠하면 전역 팔레트·QSS(창 바탕·버튼·입력창)만
    # 즉시 바뀌고, 카드 배경(`content/widget.py`가 `theme.card_style()`로
    # 위젯별 setStyleSheet을 거는 값)은 그 카드가 다음에 상태를 바꿀 때까지
    # 그대로 남는다 — 실기로 확인: 화면이 반쪽만 갈아입어 카드만 안 바뀐
    # 채로 튀어 보인다. 동작하지 않는 코드를 남기지 않는다는 방침대로 뺀다.
    # 카드 스타일을 전역 .qss로 옮기고 나면(지금은 상태별 값이라 위젯별
    # setStyleSheet이 유일한 경로다) 다시 볼 것.
    apply_theme(app)

    # 설정 파일 로드
    app_config = config.update_config()
    
    # 번역 시스템 초기화
    translator = QTranslator()
    
    set_language(app_config, translator)

    # 아이콘도 번역과 같은 기준(__file__)으로 해석한다 (#43)
    icon_path = resource_path('resources/icon.png')
    app.setWindowIcon(QIcon(icon_path))
    # 메인 UI 실행
    ex = VodDownloader()
    sys.exit(app.exec())
