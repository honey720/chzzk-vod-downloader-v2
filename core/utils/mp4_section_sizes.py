"""구간이 받을 크기를 색인 없이 센다 (#309).

대기 중인 구간 카드는 받을 크기를 적는다. 그 값은 색인(``Mp4Index``)이 있으면
``core.utils.mp4_ranges.sections_sizes``로 세지만, 긴 영상의 색인은 샘플마다 값을 펴느라
만드는 데만 몇 초가 걸리고 그동안 화면이 굼떠진다. 이 모듈은 받아 둔 moov의 표에서 **구간에
닿는 샘플만** 찾아 같은 값을 낸다 — 색인은 다운로드를 시작할 때 엔진이 만든다.

값은 색인으로 센 것과 **바이트까지 같다.** 같은 규칙을 다른 길로 따라간다.

- 프레임 번호 ↔ 샘플: 색인은 샘플을 표시 시각으로 정렬해 번호를 매긴다. 여기서는 정렬하지
  않고, 샘플 하나의 순위를 그 둘레의 샘플만 견줘 구한다. 표시 시각은 DTS에서 (PTS − DTS)의
  폭 안에 있고 DTS는 줄지 않으므로, 순위가 엇갈릴 수 있는 샘플은 그 폭 안의 것뿐이다
- 시각(초): 색인과 같은 식(정수 ÷ 정수)으로 필요한 샘플만 계산한다. 프레임 고르기는 색인
  쪽과 같은 함수(``snap_to_frame``)에 프레임 시각을 그때그때 내주는 열을 넣어 쓴다
- 위치 · 크기: 청크 위치(stco/co64)와 샘플 크기(stsz)는 구간의 양 끝 청크의 것만 읽는다.
  파일의 머리 길이(첫 샘플의 위치)만 청크 위치 표 전체의 최솟값이라 표를 한 번 훑는다
  (``CHUNK_ITEMS``개씩 잘라서)

이 길로 따라갈 수 없는 모양(구간 수가 아주 많은 stts, 오디오의 ctts, 샘플이 없는 청크 등)은
``NeedsFullIndex``로 알린다. ``moov_sections_sizes`` · ``moov_head_size``는 그때 색인을 만들어
같은 값을 낸다 — 느릴 뿐 값은 같다. 표가 어긋난 moov도 색인 쪽이 판정하게 넘긴다.
"""

import struct
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from fractions import Fraction
from itertools import accumulate, islice
from operator import lt, mul

import core.api.mp4 as mp4
from core.api.mp4 import (
    MP4_FRAGMENTED,
    MP4_INVALID,
    MP4_UNSUPPORTED,
    Mp4Error,
    _boxes,
    _entry_count,
    _first_shown,
    _leaf_boxes,
    _NeedsFullParse,
    _read_table,
    _time_track,
    _timescale,
    _TimeTrack,
    _u32_table,
)
from core.models.mp4_index import Mp4Index, Mp4Raw, parse_gate
from core.models.plan import TimeRange
from core.models.sample_column import CHUNK_ITEMS, between_chunks, in_chunks, reading_tables
from core.utils.hybrid_cut import SOURCE_LEAD_SECONDS
from core.utils.mp4_partial import _BOX_HEADER_BYTES, merge_ranges
from core.utils.mp4_ranges import (
    _AUDIO_SEEK_SLACK,
    estimated_section_file_size,
    sections_file_size,
    sections_head_size,
    sections_sizes,
)
from core.utils.stop import check_stop
from core.utils.timecode import snap_to_frame

# stts의 구간 수가 이보다 많으면 색인으로 넘긴다 — 구간마다 파이썬 코드가 한 번씩 돈다.
# 샘플 길이가 거의 매번 바뀌는 영상이다(``core.api.mp4._SUMMARY_MAX_RUNS``와 같은 값)
_MAX_RUNS = 50_000
# 샘플 하나의 순위를 구할 때 견주는 둘레의 샘플 수가 이보다 많으면 색인으로 넘긴다.
# B프레임의 재배열은 몇 프레임 안에서 끝난다 — 이만큼 넓으면 표가 이상한 것이다
_MAX_WINDOW = 512
_MAX_SIGNED_64 = (1 << 63) - 1  # 색인의 위치 열(64비트 부호 있는 정수)에 드는 가장 큰 값
# 샘플 크기 표(stsz)를 이 개수씩 묶어 묶음마다의 합을 한 번 세어 둔다 — 구간이 몇 개든 표는 한 번만
# 훑고, 구간마다는 묶음의 합과 양 끝의 자투리만 더한다
_SIZE_BLOCK = 1 << 13


class NeedsFullIndex(Exception):
    """색인 없이 셀 수 없는 모양이다 — 색인을 만들어 같은 값을 구한다."""


class _Track:
    """트랙 하나의 표 — 샘플마다 펴지 않고, 물을 때마다 그 샘플의 값만 계산한다.

    시각은 틱이다. "DTS"는 편집 목록의 media_time을 뺀 값(색인의 ``decoded``)이고, "표시
    시각"은 거기에 그 샘플의 (PTS − DTS)를 더한 값(색인의 ``presented``)이다.
    """

    def __init__(self, data: bytes, time: _TimeTrack, boxes: dict[bytes, tuple[int, int]]):
        self._data = data
        self._time = time
        self.count = time.count
        self.timescale = time.timescale
        self.empty_edit = time.empty_edit
        self.has_composition = time.composition is not None
        if self.count == 0 or len(time.runs) > _MAX_RUNS:
            raise NeedsFullIndex
        self._runs = time.runs
        self._deltas = time.deltas
        self._starts = [0, *accumulate(time.runs)][:-1]  # 구간의 첫 샘플 번호
        # 구간의 첫 샘플의 DTS — media_time을 빼기 전의 값
        self._bases = [0, *accumulate(map(mul, time.runs, time.deltas))][:-1]
        self.lowest, self.highest = time.spread()
        self.width = self.highest - self.lowest
        # 시각(초) = (shift + 틱 × step) ÷ scale — origin을 정한 뒤 채운다(set_origin)
        self._shift = self._step = self._scale = 0
        self._block_sums: list[int] | None = None  # 묶음마다의 크기 합의 누적 — 필요할 때 센다
        self._read_positions(boxes)
        self._read_sync(boxes)

    # ---------------------------------------------------------------- 시각

    def set_origin(self, origin: Fraction) -> None:
        """0이 되는 시각을 정한다 — 색인의 ``_to_track``과 같은 식의 재료다."""
        shift = self.empty_edit - origin
        self._shift = shift.numerator * self.timescale
        self._step = shift.denominator
        self._scale = shift.denominator * self.timescale

    def seconds(self, ticks: int) -> float:
        """틱을 초로 — 색인이 샘플마다 계산해 둔 값과 비트까지 같다(정수 ÷ 정수)."""
        return (ticks * self._step + self._shift) / self._scale

    def _run_of(self, sample: int) -> int:
        return bisect_right(self._starts, sample) - 1

    def delta(self, sample: int) -> int:
        """그 샘플의 길이(틱)."""
        return self._deltas[self._run_of(sample)]

    def decoded(self, sample: int) -> int:
        """그 샘플의 DTS."""
        run = self._run_of(sample)
        return (
            self._bases[run]
            + (sample - self._starts[run]) * self._deltas[run]
            - self._time.media_time
        )

    def presented(self, sample: int) -> int:
        """그 샘플의 표시 시각."""
        return self.decoded(sample) + self._time.composition_at(sample)

    def below(self, ticks: int) -> int:
        """DTS가 ticks보다 작은 샘플의 수 — DTS는 줄지 않으므로 그 수가 곧 경계의 샘플 번호다."""
        wanted = ticks + self._time.media_time
        run = bisect_left(self._bases, wanted) - 1  # 첫 DTS가 wanted보다 작은 마지막 구간
        if run < 0:
            return 0
        delta, length = self._deltas[run], self._runs[run]
        inside = length if delta == 0 else min(length, -((self._bases[run] - wanted) // delta))
        return self._starts[run] + inside

    def run_spans(self) -> list[tuple[int, int, int]]:
        """stts의 구간마다 (첫 샘플, 끝 샘플 + 1, 구간 번호) — 샘플이 없는 구간은 뺀다."""
        return [
            (start, start + length, number)
            for number, (start, length) in enumerate(zip(self._starts, self._runs))
            if length
        ]

    def run_delta(self, number: int) -> int:
        return self._deltas[number]

    # ---------------------------------------------------------------- 표시 순서

    def _window(self, first: int, last: int) -> range:
        if last - first > _MAX_WINDOW:
            raise NeedsFullIndex
        return range(first, last)

    def count_before(self, ticks: int) -> int:
        """표시 시각이 ticks보다 이른 샘플의 수."""
        if not self.has_composition:
            return self.below(ticks)
        sure = self.below(ticks - self.highest)  # 이 앞의 샘플은 모두 ticks보다 이르다
        maybe = self._window(sure, self.below(ticks - self.lowest))
        return sure + sum(self.presented(sample) < ticks for sample in maybe)

    def rank(self, sample: int) -> int:
        """표시 순서에서 그 샘플의 자리 — (표시 시각, 샘플 번호)로 정렬했을 때의 번호(0부터)."""
        if not self.has_composition:
            return sample  # DTS가 곧 표시 시각이고 줄지 않는다 — 안정 정렬은 순서를 바꾸지 않는다
        shown = self.presented(sample)
        sure = self.below(shown - self.highest)
        maybe = self._window(sure, self.below(shown - self.lowest + 1))
        return sure + sum((self.presented(other), other) < (shown, sample) for other in maybe)

    def at_rank(self, rank: int) -> int:
        """표시 순서의 그 자리에 놓이는 샘플."""
        if not self.has_composition:
            return rank
        near = self.decoded(rank)  # 그 자리의 샘플은 DTS가 이 값에서 폭 안에 있다
        around = self._window(self.below(near - self.width), self.below(near + self.width + 1))
        # 둘레의 샘플을 표시 순서로 늘어놓으면 순위도 그 순서다 — 순위가 rank인 것을 이분으로 찾는다
        ordered = sorted(around, key=lambda sample: (self.presented(sample), sample))
        low, high = 0, len(ordered)
        while low < high:
            middle = (low + high) // 2
            if self.rank(ordered[middle]) < rank:
                low = middle + 1
            else:
                high = middle
        if low == len(ordered) or self.rank(ordered[low]) != rank:
            raise NeedsFullIndex  # 표가 어긋났다 — 색인 쪽이 판정한다
        return ordered[low]

    # ---------------------------------------------------------------- 위치 · 크기

    def _read_positions(self, boxes: dict[bytes, tuple[int, int]]) -> None:
        """stsc · stco/co64 · stsz의 자리를 적어 둔다. 색인과 다르게 읽힐 모양이면 넘긴다."""
        data = self._data
        name, self._offset_code, self._offset_bytes = (
            (b"co64", ">Q", 8) if b"co64" in boxes else (b"stco", ">I", 4)
        )
        self._chunk_count = _entry_count(data, boxes[name], self._offset_bytes)
        self._chunk_table = boxes[name][0] + 8
        self._chunk_typecode = "Q" if name == b"co64" else "I"
        stsz = boxes[b"stsz"]
        self._uniform_size = struct.unpack_from(">I", data, stsz[0] + 4)[0]
        if not self._uniform_size:
            _entry_count(data, stsz, 4, at=8)  # 표가 상자 안에 드는지
        self._size_table = stsz[0] + 12
        # stsc의 구간마다 (첫 청크(0부터), 청크당 샘플 수, 구간의 첫 샘플)
        runs = _u32_table(data, boxes[b"stsc"], 3)
        self._chunk_runs: list[tuple[int, int, int]] = []
        sample = 0
        for number, (first_chunk, per_chunk, _description) in enumerate(runs):
            last_chunk = runs[number + 1][0] - 1 if number + 1 < len(runs) else self._chunk_count
            # 색인은 샘플이 없는 청크 · 순서가 뒤집힌 구간 · 표 밖의 청크도 저마다의 방식으로
            # 다룬다 — 그 모양은 따라가지 않고 넘긴다
            if number == 0 and first_chunk != 1:
                raise NeedsFullIndex
            if per_chunk < 1 or last_chunk < first_chunk or last_chunk > self._chunk_count:
                raise NeedsFullIndex
            self._chunk_runs.append((first_chunk - 1, per_chunk, sample))
            sample += (last_chunk - (first_chunk - 1)) * per_chunk
        if sample != self.count:
            raise NeedsFullIndex
        self._chunk_run_samples = [run[2] for run in self._chunk_runs]

    def _chunk_of(self, sample: int) -> tuple[int, int, int]:
        """그 샘플이 든 청크의 (청크 번호, 첫 샘플, 끝 샘플 + 1)."""
        first_chunk, per_chunk, base = self._chunk_runs[
            bisect_right(self._chunk_run_samples, sample) - 1
        ]
        inside = (sample - base) // per_chunk
        start = base + inside * per_chunk
        return first_chunk + inside, start, start + per_chunk

    def _chunk_offset(self, chunk: int) -> int:
        offset = struct.unpack_from(
            self._offset_code, self._data, self._chunk_table + chunk * self._offset_bytes
        )[0]
        if offset > _MAX_SIGNED_64:
            raise NeedsFullIndex
        return offset

    def _sizes_sum(self, first: int, end: int) -> int:
        if self._uniform_size:
            return self._uniform_size * (end - first)
        return sum(_read_table(self._data, self._size_table + first * 4, end - first, "I"))

    def sizes_between(self, first: int, end: int) -> int:
        """샘플 first부터 end 앞까지의 크기의 합 — 긴 범위도 표를 다시 훑지 않는다.

        처음 불릴 때 표를 ``_SIZE_BLOCK``개씩 묶어 묶음마다의 합을 한 번 세어 둔다(조각 사이에서
        그만둘지 묻고, 조작 중이면 쉰다). 그 뒤로는 범위에 온전히 든 묶음의 합과 양 끝의
        자투리(묶음 하나보다 적다)만 더한다.
        """
        if self._uniform_size or end - first <= _SIZE_BLOCK:
            return self._sizes_sum(first, end)
        if self._block_sums is None:
            sums = [0]
            for start in range(0, self.count, _SIZE_BLOCK):
                between_chunks()
                sums.append(sums[-1] + self._sizes_sum(start, min(start + _SIZE_BLOCK, self.count)))
            self._block_sums = sums
        first_block = -(-first // _SIZE_BLOCK)  # first가 든 묶음의 다음 경계
        end_block = end // _SIZE_BLOCK
        if first_block >= end_block:
            return self._sizes_sum(first, end)
        return (
            self._sizes_sum(first, first_block * _SIZE_BLOCK)
            + self._block_sums[end_block]
            - self._block_sums[first_block]
            + self._sizes_sum(end_block * _SIZE_BLOCK, end)
        )

    def chunks_between(self, first_sample: int, last_sample: int) -> int:
        """샘플 first~last가 걸친 청크 수 — ``mp4_ranges._chunks_between``."""
        return self._chunk_of(last_sample)[0] - self._chunk_of(first_sample)[0] + 1

    def chunk_span(self, first_sample: int, last_sample: int) -> tuple[int, int]:
        """샘플 first~last가 든 청크들의 (첫 바이트, 마지막 바이트 + 1) — ``mp4_ranges._chunk_span``."""
        first_chunk, _start, _end = self._chunk_of(first_sample)
        last_chunk, start, end = self._chunk_of(last_sample)
        return (
            self._chunk_offset(first_chunk),
            self._chunk_offset(last_chunk) + self._sizes_sum(start, end),
        )

    def first_byte(self) -> int:
        """이 트랙의 샘플이 놓인 가장 앞 위치 — 청크의 첫 샘플이 그 청크에서 가장 앞이다."""
        table = _read_table(self._data, self._chunk_table, self._chunk_count, self._chunk_typecode)
        if max(map(max, in_chunks(table))) > _MAX_SIGNED_64:
            raise NeedsFullIndex
        return min(map(min, in_chunks(table)))

    # ---------------------------------------------------------------- 키프레임

    def _read_sync(self, boxes: dict[bytes, tuple[int, int]]) -> None:
        """stss — 단독 디코드 가능한 샘플의 번호(1부터). 상자가 없으면 모든 샘플이다."""
        if b"stss" not in boxes:
            self._sync: Sequence[int] = range(1, self.count + 1)
            return
        span = boxes[b"stss"]
        numbers = _read_table(self._data, span[0] + 8, _entry_count(self._data, span, 4), "I")
        # 오름차순이고 샘플 수 안이어야 번호로 찾을 수 있다. 조각의 경계도 한 칸 겹쳐 본다
        for start in range(0, len(numbers), CHUNK_ITEMS):
            check_stop()
            part = numbers[start : start + CHUNK_ITEMS + 1]
            if not all(map(lt, part, islice(part, 1, None))):
                raise NeedsFullIndex
        if numbers and (numbers[0] < 1 or numbers[-1] > self.count):
            raise NeedsFullIndex
        self._sync = numbers

    def keyframe_covering(self, limit: tuple[int, int] | None) -> int | None:
        """표시 순서로 limit(표시 시각, 샘플 번호) 이하인 표시되는 키프레임 가운데 가장 뒤의 샘플.

        limit이 None이거나 그런 키프레임이 없으면, 표시되는 키프레임 가운데 가장 앞의 것이다.
        표시되는 키프레임이 하나도 없으면 None.
        """
        sync = self._sync
        if limit is not None:
            best: tuple[int, int] | None = None
            # DTS가 (limit의 표시 시각 − 가장 작은 PTS − DTS)보다 큰 샘플은 limit보다 늦다
            position = bisect_left(sync, self.below(limit[0] - self.lowest + 1) + 1) - 1
            for scanned, at in enumerate(range(position, -1, -1)):
                sample = sync[at] - 1
                decoded = self.decoded(sample)
                # 이 앞의 샘플은 DTS가 더 작다 — 찾은 것보다 늦을 수 없거나 모두 가려졌다
                if decoded + self.highest < 0 or (
                    best is not None and decoded + self.highest <= best[0]
                ):
                    break
                if scanned > _MAX_WINDOW:
                    raise NeedsFullIndex
                key = (decoded + self._time.composition_at(sample), sample)
                if key[0] >= 0 and key <= limit and (best is None or key > best):
                    best = key
            if best is not None:
                return best[1]
        best = None
        for scanned, number in enumerate(sync):
            sample = number - 1
            decoded = self.decoded(sample)
            if best is not None and decoded + self.lowest >= best[0]:
                break  # 이 뒤의 샘플은 DTS가 더 크다 — 찾은 것보다 이를 수 없다
            if scanned > _MAX_WINDOW:
                raise NeedsFullIndex
            key = (decoded + self._time.composition_at(sample), sample)
            if key[0] >= 0 and (best is None or key < best):
                best = key
        return best[1] if best is not None else None


class _FramePts:
    """표시되는 영상 프레임의 PTS(초)를 프레임 번호로 내주는 열 — 색인의 ``frame_pts``와 같은 값.

    값을 미리 만들지 않는다. ``snap_to_frame`` · ``bisect``가 묻는 번호만 그때 계산한다.
    """

    def __init__(self, video: _Track, hidden: int):
        self._video = video
        self._hidden = (
            hidden  # 편집 목록이 가린(표시 시각이 음수인) 샘플의 수 — 표시 순서의 맨 앞이다
        )

    def __len__(self) -> int:
        return self._video.count - self._hidden

    def sample(self, frame: int) -> int:
        """그 프레임의 샘플 — 색인의 ``frame_samples[frame]``."""
        return self._video.at_rank(frame + self._hidden)

    def frame_of(self, sample: int) -> int:
        """그 샘플의 프레임 번호."""
        return self._video.rank(sample) - self._hidden

    def __getitem__(self, frame: int) -> float:
        if not 0 <= frame < len(self):
            raise IndexError(frame)
        return self._video.seconds(self._video.presented(self.sample(frame)))


class LightTables:
    """받아 둔 moov에서 구간의 크기를 세는 데 필요한 표만 읽어 둔 것 (#309).

    Raises:
        NeedsFullIndex: 색인 없이 셀 수 없는 모양인 경우
        Mp4Error: 조각난 mp4이거나 영상 트랙이 없는 등 색인도 만들 수 없는 경우
    """

    def __init__(self, raw: Mp4Raw):
        self._moov_range = raw.moov_range
        try:
            self._read(raw.moov)
        except _NeedsFullParse as e:
            raise NeedsFullIndex from e
        except (struct.error, IndexError, ValueError, OverflowError) as e:
            raise NeedsFullIndex from e  # 손상된 표 — 색인 쪽이 판정한다

    def _read(self, moov: bytes) -> None:
        top = list(_boxes(moov, 0, len(moov)))
        if len(top) != 1 or top[0][0] != b"moov":
            raise Mp4Error(MP4_INVALID, "moov 상자가 아니다")
        _, moov_body, moov_end = top[0]
        movie_timescale = 0
        tracks: dict[bytes, _Track] = {}
        times: dict[bytes, _TimeTrack] = {}
        for box_type, body, body_end in _boxes(moov, moov_body, moov_end):
            check_stop()
            if box_type == b"mvex":
                raise Mp4Error(MP4_FRAGMENTED)
            if box_type == b"mvhd":
                movie_timescale = _timescale(moov, body)
            elif box_type == b"trak":
                if movie_timescale <= 0:
                    raise Mp4Error(MP4_INVALID, "mvhd가 trak보다 앞에 없다")
                time = _time_track(moov, body, body_end, movie_timescale)
                if time is not None and time.handler not in tracks:
                    tracks[time.handler] = _Track(moov, time, _leaf_boxes(moov, body, body_end))
                    times[time.handler] = time
        video = tracks.get(b"vide")
        if video is None:
            raise Mp4Error(MP4_UNSUPPORTED, "영상 트랙이 없다")
        audio = tracks.get(b"soun")
        if audio is not None and audio.has_composition:
            raise NeedsFullIndex  # 오디오의 표시 시각이 샘플 순서가 아닐 수 있다

        video_first = _first_shown(times[b"vide"])
        if video_first is None:
            raise Mp4Error(MP4_UNSUPPORTED, "표시되는 영상 프레임이 없다")
        origin = video.empty_edit + Fraction(video_first, video.timescale)
        audio_first = _first_shown(times[b"soun"]) if audio is not None else None
        if audio_first is not None:
            origin = min(origin, audio.empty_edit + Fraction(audio_first, audio.timescale))
        video.set_origin(origin)
        if audio is not None:
            audio.set_origin(origin)

        # 가장 많은 샘플 길이 — 색인의 fps와 같은 값(``core.api.mp4._summarize``와 같은 셈)
        seen: dict[int, int] = {}
        for run, delta in zip(times[b"vide"].runs, times[b"vide"].deltas):
            if run:
                seen[delta] = seen.get(delta, 0) + run
        modal_delta = max(seen.items(), key=lambda entry: entry[1])[0]
        if modal_delta <= 0:
            raise Mp4Error(MP4_INVALID, "영상 샘플 길이가 0이다")
        self._fps = Fraction(video.timescale, modal_delta)
        self._video = video
        self._audio = audio
        self._frames = _FramePts(video, video.count_before(0))

    def head_size(self) -> int:
        """구간 다운로드가 만드는 파일의 머리 길이 — ``mp4_ranges.sections_head_size``와 같은 값.

        Raises:
            NeedsFullIndex: 색인 없이 셀 수 없는 모양인 경우
            Mp4Error: moov가 샘플보다 뒤에 있는 등 부분 파일을 만들 수 없는 경우
        """
        with _translated():
            tracks = [self._video] + ([self._audio] if self._audio is not None else [])
            first_sample = min(track.first_byte() for track in tracks)
        if self._moov_range is None or self._moov_range[1] >= first_sample:
            raise Mp4Error(MP4_UNSUPPORTED, "moov가 샘플보다 뒤에 있다")
        if first_sample - (self._moov_range[1] + 1) < _BOX_HEADER_BYTES:
            raise Mp4Error(MP4_UNSUPPORTED, "moov와 첫 샘플 사이에 mdat 머리가 들어갈 자리가 없다")
        return first_sample

    def sections_sizes(self, selections: Iterable[TimeRange]) -> tuple[int, int]:
        """(받을 바이트의 합, 파일의 머리 길이) — ``mp4_ranges.sections_sizes``와 같은 값.

        Raises:
            NeedsFullIndex: 색인 없이 셀 수 없는 모양인 경우
            ValueError: 구간이 영상 밖인 경우
            Mp4Error: moov가 샘플보다 뒤에 있는 등 부분 파일을 만들 수 없는 경우
        """
        spans = []
        for selection in selections:
            check_stop()
            with _translated():
                spans.append(self._byte_range(selection))
        head_size = self.head_size()
        merged = merge_ranges(spans)
        return sum(last - first + 1 for first, last in merged), head_size

    def sections_file_size(self, selections: Iterable[TimeRange]) -> int:
        """구간마다 만들어질 파일 크기의 합(추정) — ``mp4_ranges.sections_file_size``와 같은 값.

        Raises:
            NeedsFullIndex: 색인 없이 셀 수 없는 모양인 경우
            ValueError: 구간이 영상 밖인 경우
            Mp4Error: moov가 샘플보다 뒤에 있는 등 부분 파일을 만들 수 없는 경우
        """
        total = 0
        for selection in selections:
            check_stop()
            with _translated():
                self._byte_range(selection)  # 받을 수 있는 구간인지 — 색인 쪽과 같은 판정
                total += self._file_size(selection)
        self.head_size()  # 부분 파일을 만들 수 없는 배치면 여기서 Mp4Error
        return total

    def _file_size(self, selection: TimeRange) -> int:
        """구간 하나의 파일 크기 추정 — 그 구간에 드는 샘플의 바이트 + 샘플 수로 어림한 머리."""
        frames, video = self._frames, self._video
        first_frame = snap_to_frame(selection.start, frames, self._fps, "start")
        last_frame = max(first_frame, snap_to_frame(selection.end, frames, self._fps, "end"))
        if first_frame == 0 and last_frame == len(frames) - 1:
            # 영상 전체 — 받은 것이 곧 원본이다. 가장 뒤 샘플이 끝나는 위치가 파일의 크기다
            return max(
                track.chunk_span(0, track.count - 1)[1]
                for track in (video, self._audio)
                if track is not None and track.count
            )
        low = frames.sample(first_frame)
        end_sample = frames.sample(last_frame)
        high = self._last_decoded(low, end_sample)
        video_bytes = video.sizes_between(low, high + 1)
        audio_samples = audio_bytes = audio_chunks = 0
        if self._audio is not None:
            time_to = frames[last_frame] + video.delta(end_sample) / video.timescale
            needed = self._audio_samples(frames[first_frame], time_to)
            if needed is not None:
                audio_samples = needed[1] - needed[0] + 1
                audio_bytes = self._audio.sizes_between(needed[0], needed[1] + 1)
                audio_chunks = self._audio.chunks_between(needed[0], needed[1])
        return estimated_section_file_size(
            last_frame - first_frame + 1,
            video_bytes,
            audio_samples,
            audio_bytes,
            video.chunks_between(low, high),
            audio_chunks,
        )

    def _byte_range(self, selection: TimeRange) -> tuple[int, int]:
        """구간 하나의 (첫 바이트, 마지막 바이트) — ``mp4_ranges.selection_byte_ranges``와 같은 범위."""
        frames, video = self._frames, self._video
        first_frame = snap_to_frame(selection.start, frames, self._fps, "start")
        last_frame = max(first_frame, snap_to_frame(selection.end, frames, self._fps, "end"))
        # 범위가 시작하는 키프레임 — 첫 프레임보다 조금 앞의 시각을 덮는 것
        covered = bisect_right(frames, frames[first_frame] - SOURCE_LEAD_SECONDS)
        limit = None
        if covered:
            sample = frames.sample(covered - 1)
            limit = (video.presented(sample), sample)
        first_sample = video.keyframe_covering(limit)
        keyframe = frames.frame_of(first_sample) if first_sample is not None else 0
        if first_sample is None:
            first_sample = frames.sample(0)  # 키프레임이 없다 — 색인은 0번 프레임에서 시작한다
        if keyframe > last_frame:
            raise ValueError(
                "구간에 든 프레임이 없다"
            )  # 색인 쪽은 빈 목록의 max()로 같은 예외를 낸다
        last_sample = self._last_decoded(first_sample, frames.sample(last_frame))
        spans = [video.chunk_span(first_sample, last_sample)]

        if self._audio is not None:
            time_from = video.seconds(video.decoded(first_sample)) - _AUDIO_SEEK_SLACK
            end_sample = frames.sample(last_frame)
            time_to = frames[last_frame] + video.delta(end_sample) / video.timescale
            needed = self._audio_samples(time_from, time_to)
            if needed is not None:
                spans.append(self._audio.chunk_span(*needed))
        return min(span[0] for span in spans), max(span[1] for span in spans) - 1

    def _last_decoded(self, first_sample: int, end_sample: int) -> int:
        """표시 순서로 first~end 사이인 샘플 가운데 번호가 가장 큰 것.

        B프레임은 끝 프레임보다 먼저 표시되면서 나중에 디코드될 수 있다.
        """
        video = self._video
        low = (video.presented(first_sample), first_sample)
        high = (video.presented(end_sample), end_sample)
        # 끝 프레임보다 뒤의 샘플 가운데 표시 시각이 끝 프레임 이하일 수 있는 것은 DTS가 그 폭 안이다
        for sample in reversed(
            video._window(
                end_sample + 1, max(end_sample + 1, video.below(high[0] - video.lowest + 1))
            )
        ):
            if low <= (video.presented(sample), sample) <= high:
                return sample
        return end_sample

    def _audio_samples(self, time_from: float, time_to: float) -> tuple[int, int] | None:
        """시작이 time_to보다 이르고 끝이 time_from보다 늦은 오디오 샘플의 (첫 번호, 마지막 번호).

        오디오는 표시 시각이 샘플 순서다(ctts가 있으면 여기까지 오지 않는다). 시작 시각은 줄지
        않고, 길이가 같은 구간(stts) 안에서는 끝 시각도 줄지 않는다 — 구간마다 경계를 이분으로
        찾는다.
        """
        audio = self._audio

        def start(sample: int) -> float:
            return audio.seconds(audio.decoded(sample))

        # 시작이 time_to보다 이른 샘플의 수
        low, high = 0, audio.count
        while low < high:
            middle = (low + high) // 2
            if start(middle) < time_to:
                low = middle + 1
            else:
                high = middle
        limit = low
        runs = audio.run_spans()
        first = last = None
        for begin, end, number in runs:
            end = min(end, limit)
            if begin >= end:
                break
            length = audio.run_delta(number) / audio.timescale
            low, high = begin, end
            while low < high:
                middle = (low + high) // 2
                if start(middle) + length > time_from:
                    high = middle
                else:
                    low = middle + 1
            if low < end:
                first = low
                break
        if first is None:
            return None
        for begin, end, number in reversed(runs):
            end = min(end, limit)
            if begin >= end:
                continue
            if start(end - 1) + audio.run_delta(number) / audio.timescale > time_from:
                last = end - 1
                break
        return first, last


@contextmanager
def _translated():
    """표를 읽다 난 낮은 수준의 예외를 "색인으로 넘긴다"로 바꾼다 — 손상된 표는 색인 쪽이 판정한다."""
    try:
        yield
    except _NeedsFullParse as e:
        raise NeedsFullIndex from e
    except (struct.error, IndexError, OverflowError) as e:
        raise NeedsFullIndex from e


def _full_index(raw: Mp4Raw) -> Mp4Index:
    """색인을 만든다 — 앱 전체에서 한 번에 하나만 도는 해석의 차례를 기다린다."""
    with parse_gate():
        # 모듈 전역을 호출 시점에 조회한다 — 테스트의 monkeypatch 지점
        return mp4.index_mp4(raw).index


def moov_sections_sizes(raw: Mp4Raw, selections: Iterable[TimeRange]) -> tuple[int, int]:
    """받아 둔 moov로 (받을 바이트의 합, 파일의 머리 길이)를 센다 — 색인을 남기지 않는다 (#309).

    색인으로 센 값(``mp4_ranges.sections_sizes``)과 같다. 표에서 구간에 닿는 샘플만 찾아 세고,
    그렇게 셀 수 없는 모양이면 색인을 만들어 세고 버린다.

    Raises:
        ValueError: 구간이 영상 밖인 경우
        Mp4Error: 색인을 만들 수 없거나 부분 파일을 만들 수 없는 경우
        StopRequested: 그만두라는 답을 받은 경우(``core.utils.stop``)
    """
    wanted = list(selections)
    try:
        with reading_tables():  # 이 스레드의 조각을 잘게 — 조작 중이면 조각 사이에서 쉰다
            return LightTables(raw).sections_sizes(wanted)
    except NeedsFullIndex:
        return sections_sizes(_full_index(raw), wanted)


def moov_sections_file_size(raw: Mp4Raw, selections: Iterable[TimeRange]) -> int:
    """받아 둔 moov로 구간마다 만들어질 파일 크기의 합(추정)을 센다 — 색인을 남기지 않는다 (#309).

    색인으로 센 값(``mp4_ranges.sections_file_size``)과 같다. 표에서 구간에 닿는 샘플만 찾아
    세고, 그렇게 셀 수 없는 모양이면 색인을 만들어 세고 버린다.

    Raises:
        ValueError: 구간이 영상 밖인 경우
        Mp4Error: 색인을 만들 수 없거나 부분 파일을 만들 수 없는 경우
        StopRequested: 그만두라는 답을 받은 경우(``core.utils.stop``)
    """
    wanted = list(selections)
    try:
        with reading_tables():  # 이 스레드의 조각을 잘게 — 조작 중이면 조각 사이에서 쉰다
            return LightTables(raw).sections_file_size(wanted)
    except NeedsFullIndex:
        return sections_file_size(_full_index(raw), wanted)


def moov_head_size(raw: Mp4Raw) -> int:
    """받아 둔 moov로 구간 다운로드가 만드는 파일의 머리 길이를 구한다 (#309).

    색인으로 구한 값(``mp4_ranges.sections_head_size``)과 같다.

    Raises:
        Mp4Error: 색인을 만들 수 없거나 부분 파일을 만들 수 없는 경우
        StopRequested: 그만두라는 답을 받은 경우(``core.utils.stop``)
    """
    try:
        with reading_tables():  # 이 스레드의 조각을 잘게 — 조작 중이면 조각 사이에서 쉰다
            return LightTables(raw).head_size()
    except NeedsFullIndex:
        return sections_head_size(_full_index(raw))
