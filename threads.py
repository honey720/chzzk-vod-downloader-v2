import os
import threading
import requests
from time import time, strftime, gmtime

from PyQt5.QtCore import QThread, pyqtSignal
from concurrent.futures import ThreadPoolExecutor, as_completed


class DownloadThread(QThread):
    """
    VOD 파일을 multi-thread로 다운로드하는 작업 스레드 클래스
    """
    progress = pyqtSignal(int, str)
    completed = pyqtSignal(str)
    paused = pyqtSignal()
    resumed = pyqtSignal()
    stopped = pyqtSignal(str)
    update_threads = pyqtSignal(int, int, int, int)
    update_time = pyqtSignal(str, str)
    update_active_threads = pyqtSignal(int)
    update_avg_speed = pyqtSignal(float)

    def __init__(self, video_url, output_path, height, initial_threads=None):
        super().__init__()
        self.video_url = video_url
        self.output_path = output_path
        self.height = height
        self._is_paused = False
        self._is_stopped = False
        self.lock = threading.Lock()

        # 진행도/실패/재시작/스레드 관련 변수
        self.completed_threads = 0
        self.failed_threads = 0
        self.restart_threads = 0
        self.completed_progress = 0

        # CPU 개수를 바탕으로 초깃값 지정
        if initial_threads is None:
            self.adjust_threads = min(32, os.cpu_count() + 4)
        else:
            self.adjust_threads = initial_threads

        self.max_threads = self.adjust_threads
        self.total_active_speed = 0
        self.future_count = 0
        self.future_dict = {}
        self.remaining_ranges = []

    def run(self):
        """
        스레드가 시작될 때 자동으로 호출되는 메서드.
        실제 다운로드 파이프라인이 여기서 진행된다.
        """
        try:
            self.start_time = time()
            total_size = self._get_total_size()

            # part_size 결정(해상도별 가중 적용)
            part_size = self._decide_part_size()

            # 다운로드할 구간 분할
            ranges = [
                (i * part_size, min((i + 1) * part_size - 1, total_size - 1))
                for i in range((total_size + part_size - 1) // part_size)
            ]
            self.total_ranges = len(ranges)
            self.thread_progress = [0] * self.total_ranges
            self.max_threads = len(ranges)

            # 빈 파일 생성(사이즈: 0)
            with open(self.output_path, 'wb'):
                pass

            # 조정 스레드(동적 스레드 갯수 조절)
            adjust_thread = threading.Thread(
                target=self._adjust_threads_loop, daemon=True
            )

            with ThreadPoolExecutor(max_workers=self.max_threads) as executor:
                self.remaining_ranges = self._get_remaining_ranges(ranges)
                adjust_thread.start()
                futures = []

                while not self._is_stopped:
                    # (1) 현재 활성 스레드 수보다 적으면 -> 추가 스레드 할당
                    while self.future_count < self.adjust_threads and self.remaining_ranges:
                        for part_num in range(self.adjust_threads):
                            if not self.remaining_ranges:
                                break
                            with self.lock:
                                if self.future_dict.get(part_num) is None:
                                    start, end = self.remaining_ranges.pop(0)
                                    self.future_count += 1
                                    future = executor.submit(
                                        self._download_part,
                                        start, end, part_num, total_size
                                    )
                                    futures.append(future)
                                    self.future_dict[part_num] = (start, end, future)
                                    self.update_active_threads.emit(self.future_count)

                    # (2) 완료된 future 처리
                    for future in as_completed(futures):
                        future.add_done_callback(self._download_completed_callback)
                        futures.remove(future)
                        break  # 한 번에 모두 확인하면 속도 저하 -> 첫 번째 완료만 처리하고 반복

                    # (3) 남은 작업이 없고 스레드도 없으면 종료
                    if not self.remaining_ranges and not self.future_dict:
                        break

                if not self._is_stopped:
                    self.completed.emit("다운로드 완료!")
                else:
                    self.stopped.emit("다운로드 중지됨")

        except requests.RequestException as e:
            self._is_stopped = True
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
        paused_signal_sent = False  # paused 시그널이 이미 전송되었는지 여부를 추적
        while not self._is_stopped:
            try:
                headers = {'Range': f'bytes={start}-{end}'}
                response = requests.get(self.video_url, headers=headers, stream=True)
                response.raise_for_status()
                part_start_time = time()

                with open(self.output_path, 'r+b') as f:
                    f.seek(start)
                    for chunk in response.iter_content(chunk_size=8192):
                        if self._is_stopped:
                            return
                        while self._is_paused:
                            if not paused_signal_sent:  # paused 상태일 때 한 번만 시그널 전송
                                self.paused.emit()
                                paused_signal_sent = True
                            self.sleep(1)
                        paused_signal_sent = False

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
                    self.completed_threads += 1
                    self.completed_progress += downloaded_size
                    self.thread_progress[part_num] = 0
                return part_num

            except requests.RequestException as e:
                with self.lock:
                    self._download_failed_callback(start, end, part_num)
                print(e)
                # 재시도(while 루프)에 의해 계속 시도

    def _check_speed_and_update_progress(self, part_num, downloaded_size, total_size, speed_kb_s):
        """
        스레드가 다운로드 중일 때 속도 체크 및 진행 상황 업데이트.
        """
        with self.lock:
            self.thread_progress[part_num] = downloaded_size
            self.update_progress(total_size)

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
                        self.future_count -= 1
                    self.update_progress()  # 즉각적 진행도 반영
                    self.update_active_threads.emit(self.future_count)
                    self.update_threads.emit(
                        self.completed_threads,
                        self.total_ranges,
                        self.failed_threads,
                        self.restart_threads,
                    )
                    break

        except Exception as e:
            # 일부 스레드가 오류로 중단된 경우
            self._is_stopped = True
            self.stopped.emit("다운로드 실패")
            print(e)

    def _download_failed_callback(self, start, end, part_num):
        """
        예외 발생 시 파일 구간을 다시 다운로드할 수 있도록 remaining_ranges에 등록.
        """
        self.failed_threads += 1
        self.thread_progress[part_num] = 0
        self.remaining_ranges.append((start, end))

    def _download_stop_callback(self, start, end, part_num):
        """
        특정 스레드를 중도 중단하고, 해당 구간을 재시작하도록 설정.
        """
        self.restart_threads += 1
        self.thread_progress[part_num] = 0
        self.remaining_ranges.append((start, end))

    def _adjust_threads_loop(self):
        """
        다운로드 진행 중, 속도 등에 따라 스레드 수를 동적으로 조정하는 예시 스레드.
        """
        adjust_count = 0
        while not self._is_stopped and self.remaining_ranges:
            if self._is_paused:
                continue
            threading.Event().wait(1)

            avg_active_speed = (
                self.total_active_speed / self.future_count if self.future_count > 0 else 0
            )

            if avg_active_speed > 2:
                adjust_count += 1
            elif avg_active_speed < 1:
                adjust_count -= 1
            else:
                if adjust_count > 0:
                    adjust_count -= 1
                elif adjust_count < 0:
                    adjust_count += 1

            if adjust_count > 4:
                self.adjust_threads = min(self.max_threads, self.adjust_threads * 2)
                adjust_count = 0
            elif adjust_count < -4:
                self.adjust_threads = max(1, self.adjust_threads // 2)
                adjust_count = 0

    # ============ 유틸 메서드 ============

    def _get_total_size(self):
        """
        HEAD 요청으로 total_size를 구한다.
        """
        response = requests.head(self.video_url)
        response.raise_for_status()
        return int(response.headers.get('content-length', 0))

    def _decide_part_size(self):
        """
        해상도에 따라 파트 크기 가중치를 달리 부여한다.
        """
        base_part_size = 1024 * 1024  # 1MB
        if self.height == '144':
            return base_part_size * 1
        elif self.height in ['360', '480']:
            return base_part_size * 2
        elif self.height == '720':
            return base_part_size * 5
        else:
            return base_part_size * 10

    def _get_remaining_ranges(self, ranges):
        """
        중단 이후 재시작 같은 상황 고려(현재 파일크기 등을 바탕으로),
        아직 다운로드되지 않은 구간만 남겨 반환한다.
        """
        with open(self.output_path, 'r+b') as f:
            f.seek(0, 2)
            file_size = f.tell()

        remaining = []
        for start, end in ranges:
            if start >= file_size or end >= file_size:
                remaining.append((start, end))
        return remaining

    # ============ 진행 상황 업데이트 / 제어 메서드 ============

    def update_progress(self, total_size=None):
        # print("다운로드 현황을 업데이트 합니다.") # Debugging
        """
        진행률, 다운로드 속도, 예상 남은 시간 등 정보를 계산 후 시그널로 전송한다.
        """
        if self._is_stopped or self._is_paused:  # 중단 플래그 확인
            # print(self._is_stopped, self._is_paused) # Debugging
            return
        if total_size is None:
            # 콜백에서 이 메서드를 호출할 때 total_size를 명시적으로 안 넘겼다면
            # 계산된 total_size가 이미 변경된 적은 없으므로 그냥 리턴
            return

        active_downloaded_size = sum(self.thread_progress)
        total_downloaded_size = self.completed_progress + active_downloaded_size

        elapsed_time = time() - self.start_time
        elapsed_time_str = strftime('%H:%M:%S', gmtime(elapsed_time))

        # 초당 MB 단위 속도 계산
        total_active_speed = total_downloaded_size / elapsed_time / 1024 / 1024
        self.total_active_speed = total_active_speed

        avg_active_speed = (
            total_active_speed / self.future_count if self.future_count > 0 else 0
        )
        progress = int((total_downloaded_size / total_size) * 100)

        status_message = (
            f"{total_downloaded_size / (1024 * 1024):.2f}MB/"
            f"{total_size / (1024 * 1024):.2f}MB "
            f"({total_active_speed:.1f} MB/s)"
        )

        if total_active_speed > 0:
            remaining_time = (
                (total_size - total_downloaded_size)
                / (total_active_speed * 1024 * 1024)
            )
            completion_time = elapsed_time + remaining_time
            completion_time_str = strftime('%H:%M:%S', gmtime(completion_time))
        else:
            completion_time_str = "N/A"

        # 시그널 전송
        self.progress.emit(progress, status_message)
        self.update_time.emit(elapsed_time_str, completion_time_str)
        self.update_avg_speed.emit(avg_active_speed)

    # ============ 일시정지/중지 메서드 ============

    def pause(self):
        # print("다운로드를 정지합니다.") # Debugging
        self._is_paused = True

    def resume(self):
        # print("다운로드를 재개합니다.") # Debugging
        self._is_paused = False
        self.resumed.emit()

    def stop(self):
        # print("다운로드를 중단합니다.") # Debugging
        self._is_paused = False
        self._is_stopped = True

        self.stopped.emit("다운로드 중단")
