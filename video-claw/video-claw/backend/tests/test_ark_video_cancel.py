"""Ark cancel follows the documented DELETE. No test opens a socket."""

import pytest

from models.middle.adapter import (
    CANCEL_CANCELLED,
    CANCEL_COMPLETED,
    CANCEL_REJECTED,
    CANCEL_UNCERTAIN,
    CANCEL_UNSUPPORTED,
)
from models.middle.providers import ArkVideoAdapter, DashScopeVideoAdapter
from models.video_seedance import SeedanceVideoClient


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _Transport:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self._next()

    def delete(self, url, **kwargs):
        self.calls.append(("DELETE", url, kwargs))
        return self._next()

    def _next(self):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _client():
    return SeedanceVideoClient(api_key="unit-test-key", base_url="https://ark.cn-beijing.volces.com/api/v3")


def _task(status, **extra):
    body = {"id": "task-1", "status": status}
    body.update(extra)
    return _Resp(200, body)


@pytest.fixture(autouse=True)
def _no_live_http(monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("live http")

    monkeypatch.setattr("models.video_seedance.requests.get", explode)
    monkeypatch.setattr("models.video_seedance.requests.delete", explode)
    monkeypatch.setattr("models.video_seedance.requests.post", explode)


def _url():
    return "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks/task-1"


def test_queued_delete_then_cancelled_is_success():
    transport = _Transport([
        _task("queued"),
        _Resp(200, {}),
        _task("cancelled"),
    ])
    result = _client().cancel_task("task-1", transport=transport)
    assert result["outcome"] == CANCEL_CANCELLED
    assert [call[0] for call in transport.calls] == ["GET", "DELETE", "GET"]
    assert transport.calls[1][1] == _url()
    assert transport.calls[0][2]["headers"]["Authorization"] == "Bearer unit-test-key"
    assert "cancelled" in result["message"]


def test_running_is_unsupported_and_does_not_delete():
    transport = _Transport([_task("running")])
    result = _client().cancel_task("task-1", transport=transport)
    assert result["outcome"] == CANCEL_UNSUPPORTED
    assert [call[0] for call in transport.calls] == ["GET"]
    assert "running" in result["message"]
    assert "报价保持保留" in result["message"]


def test_delete_non_200_is_rejected_without_a_documented_code():
    transport = _Transport([_task("queued"), _Resp(400, {"error": {"code": "Unknown"}})])
    result = _client().cancel_task("task-1", transport=transport)
    assert result["outcome"] == CANCEL_REJECTED
    assert result["http_status"] == 400
    assert [call[0] for call in transport.calls] == ["GET", "DELETE"]
    assert "HTTP 400" in result["message"]
    assert "文档未说明" in result["message"]
    assert "报价保持保留" in result["message"]


def test_timeout_is_uncertain_and_does_not_delete():
    transport = _Transport([TimeoutError("slow")])
    result = _client().cancel_task("task-1", transport=transport)
    assert result["outcome"] == CANCEL_UNCERTAIN
    assert [call[0] for call in transport.calls] == ["GET"]
    assert "超时" in result["message"]


def test_delete_timeout_after_queue_is_uncertain():
    transport = _Transport([_task("queued"), TimeoutError("slow delete")])
    result = _client().cancel_task("task-1", transport=transport)
    assert result["outcome"] == CANCEL_UNCERTAIN
    assert [call[0] for call in transport.calls] == ["GET", "DELETE"]
    assert "超时" in result["message"]


def test_succeeded_is_completion_and_does_not_delete():
    transport = _Transport([
        _task("succeeded", content={"video_url": "https://cdn.example/done.mp4"}),
    ])
    result = _client().cancel_task("task-1", transport=transport)
    assert result["outcome"] == CANCEL_COMPLETED
    assert result["video_url"] == "https://cdn.example/done.mp4"
    assert [call[0] for call in transport.calls] == ["GET"]
    assert "没有调用删除" in result["message"]


def test_delete_200_then_succeeded_is_a_completion_race():
    transport = _Transport([
        _task("queued"),
        _Resp(200, {}),
        _task("succeeded", content={"video_url": "https://cdn.example/race.mp4"}),
    ])
    result = _client().cancel_task("task-1", transport=transport)
    assert result["outcome"] == CANCEL_COMPLETED
    assert result["video_url"] == "https://cdn.example/race.mp4"
    assert result["outcome"] != CANCEL_CANCELLED


def test_delete_200_then_missing_record_is_uncertain():
    transport = _Transport([
        _task("queued"),
        _Resp(200, {}),
        _Resp(404, {"error": {"message": "not found"}}),
    ])
    result = _client().cancel_task("task-1", transport=transport)
    assert result["outcome"] == CANCEL_UNCERTAIN
    assert "结果不确定" in result["message"]
    assert "报价保持保留" in result["message"]


def test_already_cancelled_does_not_delete_the_record():
    transport = _Transport([_task("cancelled")])
    result = _client().cancel_task("task-1", transport=transport)
    assert result["outcome"] == CANCEL_CANCELLED
    assert [call[0] for call in transport.calls] == ["GET"]


def test_failed_and_unknown_status_do_not_delete():
    failed = _Transport([_task("failed")])
    result = _client().cancel_task("task-1", transport=failed)
    assert result["outcome"] == CANCEL_REJECTED
    assert [call[0] for call in failed.calls] == ["GET"]
    unknown = _Transport([_task("mystery")])
    result = _client().cancel_task("task-1", transport=unknown)
    assert result["outcome"] == CANCEL_UNCERTAIN
    assert [call[0] for call in unknown.calls] == ["GET"]


def test_adapter_cancel_uses_the_client_and_other_vendors_do_not():
    transport = _Transport([_task("queued"), _Resp(200, {}), _task("cancelled")])
    result = ArkVideoAdapter().cancel("task-1", transport=transport)
    assert result["outcome"] == CANCEL_CANCELLED
    assert [call[0] for call in transport.calls] == ["GET", "DELETE", "GET"]
    other = DashScopeVideoAdapter().cancel("task-1")
    assert other["outcome"] == CANCEL_UNSUPPORTED
    assert "没有已核实的取消接口" in other["message"]


def test_missing_key_does_not_call_http():
    result = SeedanceVideoClient(api_key="", base_url="https://ark.cn-beijing.volces.com/api/v3").cancel_task("task-1")
    assert result["outcome"] == CANCEL_UNCERTAIN
    assert "未配置视频厂商密钥" in result["message"]
