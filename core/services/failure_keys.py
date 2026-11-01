"""다운로드 실패 예외를 안내 키로 고른다 — 엔진 쪽(서비스 핸들)이 고르고 화면 쪽은 키만 받는다 (#484).

#134에서 뷰모델에 두었던 판정을 그대로 옮겼다. 예외 객체가 화면 쪽으로 넘어가지 않게 —
화면 쪽은 키를 번역 문구로 바꾸기만 한다.
"""

import requests

from core.chzzk.playback_tracks import StreamSelectionError
from core.engine.base import (
    DiskSpaceError,
    FileSizeLimitError,
    NetworkLostError,
    PostprocessError,
)
from core.engine.integrity import TruncatedSegmentError
from core.sections.hybrid import CutError
from core.sources.hls_aes_downloader import DecryptionError
from core.utils.ffmpeg import FFmpegNotFoundError


def failure_message_key(exc: BaseException) -> str | None:
    """다운로드 실패 예외를 안내 키로 매핑한다 (#134, #127의 조회 경로 방식).

    원시 예외 문자열은 유저에게 보내지 않는다 — PostprocessError는 ffmpeg
    stderr·실행 경로를, OSError는 전체 파일 경로를 품는다. 상세는 다운로드
    로그가 이미 담당한다. 매핑에 없는 예외는 None(사유 생략)으로 둔다.
    """
    space_key = getattr(exc, "space_key", None)
    if space_key:
        # 쓰다가 공간이 없어 난 실패 (#309) — 엔진이 적어 둔 키를 다른 판정보다 먼저 쓴다.
        # 자르다 난 실패여도 원인이 공간 부족(또는 FAT의 파일 크기 한계)이면 그것으로 알린다
        return space_key
    if isinstance(exc, FileSizeLimitError):
        # 저장 폴더의 파일 시스템(FAT 계열)이 담을 수 없는 크기의 파일이다 (#309)
        return exc.message_key
    if isinstance(exc, PostprocessError):
        # PostprocessError는 항상 FFmpegError를 원인으로 체인한다(base.py의
        # 유일한 raise 지점, `raise PostprocessError(...) from e`). 원인
        # 유형으로 안내를 가른다 — "ffmpeg 실행 파일을 못 찾음"과 "ffmpeg는
        # 돌았지만 입력이 무효함"은 유저가 할 수 있는 조치가 다르다(#180
        # 조사 — 이 둘을 하나의 문구로 뭉뚱그린 탓에 macOS 실기 진단이
        # ffmpeg 설치 문제로 잘못 쏠렸다)
        if isinstance(exc.__cause__, FFmpegNotFoundError):
            return "Postprocessing failed - ffmpeg not found"
        if isinstance(exc.__cause__, CutError):
            # 구간을 자르다 실패했다 (#309) — 받은 데이터가 손상된 것이 아니다. 컷은 ffmpeg의
            # 실패를 CutError로 바꿔 올리므로 그 원인이 "실행 파일 없음"이면 그 안내를 준다
            if isinstance(exc.__cause__.__cause__, FFmpegNotFoundError):
                return "Postprocessing failed - ffmpeg not found"
            return "Section cut failed"
        return "Postprocessing failed - invalid segments"
    if isinstance(exc, DecryptionError):
        return "Decryption failed"
    if isinstance(exc, StreamSelectionError):
        # 고른 해상도의 스트림을 마스터 플레이리스트에서 하나로 정하지 못했다 (#318)
        return exc.message_key
    if isinstance(exc, NetworkLostError):
        # 받는 중인 작업자가 모두 연결 오류로 실패했고 한동안 아무것도 받지 못했다 (#329)
        return exc.message_key
    if isinstance(exc, TruncatedSegmentError):
        # 다시 받아도 세그먼트가 계속 잘려 왔다 (#321)
        return exc.message_key
    if isinstance(exc, requests.HTTPError):
        status = exc.response.status_code if exc.response is not None else None
        if status in (401, 403):
            return "Viewing permission required"
        if status == 404:
            return "Video not found"
        return "Network connection error"
    if isinstance(exc, requests.RequestException):
        return "Network connection error"
    if isinstance(exc, DiskSpaceError):
        # 여유 공간이 만들 파일 · 받을 양보다 적었다 (#309) — 구간이 있는지에 따라 키가 다르다
        return exc.message_key
    if isinstance(exc, OSError):
        return "Failed to save file"
    return None
