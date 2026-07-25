"""파일(범위 분할) 다운로드 엔진 — BaseDownloader 하위 구현 (#73, #82, SPEC §5·§6).

공통 실행 엔진(워커 풀·스케일링·관측·재큐잉·일시정지)은
core/downloaders/base.py의 BaseDownloader로 이주했다(#82). 이 클래스에는
파일 경로 고유 부분만 남는다:

- prepare: HEAD로 총 크기 조회 → 해상도별 part_size 결정 → 바이트 범위 분할
  (ranges.py 사용). 총 크기를 미리 알므로 ProgressEvent.total_size를 채운다
- _download_part: 파트(바이트 구간) 단위 다운로드 — 저속 재시도·일시정지·
  중단 핸들링 포함. 규칙은 tests/unit/core/test_file_downloader_rules.py가 박제한다
- postprocess 없음 (베이스 기본 no-op)

스레드 스케일링 기준 속도는 베이스 기본값(4 MB/s — 구 고정 임계 4/2와 동일)을
그대로 쓴다.
"""

import os
import time as tm

import requests

from core.api.session import get_thread_session
from core.downloaders.base import BaseDownloader
from core.downloaders.ranges import decide_part_size, split_ranges
from core.models.content import Content, ContentType
from core.models.download_state import DownloadState


class FileDownloader(BaseDownloader):
    """VOD 파일을 범위 분할 멀티스레드로 다운로드하는 엔진.

    호출 규약: 소유자(서비스·스크립트)가 DownloadTaskModel.start()로 RUNNING
    전이를 마친 뒤 run()을 호출한다. run()은 완료·중단·실패까지 블로킹한다.
    """

    run_thread_name = "DownloadThread"
    worker_pool_prefix = "DownloadWorker"
    # 구 코드와 동일하게 요청 예외만 실패로 처리한다 — 그 외 예외는 전파
    _failure_exceptions = (requests.RequestException,)

    @classmethod
    def supports(cls, content: Content) -> bool:
        """일반 VOD(video)와 클립(clip)을 처리한다 — 매니페스트가 파일 URL을 준다."""
        return content.content_type in (ContentType.CHZZK_VIDEO, ContentType.CHZZK_CLIP)

    # ============ 작업 목록·수신 준비 (구 run의 파일 고유 부분) ============

    def prepare(self, content: Content) -> list[tuple[int, int]]:
        """총 크기를 조회하고 해상도별 part_size로 바이트 범위 목록을 만든다."""
        total_size = self.s.total_size = self._get_total_size()

        # part_size 결정(해상도별 가중 적용)
        self._part_size = decide_part_size(self.s.content_type, self.s.resolution)

        # 다운로드할 구간 분할
        return split_ranges(total_size, self._part_size)

    def _download_start_log_args(self) -> tuple:
        return (self.s.total_size, self._part_size, self.s.total_ranges, self.s.adjust_threads)

    def _prepare_output(self) -> None:
        """빈 파일 생성(사이즈: 0)."""
        with open(self.s.output_path, "wb"):
            pass

    def _initial_queue(self, items: list) -> list:
        """중단 이후 재시작 같은 상황을 고려해 미수신 구간만 큐에 넣는다."""
        return self._get_remaining_ranges(items)

    def _log_item_start(self, part_num: int, item) -> None:
        start, end = item
        self.logger.log_thread_start(part_num, start, end)

    def _download_item(self, item, part_num: int):
        start, end = item
        return self._download_part(start, end, part_num, self.s.total_size)

    def _cleanup_partial(self) -> None:
        """실패·중단 시 다운로드 파일 삭제."""
        if os.path.exists(self.s.output_path):
            os.remove(self.s.output_path)

    # ============ 다운로드 동작 관련 메서드들 ============

    def _download_part(self, start: int, end: int, part_num: int, total_size: int):
        """
        파일의 특정 구간(start~end)을 다운로드하는 함수.
        속도가 느릴 경우 재시도 로직, 일시정지/중지 핸들링을 포함한다.
        """
        slow_count = 0
        downloaded_size = 0
        while not self.state == DownloadState.WAITING:
            try:
                headers = {"Range": f"bytes={start}-{end}"}
                # 스레드로컬 세션으로 같은 워커의 반복 요청 간 연결을 재사용한다 (#31)
                response = get_thread_session().get(
                    self.s.base_url, headers=headers, stream=True, timeout=30
                )
                response.raise_for_status()
                part_start_time = tm.time()

                with open(self.s.output_path, "r+b") as f:
                    f.seek(start)
                    for chunk in response.iter_content(chunk_size=8192):
                        if self.state == DownloadState.WAITING:
                            return part_num
                        if self.state == DownloadState.PAUSED:
                            self.s._pause_event.wait()

                        if chunk:
                            f.write(chunk)
                            downloaded_size += len(chunk)
                            elapsed = tm.time() - part_start_time

                            if elapsed > 0:
                                speed_kb_s = downloaded_size / elapsed / 1024
                                self._check_speed_and_update_progress(
                                    part_num, downloaded_size, total_size, speed_kb_s
                                )
                                if speed_kb_s < 100:
                                    slow_count += 1
                                    if slow_count > 5:
                                        # 속도가 너무 느리면 스레드 재시작
                                        with self.lock:
                                            self._requeue_slow((start, end), part_num)
                                        return part_num
                                else:
                                    slow_count = 0

                            if downloaded_size >= (end - start + 1):
                                break

                # 성공적으로 마무리된 경우
                with self.lock:
                    self.s.completed_threads += 1
                    self.s.completed_progress += downloaded_size
                    self.s.threads_progress[part_num] = 0
                self.logger.log_thread_complete(part_num, downloaded_size)
                return part_num

            except (requests.RequestException, requests.Timeout) as e:
                with self.lock:
                    self._requeue_failed((start, end), part_num)
                self.logger.log_error(f"Part {part_num} download failed", e)
                return part_num

    # ============ 유틸 메서드 ============

    def _get_total_size(
        self,
    ) -> int:  #: TODO: 컨텐츠 아이템에 content-length 추가(중복된 로직 제거)
        """
        HEAD 요청으로 total_size를 구한다.
        """
        response = get_thread_session().head(self.s.base_url)
        response.raise_for_status()
        size = int(response.headers.get("content-length", 0))
        if size == 0:
            resp = get_thread_session().get(self.s.base_url, stream=True)
            resp.raise_for_status()
            size = int(resp.headers.get("content-length", 0))
            resp.close()
        return size

    def _get_remaining_ranges(self, ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
        """
        중단 이후 재시작 같은 상황 고려(현재 파일크기 등을 바탕으로),
        아직 다운로드되지 않은 구간만 남겨 반환한다.
        """
        with open(self.s.output_path, "r+b") as f:
            f.seek(0, 2)
            file_size = f.tell()

        remaining = []
        for start, end in ranges:
            if start >= file_size or end >= file_size:
                remaining.append((start, end))
        return remaining
