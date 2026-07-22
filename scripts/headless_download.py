"""GUI 없이 VOD/클립을 다운로드하는 헤드리스 스크립트 (#52).

"core는 CLI·다른 UI로 교체 가능해야 한다"(SPEC §3.1)를 실물로 검증하고,
향후 E2E 자동 테스트의 진입점을 제공한다. 새 다운로드 로직을 작성하지 않고
기존 파이프라인(ContentWorker → NetworkManager → DownloadManager → DownloadThread)을
그대로 재사용한다.

사용법:
    uv run python scripts/headless_download.py <VOD/클립 URL> [옵션]

옵션:
    --resolution N   원하는 해상도(예: 720). 생략 시 최고 화질(auto)
    --output PATH    저장 폴더 (생략 시 현재 작업 디렉토리)
    --timeout SEC    다운로드 제한 시간(초). 초과 시 실패로 종료 (기본 600, 최대 7200)
    --list           다운로드하지 않고 사용 가능한 해상도만 출력

예)
    uv run python scripts/headless_download.py https://chzzk.naver.com/clips/xxxx
    uv run python scripts/headless_download.py https://chzzk.naver.com/video/123 --resolution 720

종료 코드:
    0  다운로드 성공
    1  다운로드 실패(네트워크·타임아웃 등)
    2  잘못된 인자·URL, 또는 조회 실패(권한/암호화 등)

Qt 의존에 대하여:
    DownloadThread·MonitorThread는 QThread이고, 완료·진행 상황을 큐 연결(queued
    connection) 시그널로 메인 스레드에 전달한다. 이 시그널이 배달되려면 Qt 이벤트
    루프가 필요하므로 QCoreApplication을 사용한다. GUI 위젯(QApplication)은 쓰지 않으며
    창은 뜨지 않는다 — 이슈 #52가 허용한 "QApplication 없이, 필요 시 QCoreApplication까지"의
    최소 범위다.
"""

import argparse
import logging
import os
import sys
from pathlib import Path

# scripts/ 하위에서 실행해도 저장소 루트 모듈을 import할 수 있게 한다
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QCoreApplication, QTimer  # noqa: E402

import config.config as config  # noqa: E402
from config.log_setup import setup_logging  # noqa: E402
from content.data import ContentItem  # noqa: E402
from content.worker import ContentWorker  # noqa: E402
from download.manager import DownloadManager  # noqa: E402

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
    """ContentWorker를 동기 실행해 (result, content_type)을 반환한다.

    ContentWorker.run은 finished/error 시그널로 결과를 알린다. 같은 스레드에서
    직접 호출하면 시그널이 즉시(direct connection) 배달되므로 이벤트 루프 없이 캡처한다.

    Returns:
        tuple[tuple, str] | None: 성공 시 (result, content_type), 실패 시 None
    """
    captured: dict = {}

    worker = ContentWorker(vod_url, cookies, download_path)
    worker.finished.connect(lambda result, ct: captured.update(result=result, content_type=ct))
    worker.error.connect(lambda msg: captured.update(error=msg))
    worker.run()

    if "error" in captured:
        logger.error("조회 실패: %s", captured["error"].replace("\n", " | "))
        return None
    return captured["result"], captured["content_type"]


def _select_resolution(unique_reps: list, resolution: int | None):
    """unique_reps에서 원하는 해상도의 (resolution, base_url)을 고른다.

    resolution이 None이면 최고 화질(목록의 마지막)을 쓴다. m3u8은 base_url이 None이며
    실제 URL은 다운로드 스레드가 해상도로 뒤늦게 해석한다.

    Returns:
        tuple[int, str | None] | None: (해상도, base_url). 매칭 실패 시 None
    """
    if resolution is None:
        rep = unique_reps[-1]
        return rep[0], rep[1]
    for rep in unique_reps:
        if rep[0] == resolution:
            return rep[0], rep[1]
    return None


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
        available = ", ".join(f"{rep[0]}p" for rep in unique_reps)
        logger.error("해상도 %sp를 찾을 수 없습니다. 사용 가능: %s", resolution, available)
        return None
    item.resolution, item.base_url = selected

    filename = f"{item.title} {item.resolution}p.mp4"
    item.output_path = os.path.join(item.download_path, filename)
    return item


class _HeadlessRunner:
    """DownloadManager를 구동하고 완료/실패/타임아웃을 종료 코드로 환원한다."""

    def __init__(self, item: ContentItem, timeout: int) -> None:
        self.item = item
        self.timeout = timeout
        self.exit_code = 1  # 완료 신호를 받기 전까지는 실패로 간주
        self.manager = DownloadManager()

    def run(self) -> int:
        """이벤트 루프를 돌려 다운로드가 끝날 때까지 대기한 뒤 종료 코드를 반환한다."""
        app = QCoreApplication(sys.argv)

        self.manager.progress.connect(self._on_progress)
        self.manager.finished.connect(self._on_finished)
        self.manager.stopped.connect(self._on_stopped)

        # 타임아웃: 상한을 넘으면 실패로 종료 (스레드 중지 후 루프 탈출)
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(self._on_timeout)
        timer.start(self.timeout * 1000)

        logger.info(
            "다운로드 시작: %s (%sp) -> %s",
            self.item.title,
            self.item.resolution,
            self.item.output_path,
        )
        self.manager.start(self.item)
        app.exec()
        return self.exit_code

    def _on_progress(self, rem, size, spd, prog, item) -> None:
        """모니터 스레드의 진행 상황을 stdout 로그로 남긴다."""
        logger.info("진행률 %3s%% | 속도 %s | 남은시간 %s | 누적 %s bytes", prog, spd, rem, size)

    def _on_finished(self, item, download_time) -> None:
        """정상 완료: 결과 파일 크기를 로그로 남기고 성공 코드로 종료한다."""
        size = os.path.getsize(item.output_path) if os.path.exists(item.output_path) else 0
        logger.info(
            "다운로드 완료: %s (%s bytes, 소요 %s)",
            item.output_path,
            f"{size:,}",
            download_time,
        )
        self.exit_code = 0 if size > 0 else 1
        QCoreApplication.quit()

    def _on_stopped(self, item) -> None:
        """다운로드 실패(스레드 중단): 실패 코드로 종료한다."""
        logger.error("다운로드 실패: %s", item.vod_url)
        self.exit_code = 1
        QCoreApplication.quit()

    def _on_timeout(self) -> None:
        """제한 시간 초과: 스레드를 중지하고 실패 코드로 종료한다."""
        logger.error("제한 시간(%d초) 초과 — 다운로드를 중단합니다.", self.timeout)
        self.exit_code = 1
        self.manager.stop()
        QCoreApplication.quit()


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
        "--list", action="store_true", help="다운로드하지 않고 사용 가능한 해상도만 출력"
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

    fetched = _fetch(args.url, _load_cookies(), download_path)
    if fetched is None:
        return 2
    result, content_type = fetched

    unique_reps = result[2]
    if args.list:
        logger.info("사용 가능한 해상도: %s", ", ".join(f"{rep[0]}p" for rep in unique_reps))
        return 0

    item = _build_item(result, content_type, args.resolution)
    if item is None:
        return 2

    return _HeadlessRunner(item, args.timeout).run()


if __name__ == "__main__":
    sys.exit(main())
