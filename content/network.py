import re
import requests
import json
import threading
from http.cookiejar import DefaultCookiePolicy
from urllib.parse import urljoin
import xml.etree.ElementTree as ET

from core.api.url_parser import extract_content_no

NAVER_API = "https://apis.naver.com"
CHZZK_API = "https://api.chzzk.naver.com"
VIDEOHUB_API = "https://api-videohub.naver.com"


def _make_session() -> requests.Session:
    """연결 재사용용 Session을 만든다 (#31).

    기존에는 매 호출 requests.get()이 독립적이라 응답의 Set-Cookie가 다음 요청으로
    이어지지 않았다. 이 이슈는 연결 재사용만 도입하므로, 응답 쿠키 저장을 차단해
    쿠키 동작을 기존과 동일하게 유지한다. 요청별 cookies= 인자는 그대로 전송된다.
    """
    session = requests.Session()
    session.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
    return session


# NetworkManager API 호출용 모듈 수준 공유 세션.
# 응답 쿠키를 저장하지 않으므로 여러 스레드에서 호출해도 상태 공유 문제가 없다.
_session = _make_session()

# 다운로드 워커용 스레드로컬 세션 저장소
_thread_local = threading.local()


def get_thread_session() -> requests.Session:
    """호출한 스레드 전용 Session을 반환한다 (없으면 생성).

    requests.Session은 스레드 간 완전한 안전이 보장되지 않으므로, 다운로드 워커처럼
    동시 요청이 많은 경로는 스레드마다 별도 Session을 사용해 연결 재사용만 취한다.
    """
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = _make_session()
        _thread_local.session = session
    return session


class NetworkManager:

    @staticmethod
    def extract_content_no(vod_url: str) -> tuple[str, str]:
        """
        치지직 VOD URL에서 type과 content_no를 추출한다.

        구현은 core/api/url_parser.py로 이동했다 (#50).
        기존 호출부 호환을 위해 시그니처를 유지하고 core 함수에 위임한다.

        Args:
            vod_url (str): 치지직 VOD URL

        Returns:
            tuple[str, str]: (type, content_no) 형식의 튜플. 매칭되지 않으면 (None, None) 반환
        """
        return extract_content_no(vod_url)

    @staticmethod
    def get_video_info(video_no: str, cookies: dict):
        """
        API를 통해 video_no에 대응하는 video_id, in_key, 메타데이터를 가져온다.
        """
        api_url = f"{CHZZK_API}/service/v2/videos/{video_no}"
        headers = {"User-Agent": "Mozilla/5.0"}
        response = _session.get(api_url, cookies=cookies, headers=headers)
        response.raise_for_status()

        content = response.json().get('content', {})
        video_id = content.get('videoId')
        in_key = content.get('inKey')
        adult = content.get('adult')
        vodStatus = content.get('vodStatus')
        liveRewindPlaybackJson = content.get('liveRewindPlaybackJson')

        metadata = {
            'title': re.sub(r'[\\/:\*\?"<>|\n]', '', content.get('videoTitle', 'Unknown Title')), # 정규식으로 특수문자 제거
            'thumbnailImageUrl': content.get('thumbnailImageUrl', ''),
            'category': content.get('videoCategoryValue', 'Unknown Category'),
            'channelName': content.get('channel', {}).get('channelName', 'Unknown Channel'),
            'channelImageUrl': content.get('channel', {}).get('channelImageUrl', ''),
            'createdDate': content.get('liveOpenDate', 'Unknown Date'),
            'duration': content.get('duration', 0),
        }
        return video_id, in_key, adult, vodStatus, liveRewindPlaybackJson, metadata
    
    @staticmethod
    def get_video_dash_manifest(video_id: str, in_key: str):
        """
        DASH 매니페스트를 요청하여 Representation 목록을 파싱한다.
        """
        manifest_url = f"{NAVER_API}/neonplayer/vodplay/v2/playback/{video_id}?key={in_key}"
        headers = {"Accept": "application/dash+xml"}
        response = _session.get(manifest_url, headers=headers)
        response.raise_for_status()

        root = ET.fromstring(response.text)
        ns = {"mpd": "urn:mpeg:dash:schema:mpd:2011"}
        reps = []
        for rep in root.findall(".//mpd:Representation", namespaces=ns):
            width = rep.get('width')
            height = rep.get('height')
            # 오디오 전용 Representation(audio/mp4)은 width/height 속성이 없다.
            # 해상도를 계산할 수 없으므로 목록에서 제외한다 (#38)
            if width is None or height is None:
                continue
            resolution = min(int(width), int(height))
            # print(width, height) # Debugging
            # print(f"Resolution: {resolution}") # Debugging
            base_url = rep.find(".//mpd:BaseURL", namespaces=ns).text
            if base_url.endswith('/hls/'):
                continue
            reps.append([resolution, base_url])
        
        sorted_reps = sorted(reps, key=lambda x: x[0])
        auto_resolution = sorted_reps[-1][0]
        auto_base_url = sorted_reps[-1][1]

        # 중복 제거한 뒤, 리스트로 변환
        return sorted_reps, auto_resolution, auto_base_url
    
    @staticmethod
    def get_video_m3u8_manifest(json_str: str):
        """
        m3u8 정보가 포함된 json형식의 문자열을 받아서 Representation 목록을 파싱한다.
        """
        data = json.loads(json_str)
        media = data.get("media", [])
        encoding_track = media[0].get("encodingTrack", [])
        reps = []
        for encoding in encoding_track:
            width = encoding.get("videoWidth")
            height = encoding.get("videoHeight")
            resolution = min(int(width), int(height))
            base_url = None
            reps.append([resolution, base_url])

        sorted_reps = sorted(reps, key=lambda x: x[0])
        auto_resolution = sorted_reps[-1][0]
        auto_base_url = sorted_reps[-1][1]
        return sorted_reps, auto_resolution, auto_base_url
    
    @staticmethod
    def get_video_m3u8_base_url(json_str: str, resolution: int) -> str:
        """
        m3u8 정보가 포함된 json형식의 문자열을 받아서 base_url을 파싱한다.
        """
        data = json.loads(json_str)
        media = data.get("media", [])
        path = media[0].get("path")
        response = _session.get(path)
        response.raise_for_status()
        content = response.text.splitlines()

        # 정규식으로 해상도 매칭
        resolution_pattern = re.compile(rf"RESOLUTION=\d+x{resolution}")
        
        for i, line in enumerate(content):
            if resolution_pattern.search(line):
                # 다음 줄이 해당 해상도의 세부 플레이리스트 경로
                relative_path = content[i + 1].strip()
                base_url = urljoin(path, relative_path)
                return base_url

        raise ValueError(f"{resolution} 해상도 스트림을 찾을 수 없습니다.")
    
    @staticmethod
    def get_clip_info(clip_no: str, cookies: dict):
        """
        API를 통해 clip_no에 대응하는 clip_id, in_key, 메타데이터를 가져온다.
        """
        api_url = f"{CHZZK_API}/service/v1/clips/{clip_no}/detail?optionalProperties=OWNER_CHANNEL"
        headers = {"User-Agent": "Mozilla/5.0"}
        response = _session.get(api_url, cookies=cookies, headers=headers)
        response.raise_for_status()

        content = response.json().get('content', {})
        video_id = content.get('videoId')
        vodStatus = content.get('vodStatus')

        metadata = {
            'title': re.sub(r'[\\/:\*\?"<>|\n]', '', content.get('clipTitle', 'Unknown Title')), # 정규식으로 특수문자 제거
            'thumbnailImageUrl': content.get('thumbnailImageUrl', ''),
            'category': content.get('clipCategory', 'Unknown Category'),
            'channelName': content.get('optionalProperty', {}).get('ownerChannel', {}).get('channelName', 'Unknown Channel'),
            'channelImageUrl': content.get('optionalProperty', {}).get('ownerChannel', {}).get('channelImageUrl', ''),
            'createdDate': content.get('createdDate', 'Unknown Date'),
            'duration': content.get('duration', 0),
        }
        return video_id, vodStatus, metadata

    @staticmethod
    def get_clip_manifest(clip_id: str, cookies: dict):
        """
        DASH 매니페스트를 요청하여 Representation 목록을 파싱한다.
        """
        manifest_url = f"{VIDEOHUB_API}/shortformhub/feeds/v3/card?serviceType=CHZZK&seedMediaId={clip_id}&mediaType=VOD"
        headers = {"User-Agent": "Mozilla/5.0"}
        response = _session.get(manifest_url, cookies=cookies, headers=headers)
        response.raise_for_status()

        data = response.json()

        resolutions = []

        #오류현상 예외처리

        content = data['card']['content']
        if 'error' in content:
            error = content['error']
            return None, None, None, error
        
        video_list = content['vod']['playback']['videos']['list']

        for video in video_list:
            encoding = video.get("encodingOption", {})
            width = encoding.get("width")
            height = encoding.get("height")
            source_url = video.get("source")

            if width and height and source_url:
                resolution = min(int(width), int(height))
                resolutions.append([resolution, source_url])

        sorted_resolutions = sorted(resolutions, key=lambda x: x[0])
        auto_resolution = sorted_resolutions[-1][0]
        auto_base_url = sorted_resolutions[-1][1]

        return sorted_resolutions, auto_resolution, auto_base_url, None