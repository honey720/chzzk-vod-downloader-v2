"""i18n JSON 카탈로그 조회 — QTranslator/.ts/.qm 대체 (#212, Phase A4).

**현재 미사용**: `#208`이 웹 전환을 철회하고 Qt 유지로 확정되면서(`#230`),
`main.py`는 지금도 `QTranslator`+`.ts`/`.qm`을 그대로 쓴다 — 이 모듈을
참조하던 웹 경로(`download_viewmodel_web.py` 등)와 그 JS 대응
(`app/resources/i18n.js`)은 `#230`에서 걷어냈다. 이 모듈·`translations/*.json`·
`scripts/extract_ts_to_json.py`를 마저 걷어낼지는 별도 판단 대상으로 남아
있다(`#230`).

`translations/<lang>.json`(`scripts/extract_ts_to_json.py`가 `.ts`에서 생성)을
읽어 원문 문자열을 조회한다.

`download/qt_bridge.py`의 `self.tr(key)` 자리를 대체한다 — 다만 그 파일의
`_failure_message`는 "매핑에 없으면 빈 문자열"이라는 별개의 업무 규칙이고,
이 모듈의 `translate()`는 "번역이 없으면 원문 그대로"라는 일반 i18n
폴백이다(Qt `tr()`이 미번역 문자열을 원문으로 보여주는 것과 동일한 원칙) —
혼동하지 않는다.

`resolve_language()`는 `main.py`의 `set_language`가 하던 언어 결정
로직(설정값 → 시스템 로케일 → 기본값)만 옮긴 것이다. 설정 파일 쓰기 같은
부작용은 호출자(Phase B)의 몫으로 분리했다 — 이 모듈은 순수 조회만 한다.
"""

import json
from pathlib import Path

DEFAULT_LANGUAGE = "en_US"

TRANSLATIONS_DIR = Path(__file__).resolve().parent.parent / "translations"


def _load_catalog(language: str) -> dict[str, str] | None:
    """카탈로그 JSON을 읽는다. 없으면 None. 캐시하지 않는다 — 파일이 작고,
    캐시가 테스트 간 상태를 새게 하는 사고 쪽이 더 크다(#199류 오염 패턴)."""
    path = TRANSLATIONS_DIR / f"{language}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def has_catalog(language: str) -> bool:
    """해당 언어의 카탈로그 파일이 존재하는지."""
    return _load_catalog(language) is not None


def resolve_language(config_language: str | None, system_locale: str | None = None) -> str:
    """사용할 언어를 결정한다 — `main.py`의 `set_language` 결정 로직과 동일한 우선순위.

    1. 설정에 언어가 저장돼 있고 그 카탈로그가 있으면 그것.
    2. 없으면 시스템 로케일의 카탈로그가 있으면 그것.
    3. 그것도 없으면 `DEFAULT_LANGUAGE`.

    설정에 저장(구 `set_language`가 시스템 로케일을 채택했을 때 config에
    써넣던 부작용)은 여기서 하지 않는다 — 호출자가 반환값을 보고 판단한다.
    """
    if config_language and has_catalog(config_language):
        return config_language
    if system_locale and has_catalog(system_locale):
        return system_locale
    return DEFAULT_LANGUAGE


def translate(key: str, language: str) -> str:
    """카탈로그에서 key를 조회한다.

    조회 실패(카탈로그 없음·키 없음)는 전부 원문(key) 그대로 반환한다 —
    예외를 던지지 않는다. 화면에 빈 문자열이나 크래시보다 원문 노출이
    안전하다는 것이 기존 Qt tr()의 폴백 동작이었고 여기서도 유지한다.
    """
    catalog = _load_catalog(language)
    if catalog is None:
        catalog = _load_catalog(DEFAULT_LANGUAGE)
    if catalog is None:
        return key
    return catalog.get(key, key)
