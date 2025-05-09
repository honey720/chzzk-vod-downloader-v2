from content.network import NetworkManager

from PySide6.QtCore import QObject, Signal

class ContentWorker(QObject):
    # 작업이 완료되면 결과를 tuple로 전달 (혹은 필요한 데이터 구조로 전달)
    finished = Signal(object, str)
    error = Signal(str)
    
    def __init__(self, vod_url: str, cookies: dict, downloadPath: str):
        super().__init__()
        self.vod_url = vod_url
        self.cookies = cookies
        self.downloadPath = downloadPath

    def run(self):
        try:
            content_type, content_no = NetworkManager.extract_content_no(self.vod_url)
            if not content_type or not content_no:
                errorMessage = self.tr("Invalid VOD URL")
                raise ValueError(f"{self.vod_url}\n{errorMessage}")
            
            if content_type == 'video':
                result = self.fetchVideo(content_no)
            elif content_type == 'clips':
                content_type = 'clip'
                result = self.fetchClip(content_no)
                
            self.finished.emit(result, content_type)
        except Exception as e:
            self.error.emit(str(e))

    def fetchVideo(self, video_no: str):
        video_id, in_key, adult, vodStatus, metadata = NetworkManager.get_video_info(video_no, self.cookies)
        if adult and not video_id:
            errorMessage = self.tr("Invalid cookies value")
            raise ValueError(f"{self.vod_url}\n{errorMessage}")
        elif vodStatus == 'NONE':
            errorMessage = self.tr("Unencoded Video(.m3u8)")
            raise ValueError(f"{self.vod_url}\n{errorMessage}")
        
        unique_reps, resolution, base_url = NetworkManager.get_video_dash_manifest(video_id, in_key)
        if not unique_reps:
            errorMessage = self.tr("Failed to get DASH manifest")
            raise ValueError(f"{self.vod_url}\n{errorMessage}")
        
        # 네트워크 작업 결과를 tuple 형태로 묶어서 전달
        return (self.vod_url, metadata, unique_reps, resolution, base_url, self.downloadPath)
            

    def fetchClip(self, clip_no: str):
        video_id, vodStatus, metadata = NetworkManager.get_clip_info(clip_no, self.cookies)
        if vodStatus == 'NONE':
            errorMessage = self.tr("Unencoded Video(.m3u8)")
            raise ValueError(f"{self.vod_url}\n{errorMessage}")
        
        unique_reps, resolution, base_url, error = NetworkManager.get_clip_manifest(video_id, self.cookies)
        if error and error.get("errorCode") == "ADULT_AUTH_REQUIRED":
            errorMessage = self.tr("Invalid cookies value")
            raise ValueError(f"{self.vod_url}\n{errorMessage}")
        
        if not unique_reps:
            errorMessage = self.tr("Failed to get DASH manifest")
            raise ValueError(f"{self.vod_url}\n{errorMessage}")
        
        return (self.vod_url, metadata, unique_reps, resolution, base_url, self.downloadPath)
