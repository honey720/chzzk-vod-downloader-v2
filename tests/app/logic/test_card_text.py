"""카드 글 함수 — 위젯 없이 상태 · 크기 · 구간 요약 · 경고 글을 만든다 (#545).

번역기를 설치하지 않으므로 글은 원문(영어)이다. 카드 위젯은 같은 함수를 부른다 — 위젯이
그리는 글은 tests/app/gui의 카드 테스트가 본다.
"""

from fractions import Fraction

import pytest

from app.viewmodels import card_text
from app.viewmodels.data import SECTION_CHECK_PENDING, SECTION_CHECK_UNVERIFIED, ContentItem
from app.viewmodels.item_state import ItemState
from core.media.representations import StreamEntry
from core.models.download_state import DownloadState
from core.models.plan import TimeRange

KB = 1024  # 바이트


def _item(
    state: DownloadState | ItemState = DownloadState.WAITING,
    content_type: str = "video",
    sections: tuple[tuple[float, float], ...] = (),
) -> ContentItem:
    """1080p 한 항목짜리 카드 데이터 — 저장 위치 C:/dl, 길이 3600초."""
    item = ContentItem(
        "https://chzzk.naver.com/video/1",
        {"title": "제목", "channelName": "채널", "createdDate": "", "duration": 3600},
        [StreamEntry(1080, "u1")],
        1080,
        "u1",
        "C:/dl",
        content_type,
        None,
    )
    item.downloadState = state
    item.selections = tuple(TimeRange(start, end) for start, end in sections)
    return item


# ---- 기본 표기 ----


class TestBasicText:
    """크기 · 시간 · 해상도의 표기."""

    @pytest.mark.parametrize(
        ("size", "expected"),
        [
            (0, "0.00 B"),
            (1023, "1023.00 B"),
            (KB, "1.00 KB"),
            (624_259_809, "595.34 MB"),
            ("x", "0 B"),
        ],
    )
    def test_size_text_uses_1024_units_with_two_decimals(self, size: object, expected: str) -> None:
        """size_text가 바이트 수를 받으면 1024 단위 · 소수 두 자리 글이어야 한다(숫자가 아니면 「0 B」).

        0 · 1023 · 1024 · 624259809 · "x" -> 0.00 B · 1023.00 B · 1.00 KB · 595.34 MB · 0 B
        """
        assert card_text.size_text(size) == expected

    @pytest.mark.parametrize(
        ("remain", "expected"),
        [("00:03:12", "3:12"), ("01:02:03", "1:02:03"), ("N/A", "N/A")],
    )
    def test_short_time_drops_zero_hours(self, remain: str, expected: str) -> None:
        """short_time이 HH:MM:SS를 받으면 시간이 0일 때 시간을 빼야 한다(형식 밖이면 그대로).

        00:03:12 · 01:02:03 · N/A -> 3:12 · 1:02:03 · N/A
        """
        assert card_text.short_time(remain) == expected

    def test_resolution_text_marks_the_source_only_when_tagged(self) -> None:
        """resolution_text가 원본 표시(resolution_tag)가 있으면 「(source)」를 붙여야 한다.

        1080 · 표시 없음 -> "1080p", 표시 있음 -> "1080p(source)"
        """
        item = _item()
        assert card_text.resolution_text(item) == "1080p"
        item.resolution_tag = "(원본)"
        assert card_text.resolution_text(item) == "1080p(source)"

    def test_rep_label_and_secondary_show_source_and_frame_rate(self) -> None:
        """rep_label · rep_secondary가 원본 · 50fps 이상 항목이면 「(source)」 · 「60fps」여야 한다.

        StreamEntry(1080, original, 59.94fps) -> "1080p(source)", "60fps"; 30fps -> "1080p", ""
        """
        source = StreamEntry(1080, "u1", frame_rate=59.94, original=True)
        plain = StreamEntry(1080, "u2", frame_rate=30.0)
        assert (card_text.rep_label(source), card_text.rep_secondary(source)) == (
            "1080p(source)",
            "60fps",
        )
        assert (card_text.rep_label(plain), card_text.rep_secondary(plain)) == ("1080p", "")


# ---- 상태 글(slot_texts) ----


class TestSlotTexts:
    """상태마다 3줄 상태 글 · 크기 글 · 상태 툴팁."""

    def test_loading_shows_the_lookup_and_an_empty_size(self) -> None:
        """조회 중 카드면 상태 글이 「Loading information...」, 크기 글이 빈 글이어야 한다.

        ItemState.LOADING -> ("Loading information...", "", None)
        """
        texts = card_text.slot_texts(_item(ItemState.LOADING))
        assert (texts.status, texts.size, texts.status_tip) == ("Loading information...", "", None)

    def test_waiting_mp4_shows_the_looked_up_size(self) -> None:
        """대기 mp4 카드면 크기 글이 조회한 크기(total_size) 그대로여야 한다.

        total_size "595.34 MB" -> ("Download waiting", "595.34 MB")
        """
        item = _item()
        item.total_size = "595.34 MB"
        texts = card_text.slot_texts(item)
        assert (texts.status, texts.size) == ("Download waiting", "595.34 MB")

    @pytest.mark.parametrize(("duration", "expected"), [(3600, "01:00:00"), (None, "")])
    def test_waiting_segment_card_shows_the_length(
        self, duration: int | None, expected: str
    ) -> None:
        """대기 세그먼트 카드면 크기 글이 영상 길이여야 한다(길이를 모르면 빈 글, #515).

        m3u8 · 길이 3600 -> "01:00:00", 길이 None -> ""
        """
        item = _item(content_type="m3u8")
        item.duration = duration
        assert card_text.slot_texts(item).size == expected

    def test_waiting_section_card_shows_the_section_summary(self) -> None:
        """대기 구간 카드면 크기 글이 구간 요약이어야 한다.

        mp4 · 구간 2개 · section_bytes 2048 -> "Sections 2 · 2.00 KB"
        """
        item = _item(sections=((0, 60), (120, 180)))
        item.section_bytes = 2 * KB
        assert card_text.slot_texts(item).size == "Sections 2 · 2.00 KB"

    def test_running_shows_progress_speed_and_remaining_time(self) -> None:
        """받는 중 카드면 상태 글이 「% · 속도 · 남은 시간 left」, 크기 글이 「해상도 · 받은 / 전체」여야 한다.

        42% · 2.5 MB/s · 00:03:12 · 받은 1024 / total_size "2.00 KB"
        -> "42% · 2.5 MB/s · 3:12 left", "1080p · 1.00 KB / 2.00 KB"
        """
        item = _item(DownloadState.RUNNING)
        item.download_progress = 42
        item.download_speed = "2.5 MB/s"
        item.download_remain_time = "00:03:12"
        item.download_size = KB
        item.total_size = "2.00 KB"
        texts = card_text.slot_texts(item)
        assert (texts.status, texts.size) == (
            "42% · 2.5 MB/s · 3:12 left",
            "1080p · 1.00 KB / 2.00 KB",
        )

    def test_running_while_preparing_shows_preparing(self) -> None:
        """받는 중이고 준비 중(preparing)이면 상태 글이 「Preparing」이어야 한다.

        RUNNING · preparing True -> "Preparing"
        """
        item = _item(DownloadState.RUNNING)
        item.preparing = True
        assert card_text.slot_texts(item).status == "Preparing"

    @pytest.mark.parametrize(
        ("preparing", "word"), [(False, "Cutting"), (True, "Preparing to cut")]
    )
    def test_cutting_shows_the_stage_and_the_section_tally(
        self, preparing: bool, word: str
    ) -> None:
        """구간을 자르는 단계면 상태 글이 「% · 단계 · 자른 수/전체」여야 한다.

        구간 2개 · post_process · 자른 1개 · 92% · cut_preparing False/True
        -> "92% · Cutting · 1/2", "92% · Preparing to cut · 1/2"
        """
        item = _item(DownloadState.RUNNING, sections=((0, 60), (120, 180)))
        item.post_process = True
        item.sections_done = 1
        item.download_progress = 92
        item.cut_preparing = preparing
        assert card_text.slot_texts(item).status == f"92% · {word} · 1/2"

    def test_segment_post_processing_shows_the_stage(self) -> None:
        """세그먼트 카드가 후처리 중이면 상태 글이 「% · Post-processing」이어야 한다.

        m3u8 · post_process · 94% -> "94% · Post-processing"
        """
        item = _item(DownloadState.RUNNING, content_type="m3u8")
        item.post_process = True
        item.download_progress = 94
        assert card_text.slot_texts(item).status == "94% · Post-processing"

    @pytest.mark.parametrize(("pending", "expected"), [(False, "59% · Paused"), (True, "Paused")])
    def test_paused_shows_progress_unless_paused_while_preparing(
        self, pending: bool, expected: str
    ) -> None:
        """일시정지 카드면 「% · Paused」, 준비 중에 멈췄으면(prepare_pending) 「Paused」만이어야 한다.

        59% · prepare_pending False/True -> "59% · Paused", "Paused"
        """
        item = _item(DownloadState.PAUSED)
        item.download_progress = 59
        item.prepare_pending = pending
        assert card_text.slot_texts(item).status == expected

    @pytest.mark.parametrize(
        ("elapsed", "expected"), [("00:02:12", "✓ Completed · 2:12"), ("", "✓ Completed")]
    )
    def test_finished_shows_the_elapsed_time_when_known(self, elapsed: str, expected: str) -> None:
        """완료 카드면 상태 글이 「✓ Completed · 걸린 시간」(모르면 시간 없이)이어야 한다.

        download_time 00:02:12 / "" -> "✓ Completed · 2:12", "✓ Completed"
        """
        item = _item(DownloadState.FINISHED)
        item.download_time = elapsed
        assert card_text.slot_texts(item).status == expected

    def test_finished_size_prefers_the_saved_file_size(self) -> None:
        """완료 카드의 크기 글이 받은 양이 아니라 저장된 파일 크기(output_bytes)여야 한다.

        받은 1024 · output_bytes 2048 -> "1080p · 2.00 KB"
        """
        item = _item(DownloadState.FINISHED)
        item.download_size = KB
        item.output_bytes = 2 * KB
        assert card_text.slot_texts(item).size == "1080p · 2.00 KB"

    def test_failed_shows_the_first_line_and_keeps_the_full_reason_in_the_tip(self) -> None:
        """실패 카드면 상태 글이 「✕ 첫 줄」, 툴팁이 사유 전문, 크기 글은 바꾸지 않음(None)이어야 한다.

        stateMessage "저장 실패\\n자세히" -> ("✕ 저장 실패", None, "저장 실패\\n자세히")
        """
        item = _item(DownloadState.FAILED)
        item.stateMessage = "저장 실패\n자세히"
        texts = card_text.slot_texts(item)
        assert (texts.status, texts.size, texts.status_tip) == (
            "✕ 저장 실패",
            None,
            "저장 실패\n자세히",
        )

    def test_failed_without_a_reason_points_to_the_log(self) -> None:
        """사유 없는 실패 카드면 상태 글 · 툴팁이 로그를 보라는 안내여야 한다.

        stateMessage "" -> "✕ Unknown error - check the log"
        """
        texts = card_text.slot_texts(_item(DownloadState.FAILED))
        assert texts.status == "✕ Unknown error - check the log"
        assert texts.status_tip == "Unknown error - check the log"

    def test_partly_failed_sections_put_the_counts_before_the_reason(self) -> None:
        """구간 일부만 실패한 카드면 상태 글이 「✕ N failed · 끝낸 수/전체 · 사유」여야 한다.

        구간 3개 · 실패 1 · 끝냄 2 · 사유 "자르기 실패" -> "✕ 1 failed · 2/3 · 자르기 실패"
        """
        item = _item(DownloadState.FAILED, sections=((0, 60), (120, 180), (240, 300)))
        item.sections_failed = 1
        item.sections_done = 2
        item.stateMessage = "자르기 실패"
        assert card_text.slot_texts(item).status == "✕ 1 failed · 2/3 · 자르기 실패"


# ---- 크기 ----


class TestSizeText:
    """받는 중 · 일시정지 크기 글의 갈래."""

    def test_segment_card_shows_only_the_received_size(self) -> None:
        """세그먼트 카드면 받은 크기만이어야 한다(받을 크기를 모른다).

        m3u8 · 받은 1024 -> "1.00 KB"
        """
        item = _item(DownloadState.RUNNING, content_type="m3u8")
        item.download_size = KB
        assert card_text.received_size_text(item) == "1.00 KB"

    def test_section_card_scales_the_estimate_by_the_transfer(self) -> None:
        """mp4 구간 카드면 「추정 크기 × 전송 진행 / 추정 크기」여야 한다.

        section_bytes 2048 · 받은 512 / 엔진이 받을 1024 -> "1.00 KB / 2.00 KB"
        """
        item = _item(DownloadState.RUNNING, sections=((0, 60),))
        item.section_bytes = 2 * KB
        item.transfer_bytes = KB
        item.download_size = KB // 2
        assert card_text.received_size_text(item) == "1.00 KB / 2.00 KB"

    def test_whole_size_falls_back_to_the_engine_size_when_not_looked_up(self) -> None:
        """조회한 크기가 숫자가 아니면 전체 크기가 엔진이 정한 크기(transfer_bytes)여야 한다.

        total_size "Checking..." · transfer_bytes 2048 · 받은 1024 -> "1.00 KB / 2.00 KB"
        """
        item = _item(DownloadState.RUNNING)
        item.total_size = "Checking..."
        item.transfer_bytes = 2 * KB
        item.download_size = KB
        assert card_text.received_size_text(item) == "1.00 KB / 2.00 KB"
        assert card_text.received_size_text(item, with_total=False) == "1.00 KB"


# ---- 구간 ----


class TestSectionText:
    """구간 요약 · 겹침 · 경고 안내."""

    def test_overlapping_sections_are_marked_and_explained(self) -> None:
        """겹치는 구간이면 요약에 「overlap」, 툴팁 줄에 받을 양 · 합 · 한 번만 받음이어야 한다.

        m3u8 · 구간 0~60 · 30~90 -> "Sections 2 · overlap · 1:30",
        ["To receive: 1:30", "Sections add up to 2:00", "Overlapping parts are received once"]
        """
        item = _item(content_type="m3u8", sections=((0, 60), (30, 90)))
        assert card_text.section_summary(item) == "Sections 2 · overlap · 1:30"
        assert card_text.section_overlap_detail(item) == [
            "To receive: 1:30",
            "Sections add up to 2:00",
            "Overlapping parts are received once",
        ]

    def test_touching_sections_do_not_overlap(self) -> None:
        """맞닿기만 한 구간이면 겹침이 아니어야 한다.

        구간 0~60 · 60~120 -> sections_overlap False
        """
        assert not card_text.sections_overlap(_item(sections=((0, 60), (60, 120))))

    def test_tally_appears_only_from_the_cut_stage_with_two_or_more_sections(self) -> None:
        """완료 구간 수가 구간 둘 이상이고 전송이 끝난 뒤에만 붙어야 한다.

        구간 1개 완료 -> "", 구간 2개 받는 중(전송) -> "", 구간 2개 완료 · 끝냄 2 -> " · 2/2"
        """
        one = _item(DownloadState.FINISHED, sections=((0, 60),))
        two = _item(DownloadState.RUNNING, sections=((0, 60), (120, 180)))
        assert card_text.section_tally(one) == ""
        assert card_text.section_tally(two) == ""
        two.downloadState = DownloadState.FINISHED
        two.sections_done = 2
        assert card_text.section_tally(two) == " · 2/2"

    def test_notice_lists_every_change_and_warnings_only_keeps_the_warnings(self) -> None:
        """구간 알림이 한 일과 경고를 차례로 적고, warnings_only면 경고만이어야 한다.

        fps 30 · 끝 당김 · 끝 늘림 · 맞출 수 없는 구간 1 · 길이 확인 중
        -> "refit to 30fps · end pulled to the video length · end extended to the video length
           · 1 outside the video · checking length", 경고만 "1 outside the video · checking length"
        """
        item = _item(sections=((0, 60),))
        item.section_refit_fps = Fraction(30)
        item.section_end_pulled = True
        item.section_end_extended = True
        item.section_unfit = frozenset({0})
        item.section_check = SECTION_CHECK_PENDING
        assert card_text.section_notice(item) == (
            "refit to 30fps · end pulled to the video length · end extended to the video length"
            " · 1 outside the video · checking length"
        )
        assert (
            card_text.section_notice(item, warnings_only=True)
            == "1 outside the video · checking length"
        )
        assert len(card_text.section_notice_detail(item)) == 5

    def test_unverified_length_has_its_own_notice(self) -> None:
        """길이를 확인하지 못했으면 알림이 「length not checked」, 툴팁 줄이 다시 확인 안내여야 한다.

        section_check UNVERIFIED -> "length not checked", ["Could not check … check again."]
        """
        item = _item(sections=((0, 60),))
        item.section_check = SECTION_CHECK_UNVERIFIED
        assert card_text.section_notice(item) == "length not checked"
        assert card_text.section_notice_detail(item) == [
            "Could not check the length of this resolution. Open the section editor to check again."
        ]

    def test_summary_carries_the_notice_after_the_size(self) -> None:
        """구간 요약이 「수 · 크기 · 알림」 차례여야 하고, with_notice False면 알림을 빼야 한다.

        mp4 · 구간 1 · section_bytes 1024 · 맞출 수 없는 구간 1
        -> "Sections 1 · 1.00 KB · 1 outside the video", 알림 없이 "Sections 1 · 1.00 KB"
        """
        item = _item(sections=((0, 60),))
        item.section_bytes = KB
        item.section_unfit = frozenset({0})
        assert card_text.section_summary(item) == "Sections 1 · 1.00 KB · 1 outside the video"
        assert card_text.section_summary(item, with_notice=False) == "Sections 1 · 1.00 KB"

    def test_size_note_says_checking_while_counting(self) -> None:
        """구간 크기를 세는 중(section_sizing)이고 값이 없으면 크기 자리가 「Checking...」이어야 한다.

        section_sizing True · section_bytes 없음 -> "Checking..."
        """
        item = _item(sections=((0, 60),))
        item.section_sizing = True
        assert card_text.section_size_note(item) == "Checking..."


# ---- 상태 값 ----


class TestStateValues:
    """진행 막대 값 · 상태 어휘 · 폴더 열기 대상."""

    @pytest.mark.parametrize(
        ("state", "progress", "expected"),
        [
            (ItemState.LOADING, 50, 0),
            (DownloadState.WAITING, 50, 0),
            (DownloadState.RUNNING, 150, 100),
            (DownloadState.PAUSED, "x", 0),
        ],
    )
    def test_progress_value_is_clamped_and_zero_before_the_start(
        self, state: DownloadState | ItemState, progress: object, expected: int
    ) -> None:
        """progress_value가 시작 전이면 0, 범위 밖이면 0~100으로 잘린 값이어야 한다.

        조회 중 50 · 대기 50 · 받는 중 150 · 일시정지 "x" -> 0 · 0 · 100 · 0
        """
        item = _item(state)
        item.download_progress = progress
        assert card_text.progress_value(item) == expected

    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            (ItemState.LOADING, "waiting"),
            (DownloadState.WAITING, "waiting"),
            (DownloadState.RUNNING, "running"),
            (DownloadState.PAUSED, "paused"),
            (DownloadState.FINISHED, "finished"),
            (DownloadState.FAILED, "failed"),
        ],
    )
    def test_card_state_maps_to_the_theme_words(
        self, state: DownloadState | ItemState, expected: str
    ) -> None:
        """card_state가 상태를 테마 상태 어휘로 옮겨야 한다(조회 중은 대기).

        LOADING · WAITING · RUNNING · PAUSED · FINISHED · FAILED
        -> waiting · waiting · running · paused · finished · failed
        """
        assert card_text.card_state(_item(state)) == expected

    def test_open_target_picks_the_folder_the_file_or_the_first_section_file(
        self, tmp_path
    ) -> None:
        """open_target이 대기면 저장 폴더, 전체면 산출물, 구간이면 남은 가장 작은 번호 파일이어야 한다.

        대기 -> C:/dl, 완료 전체 -> output_path, 완료 구간(1번 없음 · 2번 있음) -> 2번 파일
        """
        waiting = _item()
        assert card_text.open_target(waiting) == "C:/dl"
        whole = _item(DownloadState.FINISHED)
        whole.output_path = "C:/dl/제목 1080p.mp4"
        assert card_text.open_target(whole) == "C:/dl/제목 1080p.mp4"
        second = tmp_path / "제목 1080p_2.mp4"
        second.write_bytes(b"x")
        cut = _item(DownloadState.FINISHED, sections=((0, 60), (120, 180)))
        cut.section_paths = (str(tmp_path / "제목 1080p_1.mp4"), str(second))
        assert card_text.open_target(cut) == str(second)
