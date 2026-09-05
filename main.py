import sys
import os
import logging
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QIcon
from PySide6.QtCore import QTranslator, QLocale

from app.views.mainWindow import VodDownloader
import config.config as config
import app.theme as theme
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

    `theme.build_style()`로 감싸는 이유(#241 후속): Fusion은 콤보 팝업을
    선택 항목이 콤보 라벨과 겹치도록 띄우는데, v2.9.6(네이티브 스타일)은
    항상 아래로 떨어졌다 — 순정 `"Fusion"` 문자열 대신 이 래퍼를 써야
    그 배치 회귀가 같이 안 딸려온다.
    """
    app.setStyle(theme.build_style())

    # 시작 시점 적용과 실행 중 전환이 같은 함수를 탄다 — 경로가 둘이면 하나만 낡는다.
    # 팔레트 먼저 — QSS가 안 덮는 부분(스크롤 영역 뷰포트·컨텍스트 메뉴 등)을 담당한다
    qss_path = resource_path(theme.QSS_RELATIVE_PATH)
    scheme = theme.detect_color_scheme(app)
    try:
        theme.apply_color_scheme(app, scheme, qss_path)
    except (OSError, KeyError) as e:
        # 시작 시점 폴백 — apply_color_scheme()은 원자적이라 여기 오면 아무것도 안 바뀐
        # 상태다. 시작 때는 지킬 "옛 테마"가 없으므로 예전과 같이 감지된 스킴의
        # 팔레트만 건다(QSS 없이 Qt 기본 외형, #216 정책). 실행 중 전환은 이 폴백을
        # 타지 않는다 — 실패하면 옛 테마가 그대로 남는다.
        logger.warning("stylesheet load failed (%s): %s — palette only", qss_path, e)
        theme.set_color_scheme(scheme)
        app.setPalette(theme.build_palette())
    theme.follow_os_color_scheme(app, qss_path)


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
    # 실행 중 OS 테마 전환 추종은 apply_theme() 안에서 함께 건다 — #227 때는
    # 카드가 위젯별 setStyleSheet이라 반쪽만 갈아입어 뺐고(SPEC §8.5), #242로
    # 카드가 전역 .qss로 옮겨 온 뒤 "시작부터 라이트"와 "다크→라이트 전환"의
    # 렌더가 같은지를 게이트(tests/unit/test_theme_switch.py)로 재고 재도입했다.
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
