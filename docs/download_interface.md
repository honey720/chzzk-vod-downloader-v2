# 다운로드 인터페이스 설계 문서

최종 수정: 2026-03-11

---

## 현재 완성 현황

| 파일 | 상태 | 설명 |
|------|------|------|
| `app/view/base_interface.py` | ✅ 완성 | 공통 부모 인터페이스 |
| `app/view/download_interface.py` | 🔧 뼈대 완성 | 카드 위젯·드래그앤드롭 TODO |
| `app/view/main_window.py` | ✅ 연결 | DownloadInterface 인스턴스 등록 |

---

## 레이아웃 구조

```
MainWindow (FluentWindow)
└── DownloadInterface(BaseInterface)
    ├── TitleLabel "Downloads"              [고정]
    ├── UrlBar (QWidget)                    [고정]
    │   ├── LineEdit (URL 입력, Enter 지원)
    │   └── PrimaryPushButton "Fetch"
    ├── CardListArea (ListWidget 예정)      [stretch=1]
    │   └── QListWidgetItem + DownloadCardWidget
    └── FooterBar (QWidget)                 [고정]
        ├── PrimaryPushButton "Download/Pause" (토글)
        ├── PushButton "Stop"  (초기 disabled)
        ├── PushButton "Clear Finished"
        └── CaptionLabel "n / m"
```

---

## 카드 리스트 구현 방식

### 채택: qfluentwidgets ListWidget + setItemWidget()

`qfluentwidgets.ListWidget` 은 `QListWidget` 을 상속한다.
`QListWidget.setItemWidget(item, widget)` 으로 임의의 QWidget을 아이템에 붙일 수 있다.

```python
item = QListWidgetItem()
item.setSizeHint(QSize(width, 130))  # 카드 높이 고정
listWidget.addItem(item)
listWidget.setItemWidget(item, DownloadCardWidget(data))
```

**채택 이유:**
- legacy `QListView.setIndexWidget()` 방식보다 안정적
- qfluentwidgets의 스무스 스크롤·테마 자동 적용
- `setDragDropMode(InternalMove)` 로 아이템 순서 드래그 정렬 가능
- 드롭 URL 수신은 `dragEnterEvent` / `dropEvent` 오버라이드로 처리

---

## DownloadCardWidget 설계

### 레이아웃 스케치

```
┌─[상태아이콘]──────────────────────────────────────[삭제]─┐
│ [썸네일]  [채널 프로필] 채널명   [VOD / 클립 뱃지]        │
│           제목 ← 클릭 시 LineEdit 인라인 편집             │
│           [144p] [360p] [720p] [1080p]  ← 해상도 선택   │
│           다운로드 경로  ← 클릭 시 인라인 편집  [📁]      │
│                                    [ProgressRing%] 상태 │
└──────────────────────────────────────────────────────────┘
```

### 위젯 구성 요소

| 위젯 | 타입 | 비고 |
|------|------|------|
| 상태 아이콘 (좌상단) | `IconWidget` | 상태별 아이콘 전환 |
| 썸네일 | `QLabel` | 비동기 로드, 고정 높이 |
| 채널 프로필 | `QLabel` | 비동기 로드, 원형 클립 |
| 채널명 | `CaptionLabel` | |
| 컨텐츠 타입 뱃지 | `PillPushButton` or `Badge` | VOD / 클립 |
| 제목 | `BodyLabel` ↔ `LineEdit` | 클릭 시 전환, WAITING만 편집 가능 |
| 해상도 버튼 | `ToggleButton` 그룹 | hover 툴팁에 파일 크기 |
| 경로 | `CaptionLabel` ↔ `LineEdit` | 클릭 시 전환, WAITING만 편집 가능 |
| 폴더 열기 | `TransparentToolButton` | 완료 후 파일 선택 탐색기 |
| ProgressRing | `ProgressRing` | RUNNING/PAUSED 상태만 표시 |
| 상태 텍스트 | `CaptionLabel` | 속도·남은시간·완료시간 |
| 삭제 버튼 | `TransparentToolButton` | RUNNING 중 disabled |

### 상태별 UI 동작

| 상태 | 상태 아이콘 | ProgressRing | 삭제 버튼 | 설명 |
|------|------------|--------------|-----------|------|
| WAITING | `FIF.PAUSE_BOLD` (회색) | 숨김 | 활성 | 해상도·경로 편집 가능 |
| RUNNING | `FIF.SYNC` (테마색, 회전) | 표시 + % | 비활성 | 속도·남은시간 표시 |
| PAUSED | `FIF.PAUSE` (주황) | 표시 + % | 비활성 | |
| FINISHED | `FIF.ACCEPT` (초록) | 숨김 | 활성 | 총 소요시간 표시 |
| ERROR | `FIF.CLOSE` (빨강) | 숨김 | 활성 | 에러 메시지 표시 |

---

## 드래그 앤 드롭

### 내부 정렬 (카드 순서 변경)
```python
listWidget.setDragDropMode(QListView.DragDropMode.InternalMove)
listWidget.setDefaultDropAction(Qt.MoveAction)
```

### 외부 URL 드롭 (URL 텍스트 붙여넣기)
```python
def dragEnterEvent(self, event):
    if event.mimeData().hasText():
        event.acceptProposedAction()

def dropEvent(self, event):
    url = event.mimeData().text().strip()
    # fetchRequested.emit(url)
```

---

## ContentItem 데이터 모델 (legacy 기준, 변경 예정)

```python
vod_url, title, thumbnail_url
channel_name, channel_image_url
category, live_open_date, duration
content_type: str          # "VOD" | "m3u8"
unique_reps: list          # [[resolution, base_url, size], ...]
resolution, base_url, total_size
download_path, output_path
download_size, download_progress (0~100)
download_speed, download_remain_time, download_time
downloadState: DownloadState
post_process: bool
```
