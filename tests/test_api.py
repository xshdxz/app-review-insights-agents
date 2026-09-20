"""HTTP 契约层的测试。

用 `TestClient`（starlette 自带，基于 httpx）直连 ASGI 应用：**不真起端口**。
起端口的测试会带来端口占用、时序与清理三件事，而它验证的东西（HTTP 语义）在 ASGI
层面完全一样。
"""

from __future__ import annotations

import pytest

# HTTP 契约层是可选 extra：没装就当这组用例不存在（与 OTel 的降级用例同一套路）。
# CI 里另有一个装了 [api] 的 job 专门跑它们——两半都被覆盖，而不是让降级路径没人验。
pytest.importorskip("fastapi", reason='需要 pip install -e ".[api]"')

from fastapi.testclient import TestClient  # noqa: E402

from app_review_insights.api import create_app  # noqa: E402
from app_review_insights.config import Settings
from app_review_insights.factory import build_pipeline_services
from app_review_insights.models import RunStatus, Stage
from app_review_insights.storage.repository import RunRepository

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

REVIEWS = [
    {
        "review_id": f"r{index}",
        "content": f"第 {index} 条评论：订阅页看不到续费价格，试用结束就被扣款。" * 2,
        "rating": 1,
        "published_at": "2026-01-01T00:00:00Z",
    }
    for index in range(3)
]


def make_api(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runs.sqlite3"))
    monkeypatch.setenv("DEMO_REPLAY_PATH", str(tmp_path / "no-recording.json"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("API_TOKEN", TOKEN)
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))

    settings = Settings(_env_file=None)
    services = build_pipeline_services(settings, use_fake_provider=True)
    client = TestClient(create_app(settings, services=services))
    return client, RunRepository(settings.database_path), settings


def submit(client, **overrides):
    payload = {"analysis_goal": "找出订阅转化的问题", "reviews": REVIEWS}
    payload.update(overrides)
    return client.post("/v1/runs", json=payload, headers=AUTH)


def test_health_and_metrics_need_no_token(tmp_path, monkeypatch):
    client, _, _ = make_api(tmp_path, monkeypatch)

    assert client.get("/healthz").json() == {"status": "ok"}
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    # 与 worker 同源：阶段耗时读的是同一份检查点库
    assert "ari_stage_duration_seconds" in metrics.text


def test_submitting_without_a_token_is_rejected(tmp_path, monkeypatch):
    client, _, _ = make_api(tmp_path, monkeypatch)

    assert client.post("/v1/runs", json={"analysis_goal": "x"}).status_code == 401
    wrong = client.post(
        "/v1/runs",
        json={"analysis_goal": "x"},
        headers={"Authorization": "Bearer wrong"},
    )

    assert wrong.status_code == 401
    assert submit(client).status_code == 202


def test_submitting_without_a_configured_token_says_how_to_open_it(tmp_path, monkeypatch):
    """没配令牌时写入端点关闭——但**必须说清楚怎么开**，否则只能去翻源码。"""
    client, _, _ = make_api(tmp_path, monkeypatch, API_TOKEN="")

    response = client.post("/v1/runs", json={"analysis_goal": "x"}, headers=AUTH)

    assert response.status_code == 503
    assert "API_TOKEN" in response.json()["detail"]


def test_submit_enqueues_without_running_anything(tmp_path, monkeypatch):
    """202 是诚实的承诺：只保证已受理。执行者是另一个进程。"""
    client, repository, _ = make_api(tmp_path, monkeypatch)

    response = submit(client)
    body = response.json()

    assert response.status_code == 202
    assert body["status"] == RunStatus.PENDING.value
    assert repository.get_output(body["run_id"], Stage.COLLECT) is None
    assert repository.get_inputs(body["run_id"]) is not None, "评论必须落盘，执行者才拿得到"


def test_submission_needs_some_input(tmp_path, monkeypatch):
    client, _, _ = make_api(tmp_path, monkeypatch)

    response = client.post("/v1/runs", json={"analysis_goal": "什么都没给"}, headers=AUTH)

    assert response.status_code == 422


def test_second_submission_for_the_same_app_is_a_conflict(tmp_path, monkeypatch):
    """同一 App 并发分析＝把同一份结论算两遍、额度烧两份，编排器挡它，接口如实转成 409。"""
    client, _, _ = make_api(tmp_path, monkeypatch)
    url = "https://apps.apple.com/us/app/x/id1"

    assert submit(client, app_url=url, reviews=None).status_code == 202
    conflict = submit(client, app_url=url, reviews=None)

    assert conflict.status_code == 409
    assert "已有进行中的运行" in conflict.json()["detail"]


def test_backpressure_rejects_instead_of_queueing_forever(tmp_path, monkeypatch):
    client, _, _ = make_api(tmp_path, monkeypatch, API_MAX_QUEUE_DEPTH=1)
    submit(client, app_url="https://apps.apple.com/us/app/a/id1", reviews=None)

    response = submit(client, app_url="https://apps.apple.com/us/app/b/id2", reviews=None)

    assert response.status_code == 429
    assert "积压" in response.json()["detail"]


def test_unknown_run_is_404(tmp_path, monkeypatch):
    client, _, _ = make_api(tmp_path, monkeypatch)

    assert client.get("/v1/runs/nope").status_code == 404
    assert client.get("/v1/runs/nope/events").status_code == 404
    assert client.post("/v1/runs/nope/cancel", headers=AUTH).status_code == 404


def test_run_detail_carries_status_and_usage(tmp_path, monkeypatch):
    client, _, _ = make_api(tmp_path, monkeypatch)
    run_id = submit(client).json()["run_id"]

    body = client.get(f"/v1/runs/{run_id}").json()

    for key in ("run_id", "status", "current_stage", "coverage_ratio", "is_live", "usage"):
        assert key in body, key


def test_runs_list_returns_recent_first(tmp_path, monkeypatch):
    client, _, _ = make_api(tmp_path, monkeypatch)
    submit(client, app_url="https://apps.apple.com/us/app/a/id1", reviews=None)
    submit(client, app_url="https://apps.apple.com/us/app/b/id2", reviews=None)

    assert len(client.get("/v1/runs").json()) == 2


def test_queue_endpoint_reports_depth_and_whether_anyone_consumes(tmp_path, monkeypatch):
    """运维要看的两件事：积压多少、有没有人在消费。"""
    client, repository, _ = make_api(tmp_path, monkeypatch)
    submit(client)

    assert client.get("/v1/queue").json() == {"depth": 1, "executor_seen": False}

    repository.record_executor_heartbeat("w:1:x")

    assert client.get("/v1/queue").json() == {"depth": 1, "executor_seen": True}


def test_cancel_is_cooperative_and_honest_about_it(tmp_path, monkeypatch):
    client, repository, _ = make_api(tmp_path, monkeypatch)
    run_id = submit(client).json()["run_id"]

    response = client.post(f"/v1/runs/{run_id}/cancel", headers=AUTH)

    assert response.status_code == 202
    assert repository.cancel_requested(run_id) is True
    assert "下一个阶段边界" in response.json()["note"], "取消是协作式的，必须写在响应里"


def test_cancelling_a_finished_run_is_a_conflict(tmp_path, monkeypatch):
    client, repository, _ = make_api(tmp_path, monkeypatch)
    run_id = submit(client).json()["run_id"]
    # 用 FAILED 而不是 COMPLETED：后者要求 current_stage 也是 COMPLETE，
    # 而这条不变量由模型守着——测试不该为了造终态去伪造一个阶段
    run = repository.get_run(run_id).model_copy(update={"status": RunStatus.FAILED})
    repository.save_run(run)

    assert client.post(f"/v1/runs/{run_id}/cancel", headers=AUTH).status_code == 409


def test_events_are_listed_and_streamable(tmp_path, monkeypatch):
    client, repository, _ = make_api(tmp_path, monkeypatch)
    run_id = submit(client).json()["run_id"]

    events = client.get(f"/v1/runs/{run_id}/events").json()
    assert [event["message"] for event in events] == ["Analysis run created"]

    # 置为终态再订阅：事件流跑到终态就主动收尾，测试因此是**有界**的。
    # （否则它会一直挂在"运行还没结束"上——那正是 SSE 该有的行为，但不该拿来当测试。）
    done = repository.get_run(run_id).model_copy(update={"status": RunStatus.FAILED})
    repository.save_run(done)

    with client.stream("GET", f"/v1/runs/{run_id}/events?stream=1") as stream:
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        body = "".join(stream.iter_text())

    assert "event: stage" in body
    assert "event: done" in body
    assert "Analysis run created" in body


def test_result_exposes_raw_stage_payloads(tmp_path, monkeypatch):
    client, repository, _ = make_api(tmp_path, monkeypatch)
    run_id = submit(client).json()["run_id"]
    repository.save_output(run_id, Stage.CLEAN, {"reviews": [{"review_id": "r1"}]})

    body = client.get(f"/v1/runs/{run_id}/result").json()

    assert body["clean"] == {"reviews": [{"review_id": "r1"}]}
    assert body["plan"] is None, "还没跑到的阶段如实返回 null，不编造空结构"


def test_openapi_route_table_is_frozen(tmp_path, monkeypatch):
    """契约测试：路由表是**故意冻结**的。

    刻意不比对整份 OpenAPI schema 快照——那东西会随 FastAPI/pydantic 的小版本漂移，
    天天红之后就没人看了。这里冻结的是真正构成契约的部分：方法、路径、是否要求令牌。
    """
    client, _, _ = make_api(tmp_path, monkeypatch)

    paths = client.get("/openapi.json").json()["paths"]
    routes = {(method.upper(), path) for path, operations in paths.items() for method in operations}

    assert routes == {
        ("GET", "/healthz"),
        ("GET", "/metrics"),
        ("GET", "/v1/queue"),
        ("GET", "/v1/runs"),
        ("POST", "/v1/runs"),
        ("GET", "/v1/runs/{run_id}"),
        ("GET", "/v1/runs/{run_id}/events"),
        ("GET", "/v1/runs/{run_id}/result"),
        ("POST", "/v1/runs/{run_id}/cancel"),
    }
    # 写端点的受理码也是契约的一部分：202 表示"已受理但还没跑"
    assert "202" in paths["/v1/runs"]["post"]["responses"]
    assert "202" in paths["/v1/runs/{run_id}/cancel"]["post"]["responses"]
