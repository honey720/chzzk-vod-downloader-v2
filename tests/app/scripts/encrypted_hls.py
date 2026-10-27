"""테스트용 암호화 HLS(MPEG-TS) 만들기 — ffmpeg로 만든 영상을 AES-128-CBC로 암호화한다 (#309).

실제 ffmpeg(imageio-ffmpeg 동봉)로 짧은 영상을 HLS MPEG-TS로 만들고, 세그먼트마다
미디어 시퀀스 번호를 IV로 삼아 암호화한다. 플레이리스트에는 ``#EXT-X-KEY``를 넣는다.
저장소에 영상 파일을 두지 않는다.

만드는 영상: 320x240 · 30fps · 6초 · 1초 세그먼트 · 키프레임 15프레임마다 · B프레임(재정렬
지연 1) · 오디오 48kHz AAC. 영상이 오디오보다 ``LEAD``초 늦게 시작한다.
"""

import subprocess

from Crypto.Cipher import AES

from core.api.hls import HlsPlaylist, parse_media_playlist
from core.downloaders.decrypt import sequence_iv
from core.utils.ffmpeg import get_ffmpeg_exe

FPS = 30
KEY_EVERY = 15  # 키프레임 간격(프레임)
SEGMENT_FRAMES = 30  # 세그먼트 하나의 프레임 수(1초)
LEAD = 0.042667  # 영상이 오디오보다 늦게 시작하는 만큼(초) — 첫 영상 프레임의 VOD 시각


def make_encrypted_hls(folder, key: bytes, key_uri: str) -> tuple[HlsPlaylist, dict[str, bytes]]:
    """folder에 HLS MPEG-TS를 만들고, 암호화한 파일들을 돌려준다.

    Args:
        folder: ffmpeg가 플레이리스트와 세그먼트를 쓸 폴더(``pathlib.Path``)
        key: 16바이트 AES-128 키
        key_uri: 플레이리스트의 ``#EXT-X-KEY``에 적을 키 주소

    Returns:
        (플레이리스트, 파일 이름 → bytes). 파일은 ``media.m3u8``(키 태그가 든 것)과
        암호화한 ``segment-NNNNNN.ts``들이다
    """
    done = subprocess.run(
        [get_ffmpeg_exe(), "-hide_banner", "-v", "error", "-y",
         "-itsoffset", str(LEAD), "-f", "lavfi",
         "-i", f"testsrc2=size=320x240:rate={FPS}:duration=6",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6",
         "-c:a", "aac", "-ac", "2", "-pix_fmt", "yuv420p", "-c:v", "libx264",
         "-preset", "veryfast", "-bf", "2",
         "-force_key_frames", f"expr:gte(n,n_forced*{KEY_EVERY})",
         "-x264-params", "b-pyramid=none:keyint=300:min-keyint=1:scenecut=0",
         "-f", "hls", "-hls_time", "1", "-hls_list_size", "0",
         "-hls_segment_filename", "segment-%06d.ts", "media.m3u8"],
        cwd=str(folder),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    text = (folder / "media.m3u8").read_text(encoding="utf-8")
    playlist = parse_media_playlist(text)
    keyed = text.replace("#EXTINF", f'#EXT-X-KEY:METHOD=AES-128,URI="{key_uri}"\n#EXTINF', 1)
    files = {"media.m3u8": keyed.encode("utf-8")}
    for number, name in enumerate(playlist.segments):
        plain = (folder / name).read_bytes()
        pad = 16 - len(plain) % 16
        cipher = AES.new(key, AES.MODE_CBC, sequence_iv(number))
        files[name] = cipher.encrypt(plain + bytes([pad]) * pad)
    return playlist, files
