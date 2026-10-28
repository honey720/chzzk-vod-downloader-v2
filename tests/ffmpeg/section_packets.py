"""TS 구간 파일을 패킷으로 견주는 테스트 도구 (#309).

구간 파일의 영상은 "세그먼트 전부를 이어 mp4로 다시 싼 뒤 하이브리드 컷으로 자른 것"과 패킷
(시각 · 내용)이 같아야 하고, 오디오는 원본의 패킷 그대로여야 한다. 암호화 VOD의 컷 테스트와
엔진 테스트가 같이 쓴다. 대조는 제품의 구간 경로(``ts_cut`` · 엔진)를 거치지 않고 만든다.
"""

from fractions import Fraction

from core.container.mp4 import read_mp4_index
from core.utils.ffmpeg import remux_stream, run_ffmpeg
from core.cutting.hybrid import cut_frames_from_mp4
from tests.ffmpeg.reference_cut import hybrid_cut


def packets(path: str) -> dict[str, list[str]]:
    """파일의 영상 · 오디오 패킷 — 스트림마다 framecrc 줄(시각 · 길이 · 크기 · 내용의 CRC) 목록."""
    found = {}
    for stream in ("v:0", "a:0"):
        done = run_ffmpeg(
            ["-v", "error", "-i", path, "-map", f"0:{stream}", "-c", "copy", "-f", "framecrc", "-"],
            timeout=60,
        )
        assert done.returncode == 0, done.stderr
        found[stream] = [line for line in done.stdout.splitlines() if not line.startswith("#")]
        assert found[stream]  # 빈 목록끼리 같다고 통과하지 않게
    return found


def contents(lines: list[str]) -> list[tuple[str, str]]:
    """framecrc 줄마다의 (크기, 내용의 CRC) — 시각을 뺀, 패킷의 내용만."""
    cells = [[cell.strip() for cell in line.split(",")] for line in lines]
    return [(row[4], row[5]) for row in cells]


def stamps(lines: list[str]) -> list[int]:
    """framecrc 줄마다의 PTS(틱) — 오름차순."""
    return sorted(int(line.split(",")[2]) for line in lines)


class JoinedSource:
    """복호화한 TS 세그먼트 전부를 이어 다시 싼 mp4 하나 — 대조의 원본이다.

    프레임 번호는 영상 전체에서 센 번호다. 오디오의 시각은 이 파일의 것 그대로다 — 오디오가
    영상보다 먼저 시작하는 입력이라 파일의 0초가 VOD의 0초다.
    """

    def __init__(self, plain_segments: list[bytes], folder, audio_rate: int = 48000):
        self.path = str(folder / "all_segments.mp4")
        remux_stream(iter(plain_segments), self.path)

        def read(offset: int, size: int) -> bytes:
            with open(self.path, "rb") as f:
                f.seek(offset)
                return f.read(size)

        self.frames = cut_frames_from_mp4(read_mp4_index(read))
        self.packets = packets(self.path)
        # 오디오 패킷마다의 시작 시각(초) — framecrc의 PTS는 표본 수다
        self.audio_pts = [
            Fraction(int(line.split(",")[2]), audio_rate) for line in self.packets["a:0"]
        ]
        self.audio_span = Fraction(1024, audio_rate)  # AAC 덩어리 하나의 길이
        self._folder = folder

    def reference(self, first: int, last: int) -> dict[str, list[str]]:
        """프레임 first~last를 하이브리드 컷으로 자른 파일의 패킷."""
        path = str(self._folder / f"reference_{first}_{last}.mp4")
        hybrid_cut(self.path, self.frames, first, last, path)
        return packets(path)


def assert_section_matches(
    source: JoinedSource,
    path: str,
    first: int,
    last: int,
    start: float,
    end: float,
    original_tail: bool = False,
) -> None:
    """구간 파일(path)이 source의 프레임 first~last와 맞는지 단언한다.

    - 영상: 대조(하이브리드 컷)와 패킷(시각 · 내용)이 같다. original_tail(구간의 끝이 세그먼트의
      끝과 맞는다)이면 마지막 GOP 앞까지만 그렇고, 마지막 GOP는 원본의 패킷 그대로이며 PTS는
      대조와 같다
    - 오디오: 원본의 패킷 그대로다 — 구간 시작(start초)에 걸친 덩어리부터 구간 끝(end초)에 걸친
      덩어리까지. start · end는 VOD 시각이고 부르는 쪽이 제품과 따로 센다
    """
    made = packets(path)
    reference = source.reference(first, last)
    if original_tail:
        key = max(k for k in source.frames.keyframes if k <= last)
        before = key - first  # 마지막 GOP 앞의 패킷 수 — 닫힌 GOP라 디코드 순서로도 앞이다
        assert made["v:0"][:before] == reference["v:0"][:before]
        tail = contents(source.packets["v:0"])[key : last + 1]
        assert contents(made["v:0"])[before:] == tail
        assert stamps(made["v:0"]) == stamps(reference["v:0"])
    else:
        assert made["v:0"] == reference["v:0"]
    wanted = [
        packet
        for pts, packet in zip(source.audio_pts, contents(source.packets["a:0"]), strict=True)
        if pts + source.audio_span > start and pts < end
    ]
    assert wanted  # 빈 목록끼리 같다고 통과하지 않게
    assert contents(made["a:0"]) == wanted
