"""AES(SEA) 암호화 VOD 다운로드 엔진 — BaseDownloader 하위 구현 (#57, SPEC §6·§8.1).

치지직의 암호화 VOD는 HLS(MPEG-TS) 세그먼트에 AES-128-CBC가 걸려 있고, 키는
플레이리스트의 ``#EXT-X-KEY URI``가 가리키는 치지직 API가 **인증된 세션에**
평문 HTTP로 내준다. 즉 이 경로는 유저 본인의 쿠키로 유저 본인이 볼 권한이
있는 컨텐츠를 받는 것이며, 쿠키가 없으면 키 요청이 403으로 실패해 아무것도
받아지지 않는다(실측). 권한을 만들어내지 않는다.

m3u8(라이브 다시보기) 경로와의 차이:
- 컨테이너가 MPEG-TS(.ts)다 — fMP4가 아니므로 ``EXT-X-MAP`` 초기화 세그먼트가
  없고, 병합 대상은 세그먼트 파일 그 자체뿐이다
- 세그먼트마다 복호화 단계가 있다. IV는 ``#EXT-X-KEY``에 명시되면 그 값을,
  없으면 미디어 시퀀스 번호를 쓴다(RFC 8216 §5.2 — 치지직 실측은 후자)

키 취득은 쿠키가 필요해 core가 직접 할 수 없다(core→app 의존 금지).
``requires_key_resolution=True``로 선언하고 서비스가 주입한 리졸버를 쓴다 —
base_url 해석(#75)과 같은 주입 방식이다.

**키 값은 로그·예외 메시지·커밋에 절대 싣지 않는다.**
"""

import os
import shutil
import time as tm
from urllib.parse import urljoin

import requests

from core.api.hls import parse_media_playlist
from core.api.session import get_thread_session
from core.downloaders.base import BaseDownloader
from core.downloaders.decrypt import decrypt_segment, looks_like_ts, sequence_iv
from core.models.content import Content, ContentType
from core.models.download_state import DownloadState
from core.models.plan import DownloadPlan
from core.utils.paths import temp_dir_for


class DecryptionError(Exception):
    """복호화 결과가 유효한 미디어가 아닐 때 — 키·IV 규칙 불일치 신호."""


class HlsAesDownloader(BaseDownloader):
    """AES-128-CBC로 암호화된 HLS(TS) VOD를 세그먼트 단위로 받아 복호화·병합하는 엔진.

    호출 규약: 소유자(서비스·스크립트)가 data.base_url에 HLS 미디어 플레이리스트
    URL을 채우고 DownloadTaskModel.start()로 RUNNING 전이를 마친 뒤 run()을
    호출한다. run()은 완료·중단·실패까지 블로킹한다.
    """

    run_thread_name = "DownloadHlsAesThread"
    worker_pool_prefix = "DownloadHlsAesWorker"
    # base_url(HLS 미디어 플레이리스트 URL)은 SEA 매니페스트 파싱 시점에 해상도별로
    # 확정되어 Content에 실려 온다 — m3u8(라이브 다시보기)처럼 재해석하지 않는다
    requires_base_url_resolution = False
    requires_key_resolution = True
    # m3u8 경로와 동일하게 모든 예외를 실패로 처리한다 (플레이리스트·키 취득 실패 포함)
    _failure_exceptions = (Exception,)

    def __init__(self, data, logger, **callbacks):
        super().__init__(data, logger, **callbacks)
        # 세그먼트 저장용 임시 폴더 (실패 정리 경로가 참조하므로 실행 전에 확정).
        # 산출물 파일명에서 파생해 다운로드 간 폴더 공유·상호 삭제를 막는다 (#105)
        self.temp_dir = temp_dir_for(self.s.output_path)
        self._key: bytes | None = None
        self._playlist = None

    @classmethod
    def supports(cls, content: Content) -> bool:
        """AES(SEA) 암호화 VOD를 처리한다."""
        return content.content_type is ContentType.CHZZK_VIDEO_HLS_AES

    # ============ 작업 목록·수신 준비 ============

    def prepare(self, content: Content) -> DownloadPlan:
        """플레이리스트를 받아 세그먼트 목록을 만들고 복호화 키를 취득한다."""
        response = get_thread_session().get(self.s.base_url)
        response.raise_for_status()
        playlist = parse_media_playlist(response.text)

        if playlist.key is None:
            raise DecryptionError("암호화 정보(#EXT-X-KEY)가 없는 플레이리스트다")
        if not playlist.key.is_aes_128:
            # AES-128 외의 방식은 지원하지 않는다 — 우회를 시도하지 않고 실패시킨다
            raise DecryptionError(f"지원하지 않는 암호화 방식: {playlist.key.method}")

        key_uri = urljoin(self.s.base_url, playlist.key.uri)
        self._key = self._resolve_key(content, key_uri)
        self._playlist = playlist
        self.s.merged_segments = 0
        # 세그먼트 임시 파일명 0채움 자릿수 — sorted() 병합 순서의 전제
        self.width = len(str(len(playlist.segments)))

        # 워커 풀을 띄우기 전에 키·IV 규칙을 첫 세그먼트로 검증한다.
        # 여기서 실패하면 run()의 실패 경로로 깔끔히 빠져 수 GB짜리 쓰레기
        # 파일을 만들지 않는다 (풀 안에서 터지면 정리 경로가 복잡해진다)
        if playlist.segments:
            self._verify_key(playlist)

        # 전체 바이트 크기는 미리 알 수 없고(total_size=None), 병합 후처리가 필요하다
        return DownloadPlan(
            items=tuple(enumerate(playlist.segments)),
            total_size=None,
            requires_postprocess=True,
        )

    def _download_start_log_args(self) -> tuple:
        # 전체 크기·파트 크기를 미리 알 수 없다 (m3u8 경로와 동일하게 0)
        return (0, 0, self.s.total_ranges, self.s.adjust_threads)

    def _prepare_output(self) -> None:
        """임시 폴더를 재생성한다 (TS 경로에는 초기화 세그먼트가 없다)."""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
        os.makedirs(self.temp_dir)

    def _log_item_start(self, part_num: int, item) -> None:
        _index, segment = item
        self.logger.log_m3u8_thread_start(part_num, segment)

    def _download_item(self, item, part_num: int):
        index, segment = item
        return self._download_segment(
            index=index, segment=segment, part_num=part_num, total_ranges=self.s.total_ranges
        )

    def _cleanup_partial(self) -> None:
        """실패·중단 시 임시 폴더와 다운로드 파일 삭제."""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
        if os.path.exists(self.s.output_path):
            os.remove(self.s.output_path)

    def _cleanup_after_run(self) -> None:
        """임시 폴더 삭제 (병합 후에는 빈 폴더만 남는다)."""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def _progress_total_size(self) -> int | None:
        # 전체 바이트 크기를 미리 알 수 없다 — 세그먼트 수 기반 계산은 어댑터가 한다
        return None

    # ============ 키 취득 ============

    def _resolve_key(self, content: Content, key_uri: str) -> bytes:
        """주입된 리졸버로 복호화 키를 취득한다 (쿠키가 필요해 앱 계층이 수행).

        Raises:
            DecryptionError: 리졸버 미주입이거나 키 길이가 AES-128이 아닌 경우
        """
        if self._key_resolver is None:
            raise DecryptionError("암호화 VOD 다운로드에는 key_resolver 주입이 필요하다")
        key = self._key_resolver(content, key_uri)
        if len(key) != 16:
            # 값은 싣지 않고 길이만 보고한다
            raise DecryptionError(f"AES-128 키 길이가 올바르지 않다 (길이: {len(key)})")
        return key

    def _verify_key(self, playlist) -> None:
        """첫 세그먼트를 받아 복호화 결과가 MPEG-TS인지 확인한다 (prepare 단계).

        키가 틀리면 출력이 난수라 이 검사를 통과할 수 없다. 실행 전에 확인해
        잘못된 결과 파일을 만들지 않는 것이 목적이다.

        Raises:
            DecryptionError: 복호화 결과가 유효한 미디어가 아닌 경우
        """
        url = urljoin(self.s.base_url, playlist.segments[0])
        response = get_thread_session().get(url, timeout=30)
        response.raise_for_status()
        plain = decrypt_segment(response.content, self._key, self._segment_iv(0))
        if not looks_like_ts(plain):
            raise DecryptionError(
                "복호화 결과가 MPEG-TS가 아니다 — 키 또는 IV 규칙이 맞지 않는다"
            )

    def _segment_iv(self, index: int) -> bytes:
        """세그먼트의 IV — 명시 IV가 있으면 그 값, 없으면 미디어 시퀀스 번호."""
        explicit = self._playlist.key.iv
        return explicit if explicit is not None else sequence_iv(self._playlist.sequence_of(index))

    # ============ 후처리: 순서 보장 병합 ============

    def postprocess(self) -> None:
        """복호화 세그먼트들을 순서 그대로 ffmpeg stdin에 흘려 mp4로 재포장한다 (#88·#92).

        바이트 연결만으로는 MPEG-TS 스트림이 .mp4 이름으로 저장되는 컨테이너
        불일치에 더해, 라이브 원본 타임라인(시작 오프셋≠0)과 전역 인덱스
        부재가 그대로다 — m3u8(fMP4) 경로와 같은 제약이라 같은 처리를 한다.
        #92부터 중간 병합 파일 없이 단일 패스로 재포장하며, 실패 시 폴백
        없이 명확히 실패하고 세그먼트를 보존한다 (규칙은 base의
        _remux_streamed 참조).
        """
        self._on_merge_start()
        segment_files = sorted(os.listdir(self.temp_dir))
        self._remux_streamed([os.path.join(self.temp_dir, f) for f in segment_files])

    # ============ 다운로드 동작 ============

    def _download_segment(self, index: int, segment: str, part_num: int, total_ranges: int):
        """개별 세그먼트를 받아 복호화해 임시 파일로 저장한다 (재시도 포함).

        저속 재시도·일시정지·중단 판정 규칙은 m3u8 경로와 동일하다. 차이는
        세그먼트 전체를 메모리에 모은 뒤 CBC 복호화해서 쓴다는 점이다 —
        CBC는 앞 블록에 의존하므로 스트리밍 중 부분 기록을 할 수 없다.
        """
        slow_count = 0
        downloaded_size = 0
        segment_url = urljoin(self.s.base_url, segment)
        while not self.state == DownloadState.WAITING:
            try:
                # 스레드로컬 세션으로 같은 워커의 세그먼트 요청 간 연결을 재사용한다 (#31)
                response = get_thread_session().get(segment_url, stream=True, timeout=30)
                response.raise_for_status()
                part_start_time = tm.time()

                buffer = bytearray()
                for chunk in response.iter_content(chunk_size=8192):
                    if self.state == DownloadState.WAITING:
                        return part_num
                    if self.state == DownloadState.PAUSED:
                        self.s._pause_event.wait()

                    if chunk:
                        buffer.extend(chunk)
                        downloaded_size += len(chunk)
                        elapsed = tm.time() - part_start_time

                        if elapsed > 0:
                            speed_kb_s = downloaded_size / elapsed / 1024
                            self._check_speed_and_update_progress(
                                part_num, downloaded_size, total_ranges, speed_kb_s
                            )
                            if speed_kb_s < 100:
                                slow_count += 1
                                if slow_count > 5:
                                    # 속도가 너무 느리면 스레드 재시작
                                    with self.lock:
                                        self._requeue_slow((index, segment), part_num)
                                    return part_num
                            else:
                                slow_count = 0

                try:
                    plain = decrypt_segment(bytes(buffer), self._key, self._segment_iv(index))
                except (ValueError, DecryptionError) as e:
                    # 복호화 실패는 재시도해도 낫지 않는다(키·정렬 문제). 재큐잉하면
                    # 무한 루프가 되고, 그대로 전파하면 future_dict가 정리되지 않아
                    # 실행 루프가 끝나지 않는다 — 다운로드 전체를 중단시킨다
                    self._fail_fatally(e, "Segment decryption failed")
                    return part_num

                temp_file = os.path.join(self.temp_dir, f"{index:0{self.width}d}.ts")
                with open(temp_file, "wb") as f:
                    f.write(plain)

                # 성공적으로 마무리된 경우
                with self.lock:
                    self.s.completed_threads += 1
                    self.s.completed_progress += downloaded_size
                    self.s.threads_progress[part_num] = 0
                self.logger.log_thread_complete(part_num, downloaded_size)
                return part_num

            except (requests.RequestException, requests.Timeout) as e:
                with self.lock:
                    self._requeue_failed((index, segment), part_num, e)
                self.logger.log_error(f"Part {part_num} download failed", e)
                return part_num

