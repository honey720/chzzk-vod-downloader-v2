"""translations/*.json 카탈로그 회귀 캡처 (#212) — 필수 게이트.

`.ts`(XML) 원문을 이 테스트가 **독립적으로** 다시 파싱해(추출 스크립트와
같은 코드를 재사용하지 않는다 — 스크립트 자체의 버그를 놓칠 수 있어서)
저장소에 커밋된 `translations/*.json`이 원문 기준 1:1로 누락 없이
대응하는지 확인한다. 이관 중 문자열이 조용히 빠지는 회귀를 잡는 게 목적이다.
"""

import json
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TRANSLATIONS_DIR = REPO_ROOT / "translations"


def _ground_truth_from_ts(ts_path: Path) -> dict[str, str]:
    """추출 스크립트와 독립적으로 .ts를 다시 파싱한다 — 오라클 역할."""
    tree = ET.parse(ts_path)
    root = tree.getroot()
    catalog: dict[str, str] = {}
    for context in root.findall("context"):
        for message in context.findall("message"):
            source_el = message.find("source")
            if source_el is None or source_el.text is None:
                continue
            translation_el = message.find("translation")
            if translation_el is None or translation_el.get("type") == "unfinished":
                catalog[source_el.text] = source_el.text
            else:
                catalog[source_el.text] = (
                    translation_el.text if translation_el.text is not None else source_el.text
                )
    return catalog


def _committed_json(language: str) -> dict[str, str]:
    path = TRANSLATIONS_DIR / f"{language}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _ts_languages() -> list[str]:
    return sorted(p.stem for p in TRANSLATIONS_DIR.glob("*.ts"))


class TestJsonCatalogMatchesTsGroundTruth:
    """이관 전(.ts) 원문·번역문 스냅샷과 이관 후(.json)가 1:1로 대응하는지."""

    def test_ts_files_exist(self):
        # 이 테스트 스위트 자체가 무의미해지는 상황(번역 파일이 아예 없어짐)을 방지
        assert _ts_languages(), "translations/*.ts 파일을 찾지 못했다"

    def test_every_language_json_matches_its_ts_exactly(self):
        for language in _ts_languages():
            ts_path = TRANSLATIONS_DIR / f"{language}.ts"
            expected = _ground_truth_from_ts(ts_path)
            actual = _committed_json(language)

            missing = expected.keys() - actual.keys()
            extra = actual.keys() - expected.keys()
            assert not missing, f"{language}.json에서 .ts 대비 누락된 키: {sorted(missing)[:10]}"
            assert not extra, f"{language}.json에 .ts에 없는 여분 키: {sorted(extra)[:10]}"

            mismatched = {
                k: (expected[k], actual[k]) for k in expected if expected[k] != actual[k]
            }
            assert not mismatched, f"{language}.json 번역문 불일치: {list(mismatched.items())[:5]}"

    def test_no_cross_context_translation_conflicts_in_ts(self):
        """같은 원문이 다른 <context>에서 다른 번역으로 갈리면 평평한(flat) JSON
        구조 자체가 손실을 낸다 — 이관 설계의 전제(#212 조사)가 여전히 유효한지
        회귀 감시한다. 이게 깨지면 JSON을 context별로 분리해야 한다."""
        for language in _ts_languages():
            ts_path = TRANSLATIONS_DIR / f"{language}.ts"
            tree = ET.parse(ts_path)
            root = tree.getroot()
            by_source: dict[str, set[str]] = {}
            for context in root.findall("context"):
                for message in context.findall("message"):
                    source_el = message.find("source")
                    translation_el = message.find("translation")
                    if source_el is None or source_el.text is None:
                        continue
                    translation = translation_el.text if translation_el is not None else None
                    by_source.setdefault(source_el.text, set()).add(translation)

            conflicts = {s: t for s, t in by_source.items() if len(t) > 1}
            assert not conflicts, (
                f"{language}.ts에서 컨텍스트 간 번역 충돌 발견 (평평한 JSON 전제가 깨짐): "
                f"{list(conflicts.items())[:5]}"
            )


class TestCatalogParity:
    """언어 간 키 집합이 일치하는지 — 한쪽에만 있는 문자열은 그 언어에서 원문 노출로 샌다."""

    def test_all_language_catalogs_have_identical_key_sets(self):
        languages = _ts_languages()
        assert len(languages) >= 2, "언어가 2개 미만이라 대조 의미가 없다"

        keysets = {lang: set(_committed_json(lang).keys()) for lang in languages}
        first_lang, first_keys = next(iter(keysets.items()))
        for lang, keys in keysets.items():
            assert keys == first_keys, (
                f"{lang}과 {first_lang}의 카탈로그 키 집합이 다르다 — "
                f"{lang}에만 있음: {sorted(keys - first_keys)[:5]}, "
                f"{first_lang}에만 있음: {sorted(first_keys - keys)[:5]}"
            )


class TestJsCatalogContract:
    """app/resources/i18n.js가 Python 쪽(app/i18n.py)과 같은 계약을 따르는지.

    이 저장소에는 JS 실행기(Node 등)가 없다 — 실제로 JS를 실행해 검증하지는
    않는다. 여기서 확인하는 건 (a) 파일이 존재하고 (b) 계약에 필요한 함수명
    (setCatalog/translate)이 소스에 실제로 정의돼 있다는 것뿐이다 — 로직
    정확성은 코드 리뷰와 Phase C 통합 시점의 실기 확인에 의존한다는 걸
    명시적으로 남긴다(과신 금지).
    """

    def test_i18n_js_file_exists(self):
        path = REPO_ROOT / "app" / "resources" / "i18n.js"
        assert path.exists()

    def test_i18n_js_defines_expected_function_names(self):
        path = REPO_ROOT / "app" / "resources" / "i18n.js"
        text = path.read_text(encoding="utf-8")
        assert "function setCatalog(" in text
        assert "function translate(" in text
