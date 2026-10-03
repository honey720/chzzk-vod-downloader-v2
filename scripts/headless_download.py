"""GUI 없이 VOD/클립을 다운로드하는 헤드리스 스크립트 (#52, #75).

"core는 CLI·다른 UI로 교체 가능해야 한다"(SPEC §3.1)를 실물로 검증하고,
향후 E2E 자동 테스트의 진입점을 제공한다. 새 다운로드 로직을 작성하지 않고
core 파이프라인(metadata_service → DownloadService → 다운로더 엔진)을 그대로
재사용한다.

사용법:
    uv run python scripts/headless_download.py <VOD/클립 URL> [옵션]

옵션:
    --resolution N   원하는 해상도(예: 720). 생략 시 최고 화질(auto)
    --output PATH    저장 폴더 (생략 시 현재 작업 디렉토리)
    --timeout SEC    다운로드 제한 시간(초). 초과 시 실패로 종료 (기본 600, 최대 7200)
    --list           다운로드하지 않고 사용 가능한 해상도와 그 프레임률을 출력
    --section A-B    받을 구간. 타임코드 HH:MM:SS:FF-HH:MM:SS:FF, 여러 번 줄 수 있다 (#309).
                     구간마다 `{제목} {해상도}p_N.mp4` 파일이 하나씩 생긴다(N은 준 순서).
                     인코딩이 끝난 VOD(mp4)만 받는다

예)
    uv run python scripts/headless_download.py https://chzzk.naver.com/clips/xxxx
    uv run python scripts/headless_download.py https://chzzk.naver.com/video/123 --resolution 720
    uv run python scripts/headless_download.py https://chzzk.naver.com/video/123 \
        --section 00:10:05:00-00:10:16:00 --section 00:42:00:12-00:42:31:00

종료 코드:
    0  다운로드 성공
    1  다운로드 실패(네트워크·타임아웃 등)
    2  잘못된 인자·URL, 또는 조회 실패(권한/암호화 등)

Qt 의존에 대하여:
    #75에서 DownloadService(콜백 계약) 기준으로 갱신하면서 Qt 의존이 완전히
    사라졌다 — 구 버전이 쓰던 QCoreApplication·QTimer·Signal 큐 연결이 필요
    없다. 완료·실패는 워커 스레드 콜백으로 받고, 메인 스레드는 핸들 대기
    (handle.wait)로 동기화한다. 조회도 ContentWorker(Qt) 대신
    core/services/metadata_service.py를 직접 호출한다.
"""

import argparse
import logging
import os
import sys
from pathlib import Path
from time import gmtime, strftime

# scripts/ 하위에서 실행해도 저장소 루트 모듈을 import할 수 있게 한다
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config.config as config  # noqa: E402
from app.log_setup import setup_logging  # noqa: E402
from app.viewmodels.data import ContentItem  # noqa: E402
from app.network import NetworkManager  # noqa: E402
from fractions import Fraction  # noqa: E402

from core.api.mp4 import Mp4Error, fetch_mp4_head  # noqa: E402
from core.models.mp4_index import Mp4Head  # noqa: E402
from core.models.events import ProgressEvent  # noqa: E402
from core.models.plan import TimeRange  # noqa: E402
from core.services import metadata_service  # noqa: E402
from core.services.download_service import DownloadService  # noqa: E402
from core.services.metadata_service import MetadataError  # noqa: E402
from core.utils.mp4_ranges import selection_byte_ranges  # noqa: E402
from core.utils.paths import build_output_path, build_section_output_paths  # noqa: E402
from core.utils.selections import validate_selections  # noqa: E402
from core.utils.timecode import (  # noqa: E402
    TIMECODE_INVALID_FORMAT,
    TimecodeError,
    format_milliseconds,
    parse_timecode,
)
from core.models.download_data import DownloadData  # noqa: E402
from app.download_logger import DownloadLogger  # noqa: E402
from app.download_resolvers import resolve_aes_key, resolve_m3u8_base_url  # noqa: E402
from app.download_task import DownloadTask  # noqa: E402

logger = logging.getLogger("headless")

TIMEOUT_DEFAULT = 600
TIMEOUT_MAX = 7200


def _load_cookies() -> dict:
    """앱과 동일하게 config.json에서 쿠키를 읽는다 (공개 컨텐츠는 빈 값이어도 무방)."""
    data = config.load_config().get("cookies", {})
    return {
        "NID_AUT": data.get("NID_AUT", ""),
        "NID_SES": data.get("NID_SES", ""),
    }


def _fetch(vod_url: str, cookies: dict, download_path: str):
    """core 메타데이터 서비스를 직접 호출해 (result, content_type)을 반환한다.

    에러 메시지 형식은 구 ContentWorker 경유("<url> | <메시지>")와 동일하다 —
    번역기 없는 환경이므로 i18n 키 원문(영문)이 그대로 출력된다.

    Returns:
        tuple[tuple, str] | None: 성공 시 (result, content_type), 실패 시 None
    """
    try:
        return metadata_service.fetch_content(vod_url, cookies, download_path, api=NetworkManager)
    except MetadataError as e:
        logger.error("조회 실패: %s", str(e).replace("\n", " | "))
        return None
    except Exception:
        logger.exception("조회 실패: %s", vod_url)
        return None


def _select_resolution(unique_reps: list, resolution: int | None):
    """unique_reps에서 원하는 해상도의 항목을 고른다.

    resolution이 None이면 최고 화질(목록의 마지막)을 쓴다. 해상도가 같은 항목이 둘이면
    뒤의 것(원본)을 고른다 — 기본 선택과 같은 규칙이다(#318). m3u8은 base_url이 None이며
    실제 URL은 다운로드 시작 시점에 resolver가 고른 항목의 스트림으로 해석한다.

    Returns:
        고른 항목(``[해상도, base_url]``). 매칭 실패 시 None
    """
    if resolution is None:
        return unique_reps[-1]
    matches = [rep for rep in unique_reps if rep[0] == resolution]
    return matches[-1] if matches else None


def _rep_name(rep: list) -> str:
    """목록 항목의 이름 — `1080p`, 원본이면 `1080p(원본)`. 카드의 해상도 버튼의 본 글자와 같다 (#318)."""
    name = f"{rep[0]}p"
    if ContentItem.rep_is_original(rep):
        name += "(원본)"
    return name


def _rep_label(rep: list) -> str:
    """목록 항목을 카드의 해상도 버튼과 같은 표시로 — `1080p(원본) · 60fps`.

    원본이면 `(원본)`을 붙이고, 50fps 이상이면 버튼의 보조 글자 자리에 오는 `60fps`를
    ` · ` 뒤에 적는다.
    """
    label = _rep_name(rep)
    frame_rate = ContentItem.rep_frame_rate(rep)
    if frame_rate is not None:
        label += f" · {frame_rate}fps"
    return label


def _build_item(result: tuple, content_type: str, resolution: int | None) -> ContentItem | None:
    """워커 결과로 ContentItem을 만들고 선택한 해상도를 반영한다."""
    vod_url, metadata, unique_reps, auto_resolution, auto_base_url, download_path, lrpj = result

    item = ContentItem(
        vod_url,
        metadata,
        unique_reps,
        auto_resolution,
        auto_base_url,
        download_path,
        content_type,
        lrpj,
    )

    selected = _select_resolution(unique_reps, resolution)
    if selected is None:
        available = ", ".join(_rep_label(rep) for rep in unique_reps)
        logger.error("해상도 %sp를 찾을 수 없습니다. 사용 가능: %s", resolution, available)
        return None
    item.select_rep(selected)

    # 조립·중복 회피는 core가 단일 지점으로 담당한다 — GUI(manager)와 동일 (#105)
    item.output_path = build_output_path(
        item.download_path, item.title, item.resolution, item.resolution_tag
    )
    return item


def _parse_sections(texts: list[str], fps) -> list[tuple[float, float]]:
    """`시작-끝` 타임코드 목록을 (시작, 끝) 초 쌍으로 바꾼다.

    Raises:
        TimecodeError: `-`로 나뉘지 않았거나 타임코드가 형식에 맞지 않는 경우
    """
    pairs = []
    for text in texts:
        start, separator, end = text.partition("-")
        if not separator:
            raise TimecodeError(TIMECODE_INVALID_FORMAT, text)
        pairs.append((parse_timecode(start, fps), parse_timecode(end, fps)))
    return pairs


def _resolve_sections(
    item: ContentItem, texts: list[str]
) -> tuple[tuple[TimeRange, ...], Mp4Head] | None:
    """구간 옵션을 검증해 TimeRange 목록으로 바꾼다. 받을 프레임과 크기를 로그로 남긴다.

    타임코드의 프레임 칸은 그 영상의 프레임률로 읽어야 하므로 moov를 먼저 받는다.
    받은 moov는 함께 돌려준다 — 엔진에 넘겨 다시 받지 않게 한다.

    Returns:
        (구간 목록, 받은 moov). 형식·검증 오류나 moov를 읽지 못한 경우 None
    """
    try:
        head = fetch_mp4_head(item.base_url)
        index = head.index
        pairs = _parse_sections(texts, index.fps)
    except TimecodeError as e:
        logger.error("구간 형식 오류: %s", e)
        return None
    except Mp4Error as e:
        logger.error("영상 색인을 읽지 못했습니다: %s", e)
        return None
    except Exception:
        logger.exception("영상 색인을 받지 못했습니다")
        return None

    violations = validate_selections(pairs, index.duration, index.fps)
    if violations:
        for number, keys in violations.items():
            logger.error("구간 %d (%s): %s", number + 1, texts[number], ", ".join(keys))
        return None

    selections = tuple(TimeRange(start, end) for start, end in pairs)
    for number, (text, selection) in enumerate(zip(texts, selections), start=1):
        picked = selection_byte_ranges(index, selection)
        logger.info(
            "구간 %d: %s -> 프레임 %d~%d (첫 프레임 %s · 끝 프레임 %s), 받을 범위 %s bytes",
            number,
            text,
            picked.first_frame,
            picked.last_frame,
            format_milliseconds(index.frame_pts[picked.first_frame]),
            format_milliseconds(index.frame_pts[picked.last_frame]),
            f"{picked.total_size:,}",
        )
    return selections, head


class _HeadlessRunner:
    """DownloadService를 구동하고 완료/실패/타임아웃을 종료 코드로 환원한다."""

    def __init__(
        self,
        item: ContentItem,
        timeout: int,
        selections: tuple[TimeRange, ...] = (),
        mp4_head: Mp4Head | None = None,
    ) -> None:
        self.item = item
        self.timeout = timeout
        self.selections = selections
        self.mp4_head = mp4_head  # 구간을 해석하며 받은 moov — 엔진이 다시 받지 않게 넘긴다
        self.section_paths: tuple[str, ...] = ()
        self.exit_code = 1  # 완료 신호를 받기 전까지는 실패로 간주
        self.service = DownloadService(
            base_url_resolver=resolve_m3u8_base_url, key_resolver=resolve_aes_key
        )
        self.task: DownloadTask | None = None

    def run(self) -> int:
        """다운로드를 제출하고 끝날 때까지 대기한 뒤 종료 코드를 반환한다."""
        data = DownloadData(
            self.item.base_url,
            self.item.vod_url,
            self.item.output_path,
            self.item.resolution,
            self.item.content_type,
        )
        if self.selections:
            # 구간 파일명은 시작할 때 한꺼번에 배정한다 — 예약은 엔진이 끝날 때 푼다 (#309)
            self.section_paths = build_section_output_paths(
                self.item.download_path, self.item.title, self.item.resolution, len(self.selections)
            )
            data.content.selections = self.selections
            data.content.selection_paths = self.section_paths
            data.content.mp4_head = self.mp4_head
        # 고른 변형을 다시 찾는 값 (#318) — 스트림 값이 없는 아이템은 해상도로 찾는다
        data.content.stream = getattr(self.item, "stream", None)
        task_logger = DownloadLogger()
        # GUI 브리지와 동일하게 상태 전이 흡수·다운로드 정보 로깅은 태스크 어댑터가 담당
        self.task = DownloadTask(data, self.item, task_logger)
        self.task.start()

        logger.info(
            "다운로드 시작: %s (%sp) -> %s",
            self.item.title,
            self.item.resolution,
            " · ".join(self.section_paths) if self.section_paths else self.item.output_path,
        )
        handle = self.service.submit(
            data.content,
            data=data,
            task_logger=task_logger,
            on_progress=lambda event: self._on_progress(event, data),
            on_finished=self._on_finished,
            on_failed=self._on_failed,
            on_merge_start=self._on_merge_start,
        )

        # 타임아웃: 상한을 넘으면 중지 후 실패로 종료 (구 QTimer 역할)
        if not handle.wait(self.timeout):
            logger.error("제한 시간(%d초) 초과 — 다운로드를 중단합니다.", self.timeout)
            self.exit_code = 1
            self.task.stop()
            handle.wait()  # 워커 정리 대기
        return self.exit_code

    def _on_progress(self, event: ProgressEvent, data: DownloadData) -> None:
        """진행 상황을 stdout 로그로 남긴다 (워커 스레드에서 호출)."""
        rem, size, spd, prog = _format_progress(event, data, self.item)
        logger.info("진행률 %3s%% | 속도 %s | 남은시간 %s | 누적 %s bytes", prog, spd, rem, size)

    def _on_finished(self) -> None:
        """정상 완료: 결과 파일 크기를 로그로 남기고 성공 코드를 기록한다.

        구간 다운로드는 구간 파일마다 한 줄씩 남기고, 모두 있어야 성공이다.
        """
        download_time = strftime(
            "%H:%M:%S", gmtime(self.task.data.end_time - self.task.data.start_time)
        )
        sizes = []
        for path in self.section_paths or (self.item.output_path,):
            size = os.path.getsize(path) if os.path.exists(path) else 0
            sizes.append(size)
            logger.info("다운로드 완료: %s (%s bytes, 소요 %s)", path, f"{size:,}", download_time)
        self.exit_code = 0 if all(size > 0 for size in sizes) else 1

    def _on_failed(self, exc: BaseException) -> None:
        """다운로드 실패: 실패 코드를 기록한다 (상세 로그는 엔진·서비스가 남긴다)."""
        self.item.post_process = False
        logger.error("다운로드 실패: %s (%s)", self.item.vod_url, exc)
        self.exit_code = 1

    def _on_merge_start(self) -> None:
        """m3u8 병합 단계 진입 — 진행률 계산 방식 전환 플래그."""
        self.item.post_process = True


def _format_progress(
    event: ProgressEvent, data: DownloadData, item: ContentItem
) -> tuple[str, str, str, int]:
    """ProgressEvent를 (남은 시간, 크기, 속도, %)로 변환한다.

    계산식은 GUI viewmodel(app/viewmodels/download_viewmodel.py)의 변환식과 동일하다 — 파일 경로는
    바이트 기반, m3u8은 세그먼트 수 기반. Qt 모듈 import를 피하기 위해(이 스크립트의
    Qt 무의존 유지) 여기서 별도로 구현한다.
    """
    speed_mb = event.speed or 0.0
    remaining_time_str = "N/A"

    if item.is_segment_based:
        # 병합 분모: m3u8은 초기화 세그먼트(EXT-X-MAP) 1개를 더 병합한다 (hls_aes는 없음)
        merge_total = data.max_threads + (1 if item.content_type == "m3u8" else 0)
        if item.post_process:
            progress = int((data.merged_segments / merge_total) * 100) if merge_total > 0 else 0
        else:
            progress = (
                int((data.completed_threads / data.max_threads) * 100)
                if data.max_threads > 0
                else 0
            )
        if speed_mb > 0 and data.completed_threads > 0:
            avg_segment_size = event.downloaded_size / data.completed_threads
            remaining_segments = data.max_threads - data.completed_threads
            remaining_time = (avg_segment_size * remaining_segments) / (speed_mb * 1024 * 1024)
            remaining_time_str = strftime("%H:%M:%S", gmtime(remaining_time))
    else:
        total_size = event.total_size or 0
        progress = int((event.downloaded_size / total_size) * 100) if total_size > 0 else 0
        if speed_mb > 0:
            remaining_time = (total_size - event.downloaded_size) / (speed_mb * 1024 * 1024)
            remaining_time_str = strftime("%H:%M:%S", gmtime(remaining_time))

    return remaining_time_str, str(event.downloaded_size), f"{speed_mb:.1f} MB/s", progress


def _format_fps(rate: Fraction | None) -> str:
    """프레임률을 표시 문자열로 — 정수면 `60fps`, 아니면 소수 둘째 자리까지(`29.97fps`), 없으면 `fps 모름`."""
    if rate is None:
        return "fps 모름"
    if rate.denominator == 1:
        return f"{rate.numerator}fps"
    return f"{float(rate):.2f}".rstrip("0").rstrip(".") + "fps"


def _declared_rate(rep: list, rates: dict[str, Fraction]) -> Fraction | None:
    """목록 항목의 프레임률 — 매니페스트의 정확한 비(rates)가 먼저고, 없으면 항목이 든 선언값이다.

    항목이 든 값은 조회 때 응답에서 읽은 것이다(마스터 플레이리스트의 FRAME-RATE 등, #318).
    소수로 들어 있어 분모 1001까지의 가까운 비로 옮긴다. 표시용 값이다 — 프레임 번호
    계산에는 쓰지 않는다.
    """
    rate = rates.get(rep[1])
    if rate is not None:
        return rate
    declared = getattr(rep, "frame_rate", None)
    if not declared or declared <= 0:
        return None
    return Fraction(declared).limit_denominator(1001)


def _format_resolutions(unique_reps: list, rates: dict[str, Fraction]) -> str:
    """`--list`가 찍는 한 줄 — 해상도마다 이름과 선언된 프레임률을 붙인다.

    이름은 `<해상도>p`이고 원본이면 `(원본)`이 붙는다(#318). 프레임률은 구간을 프레임
    번호로 정할 때 필요해 모든 항목에 적는다(#309).

    Args:
        unique_reps: [해상도, base_url] 목록
        rates: ``{base_url: 프레임률}`` — 여기에도 없고 항목에도 없으면 `fps 모름`으로 찍는다
    """
    return ", ".join(
        f"{_rep_name(rep)} · {_format_fps(_declared_rate(rep, rates))}" for rep in unique_reps
    )


def _fetch_frame_rates(vod_url: str, cookies: dict, content_type: str) -> dict[str, Fraction]:
    """매니페스트가 선언한 해상도별 프레임률을 조회한다. 읽지 못하면 빈 dict(전부 `fps 모름`).

    DASH 매니페스트가 있는 타입(인코딩 완료 VOD · 암호화 VOD)만 읽는다. 다시보기와
    클립은 매니페스트에서 프레임률을 읽는 길이 없어 조회하지 않는다.
    """
    if content_type not in ("video", "hls_aes"):
        return {}
    try:
        _kind, content_no = NetworkManager.extract_content_no(vod_url)
        info = NetworkManager.get_video_info(content_no, cookies)
        return NetworkManager.get_video_frame_rates(info.video_id, info.in_key, cookies)
    except Exception:
        logger.exception("프레임률을 읽지 못했습니다: %s", vod_url)
        return {}


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """커맨드라인 인자를 파싱한다."""
    parser = argparse.ArgumentParser(
        description="GUI 없이 치지직 VOD/클립을 다운로드한다 (#52).",
    )
    parser.add_argument("url", help="치지직 VOD 또는 클립 URL")
    parser.add_argument(
        "--resolution", type=int, default=None, help="원하는 해상도(예: 720). 생략 시 최고 화질"
    )
    parser.add_argument("--output", default=None, help="저장 폴더 (생략 시 현재 디렉토리)")
    parser.add_argument(
        "--timeout",
        type=int,
        default=TIMEOUT_DEFAULT,
        help=f"다운로드 제한 시간(초, 기본 {TIMEOUT_DEFAULT}, 최대 {TIMEOUT_MAX})",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="다운로드하지 않고 사용 가능한 해상도와 프레임률을 출력",
    )
    parser.add_argument(
        "--section",
        action="append",
        default=[],
        metavar="시작-끝",
        help="받을 구간(타임코드 HH:MM:SS:FF-HH:MM:SS:FF). 여러 번 줄 수 있다",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """헤드리스 다운로드 진입점. 종료 코드를 반환한다."""
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    setup_logging(logging.INFO)

    if args.timeout <= 0 or args.timeout > TIMEOUT_MAX:
        logger.error("--timeout은 1~%d초 범위여야 합니다.", TIMEOUT_MAX)
        return 2

    download_path = args.output or os.getcwd()
    if not os.path.isdir(download_path):
        logger.error("저장 폴더가 존재하지 않습니다: %s", download_path)
        return 2

    cookies = _load_cookies()
    fetched = _fetch(args.url, cookies, download_path)
    if fetched is None:
        return 2
    result, content_type = fetched

    unique_reps = result[2]
    if args.list:
        rates = _fetch_frame_rates(args.url, cookies, content_type)
        logger.info("사용 가능한 해상도: %s", _format_resolutions(unique_reps, rates))
        return 0

    item = _build_item(result, content_type, args.resolution)
    if item is None:
        return 2

    selections: tuple[TimeRange, ...] = ()
    mp4_head = None
    if args.section:
        if content_type != "video":
            logger.error("구간 다운로드는 인코딩이 끝난 VOD(mp4)만 지원합니다: %s", content_type)
            return 2
        resolved = _resolve_sections(item, args.section)
        if resolved is None:
            return 2
        selections, mp4_head = resolved

    return _HeadlessRunner(item, args.timeout, selections, mp4_head).run()


if __name__ == "__main__":
    sys.exit(main())
