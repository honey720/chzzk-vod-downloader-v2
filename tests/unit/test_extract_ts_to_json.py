"""scripts/extract_ts_to_json.py 검증 (#212).

실제 translations/ 디렉토리는 건드리지 않는다 — tmp_path에 최소 재현 .ts를
만들어 그 파일을 대상으로 검증한다.
"""

from pathlib import Path

import scripts.extract_ts_to_json as extract_module

_TS_TEMPLATE = """\
<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE TS>
<TS version="2.1" language="{lang}">
<context>
    <name>Widget</name>
    <message>
        <source>Hello</source>
        <translation>{hello}</translation>
    </message>
    <message>
        <source>Unfinished string</source>
        <translation type="unfinished"></translation>
    </message>
</context>
<context>
    <name>OtherWidget</name>
    <message>
        <source>Hello</source>
        <translation>{hello}</translation>
    </message>
</context>
</TS>
"""


def _write_ts(path: Path, lang: str, hello: str):
    path.write_text(_TS_TEMPLATE.format(lang=lang, hello=hello), encoding="utf-8")


class TestExtractCatalog:
    def test_flattens_source_to_translation(self, tmp_path):
        ts_path = tmp_path / "ko_KR.ts"
        _write_ts(ts_path, "ko_KR", "안녕")

        catalog = extract_module.extract_catalog(ts_path)

        assert catalog["Hello"] == "안녕"

    def test_unfinished_translation_falls_back_to_source(self, tmp_path):
        ts_path = tmp_path / "ko_KR.ts"
        _write_ts(ts_path, "ko_KR", "안녕")

        catalog = extract_module.extract_catalog(ts_path)

        assert catalog["Unfinished string"] == "Unfinished string"

    def test_duplicate_source_across_contexts_collapses_to_one_key(self, tmp_path):
        """두 컨텍스트(Widget/OtherWidget)에 같은 원문 "Hello"가 있어도
        평평한 dict에서는 키 하나로 합쳐진다 — #212가 확인한 무충돌 전제."""
        ts_path = tmp_path / "ko_KR.ts"
        _write_ts(ts_path, "ko_KR", "안녕")

        catalog = extract_module.extract_catalog(ts_path)

        assert list(catalog.keys()).count("Hello") == 1


class TestMain:
    def test_writes_json_next_to_ts_for_every_ts_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(extract_module, "TRANSLATIONS_DIR", tmp_path)
        _write_ts(tmp_path / "ko_KR.ts", "ko_KR", "안녕")
        _write_ts(tmp_path / "en_US.ts", "en_US", "Hello")

        exit_code = extract_module.main()

        assert exit_code == 0
        assert (tmp_path / "ko_KR.json").exists()
        assert (tmp_path / "en_US.json").exists()

    def test_returns_error_when_no_ts_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(extract_module, "TRANSLATIONS_DIR", tmp_path)

        assert extract_module.main() == 1

    def test_output_is_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(extract_module, "TRANSLATIONS_DIR", tmp_path)
        _write_ts(tmp_path / "ko_KR.ts", "ko_KR", "안녕")

        extract_module.main()
        first = (tmp_path / "ko_KR.json").read_text(encoding="utf-8")
        extract_module.main()
        second = (tmp_path / "ko_KR.json").read_text(encoding="utf-8")

        assert first == second
