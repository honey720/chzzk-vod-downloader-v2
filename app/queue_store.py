"""비밀 주소를 제외한 미완료 VOD 목록을 저장하고 대기로 복원한다."""

import json
import re
import os
import math
import uuid
from dataclasses import asdict
from pathlib import Path
from fractions import Fraction
from core.models.plan import TimeRange
from core.utils.selections import MAX_SELECTIONS
from urllib.parse import urlsplit, urlunsplit

from app.viewmodels.data import ContentItem
from core.api.representations import StreamEntry
from core.models.content import StreamKey
from core.utils.persistence import FileLease, atomic_json


def encode_item(item: ContentItem) -> dict:
    """만료되는 재생 주소와 쿠키 없이 카드의 선택 정보만 직렬화한다."""
    url = urlsplit(item.vod_url)
    return {
        "id": item.queue_id,
        "url": urlunsplit((url.scheme, url.netloc, url.path, "", "")),
        "title": item.title,
        "default_title": item.default_title,
        "channel": item.channel_name,
        "duration": item.duration,
        "created": item.live_open_date,
        "type": item.content_type,
        "resolution": item.resolution,
        "resolution_tag": item.resolution_tag,
        "stream": asdict(item.stream) if item.stream else None,
        "download_path": item.download_path,
        "output_path": item.output_path,
        "selections": [asdict(part) for part in item.selections],
        "section_frame_rate": str(item.section_frame_rate) if item.section_frame_rate else None,
        "state": item.downloadState.name,
        "resume_available": item.resume_available,
        "merge_only": item.merge_only,
        "message": re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1", item.stateMessage),
    }


def decode_item(record: dict) -> ContentItem:
    """저장된 항목을 실행 없이 대기 상태로 복원한다."""
    if not isinstance(record, dict):
        raise ValueError("잘못된 대기 항목")
    for name in ("id", "url", "title", "download_path"):
        if not isinstance(record.get(name), str):
            raise ValueError("대기 항목의 필수 정보가 없습니다")
    url = urlsplit(record["url"])
    if url.scheme != "https" or url.hostname not in {"chzzk.naver.com", "www.chzzk.naver.com"}:
        raise ValueError("지원하지 않는 영상 주소")
    resolution = record.get("resolution")
    if resolution is not None and (
        isinstance(resolution, bool) or not str(resolution).isdigit() or int(resolution) <= 0
    ):
        raise ValueError("잘못된 화질")
    for name in (
        "channel",
        "created",
        "type",
        "output_path",
        "resolution_tag",
        "default_title",
        "message",
    ):
        if name in record and not isinstance(record[name], str):
            raise ValueError("잘못된 표시 정보")
    if (
        not isinstance(record.get("duration", 0), (int, float))
        or not math.isfinite(record.get("duration", 0))
        or record.get("duration", 0) < 0
    ):
        raise ValueError("잘못된 영상 길이")
    stream = StreamKey(**record["stream"]) if record.get("stream") else None
    reps = [StreamEntry(resolution, "", stream=stream)] if resolution else []
    item = ContentItem(
        record["url"],
        {
            "title": record["title"],
            "channelName": record.get("channel", ""),
            "createdDate": record.get("created", ""),
            "duration": record.get("duration", 0),
        },
        reps,
        resolution,
        "",
        record["download_path"],
        record.get("type", ""),
        None,
    )
    parts = record.get("selections", [])
    if not isinstance(parts, list) or len(parts) > MAX_SELECTIONS:
        raise ValueError("잘못된 구간 목록")
    item.selections = tuple(TimeRange(**part) for part in parts)
    if any(
        not math.isfinite(v) or v < 0 for part in item.selections for v in (part.start, part.end)
    ):
        raise ValueError("잘못된 구간 시각")
    rate = record.get("section_frame_rate")
    if rate is not None:
        item.section_frame_rate = Fraction(rate)
        if item.section_frame_rate <= 0:
            raise ValueError("잘못된 프레임률")
    if item.selections:
        # 종료 뒤 조회용 색인은 재사용하지 않는다. 구간 시각을 보존하고 편집/조회로 재확인한다.
        item.section_check = "unverified"
    item.queue_id = record["id"]
    item.default_title = record.get("default_title", item.title)
    item.resolution_tag = record.get("resolution_tag", "")
    item.output_path = record.get("output_path", "")
    item.stream = stream
    item.keep_selection = True
    item.needs_refresh = True
    item.resume_available = bool(record.get("resume_available"))
    item.merge_only = bool(record.get("merge_only"))
    item.last_state = record.get("state", "WAITING")
    item.stateMessage = record.get("message", "")
    if item.content_type == "m3u8" and item.output_path and not item.selections:
        from app.resume_actions import restore_output_path

        try:
            restore_output_path(item)
        except Exception:
            item.stateMessage = "보존된 데이터를 확인할 수 없습니다. 저장소를 확인해 주세요."
    if not os.path.isdir(item.download_path):
        item.stateMessage = "저장소 연결 필요"
    return item


class QueueStore:
    """앱 데이터 폴더의 목록 파일과 마지막 정상 사본을 관리한다."""

    def __init__(self, directory: Path):
        self.path = directory / "queue.json"
        self.backup = directory / "queue.json.backup"
        self.lease = FileLease(directory / "queue.lock")
        self.last_good: dict | None = None
        self.warning = ""

    def load(self) -> list[ContentItem]:
        """손상된 원본을 보존하고 가능한 정상 항목만 복원한다."""
        self.lease.acquire()
        data = None
        for path in (self.path, self.backup):
            if not path.exists():
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(value, dict) or value.get("version") != 1:
                    raise ValueError
                if not isinstance(value.get("items"), list):
                    raise ValueError
                data = value
                break
            except (ValueError, OSError):
                self.warning = "대기열 기록 일부를 복원하지 못했습니다. 원본은 보존됩니다."
                if path == self.path:
                    # 복구 실패 뒤 빈 목록으로 유일한 원본을 덮어쓰지 않는다.
                    path.rename(path.with_name("queue.json.broken-" + uuid.uuid4().hex))
        self.last_good = data
        result = []
        seen = set()
        for record in (data or {}).get("items", []):
            try:
                if record.get("state") == "FINISHED":
                    continue
                item = decode_item(record)
                if item.queue_id in seen:
                    raise ValueError
                seen.add(item.queue_id)
                result.append(item)
            except (AttributeError, TypeError, ValueError, ZeroDivisionError):
                self.warning = "일부 대기 항목을 복원하지 못했습니다. 정상 항목은 복원했습니다."
        return result

    def save(self, records: list[dict]) -> None:
        """직렬화가 끝난 미완료 목록을 원자적으로 저장한다."""
        if self.lease._file is None:
            raise OSError("대기열 저장 소유권이 없습니다")
        value = {"version": 1, "items": records}
        if value == self.last_good:
            return
        if self.last_good is not None:
            atomic_json(self.backup, self.last_good)
        atomic_json(self.path, value)
        self.last_good = value

    def close(self) -> None:
        """앱이 종료될 때 목록 쓰기 소유권을 반환한다."""
        self.lease.release()
