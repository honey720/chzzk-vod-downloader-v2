"""m3u8(세그먼트 분할) 다운로드 엔진 — 표준 threading 기반 (#74, SPEC §5·§6).

구 download/download_m3u8.py의 DownloadM3U8Thread(QThread)와
download/monitor_m3u8.py의 MonitorM3U8Thread(QThread)에 나뉘어 있던
실행·관측 로직을 Qt 없이 한 클래스로 흡수했다 — 이슈 #73(파일 다운로더)과
같은 패턴이다. 로직(플레이리스트 파싱·세그먼트 스케줄링·해상도별 적응형
스레드 스케일링·느린 세그먼트 재시도·실패 재큐잉·순서 보장 병합)은 식 그대로
이동했다 — 규칙은 tests/unit/core/test_m3u8_downloader_rules.py가 박제한다.

파일 경로 엔진(FileDownloader)과의 차이:
- base_url은 m3u8 플레이리스트 URL이며 **호출 전에 해석이 끝나 있어야 한다**.
  치지직 API 조회(쿠키·NetworkManager)는 아직 앱 영역이므로 어댑터가 수행한다.
- 스레드 스케일링 기준 속도가 해상도별 테이블(_get_standard_speed)을 따른다.
- 다운로드 완료 후 병합 단계가 있다. 병합 시작은 on_merge_start 콜백으로
  알린다 — 앱 어댑터가 UI 플래그(item.post_process)로 변환한다.
- 전체 크기를 미리 알 수 없어 ProgressEvent.total_size는 None이다.
  진행률 계산(세그먼트 수 기반)은 어댑터의 몫이다.

- 관측(속도 측정·스레드 조정·진행 통지)은 엔진이 소유하는 일반 스레드
  (_monitor_loop)가 수행하고, 결과는 core/models/events.py의 ProgressEvent
  콜백으로 보고한다. 완료·실패도 같은 계약의 콜백으로 알린다.
- 일시정지·중단은 DownloadTaskModel의 상태와 pause_event를 그대로 사용한다(#60).
- 콜백은 작업 스레드에서 호출된다 — 어댑터는 Signal emit까지만 해야 한다
  (스레드 규칙은 core/models/events.py docstring 참고).

data·logger 매개변수는 앱 영역(download/data.py의 DownloadData,
download/logger.py의 DownloadLogger)과 호환되는 객체를 주입받는다.
core에서 직접 import하지 않는 이유는 core→app 의존 금지 때문이다 (#72와 동일한 방식).
"""

import os
import shutil
import threading
import time as tm
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

import requests

from core.api.session import get_thread_session
from core.models.download_state import DownloadState
from core.models.events import (
    FailedCallback,
    FinishedCallback,
    ProgressCallback,
    ProgressEvent,
)


class M3U8Downloader:
    """m3u8 VOD를 세그먼트 단위 멀티스레드로 다운로드·병합하는 엔진.

    호출 규약: 소유자(어댑터·스크립트)가 data.base_url에 m3u8 플레이리스트 URL을
    채우고 DownloadTaskModel.start()로 RUNNING 전이를 마친 뒤 run()을 호출한다.
    run()은 완료·중단·실패까지 블로킹한다.
    """

    def __init__(
        self,
        data,
        logger,
        on_progress: ProgressCallback | None = None,
        on_finished: FinishedCallback | None = None,
        on_failed: FailedCallback | None = None,
        on_merge_start: Callable[[], None] | None = None,
    ):
        """엔진을 생성한다.

        Args:
            data: DownloadData 호환 공유 데이터 (진행률·엔진 변수·model 보유)
            logger: DownloadLogger 호환 로거
            on_progress: 관측 주기마다 ProgressEvent를 받는 콜백
            on_finished: 정상 완료 시 인자 없이 호출되는 콜백
            on_failed: 실패 시 예외 객체를 그대로 받는 콜백
            on_merge_start: 세그먼트 병합 단계 진입 시 인자 없이 호출되는 콜백
        """
        self.s = data
        self.model = data.model
        self.logger = logger
        self.lock = threading.Lock()
        self.future_dict: dict = {}
        self.adjust_count = 0
        self._on_progress: ProgressCallback = on_progress or (lambda event: None)
        self._on_finished: FinishedCallback = on_finished or (lambda: None)
        self._on_failed: FailedCallback = on_failed or (lambda exc: None)
        self._on_merge_start: Callable[[], None] = on_merge_start or (lambda: None)

    @property
    def state(self) -> DownloadState:
        """현재 다운로드 상태 (DownloadTaskModel에 위임)."""
        return self.model.state

    def set_on_progress(self, callback: ProgressCallback) -> None:
        """진행 이벤트 콜백을 등록한다 (어댑터가 생성 후 연결하는 경우용)."""
        self._on_progress = callback

    # ============ 실행 파이프라인 (구 DownloadM3U8Thread.run) ============

    def run(self) -> None:
        """다운로드·병합 파이프라인을 실행한다. 관측 스레드도 여기서 소유·시작한다."""
        monitor = threading.Thread(target=self._monitor_loop, name="DownloadMonitor", daemon=True)
        # 세그먼트 저장용 임시 폴더 경로 설정 (예외 정리 경로가 참조하므로 가장 먼저)
        self.temp_dir = os.path.join(os.path.dirname(self.s.output_path), "CVDv2_temp")
        try:
            self.s.start_time = tm.time()

            response = get_thread_session().get(self.s.base_url)
            response.raise_for_status()
            lines = response.text.splitlines()
            segments = [line for line in lines if line and not line.startswith("#")]
            init_segment = None
            self.s.merged_segments = 0

            self.s.max_threads = self.s.total_ranges = len(segments)
            self.width = len(str(self.s.total_ranges))
            self.s.adjust_threads = min(self.s.adjust_threads, self.s.max_threads)
            self.s.threads_progress = [0] * self.s.total_ranges
            self.logger.log_download_start(0, 0, self.s.total_ranges, self.s.adjust_threads)

            # 세그먼트 저장용 임시 폴더가 있다면 내용 포함 삭제 후 재생성
            if os.path.exists(self.temp_dir):
                shutil.rmtree(self.temp_dir)
            os.makedirs(self.temp_dir)

            for line in lines:
                if line.startswith("#EXT-X-MAP:"):
                    init_segment = line.split("URI=")[1].strip('"')
            init_url = urljoin(self.s.base_url, init_segment)
            init_segment_path = os.path.join(self.temp_dir, f"{0:0{self.width}d}.m4s")
            # 초기화 세그먼트 다운로드
            with open(init_segment_path, "wb") as f:
                f.write(get_thread_session().get(init_url).content)

            # 진행률 배열이 준비된 뒤에 관측을 시작한다
            monitor.start()

            with ThreadPoolExecutor(
                max_workers=self.s.max_threads, thread_name_prefix="DownloadM3U8Worker"
            ) as executor:
                self.s.remaining_ranges = list(enumerate(segments))
                # 재사용 시 초기화 필수
                with self.lock:
                    self.s.future_count = 0
                    self.future_dict = {}

                while not self.state == DownloadState.WAITING:
                    # (1) 현재 활성 스레드 수보다 적으면 -> 추가 스레드 할당
                    while self.s.future_count < self.s.adjust_threads and self.s.remaining_ranges:
                        for part_num in range(self.s.adjust_threads):
                            if not self.s.remaining_ranges:
                                break
                            with self.lock:
                                if part_num not in self.future_dict:
                                    segment_index, segment = self.s.remaining_ranges.pop(0)
                                    self.s.future_count += 1
                                    self.logger.log_m3u8_thread_start(part_num, segment)
                                    future = executor.submit(
                                        self._download_segment,
                                        index=segment_index,
                                        segment=segment,
                                        part_num=part_num,
                                        total_ranges=self.s.total_ranges,
                                    )
                                    future.add_done_callback(self._download_completed_callback)
                                    self.future_dict[part_num] = (segment, future)

                    # (2) 주기적으로 상태 확인 (non-blocking)
                    tm.sleep(0.1)

                    # (3) 남은 작업이 없고 스레드도 없으면 종료
                    if not self.s.remaining_ranges and not self.future_dict:
                        break

                if self.state == DownloadState.RUNNING:
                    # (4) 다운로드 완료 후 파일 합치기
                    self._on_merge_start()
                    with open(self.s.output_path, "wb") as final_f:
                        segment_files = sorted(os.listdir(self.temp_dir))
                        for seg_file in segment_files:
                            # 다운로드 중지 상태라면 중단
                            if self.state == DownloadState.RUNNING:
                                seg_path = os.path.join(self.temp_dir, seg_file)
                                with open(seg_path, "rb") as seg_f:
                                    while True:
                                        chunk = seg_f.read(8192)
                                        if not chunk:
                                            break
                                        final_f.write(chunk)
                                        # 일시정지 상태라면 대기
                                        if self.state == DownloadState.PAUSED:
                                            self.s._pause_event.wait()
                                # 세그먼트 파일 합친 후 삭제
                                self.safe_remove(seg_path)
                                self.s.merged_segments += 1

                    self.s.end_time = tm.time()
                    total_time = self.s.end_time - self.s.start_time
                    self.logger.log_download_complete(total_time)
                    self.logger.save_and_close()
                    self._on_finished()

            # (5) 임시 폴더 삭제
            shutil.rmtree(self.temp_dir)

        except Exception as e:
            # 오류 발생 시 임시 파일과 다운로드 파일 삭제
            if os.path.exists(self.temp_dir):
                shutil.rmtree(self.temp_dir)
            if os.path.exists(self.s.output_path):
                os.remove(self.s.output_path)
            self._on_failed(e)
            self.logger.log_exception("Download failed", e)
            self.logger.save_and_close()

        # 사용자가 강제로 중단한 경우 임시 파일과 다운로드 파일 삭제
        if self.state == DownloadState.WAITING:
            if os.path.exists(self.temp_dir):
                shutil.rmtree(self.temp_dir)
            if os.path.exists(self.s.output_path):
                os.remove(self.s.output_path)

    # ============ 다운로드 동작 관련 메서드들 ============

    def _download_segment(self, index: int, segment: str, part_num: int, total_ranges: int):
        """
        개별 세그먼트 다운로드(재시도 포함)
        """
        slow_count = 0
        downloaded_size = 0
        segment_url = urljoin(self.s.base_url, segment)
        while not self.state == DownloadState.WAITING:
            try:
                # 스레드로컬 세션으로 같은 워커의 세그먼트 요청 간 연결을 재사용한다 (#31)
                response = get_thread_session().get(segment_url, stream=True, timeout=30)
                response.raise_for_status()
                part_start_time = tm.time()

                temp_file = os.path.join(self.temp_dir, f"{index + 1:0{self.width}d}.m4v")
                with open(temp_file, "wb") as f:
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
                                    part_num, downloaded_size, total_ranges, speed_kb_s
                                )
                                if speed_kb_s < 100:
                                    slow_count += 1
                                    if slow_count > 5:
                                        # 속도가 너무 느리면 스레드 재시작
                                        with self.lock:
                                            self._download_stop_callback(index, segment, part_num)
                                        return part_num
                                else:
                                    slow_count = 0

                # 성공적으로 마무리된 경우
                with self.lock:
                    self.s.completed_threads += 1
                    self.s.completed_progress += downloaded_size
                    self.s.threads_progress[part_num] = 0
                self.logger.log_thread_complete(part_num, downloaded_size)
                return part_num

            except (requests.RequestException, requests.Timeout) as e:
                with self.lock:
                    self._download_failed_callback(index, segment, part_num)
                self.logger.log_error(f"Part {part_num} download failed", e)
                return part_num

    def _check_speed_and_update_progress(
        self, part_num: int, downloaded_size: int, total_size: int, speed_kb_s: float
    ):
        """
        스레드가 다운로드 중일 때 속도 체크 및 진행 상황 업데이트.
        """
        with self.lock:
            self.s.threads_progress[part_num] = downloaded_size
            self.update_progress()

    # ============ 다운로드 조정 및 콜백 메서드 ============

    def _download_completed_callback(self, future):
        """
        특정 future(스레드)가 끝났을 때 호출되는 콜백.
        """
        try:
            part_num = future.result()
            # part_num 식별 후 future_dict에서 제거
            with self.lock:
                if part_num in self.future_dict:
                    del self.future_dict[part_num]
                    self.s.future_count -= 1
            self.update_progress()  # 즉각적 진행도 반영

        except Exception as e:
            # 일부 스레드가 오류로 중단된 경우
            self._on_failed(e)
            self.logger.log_error("Thread failed", e)

    def _download_failed_callback(self, index: int, segment: str, part_num: int):
        """
        예외 발생 시 세그먼트를 다시 다운로드할 수 있도록 remaining_ranges에 등록.
        """
        self.s.failed_threads += 1
        self.s.threads_progress[part_num] = 0
        self.s.remaining_ranges.append((index, segment))

    def _download_stop_callback(self, index: int, segment: str, part_num: int):
        """
        특정 스레드를 중도 중단하고, 해당 세그먼트를 재시작하도록 설정.
        """
        self.s.restart_threads += 1
        self.s.threads_progress[part_num] = 0
        self.s.remaining_ranges.append((index, segment))
        self.logger.warning(f"Part {part_num} stopped due to slow speed, will retry")

    # ============ 관측 루프 (구 MonitorM3U8Thread — 엔진이 흡수) ============

    def _monitor_loop(self):
        """주기(1초)마다 스레드 수 조정·속도 측정·진행 통지를 수행하는 관측 루프."""
        tm.sleep(1)
        while self.state in [DownloadState.RUNNING, DownloadState.PAUSED]:
            if not self.s._pause_event.is_set():
                self.s._pause_event.wait()
                self.measure_speed()
            else:
                self._adjust_threads()
                self.measure_speed()
                self.emit_progress()
            total_sleep = 1.0  # 총 1초 대기
            interval = 0.1  # 0.1초씩 대기
            elapsed = 0.0
            while elapsed < total_sleep and self.state in [
                DownloadState.RUNNING,
                DownloadState.PAUSED,
            ]:
                tm.sleep(interval)
                elapsed += interval

    def _adjust_threads(self):
        """
        다운로드 진행 중, 속도 등에 따라 스레드 수를 동적으로 조정하는 예시 스레드.
        """
        with self.lock:
            future_count = self.s.future_count

        avg_active_speed = self.s.speed_mb / future_count if future_count > 0 else 0

        standard_speed = self._get_standard_speed()

        if avg_active_speed > standard_speed:
            self.adjust_count += 1
        elif avg_active_speed < standard_speed / 2:
            self.adjust_count -= 1
        else:
            if self.adjust_count > 0:
                self.adjust_count -= 1
            elif self.adjust_count < 0:
                self.adjust_count += 1

        if self.adjust_count > 1:
            self.s.adjust_threads = min(self.s.max_threads, self.s.adjust_threads + 4)
            self.logger.log_thread_adjust(
                self.s.adjust_threads, self.s.speed_mb
            )  # 스레드 조정 로그
            self.adjust_count = 0
        elif self.adjust_count < -4:
            self.s.adjust_threads = max(1, self.s.adjust_threads // 2)
            self.logger.log_thread_adjust(
                self.s.adjust_threads, self.s.speed_mb
            )  # 스레드 조정 로그
            self.adjust_count = 0

    def measure_speed(self):
        """직전 틱 대비 다운로드 바이트 증가량으로 속도(MB/s)를 계산한다."""
        current_size = self.s.total_downloaded_size
        speed = current_size - self.s.prev_size
        self.s.prev_size = current_size

        with self.lock:
            future_count = self.s.future_count
        # MB/s로 변환
        self.s.speed_mb = speed / (1024 * 1024)
        avg_speed = self.s.speed_mb / future_count if future_count > 0 else 0
        self.logger.log_thread_debug(future_count, self.s.speed_mb, avg_speed)

    def _get_standard_speed(self) -> float:
        """
        해상도에 따른 표준 속도 값을 반환합니다.
        """
        if self.s.resolution == 144:
            return 0.2
        elif self.s.resolution in [360, 480]:
            return 0.5
        elif self.s.resolution == 720:
            return 1.2
        else:
            return 3

    # ============ 진행 상황 업데이트 ============

    def update_progress(self):
        """
        다운로드된 총량을 저장한다.
        """
        if self.state in [DownloadState.PAUSED, DownloadState.WAITING]:  # 중단 플래그 확인
            return

        active_downloaded_size = sum(self.s.threads_progress)
        self.s.total_downloaded_size = self.s.completed_progress + active_downloaded_size

    def emit_progress(self):
        """진행 상태를 집계해 ProgressEvent 콜백으로 통지한다 (구 MonitorM3U8Thread.update_progress).

        m3u8은 전체 바이트 크기를 미리 알 수 없으므로 total_size는 None이다.
        세그먼트 수 기반 진행률 계산은 어댑터가 공유 데이터로 수행한다.
        """
        active_downloaded_size = sum(self.s.threads_progress)
        self.s.total_downloaded_size = self.s.completed_progress + active_downloaded_size

        with self.lock:
            future_count = self.s.future_count

        self._on_progress(
            ProgressEvent(
                downloaded_size=self.s.total_downloaded_size,
                total_size=None,
                speed=self.s.speed_mb,
                active_threads=future_count,
            )
        )

    # ============ 유틸 메서드 ============

    def safe_remove(self, path: str, retries: int = 5, delay: float = 0.2) -> None:
        """
        파일 삭제 시도 (PermissionError 방지용 재시도 포함)
        """
        for _ in range(int(retries)):
            try:
                os.remove(path)
                return
            except PermissionError:  # [WinError 32]
                tm.sleep(delay)
        raise PermissionError(f"Failed to remove {path} after {retries} attempts")
