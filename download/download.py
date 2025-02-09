import requests
import threading
from time import time, strftime, gmtime

from PySide6.QtCore import QThread, Signal
from concurrent.futures import ThreadPoolExecutor, as_completed
from download.task import DownloadTask
from download.state import DownloadState

class DownloadThread(QThread):
    """
    VOD 파일을 multi-thread로 다운로드하는 작업 스레드 클래스
    """
    completed = Signal()
    stopped = Signal(str)
    update_threads = Signal(int, int, int, int)
    update_time = Signal(str, str)
    update_active_threads = Signal(int)
    update_avg_speed = Signal(float)

    def __init__(self, task: DownloadTask):
        super().__init__()
        self.task = task
        self.s = self.task.data
        self.future_dict = {}
        self.lock = threading.Lock()
        # TODO lock이 사용된 위치의 합리성 논의

    def run(self):
        """
        스레드가 시작될 때 자동으로 호출되는 메서드.
        실제 다운로드 파이프라인이 여기서 진행된다.
        """
        try:
            self.s.start_time = time()
            total_size = self.s.total_size = self._get_total_size()

            # part_size 결정(해상도별 가중 적용)
            part_size = self._decide_part_size()

            # 다운로드할 구간 분할
            ranges = [
                (i * part_size, min((i + 1) * part_size - 1, total_size - 1))
                for i in range((total_size + part_size - 1) // part_size)
            ]
            self.s.total_ranges = len(ranges)
            self.s.threads_progress = [0] * self.s.total_ranges
            self.s.max_threads = len(ranges)

            # 빈 파일 생성(사이즈: 0)
            with open(self.s.output_path, 'wb'):
                pass

            with ThreadPoolExecutor(max_workers=self.s.max_threads) as executor:
                self.s.remaining_ranges = self._get_remaining_ranges(ranges)
                futures = []

                while not self.task.state == DownloadState.WAITING:
                    # (1) 현재 활성 스레드 수보다 적으면 -> 추가 스레드 할당
                    while self.s.future_count < self.s.adjust_threads and self.s.remaining_ranges:
                        for part_num in range(self.s.adjust_threads):
                            if not self.s.remaining_ranges:
                                break
                            with self.lock:
                                if self.future_dict.get(part_num) is None:
                                    start, end = self.s.remaining_ranges.pop(0)
                                    self.s.future_count += 1
                                    future = executor.submit(
                                        self._download_part,
                                        start, end, part_num, total_size
                                    )
                                    futures.append(future)
                                    self.future_dict[part_num] = (start, end, future)

                    # (2) 완료된 future 처리
                    for future in as_completed(futures):
                        future.add_done_callback(self._download_completed_callback)
                        futures.remove(future)
                        break  # 한 번에 모두 확인하면 속도 저하 -> 첫 번째 완료만 처리하고 반복

                    # (3) 남은 작업이 없고 스레드도 없으면 종료
                    if not self.s.remaining_ranges and not self.future_dict:
                        break

                if self.task.state == DownloadState.RUNNING:
                    self.completed.emit()

        except requests.RequestException as e:
            self.stopped.emit("다운로드 실패")
            print(e)

    # ============ 다운로드 동작 관련 메서드들 ============

    def _download_part(self, start, end, part_num, total_size):
        """
        파일의 특정 구간(start~end)을 다운로드하는 함수.
        속도가 느릴 경우 재시도 로직, 일시정지/중지 핸들링을 포함한다.
        """
        slow_count = 0
        downloaded_size = 0
        while not self.task.state == DownloadState.WAITING:
            try:
                headers = {'Range': f'bytes={start}-{end}'}
                response = requests.get(self.s.video_url, headers=headers, stream=True)
                response.raise_for_status()
                part_start_time = time()

                with open(self.s.output_path, 'r+b') as f:
                    f.seek(start)
                    for chunk in response.iter_content(chunk_size=8192):
                        if self.task.state == DownloadState.WAITING:
                            return
                        if self.task.state == DownloadState.PAUSED:
                            self.s._pause_event.wait()

                        if chunk:
                            f.write(chunk)
                            downloaded_size += len(chunk)
                            elapsed = time() - part_start_time

                            if elapsed > 0:
                                speed_kb_s = downloaded_size / elapsed / 1024
                                self._check_speed_and_update_progress(
                                    part_num, downloaded_size, total_size, speed_kb_s
                                )
                                if speed_kb_s < 100:
                                    slow_count += 1
                                    if slow_count > 5:
                                        # 속도가 너무 느리면 스레드 재시작
                                        self._download_stop_callback(start, end, part_num)
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
                return part_num

            except requests.RequestException as e:
                with self.lock:
                    self._download_failed_callback(start, end, part_num)
                print(e)

    def _check_speed_and_update_progress(self, part_num, downloaded_size, total_size, speed_kb_s):
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
            future.result()
            # part_num 식별 후 future_dict에서 제거
            for part_num, (start, end, f) in list(self.future_dict.items()):
                if f == future:
                    with self.lock:
                        del self.future_dict[part_num]
                        self.s.future_count -= 1
                    self.update_progress()  # 즉각적 진행도 반영
                    break

        except Exception as e:
            # 일부 스레드가 오류로 중단된 경우
            self.stopped.emit("다운로드 실패")
            print(e)

    def _download_failed_callback(self, start, end, part_num):
        """
        예외 발생 시 파일 구간을 다시 다운로드할 수 있도록 remaining_ranges에 등록.
        """
        self.s.failed_threads += 1
        self.s.threads_progress[part_num] = 0
        self.s.remaining_ranges.append((start, end))

    def _download_stop_callback(self, start, end, part_num):
        """
        특정 스레드를 중도 중단하고, 해당 구간을 재시작하도록 설정.
        """
        self.s.restart_threads += 1
        self.s.threads_progress[part_num] = 0
        self.s.remaining_ranges.append((start, end))

    # ============ 유틸 메서드 ============

    def _get_total_size(self):
        """
        HEAD 요청으로 total_size를 구한다.
        """
        response = requests.head(self.s.video_url)
        response.raise_for_status()
        return int(response.headers.get('content-length', 0))

    def _decide_part_size(self):
        """
        해상도에 따라 파트 크기 가중치를 달리 부여한다.
        """
        base_part_size = 1024 * 1024  # 1MB
        if self.s.height == '144':
            return base_part_size * 1
        elif self.s.height in ['360', '480']:
            return base_part_size * 2
        elif self.s.height == '720':
            return base_part_size * 5
        else:
            return base_part_size * 10

    def _get_remaining_ranges(self, ranges):
        """
        중단 이후 재시작 같은 상황 고려(현재 파일크기 등을 바탕으로),
        아직 다운로드되지 않은 구간만 남겨 반환한다.
        """
        with open(self.s.output_path, 'r+b') as f:
            f.seek(0, 2)
            file_size = f.tell()

        remaining = []
        for start, end in ranges:
            if start >= file_size or end >= file_size:
                remaining.append((start, end))
        return remaining

    # ============ 진행 상황 업데이트 / 제어 메서드 ============

    def update_progress(self):
        """
        다운로드된 총량을 저장한다.
        """
        if self.task.state in [DownloadState.PAUSED, DownloadState.WAITING]:    # 중단 플래그 확인
            return

        active_downloaded_size = sum(self.s.threads_progress)
        self.s.total_downloaded_size = self.s.completed_progress + active_downloaded_size
