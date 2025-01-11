import re
import requests
import xml.etree.ElementTree as ET

def extract_video_no(vod_url):
    """
    치지직 VOD URL에서 video_no를 추출한다.
    """
    if not vod_url.startswith("http://") and not vod_url.startswith("https://"):
        vod_url = "https://" + vod_url
    match = re.fullmatch(r'https?://chzzk\.naver\.com/video/(?P<video_no>\d+)', vod_url)
    return match.group("video_no") if match else None

def get_video_info(video_no, cookies):
    """
    API를 통해 video_no에 대응하는 video_id, in_key, 메타데이터를 가져온다.
    """
    api_url = f"https://api.chzzk.naver.com/service/v2/videos/{video_no}"
    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(api_url, cookies=cookies, headers=headers)
    response.raise_for_status()

    content = response.json().get('content', {})
    video_id = content.get('videoId')
    in_key = content.get('inKey')

    metadata = {
        'title': content.get('videoTitle', 'Unknown Title'),
        'thumbnailImageUrl': content.get('thumbnailImageUrl', ''),
        'videoCategoryValue': content.get('videoCategoryValue', 'Unknown Category'),
        'channelName': content.get('channel', {}).get('channelName', 'Unknown Channel'),
        'channelImageUrl': content.get('channel', {}).get('channelImageUrl', ''),
        'liveOpenDate': content.get('liveOpenDate', 'Unknown Date'),
        'duration': content.get('duration', 0),
    }
    return video_id, in_key, metadata

def get_dash_manifest(video_id, in_key):
    """
    DASH 매니페스트를 요청하여 Representation 목록을 파싱한다.
    """
    manifest_url = f"https://apis.naver.com/neonplayer/vodplay/v2/playback/{video_id}?key={in_key}"
    headers = {"Accept": "application/dash+xml"}
    response = requests.get(manifest_url, headers=headers)
    response.raise_for_status()

    root = ET.fromstring(response.text)
    ns = {"mpd": "urn:mpeg:dash:schema:mpd:2011"}
    reps = []
    for rep in root.findall(".//mpd:Representation", namespaces=ns):
        width = rep.get('width')
        height = rep.get('height')
        base_url = rep.find(".//mpd:BaseURL", namespaces=ns).text

        if base_url.endswith('/hls/'):
            continue

        reps.append((width, height, base_url))

    # 중복 제거한 뒤, 리스트로 변환
    return list(set(reps))
