"""임시 확인용 — 러너의 ffmpeg가 fMP4 입력의 오디오 비트레이트를 어떻게 보여 주는지 찍는다 (#309).

원인을 확정하면 지운다. 결과는 pytest의 경고 요약으로 나온다.
"""

import re
import struct
import warnings

from core.utils.ffmpeg import get_ffmpeg_exe, run_ffmpeg
from core.utils.hybrid_cut import _audio_command, _read_params, plan_cut
from tests.unit.core.test_m3u8_sections import _make_hls, _Source


def _descriptor_bitrates(data: bytes) -> str:
    """esds의 maxBitrate·avgBitrate와 btrt의 값을 찾아 글로 낸다."""
    found = []
    at = data.find(b"esds")
    if at >= 0:
        position = at + 8  # 종류(4) + 버전·플래그(4)
        for tag in (0x03, 0x04):
            assert data[position] == tag, f"tag {data[position]:#x}"
            position += 1
            while data[position] & 0x80:
                position += 1
            position += 1
            if tag == 0x03:
                position += 3  # ES_ID(2) + 플래그(1)
        max_rate, avg_rate = struct.unpack_from(">II", data, position + 5)
        found.append(f"esds max={max_rate} avg={avg_rate}")
    else:
        found.append("esds 없음")
    at = data.find(b"btrt")
    if at >= 0:
        found.append("btrt buffer=%d max=%d avg=%d" % struct.unpack_from(">III", data, at + 4))
    else:
        found.append("btrt 없음")
    return " · ".join(found)


def test_tmp_report_how_ffmpeg_shows_audio_bitrate(tmp_path):
    """확인용 출력만 낸다. 단언은 입력이 만들어졌는지뿐이다."""
    _make_hls(tmp_path, b_frames="3", pyramid="normal")
    source = _Source(tmp_path)
    names = source.playlist.segments
    lines = [
        "ffmpeg: " + run_ffmpeg(["-version"], timeout=30).stdout.splitlines()[0],
        "exe: " + get_ffmpeg_exe(),
        "init: " + _descriptor_bitrates(source.files["init.mp4"]),
    ]
    # (세그먼트 범위, 자를 프레임) — 뒤의 둘이 ubuntu에서 실패한 경우다
    cases = (
        ((0, 5), (40, 100)),
        ((0, 3), (40, 100)),
        ((0, 0), (5, 20)),
        ((0, 1), (29, 40)),
        ((3, 5), (150, 179)),
        ((1, 2), (60, 80)),
    )
    for (first, last), _cut in cases:
        path = str(tmp_path / f"part_{first}_{last}.mp4")
        with open(path, "wb") as f:
            f.write(source.files["init.mp4"])
            for name in names[first : last + 1]:
                f.write(source.files[name])
        text = run_ffmpeg(["-i", path], timeout=30).stderr
        audio = next((line.strip() for line in text.splitlines() if "Audio:" in line), "없음")
        duration = re.search(r"Duration: .*", text)
        _video, bitrate = _read_params(path, 2)
        command = _audio_command(path, source.frames, plan_cut(source.frames, 40, 100), bitrate)
        option = command[command.index("-b:a") + 1] if "-b:a" in command else "(없음)"
        lines.append(
            f"세그먼트 {first}~{last}: {duration.group(0) if duration else '?'}"
            f" | {audio} | _read_params={bitrate} | -b:a {option}"
        )
    warnings.warn("\n" + "\n".join(lines), stacklevel=1)
    assert len(names) == 6
