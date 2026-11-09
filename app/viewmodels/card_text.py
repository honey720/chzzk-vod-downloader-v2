"""목록 카드에 적는 글(상태 · 크기 · 구간 요약 · 경고)을 위젯 없이 만든다 (#545).

카드 위젯(`app/widgets/widget.py`)과 카드 편집 창이 같은 함수를 쓴다. 함수는 `ContentItem`
하나에서 글을 만들고 위젯 속성을 읽지 않는다 — 위젯 없이 테스트할 수 있다.

번역 컨텍스트는 카드 위젯과 같은 `ContentItemWidget`이다. 글을 이 모듈로 옮겨도 `.ts`의
번역 항목이 그대로 남도록 `QCoreApplication.translate("ContentItemWidget", …)`를 글자
그대로 쓴다 — lupdate는 리터럴 호출만 줍는다(project.json이 이 파일을 스캔한다).
"""

import os
from dataclasses import dataclass

from PySide6.QtCore import QCoreApplication

from app.viewmodels.clock_text import clock_text
from app.viewmodels.data import (
    SECTION_CHECK_PENDING,
    SECTION_CHECK_UNVERIFIED,
    ContentItem,
)
from app.viewmodels.item_state import ItemState
from app.viewmodels.section_edit import format_fps
from core.models.download_state import DownloadState

# 3행 슬롯 텍스트의 상태 접두(완료/실패 표시). 텍스트 안에 들어가는 글자라
# 폰트 문자를 쓴다 — 둘 다 기본 문장부호 블록(Dingbats)의 흔한 글리프다.
# 색은 파이썬이 아니라 전역 QSS `#statusLabel[state=...]`가 theme.py
# 토큰으로 정한다.
STATE_ICON = {
    "finished": "✓",  # 체크 — 완료 (U+2713)
    "failed": "✕",  # 엑스 — 실패 (U+2715)
}

# 구간 길이의 합이 합친 범위의 길이보다 이만큼(초) 넘게 길어야 겹침으로 본다 — 1ms.
# 가장 짧은 프레임(120fps ≈ 8.3ms)보다 훨씬 짧고, 소수 초 덧셈의 오차보다는 훨씬 길다
OVERLAP_SLACK = 0.001


@dataclass(frozen=True)
class SlotTexts:
    """카드 3줄 왼쪽 상태 글과 오른쪽 크기 글 — `slot_texts`가 돌려준다.

    None인 칸은 위젯이 지금 글을 그대로 둔다(실패 카드의 크기 글은 숨겨져 바꾸지 않는다).
    """

    status: str  # 상태 글(대기 카드에서도 값은 둔다 — 라벨은 pill에 자리를 내주고 숨는다)
    size: str | None  # 크기 · 구간 요약 글. None이면 바꾸지 않는다
    status_tip: str | None = None  # 상태 글의 툴팁. None이면 라벨 기본(글 그대로)


def short_time(remain: str) -> str:
    """ "HH:MM:SS" 시간을 짧은 표시("3:12")로 줄인다 — 표시 정책.

    카드의 진행 중 남은 시간 · 완료의 소요 시간(#245)과 하단바의 남은 시간(#504)이 같은 표기를
    쓴다. 계산 자체는 viewmodel(app/viewmodels/download_viewmodel.py)이 ProgressEvent 값으로
    이미 해 둔 것을 받는다(core 무관) — 여기서는 표기만 줄인다.
    형식이 예상 밖이면("N/A" 등) 받은 그대로 보여준다.
    """
    parts = remain.split(":")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return remain
    hours, minutes, seconds = (int(p) for p in parts)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def size_text(size) -> str:
    """바이트 수를 「595.34 MB」 꼴로 적는다(1024 단위, 소수 두 자리). 숫자가 아니면 「0 B」."""
    try:
        size = float(size)
    except (ValueError, TypeError):
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    unit_index = 0
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1
    return f"{size:.2f} {units[unit_index]}"


def clock(seconds: float) -> str:
    """초를 짧은 시간 표기("3:12" · "1:02:03")로 적는다 — 하루를 넘어도 시간을 그대로 센다."""
    whole = int(max(seconds, 0))
    return short_time(clock_text(whole))


# ---- 해상도 ----


def rep_label(rep) -> str:
    """해상도 버튼의 본 글자 — "1080p", 원본이면 "1080p(원본)" (#318).

    치지직 재생기의 화질 메뉴와 같은 모양이다. 프레임률은 본 글자에 넣지 않는다 —
    `rep_secondary`가 보조 글자로 준다.
    """
    label = f"{rep[0]}p"
    if ContentItem.rep_is_original(rep):
        label += QCoreApplication.translate("ContentItemWidget", "(source)")
    return label


def rep_secondary(rep) -> str:
    """해상도 버튼의 보조 글자 — 50fps 이상이면 "60fps", 아니면 빈 문자열 (#318)."""
    frame_rate = ContentItem.rep_frame_rate(rep)
    return f"{frame_rate}fps" if frame_rate is not None else ""


def resolution_text(item: ContentItem) -> str:
    """확정된 해상도의 표시 — "720p", 짧은 변이 같은 항목이 둘이고 원본을 골랐으면 "720p(원본)" (#318).

    원본 표시를 붙이는 조건은 파일명과 같다(ContentItem.resolution_tag). pill이 사라진
    뒤에는 고른 것이 둘 중 어느 쪽인지 알 표면이 이 글자뿐이다. 해상도마다 항목이
    하나인 영상은 숫자만으로 정해지므로 붙이지 않는다.
    """
    mark = (
        QCoreApplication.translate("ContentItemWidget", "(source)") if item.resolution_tag else ""
    )
    return f"{item.resolution}p{mark}"


def with_resolution(item: ContentItem, text: str) -> str:
    """확정 해상도를 크기 앞에 붙인다 — "1080p · 595.34 MB" (#245).

    대기에서는 pill이 선택을 보여주므로 붙이지 않는다(중복). 해상도가
    아직 없으면(조회 중 자리표시 등) 크기만 돌려준다.
    """
    if not item.resolution or item.downloadState == DownloadState.WAITING:
        return text
    shown_resolution = resolution_text(item)
    return f"{shown_resolution} · {text}" if text else shown_resolution


# ---- 구간 ----


def section_count(item: ContentItem) -> int:
    """이 카드가 받을 구간 수 — 전체 다운로드면 0."""
    return len(getattr(item, "selections", ()) or ())


def section_tally(item: ContentItem) -> str:
    """상태 문구에 붙이는 완료 구간 수 — " · 2/3". 붙이지 않을 때는 빈 문자열이다.

    구간이 둘보다 적으면 붙이지 않는다 — 구간이 하나인 카드와 전체 다운로드 카드의
    문구는 구간 표시가 없던 때와 같다. 전송 중(진행 · 일시정지)에도 붙이지 않는다 —
    구간은 전송이 모두 끝난 뒤 컷 단계에서 하나씩 끝나므로 그 전에는 늘 0이다. 컷
    단계 · 완료 · 실패에서 붙인다.
    """
    total = section_count(item)
    if total < 2:
        return ""
    transferring = item.downloadState in (DownloadState.RUNNING, DownloadState.PAUSED)
    if transferring and not item.post_process:
        return ""
    return f" · {item.sections_done}/{total}"


def section_total(item: ContentItem) -> float:
    """구간 길이의 합(초) — 겹치는 자리를 구간마다 센다."""
    return sum(max(s.end - s.start, 0.0) for s in item.selections)


def section_span(item: ContentItem) -> float:
    """구간들을 합친 범위의 길이(초) — 겹치는 자리를 한 번만 센다. 받을 양이다."""
    length = 0.0
    reached = None  # 지금까지 센 범위의 끝
    for selection in sorted(item.selections, key=lambda s: (s.start, s.end)):
        start = selection.start if reached is None else max(selection.start, reached)
        if selection.end > start:
            length += selection.end - start
            reached = selection.end
    return length


def sections_overlap(item: ContentItem) -> bool:
    """구간끼리 겹치는 자리가 있는지 — 길이의 합이 합친 범위의 길이보다 길다 (#309).

    맞닿기만 한 구간(앞의 끝 = 뒤의 시작)은 겹침이 아니다. 판정은 프레임 하나보다 훨씬 짧은
    여유를 둔다 — 소수 초의 덧셈 오차를 겹침으로 읽지 않는다.
    """
    return section_total(item) - section_span(item) > OVERLAP_SLACK


def section_overlap_detail(item: ContentItem) -> list[str]:
    """겹침이 있는 구간 카드의 툴팁에 더하는 줄 — 받을 양(길이) · 구간 길이의 합 · 한 번만 받는다.

    받을 양은 길이로만 적는다. 카드의 크기는 구간 파일 크기의 합(겹치는 자리를 구간마다
    센다)이라 받을 양의 크기가 아니다.
    """
    if not sections_overlap(item):
        return []
    return [
        QCoreApplication.translate("ContentItemWidget", "To receive: {0}").format(
            clock(section_span(item))
        ),
        QCoreApplication.translate("ContentItemWidget", "Sections add up to {0}").format(
            clock(section_total(item))
        ),
        QCoreApplication.translate("ContentItemWidget", "Overlapping parts are received once"),
    ]


def section_notice(item: ContentItem, warnings_only: bool = False) -> str:
    """구간 요약 뒤에 붙이는 알림 — 해상도를 바꿔 구간에 일어난 일을 짧게 적는다 (#309).

    재료는 뷰모델이 아이템에 적어 둔 값이다(``section_refit_fps`` · ``section_end_pulled`` ·
    ``section_end_extended`` · ``section_unfit`` · ``section_check``). 알릴 것이 없으면 빈 문자열이다.

    알림은 두 무게다. **한 일을 알리는 것**(프레임에 맞춤 · 끝을 옮김)과 **유저가 봐야 하는
    것**(길이를 벗어난 구간 · 길이 미확인)이다. 폭이 모자라면 카드가 앞의 것부터 뗀다.

    Args:
        warnings_only: 유저가 봐야 하는 것만 적을지
    """
    parts = []
    refit_fps = getattr(item, "section_refit_fps", None)
    if refit_fps is not None and not warnings_only:
        parts.append(
            QCoreApplication.translate("ContentItemWidget", "refit to {0}fps").format(
                format_fps(refit_fps)
            )
        )
    if getattr(item, "section_end_pulled", False) and not warnings_only:
        parts.append(
            QCoreApplication.translate("ContentItemWidget", "end pulled to the video length")
        )
    if getattr(item, "section_end_extended", False) and not warnings_only:
        parts.append(
            QCoreApplication.translate("ContentItemWidget", "end extended to the video length")
        )
    unfit = len(getattr(item, "section_unfit", ()))
    if unfit:
        parts.append(
            QCoreApplication.translate("ContentItemWidget", "{0} outside the video").format(unfit)
        )
    check = getattr(item, "section_check", "")
    if check == SECTION_CHECK_PENDING:
        parts.append(QCoreApplication.translate("ContentItemWidget", "checking length"))
    elif check == SECTION_CHECK_UNVERIFIED:
        parts.append(QCoreApplication.translate("ContentItemWidget", "length not checked"))
    return " · ".join(parts)


def section_notice_detail(item: ContentItem) -> list[str]:
    """알림의 전문 — 툴팁에 한 줄씩 적는다. 좁은 폭에서 알림을 떼도 여기에는 남는다."""
    lines = []
    if getattr(item, "section_refit_fps", None) is not None:
        lines.append(
            QCoreApplication.translate(
                "ContentItemWidget", "Sections were moved to the frames of the new frame rate."
            )
        )
    if getattr(item, "section_end_pulled", False):
        lines.append(
            QCoreApplication.translate(
                "ContentItemWidget",
                "This resolution is shorter. Sections now end at the end of the video.",
            )
        )
    if getattr(item, "section_end_extended", False):
        lines.append(
            QCoreApplication.translate(
                "ContentItemWidget",
                "This resolution is longer. Sections that reached the end now reach it.",
            )
        )
    if getattr(item, "section_unfit", ()):
        lines.append(
            QCoreApplication.translate(
                "ContentItemWidget",
                "Some sections start after the end of this resolution. "
                "They will be skipped. Edit the sections to fix them.",
            )
        )
    check = getattr(item, "section_check", "")
    if check == SECTION_CHECK_PENDING:
        lines.append(
            QCoreApplication.translate(
                "ContentItemWidget", "Checking the length of this resolution..."
            )
        )
    elif check == SECTION_CHECK_UNVERIFIED:
        lines.append(
            QCoreApplication.translate(
                "ContentItemWidget",
                "Could not check the length of this resolution. "
                "Open the section editor to check again.",
            )
        )
    return lines


def section_size_text(item: ContentItem) -> str:
    """구간 카드의 크기 — 저장소에 생길 구간 파일 크기의 합(추정). 모르면 빈 글이다 (#309).

    인코딩 완료 VOD에서 그 값을 알 때만 적는다. 겹치는 자리는 구간마다 센 값이다 — 구간마다
    파일이 하나씩 생긴다. 대기 · 준비 중 · 받는 중 · 일시정지 · 자르기에서 같은 값을 적는다.
    완료하면 실제 값으로 바뀐다(``finished_size_text``). 진행률은 이 값이 아니라 엔진이 세는
    전송량으로 계산한다(``download_viewmodel``).

    세그먼트 방식(fMP4 · TS)은 받기 전에 크기를 모른다. 영상 전체 크기를 대신 적지 않는다.
    """
    size = getattr(item, "section_bytes", None)
    if item.is_segment_based or not size:
        return ""
    return size_text(size)


def section_size_note(item: ContentItem) -> str:
    """대기 카드의 구간 요약에 적는 크기 자리의 글 — 받을 크기, 세는 중이면 "확인 중...".

    받을 크기는 구간을 확인한 뒤 백그라운드에서 센다(``section_sizing``). 그동안 자리를
    비워 두지 않고 카드의 크기 조회가 쓰는 문구를 그대로 쓴다. 세지 못했으면 빈 글이다 —
    받기 시작 때 엔진이 정한 값이 들어온다.
    """
    size = section_size_text(item)
    if size or not getattr(item, "section_sizing", False):
        return size
    return QCoreApplication.translate("ContentItemWidget", "Checking...")


def section_summary(
    item: ContentItem, with_notice: bool = True, warnings_only: bool = False, with_size: bool = True
) -> str:
    """대기 카드의 재생 시간 자리에 적는 구간 요약 (#309).

    - 세그먼트 방식(fMP4 · TS): 구간 수와 길이 — "구간 2개 · 20:00". 받을 크기를 미리 알 수
      없어 길이로 양을 알린다. 길이는 남은 시간과 같은 짧은 표기("3:12" · "1:02:03")다
    - 인코딩 완료 VOD(mp4): 구간 수와 크기 — "구간 2개 · 200.00 MB". 길이는 적지 않는다 —
      카드 툴팁과 편집 창의 합계에 있다

    구간을 다시 맞춘 알림이 있으면 그 뒤에 붙인다 — 좁은 폭에서는 카드가 알림과 크기를 뗀다.
    """
    selections = item.selections
    marker = (
        QCoreApplication.translate("ContentItemWidget", "overlap") if sections_overlap(item) else ""
    )
    if item.is_segment_based:
        # 길이는 받을 양이다 — 겹치는 자리는 한 번만 받으므로 합친 범위의 길이를 적는다.
        # 겹침이 없으면 구간 길이의 합과 같다
        span = clock(section_span(item))
        if marker:
            span = f"{marker} · {span}"  # f-string 안의 tr은 lupdate가 줍지 못한다
        summary = QCoreApplication.translate("ContentItemWidget", "Sections {0} · {1}").format(
            len(selections), span
        )
    else:
        summary = QCoreApplication.translate("ContentItemWidget", "Sections {0}").format(
            len(selections)
        )
        if marker:
            summary = f"{summary} · {marker}"
    size = section_size_note(item) if with_size else ""
    if size:
        summary = f"{summary} · {size}"
    notice = section_notice(item, warnings_only) if with_notice else ""
    return f"{summary} · {notice}" if notice else summary


# ---- 크기 ----


def transfer_fraction(item: ContentItem) -> float:
    """전송의 진행(0~1) — 받은 바이트 ÷ 엔진이 받을 바이트. 아직 모르면 0이다."""
    total = getattr(item, "transfer_bytes", None)
    try:
        received = float(item.download_size)
    except (ValueError, TypeError):
        return 0.0
    if not total or total <= 0:
        return 0.0
    return min(max(received / total, 0.0), 1.0)


def whole_size_text(item: ContentItem) -> str:
    """인코딩 완료 VOD 전체의 크기 글 — 모르면 빈 글이다.

    카드의 크기 조회가 끝났으면 그 값(``total_size``)이다. 끝나지 않았거나 실패했으면 그
    자리에 "확인 중..." 같은 안내 글이 들어 있다 — 크기로 쓰지 않고, 받기 시작 때 엔진이
    정한 크기(``transfer_bytes``)가 있으면 그것을 쓴다.
    """
    known = f"{item.total_size}" if item.total_size else ""
    if known[:1].isdigit():
        return known
    transfer = getattr(item, "transfer_bytes", None)
    return size_text(transfer) if transfer else ""


def received_size_text(item: ContentItem, with_total: bool = True) -> str:
    """진행·일시정지 카드의 크기 표기 — "받은 크기 / 받을 크기".

    - 세그먼트 기반(fMP4 · TS): 받은 크기만 — 받을 크기를 미리 알 수 없다
    - 인코딩 완료 VOD 전체: 받은 크기 / 파일 크기
    - 인코딩 완료 VOD 구간 (#309): 진행률 × 크기 / 크기. 크기는 대기 카드가 적던 값
      그대로다(구간 파일 크기의 합의 추정 — ``section_size_text``). 왼쪽은 받은 바이트가
      아니라 그 크기에 전송의 진행(받은 바이트 ÷ 엔진이 받을 바이트)을 곱한 값이다 —
      0에서 시작해 전송이 끝나면 두 숫자가 같아진다. 엔진이 받을 바이트를 아직 모르면
      (준비 중) 0이다. 자르는 동안에는 왼쪽도 그 크기다 — 전송은 끝났다
    받을 크기를 모르면 받은 크기만 적는다.

    Args:
        with_total: False면 받을 크기를 알아도 받은 크기만 적는다 — 좁은 폭의 짧은 표기
    """
    received = size_text(item.download_size)
    if item.is_segment_based:
        return received
    if section_count(item):
        total = section_size_text(item)
        if total and item.post_process:
            received = total
        elif total:
            shown = item.section_bytes * transfer_fraction(item)
            received = size_text(shown) if shown > 0 else size_text("")
    else:
        total = whole_size_text(item)
    if not with_total:
        return received
    return f"{received} / {total}" if total else received


def finished_size_text(item: ContentItem) -> str:
    """완료 카드의 크기 — 저장된 파일의 크기다. 구간 다운로드는 구간 파일 크기의 합 (#309).

    엔진이 완료하며 실제 파일에서 읽어 준 값(``output_bytes``)을 적는다 — 받은 양이 아니다.
    세그먼트를 다시 포장하거나 구간으로 자른 파일은 받은 양과 크기가 다르다. 그 값이
    없으면(앱을 다시 띄운 카드 등) 마지막으로 통지받은 받은 양이다.
    """
    made = getattr(item, "output_bytes", None)
    if made:
        return size_text(made)
    return size_text(item.download_size)


# ---- 상태 ----


def is_preparing(item: ContentItem) -> bool:
    """카드가 "준비 중"을 보일 때인지 — 받는 중이고 뷰모델이 준비가 길다고 알렸다 (#309)."""
    return item.downloadState == DownloadState.RUNNING and bool(getattr(item, "preparing", False))


def has_progress(item: ContentItem) -> bool:
    """진행분이 있는 상태인가 — 진행·일시정지. 진행바는 이때만 보인다.

    "진행 중일 때만"이 아니다(#245 정정) — 일시정지는 멈췄을 뿐 받은
    양이 있고, 그 양을 바가 계속 보여줘야 한다(색만 muted로 바꿔
    "돌고 있지 않다"를 알린다). 완료(100%)·실패는 바가 정보를 더하지
    않으므로 숨긴다.
    """
    return item.downloadState in (DownloadState.RUNNING, DownloadState.PAUSED)


def card_state(item: ContentItem) -> str:
    """현재 아이템 상태를 카드 상태 어휘(theme.CARD_STATES)로 옮긴다.

    PAUSED는 자기 색("paused" — 진행 파랑의 채도를 뺀 muted)을 가진다 —
    진행바가 남아 있어야 하는 상태라 "돌고 있지 않다"가 색으로 읽혀야
    한다(#245). LOADING(조회 중)은 대기 회색이다.
    """
    state = item.downloadState
    if state == DownloadState.RUNNING:
        return "running"
    if state == DownloadState.PAUSED:
        return "paused"
    if state == DownloadState.FINISHED:
        return "finished"
    if state == DownloadState.FAILED:
        return "failed"
    return "waiting"


def progress_value(item: ContentItem) -> int:
    """진행바에 넣을 0~100 값. 아직 시작 전(대기·조회 중)이면 0이다."""
    if item.downloadState in (DownloadState.WAITING, ItemState.LOADING):
        return 0
    try:
        return max(0, min(100, int(item.download_progress)))
    except (TypeError, ValueError):
        return 0


def open_target(item: ContentItem) -> str:
    """폴더 열기가 열 경로 — 파일이면 그 파일을 선택한 채 폴더를 열고, 폴더면 폴더를 연다.

    - 대기 카드: 저장 폴더
    - 전체 다운로드 카드: 산출물 파일(``output_path``)
    - 구간 다운로드 카드 (#309): 실제로 만들어진 구간 파일 가운데 **번호가 가장 작은 것**.
      하나도 없으면 저장 폴더. 구간 다운로드는 ``output_path``에 쓰지 않는다 — 그 이름의
      파일은 없다
    """
    if item.downloadState == DownloadState.WAITING:
        return item.download_path
    if section_count(item):
        made = [path for path in getattr(item, "section_paths", ()) if os.path.isfile(path)]
        return made[0] if made else item.download_path
    return item.output_path


def slot_texts(item: ContentItem) -> SlotTexts:
    """카드 3줄의 상태 글 · 크기 글 · 상태 툴팁 — 상태마다 다르다 (#245).

    유저가 알고 싶은 것은 상태마다 다르다 — 대기 "어느 화질로?"(pill),
    진행 "얼마나?"(%·속도·남은시간), 일시정지 "어디까지 받고 멈췄나?"
    (%·일시정지됨), 완료 "어디에?"(✓ 완료 + 폴더 버튼), 실패 "왜?"(✕ 사유).

    3행 우측(파일 크기)에는 pill이 사라진 뒤(대기 이후)부터 **확정된
    해상도**를 함께 적는다 — "1080p · 595.34 MB". 같은 자리에 붙이므로
    행이 늘지 않는다.

    ⚠️ 번역 호출은 f-string 밖에 둔다 — pyside6-lupdate는 f-string 중괄호 안의 번역 호출을
    못 읽어 -no-obsolete 재생성에서 항목이 지워진다(실측).
    """
    state = item.downloadState
    if state == ItemState.LOADING:
        return SlotTexts(
            QCoreApplication.translate("ContentItemWidget", "Loading information..."), ""
        )

    if state == DownloadState.WAITING:
        # 슬롯은 해상도 pill이 차지한다 — statusLabel은 숨겨지지만
        # 값은 유지한다(테스트·툴팁 등 텍스트 조회 경로 보존)
        status = QCoreApplication.translate("ContentItemWidget", "Download waiting")
        if section_count(item):
            # 구간 다운로드는 재생 시간 자리에 구간 요약을 적는다 (#309)
            return SlotTexts(status, section_summary(item))
        if item.is_segment_based:
            # 길이를 모르면 칸을 비운다(#515) — 응답의 길이가 null이면 None, 키가 없으면 0이다
            return SlotTexts(status, clock_text(item.duration or None))
        return SlotTexts(status, f"{item.total_size}")

    if state == DownloadState.RUNNING:
        if is_preparing(item):
            # 엔진이 받을 것을 정하는 중 (#309) — 진행률 · 속도가 아직 없다. 막대는 값 없이 움직인다
            status = QCoreApplication.translate("ContentItemWidget", "Preparing")
        elif section_count(item) and item.post_process:
            # 구간을 자르는 단계 (#309) — 속도 · 남은 시간은 뜻이 없어 적지 않고 단계
            # 문구를 적는다. 막대는 전송과 컷을 합친 하나다.
            # 자르기 전에 받은 세그먼트의 프레임 정보를 읽는 동안은 준비 중임을 적는다 —
            # 세그먼트가 많으면 그 시간이 길다
            if getattr(item, "cut_preparing", False):
                cutting_text = QCoreApplication.translate("ContentItemWidget", "Preparing to cut")
            else:
                cutting_text = QCoreApplication.translate("ContentItemWidget", "Cutting")
            status = f"{item.download_progress}% · {cutting_text}{section_tally(item)}"
        elif item.is_segment_based and item.post_process:
            # "13% · 후처리 중" — 후처리 진행률(download_progress는 후처리에서
            # 0부터 다시 차오른다)을 **앞에** 둔다. 전송 "42% · …"·일시정지
            # "13% · 일시정지됨"과 자리를 맞춰 상태가 바뀌어도 퍼센트 위치가
            # 흔들리지 않게(#245)
            postprocess_text = QCoreApplication.translate("ContentItemWidget", "Post-processing")
            status = f"{item.download_progress}% · {postprocess_text}"
        else:
            remain = short_time(item.download_remain_time)
            status = (
                f"{item.download_progress}% · {item.download_speed} · "
                + QCoreApplication.translate("ContentItemWidget", "{0} left").format(remain)
                + section_tally(item)
            )
        return SlotTexts(status, with_resolution(item, received_size_text(item)))

    if state == DownloadState.PAUSED:
        # 진행분이 먼저, 상태가 뒤 — "54% · 일시정지됨". 진행 슬롯과 같은
        # 자리에서 숫자가 그대로 이어지고, 뒤 문구만 바뀐다.
        paused_text = QCoreApplication.translate("ContentItemWidget", "Paused")
        if getattr(item, "prepare_pending", False):
            # 준비 중에 일시정지했다 (#309) — 아직 받은 것이 없어 진행률을 적지 않는다.
            # 준비가 끝나면 받기를 띄우지 않고 일시정지 상태로 서고, 그때부터 "0% · …"다
            status = paused_text
        else:
            status = f"{item.download_progress}% · {paused_text}{section_tally(item)}"
        return SlotTexts(status, with_resolution(item, received_size_text(item)))

    if state == DownloadState.FINISHED:
        # "✓ 완료 · 2:12" — 소요 시간은 진행 중의 남은 시간과 같은 짧은
        # 포맷(#245). 값은 viewmodel(app/viewmodels/download_viewmodel.py)이 엔진의
        # start_time~end_time으로 만든 "HH:MM:SS"이고, end_time은 후처리가
        # 끝난 뒤 찍히므로 유저가 체감하는 전체(로그의 "Download completed")다.
        # 값이 없으면(앱 재시작 복원 등) 시간 없이 "✓ 완료"만.
        completed = (
            f"{STATE_ICON['finished']} "
            + QCoreApplication.translate("ContentItemWidget", "Completed")
            + section_tally(item)
        )
        elapsed = short_time(item.download_time) if item.download_time else ""
        status = f"{completed} · {elapsed}" if elapsed else completed
        return SlotTexts(status, with_resolution(item, finished_size_text(item)))

    # 실패 — 사유(stateMessage)는 키 기반 매핑을 거친 번역 문자열만 온다. 매핑 밖 예외는
    # 사유가 비어 온다. SPEC §5의 "str(e) 폴백 없음"은 내부 정보(원시 HTTP 오류·경로) 노출을
    # 막기 위한 것이지 침묵이 아니다 — 틀린 문구도 없는 문구도 진단을 막는다(#183 교훈). 틀리지
    # 않으면서 다음 행동(로그 폴더 열기 #181)을 주는 안내를 띄운다(#245).
    reason = getattr(item, "stateMessage", "")
    text = (
        reason
        if reason
        else QCoreApplication.translate("ContentItemWidget", "Unknown error - check the log")
    )
    # 사유 문구는 **첫 줄이 핵심 한 줄**(무엇을 해야 하는지 포함), 둘째 줄부터는 상세다 —
    # 번역 문자열이 줄바꿈으로 나눈다. 3행에는 첫 줄만 올리고 전문은 툴팁으로(#245).
    headline = text.splitlines()[0] if text else text
    failed_sections = getattr(item, "sections_failed", 0)
    if section_count(item) >= 2 and failed_sections:
        # 일부 구간만 자르지 못했다 (#309) — 실패한 구간 수와 완료 구간 수를 사유 앞에 적는다
        tally = QCoreApplication.translate("ContentItemWidget", "{0} failed").format(
            failed_sections
        ) + section_tally(item)
        headline = f"{tally} · {headline}"
    return SlotTexts(f"{STATE_ICON['failed']} {headline}", None, text)
