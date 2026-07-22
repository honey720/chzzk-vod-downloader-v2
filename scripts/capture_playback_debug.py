"""#55 디버깅용: 재생 정보·매니페스트 응답을 시크릿 제거 후 파일로 캡처하는 스크립트.

사용법:
    uv run python capture_playback_debug.py <VOD URL 또는 videoNo>
    예) uv run python capture_playback_debug.py https://chzzk.naver.com/video/13714380
    예) uv run python capture_playback_debug.py 13714380

동작:
- 앱 설정(config.json)에 등록된 쿠키를 그대로 사용해 앱과 동일한 요청을 보낸다.
- 응답을 ./debug_captures/<videoNo>/ 아래 파일로 저장한다 (.gitignore 대상).
- 쿠키·inKey류 값·URL 서명 토큰(__gda__, hdnts 등)은 저장 전에 자동 REDACTED 처리한다.
- 저장 후 모든 파일을 재검사해 시크릿 원문이 남아 있으면 파일을 삭제하고 중단한다.
  쿠키·키 값은 화면에도 출력하지 않는다.
"""

import json
import re
import sys
from pathlib import Path

import requests

# scripts/ 하위에서 실행해도 저장소 루트의 config·core를 import할 수 있게 한다
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config.config as config  # noqa: E402
from core.api.url_parser import extract_content_no  # noqa: E402

CHZZK_API = "https://api.chzzk.naver.com"
NAVER_API = "https://apis.naver.com"
OUTPUT_ROOT = Path("debug_captures")

# 값 전체를 REDACTED로 치환할 JSON 키 (부분 일치, 소문자 비교)
_SECRET_KEY_PARTS = ("inkey", "adparameter", "cookie", "token", "auth")

# URL 쿼리 파라미터 중 서명·키 성격의 이름 패턴 (값을 REDACTED로 치환)
_URL_TOKEN_RE = re.compile(
    r"(?i)([?&;])"
    r"([a-z0-9_\-]*(?:key|token|sig|auth|policy|credential|gda|hdnts|hdnea|expire|session|lsu)[a-z0-9_\-]*)"
    r"=([^&\"'<>\s\\]+)"
)

# 이름 패턴에 안 걸려도 값이 48자 이상 hex면 서명으로 간주해 치환한다
# (예: 초기 버전에서 _lsu_sa_= 서명을 놓친 사례의 재발 방지)
_LONG_HEX_VALUE_RE = re.compile(r"(?i)([?&;])([a-z0-9_\-]+)=([A-Fa-f0-9~_%.-]{48,})")


def _redact_url_tokens(text: str) -> str:
    """문자열 안 URL의 서명·키 성격 쿼리 파라미터 값을 REDACTED로 치환한다."""
    text = _URL_TOKEN_RE.sub(r"\1\2=REDACTED", text)
    return _LONG_HEX_VALUE_RE.sub(r"\1\2=REDACTED", text)


def _redact_json(obj: object) -> object:
    """JSON 객체를 재귀 순회하며 시크릿 키 값과 URL 토큰을 REDACTED 처리한다."""
    if isinstance(obj, dict):
        out: dict = {}
        for key, value in obj.items():
            if (
                isinstance(value, str)
                and value
                and any(part in key.lower() for part in _SECRET_KEY_PARTS)
            ):
                out[key] = "REDACTED"
            else:
                out[key] = _redact_json(value)
        return out
    if isinstance(obj, list):
        return [_redact_json(v) for v in obj]
    if isinstance(obj, str):
        return _redact_url_tokens(obj)
    return obj


def _write(path: Path, text: str) -> None:
    """UTF-8(LF)로 파일을 저장하고 저장 사실을 출력한다."""
    path.write_text(text, encoding="utf-8", newline="\n")
    print(f"  저장: {path}")


def _safety_scan(out_dir: Path, secrets: list[str]) -> None:
    """저장된 모든 파일에서 시크릿 원문 잔존 여부를 재검사한다.

    발견되면 캡처 파일을 전부 삭제하고 즉시 중단한다 — 이 스크립트의 최종 안전장치.
    """
    leaked = []
    for path in out_dir.iterdir():
        text = path.read_text(encoding="utf-8")
        for secret in secrets:
            if secret and secret in text:
                leaked.append(path.name)
                break
    if leaked:
        for path in out_dir.iterdir():
            path.unlink()
        sys.exit(
            f"오류: 시크릿이 잔존해 캡처를 폐기했습니다: {leaked} — 스크립트 버그이므로 제보 바랍니다."
        )


def _summarize_manifest(xml_text: str) -> None:
    """매니페스트의 구조(BaseURL 유무·ContentProtection)를 요약 출력한다."""
    import xml.etree.ElementTree as ET

    ns = {"mpd": "urn:mpeg:dash:schema:mpd:2011"}
    root = ET.fromstring(xml_text)
    no_base_url = 0
    schemes: set[str] = set()
    for rep in root.findall(".//mpd:Representation", namespaces=ns):
        base = rep.find(".//mpd:BaseURL", namespaces=ns)
        if base is None:
            no_base_url += 1
        for cp in rep.findall("./mpd:ContentProtection", namespaces=ns):
            schemes.add(cp.get("schemeIdUri") or "?")
    for cp in root.findall(".//mpd:ContentProtection", namespaces=ns):
        schemes.add(cp.get("schemeIdUri") or "?")
    print(
        f"  [분석] BaseURL 없는 Representation: {no_base_url}개"
        + (" ← parse_dash_manifest 크래시 지점(#55)" if no_base_url else "")
    )
    print(f"  [분석] ContentProtection schemeIdUri: {sorted(schemes) or '없음'}")


def main() -> None:
    """캡처 파이프라인 진입점."""
    if len(sys.argv) != 2:
        sys.exit(__doc__)

    arg = sys.argv[1]
    if arg.isdigit():
        video_no = arg
    else:
        content_type, video_no = extract_content_no(arg)
        if content_type != "video" or not video_no:
            sys.exit(f"video URL이 아닙니다: {arg}")

    data = config.load_config().get("cookies", {})
    cookies = {k: v for k, v in data.items() if v}
    if not cookies:
        print("경고: config.json에 쿠키가 없습니다 — 비로그인 상태로 캡처합니다.")

    out_dir = OUTPUT_ROOT / video_no
    out_dir.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "Mozilla/5.0"}
    secrets: list[str] = list(cookies.values())

    # 1) 메타데이터 (inKey 포함 응답)
    resp = requests.get(
        f"{CHZZK_API}/service/v2/videos/{video_no}",
        cookies=cookies,
        headers=headers,
        timeout=15,
    )
    print(f"메타데이터 API: HTTP {resp.status_code}")
    resp.raise_for_status()
    content = resp.json().get("content", {})
    for key in ("inKey", "radioModeInKey"):
        if content.get(key):
            secrets.append(content[key])
    _write(
        out_dir / "metadata.json",
        json.dumps(_redact_json(resp.json()), ensure_ascii=False, indent=2) + "\n",
    )
    print(
        f"  adult={content.get('adult')} vodStatus={content.get('vodStatus')}"
        f" membershipBenefitType={content.get('membershipBenefitType')}"
        f" inKey={'있음' if content.get('inKey') else '없음(null)'}"
        f" liveRewindPlaybackJson={'있음' if content.get('liveRewindPlaybackJson') else '없음'}"
    )

    # 2) m3u8 재생 정보 + 마스터 플레이리스트
    lrpj = content.get("liveRewindPlaybackJson")
    if lrpj:
        try:
            pretty = json.dumps(_redact_json(json.loads(lrpj)), ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            pretty = _redact_url_tokens(lrpj)
        _write(out_dir / "live_rewind_playback.json", pretty + "\n")
        try:
            path = json.loads(lrpj)["media"][0]["path"]
            playlist = requests.get(path, cookies=cookies, timeout=15)
            print(f"마스터 플레이리스트: HTTP {playlist.status_code}")
            _write(out_dir / "master_playlist.m3u8", _redact_url_tokens(playlist.text))
        except Exception as e:  # 캡처 스크립트이므로 실패해도 나머지는 계속 진행한다
            print(f"  플레이리스트 캡처 실패: {type(e).__name__}: {e}")

    # 3) DASH 매니페스트 (앱과 동일: inKey + 쿠키)
    if content.get("videoId") and content.get("inKey"):
        manifest = requests.get(
            f"{NAVER_API}/neonplayer/vodplay/v2/playback/{content['videoId']}"
            f"?key={content['inKey']}",
            cookies=cookies,
            headers={"Accept": "application/dash+xml"},
            timeout=15,
        )
        print(f"DASH 매니페스트: HTTP {manifest.status_code}")
        _write(out_dir / "manifest.mpd", _redact_url_tokens(manifest.text))
        if manifest.status_code == 200:
            _summarize_manifest(manifest.text)
    else:
        print("DASH 매니페스트: inKey가 없어 건너뜀 (권한 없음 상태)")

    _safety_scan(out_dir, secrets)
    print(f"\n완료 — {out_dir}/ 안의 파일을 이슈/PR에 첨부해 주세요 (시크릿 제거 확인됨).")


if __name__ == "__main__":
    main()
