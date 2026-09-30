# CLAUDE.md — Chzzk VOD Downloader v2

이 저장소에서 작업하는 모든 AI 에이전트가 따르는 프로젝트 지침이다.
**행동 규칙은 `.claude/rules/` 여섯 편**에 있고 이 문서와 함께 자동 로드된다.
설계 판단의 **근거·수치·사례**는 `docs/SPEC.md`에 있다. 충돌하면 이 문서와 rules가 우선한다.

## 프로젝트 개요

치지직(Chzzk) VOD 다운로더 데스크톱 앱. PySide6 GUI이고 다운로드 엔진은 `core/`에 UI와 분리돼 있다.

## 기술 스택 (변경 금지)

- Python **3.13 고정**(`.python-version`) · 패키지 매니저 **uv** (pip 직접 사용 금지)
- GUI **PySide6 + 순정 QSS 확정**(SPEC §4.3). **새 UI 라이브러리를 도입하지 않는다.**
- HTTP requests · 암호 pycryptodome · 미디어 imageio-ffmpeg
- 테스트 **pytest** + pytest-qt · 린트 **ruff** · 빌드 **Nuitka**
- 다른 GUI 프레임워크·패키지 매니저를 제안하거나 도입하지 않는다. 신규 의존성은 이슈에서 사전 승인된 경우만.

## 명령어 (이대로 실행할 것)

```bash
uv sync                        # 의존성 설치
uv run python main.py          # 진입점
uv run ruff check .            # 린트
uv run ruff format .           # 포맷
uv run pytest                  # 전체 테스트
uv run pytest tests/unit       # 단위 테스트만
```

- 명령을 추측하지 않는다. 실패하면 임의로 우회하지 말고 실패 로그를 이슈·PR에 보고한다.
- `xvfb-run`은 리눅스 CI 전용이다.

## 아키텍처 불변 규칙 (위반 시 PR 반려)

- **단방향: app → core.** `core/`는 앱 계층(`app/`·`config/`·`ui/`)을 import하지 않는다.
- `core/`는 UI 프레임워크를 모른다 — `tests/unit/core/test_no_qt_import.py`가 AST로 검사하는 CI 게이트다.
- ⚠️ **방향 게이트는 금지 목록이 아니라 유도다**(`tests/unit/core/test_layer_direction.py`). *"`core/`는 저장소가 소유한 최상위 이름 중 `core` 이외의 것을 import하지 않는다."* 소유 이름은 저장소 루트에서 유도하고, 유도가 놓치면 메타 게이트가 실패한다. **새 최상위 디렉토리가 생겨도 자동으로 금지되고, 사라져도 목록만 비는 일이 없다.**
- 앱 경로·구현이 필요하면 **인자로 주입**받는다(`setup_logging(log_dir)`, `MetadataApi` Protocol, `base_url_resolver`, `requires_key_resolution`).
- app ↔ core 통신은 **콜백·반환값**으로만. core에 이벤트 버스를 두지 않는다. `core/`는 언제든 CLI·웹·다른 UI로 교체 가능해야 한다.
- 레이어: `views → viewmodels → core(services → downloaders/api)`. View에서 core 직접 호출 금지.
- **다운로드 경로의 Qt Signal emit은 단일 모듈에만** 둔다. ⚠️ 규칙은 「한 모듈」이고 **파일 이름은 단계마다 다르다** — 금지 대상은 emit이 여러 모듈로 흩어지는 것이지 특정 파일이 아니다.
- ⚠️ **워커 스레드 콜백을 메인 스레드로 넘기는 수단은 큐 연결 Signal뿐이다.** *"콜백에서 바로 부르면 되지 않나"*로 단순화하면 **위젯이 워커 스레드에서 갱신된다** — 예외가 나지 않고 재현이 산발적이다.
- **QThreadPool 워커의 시그널은 바운드 메서드로 연결하고, 결과가 도착할 때까지 워커 참조를 보관한다.** 순수 콜러블(`functools.partial` 등)은 워커가 파괴되는 순간 전달이 **예외도 로그도 없이 유실된다.**
- ⚠️ **서버가 알려준 주소에 인증 쿠키를 실어 보내기 전에 검사한다.** HTTPS 여부는 항상, 호스트는 **근거가 있을 때만** 잠근다. **첫 주소만 검사하는 것은 검사가 아니다** — 리다이렉트마다 다시 검사한다. **근거 없는 허용 목록을 만들지 않는다.**
- 다운로더 확장은 `core/downloaders/base.py` 상속으로만. 실행 엔진을 다시 구현하지 않는다.
- **앱은 접근 권한을 만들어내지 않는다.** 유저가 자신의 쿠키로 받을 수 있는 것만 지원한다(SPEC §8.1).

## 코드 스타일

- **PEP 8.** 줄 길이 100, `target-version = py313`. `uv run ruff check .` 와 `uv run ruff format .` 를 통과해야 한다.
- **타입힌트 필수.**
- **주석·docstring은 한국어.**

### docstring — PEP 257 + Google 스타일

**모듈 · public 클래스 · public 함수/메서드에 단다.** private(`_` 접두)는 동작이 자명하지 않을 때만.

- 첫 줄은 **무엇을 하는지 한 문장**, 서술형(`~한다`)으로 쓰고 마침표로 끝낸다.
- 한 줄로 끝나면 닫는 따옴표를 같은 줄에 둔다. 길어지면 `한 줄 요약` → `빈 줄` → `본문`.
- 본문에는 **왜 그렇게 정했는지**와 호출 규약(언제 부르는지, 재호출·예외 시 어떻게 되는지)을 적는다.
- 판단의 근거가 된 이슈·PR 번호를 단다 — `(#83)`.

절은 **필요한 것만** 쓴다. 시그니처에 이미 드러나면 절을 쓰지 않는다.

| 절 | 쓸 때 |
|---|---|
| `Args:` | 이름만으로 뜻이 안 서거나 단위·기본값·허용 범위를 밝혀야 할 때 |
| `Returns:` | 반환값의 의미가 타입만으로 안 드러날 때 |
| `Raises:` | 호출자가 잡아야 하는 예외가 있을 때 |

```python
def setup_logging(log_dir: str, log_level: int = logging.DEBUG) -> None:
    """루트 로거에 콘솔·회전 파일 핸들러를 설정한다.

    앱 시작 시 한 번만 호출한다. 재호출은 무시된다(핸들러 중복 방지).

    Args:
        log_dir: 로그 파일을 저장할 디렉토리 (없으면 생성)
        log_level: 루트 로거에 적용할 로깅 레벨 (기본값: DEBUG)
    """
```

### 인라인 주석

**모듈 상수 · 데이터클래스 필드 · 열거형 멤버**에 한 줄 `#` 주석으로 무엇인지 밝힌다.

- 매직 넘버에는 **단위와 근거**를 적는다 — `MAX_BYTES = 1024 * 1024  # 1MB`
- **코드가 이미 말하는 것을 되풀이하지 않는다.** 적을 것은 왜다.
- 지역 변수에는 달지 않는다. 설명이 필요하면 **이름을 고친다.**

테스트 코드의 docstring 규칙은 `.claude/rules/testing.md` 에 있다.

## 경로·플랫폼

- 설정은 `config.json`, 로그는 앱 데이터 폴더의 `logs/`. 저장 위치를 임의로 바꾸지 않는다.
- 파일명·경로 결정은 `core/utils/paths.py` **한 곳**(SPEC §7.1), ffmpeg 경로 탐색은 `core/utils/ffmpeg.py` **한 곳**(SPEC §6.5)에서만.
- **한 OS에만 있는 API를 분기 없이 부르지 않는다**(SPEC §8.4). 파일·폴더 열기는 `QDesktopServices` 같은 Qt 추상화로. **테스트 코드에도 적용된다** — `monkeypatch.setattr(os, "startfile", ...)`에 `raising=False`를 빠뜨리면 다른 OS 러너에서 `AttributeError`로 실패한다.
- **OS 기본 로케일에도 의존하지 않는다.** 스크립트는 스스로 인코딩을 고정한다(`sys.stdout.reconfigure(encoding="utf-8")`). **API 부재는 즉시 드러나지만 로케일 의존은 특정 조합에서만 실패한다.**

## UI·스타일 규칙

상세와 함정은 SPEC §3.4·§8.5. 아래는 **어기기 쉬운 금지만** 옮긴 것이다.

- **색·모서리·간격 토큰은 `theme.py`에서만 정의한다.** `.qss`는 `@토큰` 자리표시자만 쓴다. 색 리터럴이 `theme.py` 밖에 있으면 테스트가 실패한다.
- **위젯별 `setStyleSheet()`을 늘리지 않는다. 전역 `.qss`로 간다.** 위젯별은 테스트의 폭 계산을 바꾸고 `QPalette`보다 우선해 OS 테마 추종을 막는다.
- **카드마다 파이썬 객체가 붙는 방식을 쓰지 않는다**(`QGraphicsDropShadowEffect` 등) — O(1) 삽입을 되돌린다.
- **QSS는 서브컨트롤을 각각 스타일해야 하고 하나라도 빼면 그 부분만 네이티브로 남는다.** "위젯에 적용됨"이 "전부 적용됨"을 뜻하지 않는다.
- **가로 오버플로 금지.** 말줄임 라벨은 `sizeHint()`도 함께 override한다.
- **클래스 이름을 함부로 바꾸지 않는다** — `lupdate` 스캔 대상이라 번역 컨텍스트가 깨진다.
