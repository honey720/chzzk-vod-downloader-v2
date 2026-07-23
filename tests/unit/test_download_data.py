"""download/data.py의 core 모델 위임 검증 (#61).

DownloadData가 컨텐츠 필드는 Content로, 상태·진행률 필드는 DownloadTaskModel로
위임하면서 기존 속성 이름(엔진 인터페이스)을 그대로 유지하는지 확인한다.
"""

import pytest

from download.data import DownloadData


def _make_data() -> DownloadData:
    return DownloadData(
        "https://cdn.example/base", "https://chzzk.naver.com/video/1", "out.mp4", 1080, "video"
    )


def test_content_fields_are_delegated_to_content_model():
    """컨텐츠 필드는 Content가 소유하고, 기존 속성 이름으로 읽고 쓸 수 있어야 한다."""
    data = _make_data()

    assert data.vod_url == "https://chzzk.naver.com/video/1"
    assert data.base_url == "https://cdn.example/base"
    assert data.output_path == "out.mp4"
    assert data.resolution == 1080
    # 엔진은 문자열 비교를 하므로 content_type은 기존 문자열 값을 돌려줘야 한다
    assert data.content_type == "video"

    # 엔진이 하는 쓰기(예: m3u8 스레드의 base_url 해석)가 Content에 반영돼야 한다
    data.base_url = "https://cdn.example/1080"
    assert data.content.base_url == "https://cdn.example/1080"


def test_state_progress_fields_are_delegated_to_task_model():
    """상태·진행률 필드는 DownloadTaskModel이 단일 소유처여야 한다."""
    data = _make_data()

    data.total_size = 1000
    assert data.model.total_size == 1000

    # 엔진의 배열 초기화·개별 갱신 패턴이 모델에 그대로 반영돼야 한다
    data.threads_progress = [0] * 4
    data.threads_progress[1] = 250
    assert data.model.threads_progress == [0, 250, 0, 0]
    assert data.model.downloaded_size == 250

    data.start_time = 1.0
    data.end_time = 3.5
    assert data.model.start_time == 1.0
    assert data.model.end_time == 3.5


def test_pause_event_is_the_models_event():
    """_pause_event는 모델의 pause_event와 같은 객체여야 한다 (일시정지 연동)."""
    data = _make_data()
    assert data._pause_event is data.model.pause_event
    assert data._pause_event.is_set()  # 초기 상태는 진행 가능


def test_unknown_content_type_raises():
    """정의되지 않은 content_type 문자열은 생성 시점에 ValueError로 실패해야 한다."""
    with pytest.raises(ValueError):
        DownloadData("b", "u", "o", 720, "unknown-type")
