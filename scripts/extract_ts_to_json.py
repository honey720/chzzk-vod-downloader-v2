"""Qt `.ts`(XML) 번역 파일을 JSON 카탈로그로 1회성 변환한다 (#212, Phase A4).

`translations/<lang>.ts` → `translations/<lang>.json`. 원문(source)을 키로,
번역(translation)을 값으로 하는 평평한(flat) dict를 만든다 — 컨텍스트별로
나누지 않는다.

**평평한 구조가 안전한 이유(사전 확인)**: 두 `.ts` 파일 전체를 파싱해 같은
원문 문자열이 서로 다른 `<context>`에서 다른 번역으로 갈리는 경우가 있는지
확인했다 — 86개 `<message>` 중 고유 원문 77개, **충돌 0건**(모든 중복 원문이
컨텍스트 무관하게 항상 같은 번역). `download/qt_bridge.py`의
`_failure_message`가 이미 원문 영문 문자열 자체를 키로 쓰는 dict를 쓰고
있던 것과도 일치한다.

사용: `python scripts/extract_ts_to_json.py` (저장소 루트에서 실행).
멱등적이다 — 여러 번 돌려도 같은 결과를 덮어쓸 뿐이다. `.ts` 파일이 나중에
바뀌면(새 문자열 추가 등) 이 스크립트를 다시 돌려 JSON을 갱신한다.
"""

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")  # #204와 동일 원인 — 한글 print 보호

TRANSLATIONS_DIR = Path(__file__).resolve().parent.parent / "translations"


def extract_catalog(ts_path: Path) -> dict[str, str]:
    """.ts 파일 하나를 파싱해 {원문: 번역} 평평한 dict를 만든다.

    `unfinished`(미완료 번역)는 원문을 그대로 값으로 쓴다 — 번역 공백보다
    원문 노출이 안전하다(Qt의 tr() 미번역 폴백과 같은 원칙).
    같은 원문이 여러 컨텍스트에 나오면 마지막 값으로 덮어써진다 — 위 사전
    확인으로 이 프로젝트의 두 파일에는 충돌이 없음을 확인했다.
    """
    tree = ET.parse(ts_path)
    root = tree.getroot()
    catalog: dict[str, str] = {}
    for context in root.findall("context"):
        for message in context.findall("message"):
            source_el = message.find("source")
            if source_el is None or source_el.text is None:
                continue
            source = source_el.text
            translation_el = message.find("translation")
            if translation_el is None or translation_el.get("type") == "unfinished":
                catalog[source] = source
            else:
                catalog[source] = translation_el.text if translation_el.text is not None else source
    return catalog


def main() -> int:
    ts_files = sorted(TRANSLATIONS_DIR.glob("*.ts"))
    if not ts_files:
        print(f"[extract_ts_to_json] {TRANSLATIONS_DIR}에 .ts 파일이 없다.")
        return 1

    for ts_path in ts_files:
        catalog = extract_catalog(ts_path)
        json_path = ts_path.with_suffix(".json")
        json_path.write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"[extract_ts_to_json] {ts_path.name} -> {json_path.name} ({len(catalog)}개 키)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
