from metadata.network import NetworkManager

from PySide6.QtCore import QObject, Signal

class MetadataWorker(QObject):
    # 작업이 완료되면 결과를 tuple로 전달 (혹은 필요한 데이터 구조로 전달)
    finished = Signal(object)
    error = Signal(str)
    
    def __init__(self, vod_url: str, cookies: dict, downloadPath: str):
        super().__init__()
        self.vod_url = vod_url
        self.cookies = cookies
        self.downloadPath = downloadPath

    def run(self):
        try:
            video_no = NetworkManager.extract_video_no(self.vod_url)
            if not video_no:
                errorMessage = self.tr("Invalid VOD URL")
                raise ValueError(f"{self.vod_url}\n{errorMessage}")
        
            video_id, in_key, adult, vodStatus, metadata = NetworkManager.get_video_info(video_no, self.cookies)
            if adult and not video_id:
                errorMessage = self.tr("Invalid cookies value")
                raise ValueError(f"{self.vod_url}\n{errorMessage}")
            elif vodStatus == 'NONE':
                errorMessage = self.tr("Unencoded Video(.m3u8)")
                raise ValueError(f"{self.vod_url}\n{errorMessage}")
            
            unique_reps, resolution, base_url = NetworkManager.get_dash_manifest(video_id, in_key)
            if not unique_reps:
                errorMessage = self.tr("Failed to get DASH manifest")
                raise ValueError(f"{self.vod_url}\n{errorMessage}")
            
            # 네트워크 작업 결과를 tuple 형태로 묶어서 전달
            result = (self.vod_url, metadata, unique_reps, resolution, base_url, self.downloadPath, self.cookies)
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))