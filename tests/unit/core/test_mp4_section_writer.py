"""구간 파일 작성기(core/utils/mp4_section_writer.py)의 단위 테스트 (#309).

합성 mp4(mp4_builder)로 구간 파일을 통째로 만들어, 쓰는 쪽과 다른 코드인 해석기
(core.api.mp4)와 조립기의 기록으로 확인한다. 다시 인코딩한 조각은 손으로 만든다.

표준 영상(video_spec): 10fps 12프레임, GOP 넷 프레임씩(키프레임 = 프레임 0 · 4 · 8),
디코드 순서 I P B B. 프레임 n의 표시 시각은 n × 0.1초, 원본의 PTS는 (n + 1) × 100틱.
표준 오디오(audio_spec): 0.128초짜리 16샘플, 샘플 n의 표시 시각은 (n − 1) × 0.128초.
"""

import struct

import pytest

from core.api.mp4 import MP4_INVALID, MP4_UNSUPPORTED, Mp4Error, parse_moov, read_mp4_index
from core.models.cut import CutPiece
from core.utils.mp4_section_writer import (
    MDAT_HEADER_BYTES,
    SECTION_FILE_FTYP,
    EncodedPiece,
    build_section_head,
    covers_whole_video,
    merged_sample_description,
    plan_section_file,
    read_encoded_piece,
    section_pieces,
)

from tests.unit.core.mp4_builder import audio_spec, box, build_mp4, video_spec

# ue(v)로 적은 번호 0 · 1 · 2의 첫 바이트 — '1', '010', '011'
_ID_BITS = {0: 0x80, 1: 0x40, 2: 0x60}
_FRAME = 100  # 표준 영상의 프레임 길이(틱)
_DELAY = 100  # 표준 영상에서 키프레임의 (PTS − DTS)


def _sps(number: int, tail: bytes = b"\xaa") -> bytes:
    """번호가 number인 SPS — NAL 머리 · 프로파일 · 제약 · 레벨 뒤에 번호가 온다."""
    return bytes([0x67, 100, 0, 31, _ID_BITS[number]]) + tail


def _pps(number: int, tail: bytes = b"\xbb") -> bytes:
    return bytes([0x68, _ID_BITS[number]]) + tail


def _avc_stsd(sps: list[bytes], pps: list[bytes], kind: bytes = b"avc1") -> bytes:
    """H.264 샘플 엔트리 하나가 든 stsd 본문."""
    avcc = (
        bytes([1, 100, 0, 31, 0xFF, 0xE0 | len(sps)])
        + b"".join(struct.pack(">H", len(item)) + item for item in sps)
        + bytes([len(pps)])
        + b"".join(struct.pack(">H", len(item)) + item for item in pps)
        + b"\xfd\xf8\xf8\x00"  # High 프로파일의 뒤따르는 칸
    )
    return struct.pack(">II", 0, 1) + box(kind, bytes(78) + box(b"avcC", avcc))


_SOURCE_STSD = _avc_stsd([_sps(0)], [_pps(0)])


def _source(video_overrides: dict | None = None, audio: bool = True, **build):
    """표준 영상(+오디오)의 합성 mp4와 그 색인."""
    tracks = [video_spec(stsd=_SOURCE_STSD, **(video_overrides or {}))]
    if audio:
        tracks.append(audio_spec())
    built = build_mp4(tracks, **build)
    return built, parse_moov(built.moov)


def _piece(frames: int, number: int, fill: int) -> EncodedPiece:
    """프레임 frames개의 다시 인코딩한 조각 — I P P …, 재정렬 지연은 원본의 키프레임과 같다."""
    sizes = tuple(20 + n for n in range(frames))
    return EncodedPiece(
        timescale=1000,
        stsd=_avc_stsd([_sps(number)], [_pps(number)]),
        decode_ticks=tuple(n * _FRAME for n in range(frames)),
        composition=(_DELAY,) * frames,
        sizes=sizes,
        sync_samples=(0,),
        last_delta=_FRAME,
        payload=b"".join((bytes([fill, n]) * size)[:size] for n, size in enumerate(sizes)),
    )


def _pieces_for(plan) -> list[EncodedPiece]:
    return [
        _piece(piece.end - piece.first, number, 0xE0 + number)
        for number, piece in enumerate(plan.encoded_pieces, 1)
    ]


def _assemble(source: bytes, plan, encoded) -> bytes:
    """받기와 인코딩을 대신한다 — 대응표대로 옮기고, 조각을 붙이고, 머리를 쓴다."""
    out = bytearray(plan.encoded_offset + sum(len(made.payload) for made in encoded))
    for run in plan.runs:
        out[run.target_offset : run.target_offset + run.length] = source[
            run.source_offset : run.source_offset + run.length
        ]
    position = plan.encoded_offset
    for made in encoded:
        out[position : position + len(made.payload)] = made.payload
        position += len(made.payload)
    head = build_section_head(plan, encoded)
    assert len(head) == plan.body_offset
    out[: len(head)] = head
    return bytes(out)


def _read(data: bytes):
    return read_mp4_index(lambda offset, size: data[offset : offset + size])


def _top_boxes(data: bytes) -> list[tuple[bytes, int]]:
    """최상위 상자의 (종류, 길이) — 해석기를 쓰지 않고 직접 읽는다."""
    found, position = [], 0
    while position < len(data):
        size, kind = struct.unpack_from(">I4s", data, position)
        if size == 1:
            size = struct.unpack_from(">Q", data, position + 8)[0]
        found.append((kind, size))
        position += size
    return found


def _frame_bytes(data: bytes, index, frame: int) -> bytes:
    sample = index.frame_samples[frame]
    offset, size = index.video.offsets[sample], index.video.sizes[sample]
    return data[offset : offset + size]


# ================================================================ 조각 나누기


class TestSectionPieces:
    @pytest.mark.parametrize(
        "first, last, expected",
        [
            (0, 11, [("mid", 0, 12)]),  # 키프레임에서 시작해 영상 끝에서 끝난다
            (4, 11, [("mid", 4, 12)]),
            (1, 10, [("head", 1, 4), ("mid", 4, 8), ("tail", 8, 11)]),
            (2, 11, [("head", 2, 4), ("mid", 4, 12)]),  # 영상 끝에서 끝나면 꼬리가 없다
            (0, 9, [("mid", 0, 8), ("tail", 8, 10)]),  # 키프레임에서 시작하면 머리가 없다
            (1, 5, [("head", 1, 4), ("tail", 4, 6)]),  # 옮길 GOP가 없다
            (5, 6, [("whole", 5, 7)]),  # 한 GOP 안
            (4, 6, [("whole", 4, 7)]),  # 키프레임에서 시작해 같은 GOP 안에서 끝난다
            (9, 11, [("whole", 9, 12)]),  # 마지막 GOP의 중간에서 영상 끝까지
            (8, 11, [("mid", 8, 12)]),  # 마지막 GOP 전체
        ],
    )
    def test_pieces_follow_the_keyframes_and_the_end_of_the_video(self, first, last, expected):
        """구간이 키프레임 · 영상 끝과 어떻게 놓이느냐에 따라 조각이 정해져야 한다.

        키프레임 = 프레임 0 · 4 · 8, 프레임 12개
        -> 시작이 키프레임이면 head 없음, 끝이 프레임 11이면 tail 없음
        """
        _built, index = _source()

        pieces = section_pieces(index, first, last)

        assert [(p.kind, p.first, p.end) for p in pieces] == expected

    @pytest.mark.parametrize("first, last", [(-1, 3), (3, 2), (0, 12)])
    def test_frames_outside_the_video_are_rejected(self, first, last):
        """프레임 번호가 영상 범위를 벗어나면 ValueError여야 한다."""
        _built, index = _source()

        with pytest.raises(ValueError):
            section_pieces(index, first, last)

    def test_covers_whole_video_only_for_the_first_to_the_last_frame(self):
        """구간이 프레임 0부터 마지막 프레임까지일 때만 영상 전체로 봐야 한다."""
        _built, index = _source()

        assert covers_whole_video(index, 0, 11)
        assert not covers_whole_video(index, 0, 10)
        assert not covers_whole_video(index, 1, 11)


# ================================================================ 배치 · 대응표


class TestPlan:
    def test_runs_carry_the_moved_video_and_audio_samples_only(self):
        """대응표가 가리키는 원본 바이트는 옮기는 영상 샘플과 구간의 오디오 샘플뿐이어야 한다.

        구간 프레임 1~10 -> 옮기는 영상 = 프레임 4~7(샘플 4~7),
        오디오 = 0.1초에 걸친 샘플 1부터 1.1초 앞에서 시작하는 샘플 9까지
        """
        built, index = _source()

        plan = plan_section_file(index, built.moov, 1, 10)

        video_bytes = sum(40 + 3 * n for n in range(4, 8))
        assert plan.moved_bytes == video_bytes + 7 * 9
        assert sum(run.length for run in plan.runs) == plan.moved_bytes
        covered = set()
        for run in plan.runs:
            covered.update(range(run.source_offset, run.source_offset + run.length))
        wanted = set()
        for sample in range(4, 8):
            start = built.sample_offsets[b"vide"][sample]
            wanted.update(range(start, start + 40 + 3 * sample))
        for sample in range(1, 10):
            start = built.sample_offsets[b"soun"][sample]
            wanted.update(range(start, start + 7))
        assert covered == wanted

    def test_runs_fill_the_body_without_gaps_in_source_order(self):
        """대응표의 줄은 원본 위치 순서이고, 구간 파일의 본문을 빈틈없이 채워야 한다."""
        built, index = _source()

        plan = plan_section_file(index, built.moov, 1, 10)

        assert [run.source_offset for run in plan.runs] == sorted(
            run.source_offset for run in plan.runs
        )
        position = plan.body_offset
        for run in plan.runs:
            assert run.target_offset == position
            position += run.length
        assert position == plan.encoded_offset

    def test_adjacent_source_ranges_are_one_run(self):
        """원본에서 이어져 있는 옮길 바이트는 대응표의 한 줄이어야 한다.

        오디오 없는 영상의 프레임 4~11 -> 샘플 4~11이 원본에서 이어져 있다 -> 한 줄
        """
        built, index = _source(audio=False)

        plan = plan_section_file(index, built.moov, 4, 11)

        assert len(plan.runs) == 1
        assert plan.runs[0].source_offset == built.sample_offsets[b"vide"][4]

    def test_layout_is_ftyp_then_reserved_moov_then_mdat_header(self):
        """본문은 ftyp · moov 자리 · mdat 머리 바로 뒤에서 시작해야 한다."""
        built, index = _source()

        plan = plan_section_file(index, built.moov, 1, 10)

        assert plan.moov_offset == len(SECTION_FILE_FTYP)
        assert plan.body_offset == len(SECTION_FILE_FTYP) + plan.moov_reserved + MDAT_HEADER_BYTES

    def test_an_open_gop_is_rejected(self):
        """옮길 프레임 묶음이 디코드 순서로 이어진 샘플이 아니면 MP4_UNSUPPORTED여야 한다.

        샘플 4가 키프레임인데 샘플 5 · 6이 그보다 먼저 표시된다(열린 GOP)
        -> 프레임 0~5를 옮기려면 샘플 0~6이 필요한데 프레임은 6개다
        """
        built, index = _source(
            {
                "deltas": [100] * 8,
                "sizes": [30] * 8,
                "chunks": [8],
                "composition": [100, 300, 0, 0, 300, 0, 0, 100],
                "sync": [1, 5],
                "edits": [(800, 100)],
            },
            audio=False,
        )

        with pytest.raises(Mp4Error) as caught:
            plan_section_file(index, built.moov, 0, 6)

        assert caught.value.message_key == MP4_UNSUPPORTED

    def test_one_sample_per_chunk_and_many_samples_per_chunk_give_the_same_file(self):
        """청크마다 샘플이 하나든 여럿이든 구간 파일의 프레임과 샘플 바이트가 같아야 한다."""
        results = []
        for chunks in ([3, 3, 2, 4], [1] * 12):
            built, index = _source({"chunks": chunks})
            plan = plan_section_file(index, built.moov, 1, 10)
            data = _assemble(built.data, plan, _pieces_for(plan))
            made = _read(data)
            results.append(
                (
                    list(made.frame_pts),
                    [_frame_bytes(data, made, frame) for frame in range(len(made.frame_pts))],
                )
            )

        assert results[0] == results[1]


# ================================================================ 구간 파일 통째로


class TestSectionFile:
    def test_top_level_boxes_fill_the_file(self):
        """구간 파일은 ftyp · moov · free · mdat 넷이고 그 길이의 합이 파일 길이여야 한다."""
        built, index = _source()
        plan = plan_section_file(index, built.moov, 1, 10)
        encoded = _pieces_for(plan)

        data = _assemble(built.data, plan, encoded)

        boxes = _top_boxes(data)
        assert [kind for kind, _size in boxes] == [b"ftyp", b"moov", b"free", b"mdat"]
        assert sum(size for _kind, size in boxes) == len(data)
        assert boxes[1][1] + boxes[2][1] == plan.moov_reserved
        payloads = sum(len(made.payload) for made in encoded)
        assert boxes[3][1] == MDAT_HEADER_BYTES + plan.moved_bytes + payloads

    @pytest.mark.parametrize(
        "first, last", [(first, last) for first in range(12) for last in range(first, 12)]
    )
    def test_every_section_reads_back_as_its_frames_from_zero(self, first, last):
        """어느 구간이든 구간 파일의 프레임 수 · 표시 시각 · 샘플 바이트가 구간의 것이어야 한다.

        프레임 [first, last] -> 프레임 last − first + 1개, 표시 시각 0.0 · 0.1 · …
        옮긴 프레임의 바이트 = 원본 그 프레임의 바이트, 조각의 프레임 = 조각의 바이트
        """
        built, index = _source()
        if covers_whole_video(index, first, last):
            pytest.skip("영상 전체 구간은 구간 파일을 만들지 않는다")
        plan = plan_section_file(index, built.moov, first, last)
        encoded = _pieces_for(plan)

        data = _assemble(built.data, plan, encoded)

        made = _read(data)
        assert list(made.frame_pts) == pytest.approx([n * 0.1 for n in range(last - first + 1)])
        expected = {}
        for piece, piece_made in zip(plan.encoded_pieces, encoded):
            position = 0
            for number, size in enumerate(piece_made.sizes):
                expected[piece.first + number] = piece_made.payload[position : position + size]
                position += size
        for frame in range(first, last + 1):
            if frame not in expected:
                sample = index.frame_samples[frame]
                expected[frame] = built.sample_bytes(b"vide", sample, 40 + 3 * sample)
        assert [_frame_bytes(data, made, n) for n in range(last - first + 1)] == [
            expected[frame] for frame in range(first, last + 1)
        ]

    def test_keyframes_are_the_piece_starts_and_the_moved_keyframes(self):
        """구간 파일의 키프레임은 조각의 첫 프레임과 옮긴 키프레임이어야 한다.

        프레임 1~10 = 머리(1~3) · 옮김(4~7) · 꼬리(8~10) -> 구간 안 번호 0 · 3 · 7
        """
        built, index = _source()
        plan = plan_section_file(index, built.moov, 1, 10)

        made = _read(_assemble(built.data, plan, _pieces_for(plan)))

        assert list(made.keyframes) == [0, 3, 7]

    def test_audio_starts_on_the_sample_that_spans_the_section_start(self):
        """오디오는 구간 시작에 걸친 샘플부터 들어가고, 앞의 남는 부분만큼 음수 시각이어야 한다.

        구간 시작 0.1초 -> 오디오 샘플 1(0.0초에서 시작) 부터 -> 첫 샘플의 표시 시각 −0.1초
        끝 1.1초 앞에서 시작하는 샘플 9까지 -> 9개
        """
        built, index = _source()
        plan = plan_section_file(index, built.moov, 1, 10)

        data = _assemble(built.data, plan, _pieces_for(plan))

        audio = _read(data).audio
        assert len(audio.sizes) == 9
        assert audio.times[0] == pytest.approx(-0.1)
        assert audio.times[1] == pytest.approx(0.028)
        for number in range(9):
            offset = audio.offsets[number]
            assert data[offset : offset + 7] == built.sample_bytes(b"soun", number + 1, 7)

    def test_a_section_from_zero_leaves_out_the_hidden_first_audio_sample(self):
        """0초에서 시작하는 구간은 원본이 가린 첫 오디오 샘플을 넣지 않아야 한다.

        오디오 샘플 0은 −0.128초(편집 목록이 가린다) -> 샘플 1부터, 첫 표시 시각 0.0초
        """
        built, index = _source()
        plan = plan_section_file(index, built.moov, 0, 9)

        data = _assemble(built.data, plan, _pieces_for(plan))

        audio = _read(data).audio
        assert audio.times[0] == pytest.approx(0.0)
        offset = audio.offsets[0]
        assert data[offset : offset + 7] == built.sample_bytes(b"soun", 1, 7)

    def test_a_video_without_audio_gives_a_file_without_an_audio_track(self):
        """원본에 오디오가 없으면 구간 파일에도 오디오 트랙이 없어야 한다."""
        built, index = _source(audio=False)
        plan = plan_section_file(index, built.moov, 1, 10)

        made = _read(_assemble(built.data, plan, _pieces_for(plan)))

        assert made.audio is None
        assert len(made.frame_pts) == 10

    def test_sample_description_holds_the_source_and_the_piece_parameter_sets(self):
        """영상 샘플 설명의 avcC에 원본과 조각들의 SPS · PPS가 모두 들어 있어야 한다.

        원본 0번, 머리 1번, 꼬리 2번 -> SPS 3개 · PPS 3개
        """
        built, index = _source()
        plan = plan_section_file(index, built.moov, 1, 10)

        data = _assemble(built.data, plan, _pieces_for(plan))

        at = data.index(b"avcC") + 4
        assert data[at + 5] & 0x1F == 3
        for number in range(3):
            assert _sps(number) in data[at : at + 200]
            assert _pps(number) in data[at : at + 200]

    def test_audio_that_starts_after_the_section_start_keeps_its_delay(self):
        """오디오가 구간 시작보다 늦게 시작하면 그만큼 늦게 시작해야 한다.

        오디오 앞에 0.3초의 빈 편집이 있다 -> 구간 0초~ 에서 첫 오디오 샘플의 표시 시각 0.3초
        """
        tracks = [video_spec(stsd=_SOURCE_STSD), audio_spec(edits=[(300, -1), (1920, 0)])]
        built = build_mp4(tracks)
        index = parse_moov(built.moov)
        plan = plan_section_file(index, built.moov, 0, 9)

        made = _read(_assemble(built.data, plan, _pieces_for(plan)))

        assert made.frame_pts[0] == pytest.approx(0.0)
        assert made.audio.times[0] == pytest.approx(0.3)

    def test_durations_in_the_headers_are_the_section_length(self):
        """mvhd · tkhd의 길이는 구간 길이(무비 틱), 영상 mdhd의 길이는 프레임 길이의 합이어야 한다.

        프레임 1~10 = 10프레임 × 100틱 -> 무비 timescale 1000에서 1000, 영상 timescale 1000에서 1000
        """
        built, index = _source()
        plan = plan_section_file(index, built.moov, 1, 10)

        data = _assemble(built.data, plan, _pieces_for(plan))

        mvhd = data.index(b"mvhd") + 4
        assert struct.unpack_from(">I", data, mvhd + 16)[0] == 1000
        tkhd = data.index(b"tkhd") + 4
        assert struct.unpack_from(">I", data, tkhd + 20)[0] == 1000
        mdhd = data.index(b"mdhd") + 4  # 첫 트랙이 영상이다
        assert struct.unpack_from(">II", data, mdhd + 12) == (1000, 1000)

    def test_a_worst_case_piece_still_fits_the_reserved_space(self):
        """프레임마다 길이 · 재정렬 값이 다르고 모두 키프레임인 조각도 잡아 둔 자리에 들어가야 한다.

        GOP 하나짜리 영상의 프레임 1~10 = 조각 하나(10프레임).
        표가 가장 커지는 조각: 길이 100 · 101 · …, (PTS − DTS) 100 · 101 · …, 키프레임 10개,
        SPS 185바이트 · PPS 42바이트
        """
        built, index = _source({"sync": [1]}, audio=False)
        plan = plan_section_file(index, built.moov, 1, 10)
        assert [(p.kind, p.first, p.end) for p in plan.pieces] == [("whole", 1, 11)]
        deltas = [100 + n for n in range(10)]
        ticks = [sum(deltas[:n]) for n in range(10)]
        piece = EncodedPiece(
            timescale=1000,
            stsd=_avc_stsd([_sps(1, bytes(180))], [_pps(1, bytes(40))]),
            decode_ticks=tuple(ticks),
            composition=tuple(100 + n for n in range(10)),
            sizes=(10,) * 10,
            sync_samples=tuple(range(10)),
            last_delta=77,
            payload=bytes(100),
        )

        head = build_section_head(plan, [piece])

        assert len(head) == plan.body_offset

    def test_a_long_media_duration_is_written_in_64_bits(self):
        """트랙 길이가 32비트를 넘으면 64비트 칸으로 쓰여, 다시 읽은 표시 시각이 맞아야 한다.

        프레임 길이 500,000,000틱, 프레임 0~9 = 5,000,000,000틱 > 2^32 − 1.
        원본의 mdhd는 32비트 칸이고 길이를 0으로 적었다(해석기는 샘플 표로 길이를 잰다)
        """
        step = 500_000_000
        built, index = _source(
            {
                "timescale": 1_000_000_000,
                "deltas": [step] * 12,
                "composition": [step, 3 * step, 0, 0] * 3,
                "edits": [(6000, step)],
                "media_duration": 0,
            },
            audio=False,
        )
        plan = plan_section_file(index, built.moov, 0, 9)
        encoded = [
            EncodedPiece(
                timescale=1_000_000_000,
                stsd=_avc_stsd([_sps(1)], [_pps(1)]),
                decode_ticks=(0, step),
                composition=(step, step),
                sizes=(10, 10),
                sync_samples=(0,),
                last_delta=step,
                payload=bytes(20),
            )
        ]

        made = _read(_assemble(built.data, plan, encoded))

        assert list(made.frame_pts) == pytest.approx([n * 0.5 for n in range(10)])
        assert made.duration == pytest.approx(5.0)


# ================================================================ 머리 쓰기의 거부


class TestBuildRejects:
    def _plan(self):
        built, index = _source()
        return plan_section_file(index, built.moov, 1, 10)

    def test_a_missing_piece_is_rejected(self):
        """조각의 수가 계획과 다르면 MP4_INVALID여야 한다."""
        plan = self._plan()

        with pytest.raises(Mp4Error) as caught:
            build_section_head(plan, _pieces_for(plan)[:1])

        assert caught.value.message_key == MP4_INVALID

    def test_a_piece_with_another_frame_count_is_rejected(self):
        """조각의 프레임 수가 계획과 다르면 MP4_INVALID여야 한다.

        머리는 프레임 3개여야 하는데 4개짜리 조각
        """
        plan = self._plan()
        encoded = _pieces_for(plan)
        encoded[0] = _piece(4, 1, 0xE1)

        with pytest.raises(Mp4Error) as caught:
            build_section_head(plan, encoded)

        assert caught.value.message_key == MP4_INVALID

    def test_a_piece_with_another_timescale_is_rejected(self):
        """조각의 timescale이 원본과 다르면 MP4_INVALID여야 한다."""
        plan = self._plan()
        encoded = _pieces_for(plan)
        encoded[0] = EncodedPiece(**{**encoded[0].__dict__, "timescale": 90_000})

        with pytest.raises(Mp4Error) as caught:
            build_section_head(plan, encoded)

        assert caught.value.message_key == MP4_INVALID

    def test_a_piece_that_runs_into_the_moved_video_is_rejected(self):
        """머리 조각의 마지막 DTS가 옮긴 영상의 첫 DTS 이상이면 MP4_INVALID여야 한다.

        머리의 재정렬 지연을 0으로 두면 DTS가 PTS와 같아져 100 · 200 · 300 → 200 · 300 · 400,
        옮긴 영상의 첫 DTS는 400이다
        """
        plan = self._plan()
        encoded = _pieces_for(plan)
        encoded[0] = EncodedPiece(**{**encoded[0].__dict__, "composition": (0, 0, 0)})

        with pytest.raises(Mp4Error) as caught:
            build_section_head(plan, encoded)

        assert caught.value.message_key == MP4_INVALID

    def test_parameter_sets_that_reuse_the_source_number_are_rejected(self):
        """조각의 SPS가 원본과 내용이 다른데 번호가 같으면 MP4_INVALID여야 한다."""
        plan = self._plan()
        encoded = _pieces_for(plan)
        clashing = _avc_stsd([_sps(0, b"\xcc")], [_pps(1)])
        encoded[0] = EncodedPiece(**{**encoded[0].__dict__, "stsd": clashing})

        with pytest.raises(Mp4Error) as caught:
            build_section_head(plan, encoded)

        assert caught.value.message_key == MP4_INVALID

    def test_a_payload_that_does_not_match_the_sample_sizes_is_rejected(self):
        """조각의 샘플 바이트 길이가 샘플 크기의 합과 다르면 MP4_INVALID여야 한다."""
        plan = self._plan()
        encoded = _pieces_for(plan)
        encoded[0] = EncodedPiece(**{**encoded[0].__dict__, "payload": encoded[0].payload[:-1]})

        with pytest.raises(Mp4Error) as caught:
            build_section_head(plan, encoded)

        assert caught.value.message_key == MP4_INVALID

    def test_a_moov_larger_than_the_reserved_space_is_rejected(self):
        """moov가 잡아 둔 자리를 넘으면 MP4_INVALID여야 한다.

        조각의 SPS를 2000바이트로 키운다 — 조각마다 잡아 둔 여유는 276바이트다
        """
        plan = self._plan()
        encoded = _pieces_for(plan)
        huge = _avc_stsd([_sps(1, bytes(2000))], [_pps(1)])
        encoded[0] = EncodedPiece(**{**encoded[0].__dict__, "stsd": huge})

        with pytest.raises(Mp4Error) as caught:
            build_section_head(plan, encoded)

        assert caught.value.message_key == MP4_INVALID


# ================================================================ 영상 설정 합치기


class TestMergedSampleDescription:
    def test_identical_parameter_sets_are_kept_once(self):
        """조각의 SPS · PPS가 원본과 바이트가 같으면 한 번만 들어가야 한다."""
        merged = merged_sample_description(_SOURCE_STSD, [_SOURCE_STSD])

        assert merged == _SOURCE_STSD

    def test_the_entry_grows_by_the_added_parameter_sets(self):
        """합친 뒤 샘플 엔트리의 길이 칸이 실제 길이와 같아야 한다."""
        merged = merged_sample_description(_SOURCE_STSD, [_avc_stsd([_sps(1)], [_pps(1)])])

        assert struct.unpack_from(">I", merged, 8)[0] == len(merged) - 8
        assert len(merged) == len(_SOURCE_STSD) + (2 + len(_sps(1))) + (2 + len(_pps(1)))

    def test_pps_that_reuse_a_number_are_rejected(self):
        """조각의 PPS가 원본과 내용이 다른데 번호가 같으면 MP4_INVALID여야 한다."""
        with pytest.raises(Mp4Error) as caught:
            merged_sample_description(_SOURCE_STSD, [_avc_stsd([_sps(1)], [_pps(0, b"\xcc")])])

        assert caught.value.message_key == MP4_INVALID

    def test_a_codec_other_than_h264_is_rejected(self):
        """샘플 엔트리가 H.264가 아니면 MP4_UNSUPPORTED여야 한다."""
        hevc = _avc_stsd([_sps(0)], [_pps(0)], kind=b"hvc1")

        with pytest.raises(Mp4Error) as caught:
            merged_sample_description(hevc, [_avc_stsd([_sps(1)], [_pps(1)])])

        assert caught.value.message_key == MP4_UNSUPPORTED


# ================================================================ 조각 읽기


class TestReadEncodedPiece:
    def test_reads_the_video_samples_and_their_tables(self):
        """조각 파일에서 샘플별 DTS · (PTS − DTS) · 크기 · 키프레임 · 바이트를 읽어야 한다.

        I P B B 4샘플, 길이 100틱 -> DTS 0 · 100 · 200 · 300, 키프레임은 샘플 0
        """
        spec = video_spec(
            deltas=[100] * 4,
            sizes=[11, 12, 13, 14],
            chunks=[2, 2],
            composition=[100, 300, 0, 0],
            sync=[1],
            edits=[(400, 100)],
            stsd=_SOURCE_STSD,
        )
        built = build_mp4([spec])

        piece = read_encoded_piece(built.data)

        assert piece.timescale == 1000
        assert piece.decode_ticks == (0, 100, 200, 300)
        assert piece.composition == (100, 300, 0, 0)
        assert piece.sizes == (11, 12, 13, 14)
        assert piece.sync_samples == (0,)
        assert piece.last_delta == 100
        assert piece.stsd == _SOURCE_STSD
        assert piece.payload == b"".join(
            built.sample_bytes(b"vide", number, size) for number, size in enumerate(piece.sizes)
        )

    def test_a_file_without_moov_is_rejected(self):
        """moov가 없으면 MP4_INVALID여야 한다."""
        built = build_mp4([video_spec(stsd=_SOURCE_STSD)], include_moov=False)

        with pytest.raises(Mp4Error) as caught:
            read_encoded_piece(built.data)

        assert caught.value.message_key == MP4_INVALID

    def test_a_file_without_a_video_track_is_rejected(self):
        """영상 트랙이 없으면 MP4_INVALID여야 한다."""
        built = build_mp4([audio_spec()])

        with pytest.raises(Mp4Error) as caught:
            read_encoded_piece(built.data)

        assert caught.value.message_key == MP4_INVALID

    def test_samples_beyond_the_file_are_rejected(self):
        """샘플이 파일 끝을 넘으면 MP4_INVALID여야 한다."""
        built = build_mp4([video_spec(stsd=_SOURCE_STSD)])

        with pytest.raises(Mp4Error) as caught:
            read_encoded_piece(built.data[:-10])

        assert caught.value.message_key == MP4_INVALID


def test_pieces_are_cut_pieces():
    """조각은 컷의 조각 모델(CutPiece)이어야 한다 — 인코딩 명령이 그대로 받는다."""
    _built, index = _source()

    assert all(isinstance(piece, CutPiece) for piece in section_pieces(index, 1, 10))
