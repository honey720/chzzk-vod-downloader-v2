"""구간 컷의 입력·계획·결과·판정 모델 (#309).

구간 하나를 파일 하나로 잘라 내는 일은 네 단계다 — 프레임 정보를 모으고
(``CutFrames``), 어느 프레임을 재인코딩하고 어느 프레임을 복사할지 정하고
(``CutPlan``), ffmpeg로 자르고(``CutResult``), 결과가 원본과 맞는지 판정한다
(``CutCheck``). 계획·실행은 ``core.utils.hybrid_cut``, 판정은
``core.utils.cut_check``가 한다.
"""

from dataclasses import dataclass, field
from typing import Literal

from core.models.plan import TimeRange


@dataclass(frozen=True)
class CutFrames:
    """컷에 필요한 원본의 프레임 정보를 담는다 — 색인에서 읽은 값이다.

    시각은 모두 VOD 시작 = 0 기준의 초다(``Mp4Index`` · ``Fmp4Index``와 같다).
    """

    frame_pts: tuple[float, ...]  # 영상 프레임의 PTS, 표시 순서
    frame_dts: tuple[float, ...]  # frame_pts와 같은 순서로, 그 프레임의 DTS
    keyframes: tuple[int, ...]  # 키프레임인 프레임의 번호(오름차순)
    timescale: int  # 영상 트랙의 초당 틱 수 — 재인코딩 조각과 결과 파일이 이 값을 쓴다
    frame_duration: float  # 프레임 하나의 길이(초) — 가장 많은 샘플 길이
    audio_start: float | None  # 오디오가 시작하는 시각. 오디오가 없으면 None
    audio_end: float | None  # 오디오가 끝나는 시각. 오디오가 없으면 None
    # 입력 파일이 시작하는 시각. 세그먼트 일부만 이어 붙인 입력은 0이 아니다 —
    # ffmpeg의 -ss는 파일의 시작부터 센다
    input_start: float = 0.0
    # 원본 오디오 스트림 전체의 비트레이트(kb/s) — 컨테이너에 적힌 값이다. 입력 파일에
    # 원본의 어느 부분이 들었는지와 무관하게 같다. 알 수 없으면 None
    audio_bitrate: int | None = None


@dataclass(frozen=True)
class CutPiece:
    """컷 조각 하나 — 프레임 번호 [first, end) 구간이다."""

    # head: 구간 시작 ~ 다음 키프레임 직전(재인코딩) · mid: 키프레임 ~ 마지막 키프레임 직전(복사)
    # tail: 마지막 키프레임 ~ 구간 끝(재인코딩) · whole: 구간 전체가 한 GOP 안(재인코딩)
    kind: Literal["head", "mid", "tail", "whole"]
    first: int  # 조각의 첫 프레임 번호
    end: int  # 조각의 마지막 프레임 번호 + 1

    @property
    def reencoded(self) -> bool:
        """이 조각이 재인코딩되는지 — mid만 복사다."""
        return self.kind != "mid"


@dataclass(frozen=True)
class CutPlan:
    """구간을 어떤 조각으로 자를지 정한 계획을 담는다."""

    first: int  # 구간의 첫 프레임 번호
    last: int  # 구간의 끝 프레임 번호 (포함)
    pieces: tuple[CutPiece, ...]  # 재생 순서대로의 조각


@dataclass(frozen=True)
class CutSection:
    """구간 다운로드의 구간 하나 — 요청한 시각과, 그것을 맞춘 실제 프레임과, 산출물을 담는다."""

    selection: TimeRange  # 요청한 구간(초)
    first_frame: int  # 구간의 첫 프레임 번호
    last_frame: int  # 구간의 끝 프레임 번호 (포함)
    output_path: str  # 이 구간을 잘라 쓸 파일


@dataclass(frozen=True)
class VideoParams:
    """영상 스트림의 부호화 파라미터를 담는다 — SPS와 컨테이너에서 읽은 값이다.

    재인코딩 조각이 복사 조각과 이어지려면 이 값들이 원본과 같아야 한다.
    """

    profile_idc: int  # H.264 프로파일 (66 Baseline · 77 Main · 100 High)
    level_idc: int  # H.264 레벨 × 10 (40 = 4.0)
    chroma_format_idc: int  # 색차 형식 (1 = 4:2:0)
    bit_depth: int  # 밝기 비트 수 (8)
    coded_width: int  # 부호화 가로(픽셀) — 매크로블록 수 × 16
    coded_height: int  # 부호화 세로(픽셀)
    crop: tuple[int, int, int, int]  # 잘라 낼 (왼쪽, 위, 오른쪽, 아래) 픽셀 수
    sar: tuple[int, int]  # 화소 가로세로비 (가로, 세로). 스트림이 밝히지 않았으면 (0, 0)
    reorder_delay: int  # B프레임 재정렬 지연(프레임) — 키프레임의 (PTS − DTS)


@dataclass(frozen=True)
class SourceInfo:
    """컷 입력 파일에서 읽은 정보를 담는다."""

    video: VideoParams  # 영상 파라미터
    audio_bitrate: int | None  # 오디오를 다시 인코딩하는 비트레이트(kb/s). 오디오가 없으면 None


@dataclass(frozen=True)
class PieceInfo:
    """잘라 낸 조각 하나를 들여다본 결과를 담는다 (판정용)."""

    piece: CutPiece  # 계획의 그 조각
    video: VideoParams  # 조각 파일의 영상 파라미터
    timescale: int  # 조각 파일의 영상 트랙 timescale
    packets: int  # 조각 파일의 영상 패킷 수
    hidden_packets: int  # 편집 목록이 가린 영상 패킷 수


@dataclass(frozen=True)
class CutResult:
    """컷 한 번의 결과를 담는다."""

    output_path: str  # 만들어진 파일
    plan: CutPlan  # 실행한 계획
    source: SourceInfo  # 입력에서 읽은 정보
    # 조각을 들여다본 결과 — hybrid_cut(inspect=True)일 때만 채운다
    pieces: tuple[PieceInfo, ...] = ()


@dataclass(frozen=True)
class CutCheck:
    """잘라 낸 파일의 정합 판정 결과를 담는다. 항목마다 통과 여부와 근거 문장이 있다."""

    params: bool  # 재인코딩 조각의 파라미터가 원본과 같다
    decode: bool  # 디코드 오류가 없다
    seams: bool  # 이음매에서 PTS가 이어진다 — 프레임 누락·중복 없음
    start_end: bool  # 시작·끝이 요청과 1프레임 이내다. 편집 목록을 무시해도 같다
    av_sync: bool  # 구간 시작·끝의 (오디오 − 영상) 차이가 원본의 같은 위치와 1프레임 이내로 같다
    notes: dict[str, str] = field(default_factory=dict)  # 항목 이름 → 근거

    @property
    def ok(self) -> bool:
        """다섯 항목이 모두 통과했는지."""
        return self.params and self.decode and self.seams and self.start_end and self.av_sync
