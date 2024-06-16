import sys
import re
import requests
import xml.etree.ElementTree as ET
import threading
from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QLineEdit, QPushButton, QLabel, QFileDialog, QProgressBar, QSpinBox, QHBoxLayout)
from PyQt5.QtCore import QThread, pyqtSignal, Qt
from PyQt5.QtGui import QPixmap
from io import BytesIO
from time import time, strftime, gmtime
from concurrent.futures import ThreadPoolExecutor

class DownloadThread(QThread):
    progress = pyqtSignal(int, str)
    completed = pyqtSignal(str)
    paused = pyqtSignal()
    resumed = pyqtSignal()
    stopped = pyqtSignal()
    ready = pyqtSignal()
    update_threads = pyqtSignal(int, int, int)
    update_time = pyqtSignal(str, str)

    def __init__(self, video_url, output_path, num_threads=8):
        super().__init__()
        self.video_url = video_url
        self.output_path = output_path
        self.num_threads = num_threads
        self._is_paused = False
        self._is_stopped = False
        self.thread_progress = [0] * num_threads
        self.thread_speed = [0] * num_threads
        self.lock = threading.Lock()
        self.completed_threads = 0
        self.failed_threads = 0
        self.completed_progress = 0

    def run(self):
        try:
            self.ready.emit()
            self.start_time = time()
            response = requests.head(self.video_url)
            response.raise_for_status()

            total_size = int(response.headers.get('content-length', 0))
            part_size = total_size // self.num_threads
            ranges = [(i * part_size, (i + 1) * part_size - 1) for i in range(self.num_threads)]
            ranges[-1] = (ranges[-1][0], total_size - 1)

            with open(self.output_path, 'wb') as f:
                f.truncate(total_size)

            def download_part(start, end, part_num):
                try:
                    headers = {'Range': f'bytes={start}-{end}'}
                    response = requests.get(self.video_url, headers=headers, stream=True)
                    response.raise_for_status()
                    downloaded_size = 0
                    part_start_time = time()
                    with open(self.output_path, 'r+b') as f:
                        f.seek(start)
                        for chunk in response.iter_content(chunk_size=8192):
                            if self._is_stopped:
                                return
                            while self._is_paused:
                                self.paused.emit()
                                self.msleep(100)
                            if chunk:
                                f.write(chunk)
                                downloaded_size += len(chunk)
                                elapsed_time = time() - part_start_time
                                if elapsed_time > 0:
                                    with self.lock:
                                        self.thread_speed[part_num] = downloaded_size / elapsed_time / 1024
                                        self.thread_progress[part_num] = downloaded_size
                                        self.update_progress(total_size)
                    with self.lock:
                        self.completed_threads += 1
                        self.completed_progress += self.thread_progress[part_num]
                        self.thread_progress[part_num] = 0
                        self.update_threads.emit(self.completed_threads, self.num_threads, self.failed_threads)
                    return part_num
                except requests.RequestException as e:
                    with self.lock:
                        self.failed_threads += 1
                        self.update_threads.emit(self.completed_threads, self.num_threads, self.failed_threads)
                    print(f"다운로드 실패 (스레드 {part_num}): {e}")
                    return None

            with ThreadPoolExecutor(max_workers=self.num_threads) as executor:
                futures = [executor.submit(download_part, start, end, part_num) for part_num, (start, end) in enumerate(ranges)]
                for future in futures:
                    try:
                        future.result()
                    except Exception as e:
                        print(f"다운로드 실패: 일부 스레드가 오류로 인해 중단되었습니다. {e}")
                        self.stopped.emit()
                        return

            if not self._is_stopped:
                self.update_progress(total_size)
                self.completed.emit("다운로드 완료!")
            else:
                self.stopped.emit()
        except requests.RequestException as e:
            print(f"다운로드 실패: {e}")
            self.stopped.emit()

    def update_progress(self, total_size):
        active_downloaded_size = sum(self.thread_progress)
        total_downloaded_size = self.completed_progress + active_downloaded_size
        total_speed = sum(self.thread_speed)
        progress = int((total_downloaded_size / total_size) * 100)
        status_message = f"{total_downloaded_size / (1024 * 1024):.2f}MB/{total_size / (1024 * 1024):.2f}MB ({total_speed / 1024:.1f} MB/s)"
        elapsed_time = time() - self.start_time
        elapsed_time_str = strftime('%H:%M:%S', gmtime(elapsed_time))
        if total_speed > 0:
            remaining_time = (total_size - total_downloaded_size) / (total_speed * 1024)
            completion_time = elapsed_time + remaining_time
            completion_time_str = strftime('%H:%M:%S', gmtime(completion_time))
        else:
            completion_time_str = "N/A"
        self.progress.emit(progress, status_message)
        self.update_time.emit(elapsed_time_str, completion_time_str)

    def pause(self):
        self._is_paused = True

    def resume(self):
        self._is_paused = False
        self.resumed.emit()

    def stop(self):
        self._is_stopped = True
        self._is_paused = False

def extract_video_no(vod_url):
    if not vod_url.startswith("http://") and not vod_url.startswith("https://"):
        vod_url = "https://" + vod_url
    match = re.match(r'https?://chzzk\.naver\.com/video/(?P<video_no>\d+)', vod_url)
    if match:
        return match.group("video_no"), vod_url
    return None, vod_url

def get_video_info(video_no):
    api_url = f"https://api.chzzk.naver.com/service/v2/videos/{video_no}"
    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(api_url, headers=headers)
    response.raise_for_status()
    content = response.json().get('content', {})
    video_id = content.get('videoId')
    in_key = content.get('inKey')

    metadata = {
        'title': content.get('videoTitle', 'Unknown Title'),
        'thumbnailImageUrl': content.get('thumbnailImageUrl', 'Unknown URL'),
        'videoCategoryValue': content.get('videoCategoryValue', 'Unknown Category'),
        'channelName': content.get('channel', {}).get('channelName', 'Unknown Channel'),
        'channelImageUrl': content.get('channel', {}).get('channelImageUrl', 'Unknown URL'),
        'liveOpenDate': content.get('liveOpenDate', 'Unknown Date'),
        'duration': content.get('duration', 0)
    }

    return video_id, in_key, metadata

def get_dash_manifest(video_id, in_key):
    video_url = f"https://apis.naver.com/neonplayer/vodplay/v2/playback/{video_id}?key={in_key}"
    headers = {"Accept": "application/dash+xml"}
    response = requests.get(video_url, headers=headers)
    response.raise_for_status()
    root = ET.fromstring(response.text)
    ns = {"mpd": "urn:mpeg:dash:schema:mpd:2011", "nvod": "urn:naver:vod:2020"}
    base_url_element = root.find(".//mpd:BaseURL", namespaces=ns)
    return base_url_element.text if base_url_element is not None else None

def get_video_base_url(vod_url):
    video_no, vod_url = extract_video_no(vod_url)
    if not video_no:
        raise ValueError("Invalid VOD URL")
    
    video_id, in_key, metadata = get_video_info(video_no)
    if not video_id or not in_key:
        raise ValueError("Failed to get video info")

    base_url = get_dash_manifest(video_id, in_key)
    if not base_url:
        raise ValueError("Failed to get DASH manifest")
    
    return base_url, metadata

class VodDownloader(QWidget):
    def __init__(self):
        super().__init__()
        self.downloadThread = None
        self.metadata = None
        self.initUI()

    def initUI(self):
        layout = QVBoxLayout()

        self.urlInput = QLineEdit(self)
        self.urlInput.setPlaceholderText("치지직 URL을 입력하세요.")
        layout.addWidget(self.urlInput)

        self.fetchButton = QPushButton('Fetch Resolutions', self)
        self.fetchButton.clicked.connect(self.onFetch)
        layout.addWidget(self.fetchButton)

        self.channelNameLabel = QLabel(self)
        layout.addWidget(self.channelNameLabel)

        self.channelImageLabel = QLabel(self)
        self.channelImageLabel.setScaledContents(True)
        self.channelImageLabel.setFixedSize(100, 100)
        layout.addWidget(self.channelImageLabel)

        self.titleLabel = QLabel(self)
        layout.addWidget(self.titleLabel)

        self.durationLabel = QLabel(self)
        layout.addWidget(self.durationLabel)

        self.categoryLabel = QLabel(self)
        layout.addWidget(self.categoryLabel)

        self.liveOpenDateLabel = QLabel(self)
        layout.addWidget(self.liveOpenDateLabel)

        self.thumbnailLabel = QLabel(self)
        self.thumbnailLabel.setScaledContents(True)
        self.thumbnailLabel.setFixedSize(256, 144)
        layout.addWidget(self.thumbnailLabel)

        self.linkStatusLabel = QLabel('', self)
        layout.addWidget(self.linkStatusLabel)

        self.resolutionLabel = QLabel('Available Resolutions:', self)
        layout.addWidget(self.resolutionLabel)

        self.resolutionButtonsLayout = QVBoxLayout()
        layout.addLayout(self.resolutionButtonsLayout)

        self.threadsLabel = QLabel('Threads: (1~128)', self)
        layout.addWidget(self.threadsLabel)

        self.threadsInput = QSpinBox(self)
        self.threadsInput.setRange(1, 128)
        self.threadsInput.setValue(8)
        layout.addWidget(self.threadsInput)

        self.progressBar = QProgressBar(self)
        layout.addWidget(self.progressBar)

        self.downloadStatusLabel = QLabel('', self)
        layout.addWidget(self.downloadStatusLabel)

        time_layout = QHBoxLayout()
        self.timeLabel = QLabel('', self)
        time_layout.addWidget(self.timeLabel)
        layout.addLayout(time_layout)

        self.pauseResumeButton = QPushButton('Pause', self)
        self.pauseResumeButton.clicked.connect(self.onPauseResume)
        self.pauseResumeButton.setEnabled(False)
        layout.addWidget(self.pauseResumeButton)

        self.stopButton = QPushButton('Stop', self)
        self.stopButton.clicked.connect(self.onStop)
        self.stopButton.setEnabled(False)
        layout.addWidget(self.stopButton)

        self.threadStatusLabel = QLabel('', self)
        layout.addWidget(self.threadStatusLabel)

        self.setLayout(layout)
        self.setWindowTitle('치지직 VOD 다운로더')
        self.setGeometry(300, 300, 300, 300)

    def clear_resolutions(self):
        while self.resolutionButtonsLayout.count():
            child = self.resolutionButtonsLayout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

    def set_resolution_buttons_enabled(self, enabled):
        for i in range(self.resolutionButtonsLayout.count()):
            self.resolutionButtonsLayout.itemAt(i).widget().setEnabled(enabled)

    def clear_metadata_display(self):
        self.channelNameLabel.clear()
        self.titleLabel.clear()
        self.durationLabel.clear()
        self.categoryLabel.clear()
        self.liveOpenDateLabel.clear()
        self.channelImageLabel.clear()
        self.thumbnailLabel.clear()
        self.progressBar.setValue(0)
        self.downloadStatusLabel.clear()
        self.timeLabel.clear()
        self.threadStatusLabel.clear()

    def onFetch(self):
        vod_url = self.urlInput.text()
        self.clear_metadata_display()
        try:
            self.clear_resolutions()
            self.linkStatusLabel.setText('Fetching resolutions...')

            video_no, vod_url = extract_video_no(vod_url)
            if not video_no:
                raise ValueError("Invalid VOD URL")

            video_id, in_key, metadata = get_video_info(video_no)
            if not video_id or not in_key:
                raise ValueError("Failed to get video info")

            self.metadata = metadata

            manifest_url = f"https://apis.naver.com/neonplayer/vodplay/v2/playback/{video_id}?key={in_key}"
            response = requests.get(manifest_url, headers={"Accept": "application/dash+xml"})
            response.raise_for_status()
            root = ET.fromstring(response.text)

            self.display_metadata(metadata)

            self.representations = []
            unique_reps = set()
            ns = {"mpd": "urn:mpeg:dash:schema:mpd:2011", "nvod": "urn:naver:vod:2020"}
            for rep in root.findall(".//mpd:Representation", namespaces=ns):
                width = rep.get('width')
                height = rep.get('height')
                base_url = rep.find(".//mpd:BaseURL", namespaces=ns).text
                resolution = f"{width}x{height}"

                if resolution not in unique_reps:
                    unique_reps.add(resolution)
                    self.representations.append((width, height, base_url))
                    self.add_representation_button(width, height, base_url)
            self.linkStatusLabel.setText('Resolutions fetched successfully.')

        except Exception as e:
            self.linkStatusLabel.setText(f'오류 발생: {e}')

    def display_metadata(self, metadata):
        title = metadata.get('title', 'Unknown Title')
        thumbnail_url = metadata.get('thumbnailImageUrl', 'Unknown URL')
        category = metadata.get('videoCategoryValue', 'Unknown Category')
        channel_name = metadata.get('channelName', 'Unknown Channel')
        channel_image_url = metadata.get('channelImageUrl', 'Unknown URL')
        live_open_date = metadata.get('liveOpenDate', 'Unknown Date')
        duration = metadata.get('duration', 0)

        duration_str = f"{duration // 3600}시간 {(duration % 3600) // 60}분 {duration % 60}초"

        self.channelNameLabel.setText(f"Channel Name: {channel_name}")
        self.titleLabel.setText(f"Title: {title}")
        self.categoryLabel.setText(f"Category: {category}")
        self.liveOpenDateLabel.setText(f"Live Open Date: {live_open_date}")
        self.durationLabel.setText(f"Duration: {duration_str}")

        self.load_image_from_url(self.channelImageLabel, channel_image_url, 100, 100)
        self.load_image_from_url(self.thumbnailLabel, thumbnail_url, 256, 144)

    def load_image_from_url(self, label, url, width, height):
        response = requests.get(url)
        image = QPixmap()
        image.loadFromData(BytesIO(response.content).read())
        scaled_image = image.scaled(width, height, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        label.setPixmap(scaled_image)

    def add_representation_button(self, width, height, base_url):
        button = QPushButton(f'{width}x{height}', self)
        button.clicked.connect(lambda: self.onDownload(base_url))
        self.resolutionButtonsLayout.addWidget(button)

    def onDownload(self, base_url):
        if self.metadata:
            title = self.metadata.get('title', 'Unknown Title')
            category = self.metadata.get('videoCategoryValue', 'Unknown Category')
            live_open_date = self.metadata.get('liveOpenDate', 'Unknown Date')

            default_filename = f"{live_open_date.split(' ')[0]}) [{category}] {title}.mp4"
        else:
            default_filename = "video.mp4"

        options = QFileDialog.Options()
        output_path, _ = QFileDialog.getSaveFileName(self, "Save Video File", default_filename, "Video Files (*.mp4);;All Files (*)", options=options)
        
        if output_path:
            self.fetchButton.setEnabled(False)
            self.set_resolution_buttons_enabled(False)

            num_threads = self.threadsInput.value()

            self.downloadThread = DownloadThread(base_url, output_path, num_threads=num_threads)
            self.downloadThread.ready.connect(self.updateDownloadStatus)
            self.downloadThread.progress.connect(self.updateProgress)
            self.downloadThread.completed.connect(self.onDownloadCompleted)
            self.downloadThread.paused.connect(self.onPaused)
            self.downloadThread.resumed.connect(self.onResumed)
            self.downloadThread.stopped.connect(self.onStopped)
            self.downloadThread.update_threads.connect(self.updateThreadStatus)
            self.downloadThread.update_time.connect(self.updateTimeStatus)
            self.downloadThread.start()
            self.pauseResumeButton.setEnabled(True)
            self.stopButton.setEnabled(True)

    def updateDownloadStatus(self):
        self.downloadStatusLabel.setText("다운로드 준비 중...")
        self.updateThreadStatus(0, self.threadsInput.value(), 0)  # 초기 스레드 상태 표시
        self.updateTimeStatus("00:00:00", "00:00:00")

    def onPauseResume(self):
        if self.downloadThread:
            if self.pauseResumeButton.text() == 'Pause':
                self.downloadThread.pause()
                self.pauseResumeButton.setText('Resume')
            else:
                self.downloadThread.resume()
                self.pauseResumeButton.setText('Pause')

    def onStop(self):
        if self.downloadThread:
            self.downloadThread.stop()
            self.pauseResumeButton.setEnabled(False)
            self.stopButton.setEnabled(False)

    def updateProgress(self, value, status_message):
        self.progressBar.setValue(value)
        self.downloadStatusLabel.setText(status_message)

    def onDownloadCompleted(self, message):
        self.downloadStatusLabel.setText(message)
        self.pauseResumeButton.setEnabled(False)
        self.stopButton.setEnabled(False)
        self.fetchButton.setEnabled(True)
        self.set_resolution_buttons_enabled(True)

    def onPaused(self):
        self.downloadStatusLabel.setText("다운로드 일시정지")

    def onResumed(self):
        self.downloadStatusLabel.setText("다운로드 재개")

    def onStopped(self):
        self.downloadStatusLabel.setText("다운로드 중지됨")
        self.fetchButton.setEnabled(True)
        self.set_resolution_buttons_enabled(True)

    def updateThreadStatus(self, completed, total, failed):
        self.threadStatusLabel.setText(f"Completed Threads: {completed}/{total} (Failed: {failed})")

    def updateTimeStatus(self, elapsed, remaining):
        self.timeLabel.setText(f'경과 시간/예상 시간: {elapsed}/{remaining}')

if __name__ == '__main__':
    app = QApplication(sys.argv)
    ex = VodDownloader()
    ex.show()
    sys.exit(app.exec_())
