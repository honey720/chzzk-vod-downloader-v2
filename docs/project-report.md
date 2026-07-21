# chzzk-vod-downloader v2 — 프로젝트 리포트

> 작성일: 2026-03-10
> 버전: 2.8.0-dev-01
> 저자: honey720

---

## 1. 프로젝트 개요

**chzzk-vod-downloader v2 (CVDv2)** 는 네이버 치지직(Chzzk) 플랫폼의 VOD 다시보기와 클립 영상을 다운로드하는 데스크톱 GUI 애플리케이션입니다.

| 항목 | 내용 |
|------|------|
| 언어 | Python 3.13 |
| GUI 프레임워크 | PySide6 + qfluentwidgets (Fluent Design) |
| 빌드 | Nuitka |
| 테스트 | pytest |
| 지원 OS | Windows, macOS |
| 지원 언어 | 한국어, English |
| 레포지토리 | [honey720/chzzk-vod-downloader-v2](https://github.com/honey720/chzzk-vod-downloader-v2) |

---

## 2. 버전 전환 배경 (2.7.4 → 2.8.0)

### 구 방식 (2.7.4, `legacy/`)

- PySide6 + Qt Designer `.ui` 파일로 UI를 설계하고, `uic` 도구로 `.py`로 변환하는 방식
- `application/`, `ui/`, `content/`, `download/`, `config/` 단일 레이어 구조
- UI와 비즈니스 로직이 혼재 — 테스트 및 유지보수 어려움
- 구 코드 전체는 `legacy/` 디렉토리에 보존되어 참고용으로 유지

### 신 방식 (2.8.0, `app/` + `core/`)

- **qfluentwidgets** 라이브러리로 **순수 Python 코드**로 UI 작성 (`.ui` 파일 불필요)
- **`app/` (Frontend) ↔ `core/` (Backend)** 로 관심사 명확히 분리
- `core/`는 UI 프레임워크를 전혀 모름 → CLI, 웹, 다른 UI로 언제든 교체 가능
- **플러그인 시스템** 도입으로 치지직 외 다른 플랫폼 확장 가능
- **pytest** 기반 테스트 체계 도입
- Fluent Design System 기반 Light/Dark 테마, Mica Effect(Windows 11), DPI 스케일 자동 지원

---

## 3. 목표 기능

### URL 입력
- 텍스트 Input 직접 입력
- 드래그 앤 드롭 (치지직 카드, 메모장 URL 목록)
- Ctrl+V 붙여넣기
- URL 필터링 및 유효성 검사
- 중복 URL 체크

### 다운로드
- 멀티스레드 byte-range 병렬 다운로드 (회선 최대 속도)
- M3U8 HLS 세그먼트 병렬 다운로드 + 병합
- 동적 스레드 수 조정 (속도 기반 자동 증감)
- 다중 VOD 큐 (순차 다운로드)
- 해상도 선택 (144p ~ 1080p+)
- 일시정지 / 재개 / 중지

### 설정 (사이드바)
- 테마 설정 (Light / Dark / System)
- 쿠키 관리 (NID_AUT / NID_SES — 연령 제한 콘텐츠 접근)
- 다운로드 경로 지정
- 언어 설정 (한국어 / English)
- DPI 스케일, Mica Effect 설정

### 데이터 관리
- 설정 값: `config.json`
- 다운로드 기록: SQLite DB
- 로그: `/logs` 디렉토리

---

## 4. 아키텍처 설계

### 4.1 핵심 원칙

```
core/  →  app/ 방향의 단방향 의존성

- core는 절대 app을 import하지 않는다
- core는 UI 프레임워크를 모른다
- app ↔ core 통신은 콜백 / 이벤트 / 반환값으로만
- core는 언제든 CLI, 웹, 다른 UI로 교체 가능해야 한다
```

### 4.2 목표 디렉토리 구조

```
project/
├── CVDv2.py                          # 진입점
├── requirements.txt
│
├── app/                              # Frontend (UI 레이어)
│   ├── common/                       # UI 공통 모듈
│   │   ├── config.py                 # QConfig 기반 설정
│   │   ├── signal_bus.py             # UI 전용 Signal Bus
│   │   ├── translator.py             # 다국어 문자열
│   │   └── icon.py
│   │
│   ├── components/                   # 재사용 UI 위젯
│   │   ├── base/
│   │   └── buttons/
│   │
│   ├── views/                        # 화면 단위 (Interface)
│   │   ├── main_window.py            # FluentWindow 메인 윈도우
│   │   ├── download_interface.py     # 다운로드 화면
│   │   ├── setting_interface.py      # 설정 화면
│   │   └── about_interface.py        # 정보 화면
│   │
│   ├── viewmodels/                   # View ↔ Core 연결층 (MVVM)
│   │   ├── main_viewmodel.py
│   │   └── download_viewmodel.py
│   │
│   └── resources/                    # 정적 리소스
│       ├── images/ icons/ qss/
│       └── i18n/                     # 번역 파일
│
├── core/                             # Backend (UI 독립적)
│   ├── models/                       # 데이터 모델
│   │   └── download_item.py
│   │
│   ├── services/                     # 비즈니스 로직
│   │   ├── download_service.py
│   │   └── file_service.py
│   │
│   ├── api/                          # 외부 API 클라이언트
│   │   └── rest_client.py
│   │
│   ├── events/                       # Core 전용 이벤트 시스템
│   │   └── event_bus.py
│   │
│   ├── downloaders/                  # 내장 다운로더 (공식)
│   │   ├── base_downloader.py        # 공통 추상 인터페이스
│   │   └── chzzk_downloader.py       # 치지직 공식 다운로더
│   │
│   └── plugins/                      # 플러그인 시스템
│       ├── base.py                   # 추상 인터페이스
│       ├── loader.py                 # 플러그인 로더
│       └── registry.py               # 플러그인 레지스트리
│
├── plugins/                          # 유저 플러그인 디렉토리
│   ├── README.md                     # 플러그인 개발 가이드
│   ├── _example/                     # 예제 플러그인
│   └── [user-plugins]/               # 유저 추가 플러그인 (git 제외)
│       ├── twitch/
│       └── afreeca/
│
├── tests/                            # 테스트 (pytest)
│   ├── unit/
│   │   ├── core/                     # Core 단위 테스트
│   │   └── app/                      # App(ViewModel) 단위 테스트
│   ├── integration/                  # 통합 테스트
│   ├── fixtures/                     # 테스트 데이터
│   └── mocks/                        # Mock 객체
│
├── legacy/                           # 2.7.4 구 코드 (참고용)
└── docs/                             # 프로젝트 문서
```

### 4.3 레이어 다이어그램

```
┌──────────────────────────────────────────────┐
│  View (app/views/)                           │
│  FluentWindow, Interface, Card 위젯          │
├──────────────────────────────────────────────┤
│  ViewModel (app/viewmodels/)                 │
│  View ↔ Core 연결, UI 상태 관리             │
├──────────────────────────────────────────────┤
│  Signal Bus (app/common/signal_bus.py)       │
│  UI 컴포넌트 간 이벤트 전달                  │
├──────────────────────────────────────────────┤
│  [경계선 — core는 위를 절대 import 안 함]    │
├──────────────────────────────────────────────┤
│  Service (core/services/)                    │
│  다운로드 큐, 파일 관리 비즈니스 로직        │
├──────────────────────────────────────────────┤
│  Downloader / Plugin (core/downloaders/ + plugins/) │
│  플랫폼별 다운로드 구현체                   │
├──────────────────────────────────────────────┤
│  API Client (core/api/)                      │
│  Chzzk REST API, manifest 파싱               │
└──────────────────────────────────────────────┘
```

### 4.4 플러그인 시스템 설계 방향

```
core/plugins/base.py 에 추상 인터페이스 정의
    ↓
내장: core/downloaders/chzzk_downloader.py  (치지직 공식)
외장: plugins/twitch/downloader.py          (유저 플러그인)
      plugins/afreeca/downloader.py

로더가 plugins/ 디렉토리를 스캔 → registry에 등록 → service가 호출
```

---

## 5. 현재 개발 상태 (2.8.0-dev)

### 완료 (현재 `app/`)

- [x] qfluentwidgets 기반 `FluentWindow` + `NavigationBar` 뼈대
- [x] `app/common/config.py` — QConfig 설정 (쿠키, 경로, 언어, DPI, Mica)
- [x] `app/view/setting_interface.py` — 설정 화면
- [x] `app/view/drop_card.py` — URL 드롭 카드
- [x] `app/components/custom_cookie_setting_card.py` — 쿠키 설정 카드
- [x] 다운로드 엔진 로직 검증 (legacy 기반)

### 진행 중 / 예정

**Frontend (app/)**
- [ ] `views/download_interface.py` — 다운로드 화면 (URL 입력, 큐, 진행 카드)
- [ ] `views/record_interface.py` — 녹화 화면
- [ ] `views/favorite_interface.py` — 즐겨찾기
- [ ] `views/about_interface.py` — 정보 화면
- [ ] `viewmodels/download_viewmodel.py` — 다운로드 ViewModel
- [ ] `app/` 내부 디렉토리 구조 재정비 (`view/` → `views/`)

**Backend (core/)**
- [ ] `core/models/download_item.py` — 다운로드 아이템 데이터 모델
- [ ] `core/api/rest_client.py` — Chzzk API 클라이언트 (legacy `content/network.py` 이식)
- [ ] `core/downloaders/chzzk_downloader.py` — 치지직 다운로더 (legacy `download/` 이식)
- [ ] `core/services/download_service.py` — 다운로드 서비스 (큐 관리)
- [ ] `core/events/event_bus.py` — Core 이벤트 버스
- [ ] `core/plugins/` — 플러그인 시스템 (base, loader, registry)

**인프라**
- [ ] `plugins/` 디렉토리 + `_example` 플러그인 작성
- [ ] `tests/` 기반 pytest 테스트 작성
- [ ] SQLite 다운로드 기록 저장
- [ ] 번역 파일 (i18n) 신규 구조 적용
- [ ] Nuitka 빌드 설정

---

## 6. 다운로드 엔진 상세 (legacy 검증 기준)

### 6.1 일반 VOD (byte-range)

1. `HEAD` 요청으로 전체 파일 크기 획득
2. 해상도별 가중치로 `part_size` 결정 (144p: 1MB, 720p: 5MB, 그 외: 10MB)
3. 파일을 N개 구간으로 분할 → `ThreadPoolExecutor` 병렬 다운로드
4. 속도 100 KB/s 미만 5회 연속 → 해당 스레드 재시작
5. 실패 구간은 `remaining_ranges`에 재등록하여 재시도

### 6.2 M3U8 HLS (라이브 다시보기)

1. `liveRewindPlaybackJson` 감지 시 M3U8 플로우 분기
2. `.m3u8` 플레이리스트 파싱 → 세그먼트 목록 추출
3. `#EXT-X-MAP` 초기화 세그먼트 먼저 다운로드
4. 세그먼트를 임시 폴더(`CVDv2_temp/`)에 병렬 저장
5. 완료 후 순서대로 단일 `.mp4`로 병합 → 임시 폴더 삭제

### 6.3 동적 스레드 조정 (MonitorThread)

```
스레드당 평균 속도 > 4 MB/s  →  +4 스레드 (2회 연속 감지 시)
스레드당 평균 속도 < 2 MB/s  →  스레드 수 절반으로 감소 (5회 연속 감지 시)
```

### 6.4 다운로드 상태 머신

```
WAITING → RUNNING → FINISHED
                 ↘ PAUSED ↔ RUNNING
                 ↘ WAITING  (중지, 파일 삭제)
                 ↘ FAILED
```

---

## 7. Chzzk API 연동

| API | 용도 |
|-----|------|
| `GET /service/v2/videos/{video_no}` | VOD 메타데이터, video_id, in_key, 성인 여부 |
| DASH manifest XML | 일반 VOD 해상도 목록 및 base_url |
| M3U8 playlist | 라이브 다시보기 해상도 및 세그먼트 URL |
| 클립 API | 클립 메타데이터 및 DASH manifest |

- 성인 콘텐츠: `NID_AUT` / `NID_SES` 쿠키 필요
- URL 패턴: `https://chzzk.naver.com/(video|clips)/{content_no}`

---

## 8. 기술 스택

| 분류 | 내용 |
|------|------|
| GUI | PySide6, qfluentwidgets (Fluent Design) |
| HTTP | requests |
| 동시성 | QThread, ThreadPoolExecutor, threading.Event |
| 설정 관리 | qfluentwidgets.QConfig |
| 데이터 저장 | config.json (설정), SQLite (다운로드 기록), /logs (로그) |
| 번역 | Qt Linguist (.ts/.qm), FluentTranslator |
| 테스트 | pytest |
| 빌드 | Nuitka |

---

## 9. 참고 사항

- 본 프로젝트는 [chzzk-vod-downloader](https://github.com/24802/chzzk-vod-downloader)를 참고하여 개발되었습니다.
- UI는 [qfluentwidgets Gallery 데모](https://github.com/zhiyiYo/PyQt-Fluent-Widgets)를 참고하여 구성됩니다.
- 안정 버전이 아니며, 사용 중 발생하는 피해에 대해 개발자는 책임지지 않습니다.
- 이슈 및 제안: [GitHub Issues](https://github.com/honey720/chzzk-vod-downloader-v2/issues)
