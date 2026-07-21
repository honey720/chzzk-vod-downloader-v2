# Changelog

작업 요약. 상세 파일 변경 이력은 [activity.log](activity.log) 참고.

---

## 2026-03-11

### UI - 다운로드 인터페이스 뼈대 구성

**생성**
- `app/common/download_state.py` — `DownloadState` enum (WAITING/RUNNING/PAUSED/FINISHED/ERROR)
- `app/common/` — 공통 모듈 디렉터리 정비
- `app/view/base_interface.py` — 공통 부모 인터페이스 (`TitleLabel` + `contentLayout`)
- `app/view/download_interface.py` — 다운로드 인터페이스 (`UrlBar` + `CardListWidget` + `FooterBar`)
- `app/components/download_card.py` — `DownloadCardWidget` (썸네일·채널·제목 인라인 편집·해상도 버튼·ProgressRing·상태 아이콘)
- `2.8.0-dev/docs/download_interface.md` — 인터페이스 설계 문서
- `2.8.0-dev/scripts/log_changes.py` — 파일 변경 자동 기록 hook 스크립트

**수정**
- `app/view/main_window.py` — `downloadInterface` 더미 Widget → `DownloadInterface` 교체

### 기능
- `CardListWidget(ListWidget)` — qfluentwidgets ListWidget 기반, `setItemWidget()` 방식
- 드래그 앤 드롭 — 내부 순서 변경(InternalMove) + 외부 URL 텍스트 드롭 구분 처리
- 빈 리스트 / 드래그 오버레이 — `paintEvent` 오버라이드로 안내 문구 표시
- `DownloadCardWidget.setResolutions()` — 해상도 버튼 동적 생성
- `DownloadCardWidget.setState()` — 상태별 아이콘·ProgressRing·버튼 활성화 일괄 처리
