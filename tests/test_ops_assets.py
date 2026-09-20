"""运维资产即代码：告警规则与看板必须引用**真实存在**的指标。

一条引用不存在指标的告警规则，是"永远不会触发的假告警"——它看起来很负责，实际什么都不做。
这里拿真实的 `/metrics` 渲染结果逐条核对，把这类错误挡在提交之前。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from app_review_insights.config import Settings
from app_review_insights.monitor.health import HealthState

ROOT = Path(__file__).parents[1]
OPS = ROOT / "ops"

#: 只看我们自己的指标前缀。`up` 是 Prometheus 自带的，不在核对范围内。
_METRIC_TOKEN = re.compile(r"\b(ari_[a-z0-9_]+)\b")


def rendered_metric_names() -> set[str]:
    """真实渲染一次 /metrics（带样本数据），取出全部指标名。"""
    state = HealthState(
        duration_stats=lambda: {
            "stage": {"clean": {"count": 2, "p50": 10.0, "p95": 20.0, "max": 25.0}},
            "model": {"plan": {"count": 2, "p50": 30.0, "p95": 40.0, "max": 45.0}},
        },
        queue_stats=lambda: {"depth": 3, "oldest_wait_seconds": 12.5, "executor_seen": True},
    )
    state.record_job(success=True)
    state.record_job(success=False)
    state.set_ready(True)

    names: set[str] = set()
    for line in state.render_prometheus().splitlines():
        if not line or line.startswith("#"):
            continue
        names.add(line.split("{")[0].split(" ")[0].strip())
    return names


def referenced_metrics(expr: str) -> set[str]:
    return set(_METRIC_TOKEN.findall(expr))


def _alert_rules() -> list[dict]:
    document = yaml.safe_load((OPS / "alerts.yml").read_text(encoding="utf-8"))
    return [rule for group in document["groups"] for rule in group["rules"]]


def test_alert_rules_only_reference_metrics_we_actually_emit():
    known = rendered_metric_names()

    referenced = {metric for rule in _alert_rules() for metric in referenced_metrics(rule["expr"])}
    # 刻意**不留任何豁免名单**：留一个口子，错误指标就有地方藏。
    unknown = sorted(referenced - known)

    assert not unknown, (
        f"告警规则引用了不存在的指标：{unknown}。"
        f"实际存在的：{sorted(known)}——引用不存在的指标等于一条永不触发的假告警。"
    )


def test_every_alert_rule_is_actionable():
    """每条规则都要有严重级别与一句人能看懂的说明——否则半夜收到告警也不知道该干什么。"""
    for rule in _alert_rules():
        assert rule.get("alert"), rule
        assert rule.get("labels", {}).get("severity") in {"critical", "warning"}, rule["alert"]
        annotations = rule.get("annotations", {})
        assert annotations.get("summary"), f"{rule['alert']} 缺少 summary"
        assert annotations.get("description"), f"{rule['alert']} 缺少 description"


def test_prometheus_scrapes_only_the_long_running_process():
    """抓取目标只能指向**常驻**进程，端口还要与服务实际监听的端口一致。

    踩过的坑（D-14）：一开始 web 与 worker 都抓。但 Streamlit 的脚本按会话执行——没人打开页面
    时 web 进程里没有 metrics 端点，这个目标永远是 down，AriProcessDown 随之常态误报。
    流水线耗时写在共享 SQLite 里，worker 读同一份数据，所以不抓 web 也不丢信息。
    """
    config = yaml.safe_load((OPS / "prometheus.yml").read_text(encoding="utf-8"))
    targets = {
        entry["targets"][0] for job in config["scrape_configs"] for entry in job["static_configs"]
    }
    settings = Settings(_env_file=None)

    assert targets == {f"worker:{settings.worker_health_port}"}
    assert config["rule_files"], "没有挂载告警规则文件，规则不会生效"


def test_dashboard_panels_only_reference_metrics_we_actually_emit():
    dashboard = json.loads(
        (OPS / "grafana" / "dashboards" / "ari-overview.json").read_text(encoding="utf-8")
    )
    known = rendered_metric_names()

    referenced = {
        metric
        for panel in dashboard["panels"]
        for target in panel["targets"]
        for metric in referenced_metrics(target["expr"])
    }

    assert referenced, "看板里一个我们的指标都没有？"
    assert not referenced - known, f"看板引用了不存在的指标：{sorted(referenced - known)}"


def test_every_dashboard_panel_has_a_target_and_a_position():
    dashboard = json.loads(
        (OPS / "grafana" / "dashboards" / "ari-overview.json").read_text(encoding="utf-8")
    )

    for panel in dashboard["panels"]:
        assert panel.get("title"), panel
        assert panel.get("targets"), f"{panel['title']} 没有查询"
        assert set(panel.get("gridPos", {})) >= {"h", "w", "x", "y"}, panel["title"]


def test_container_healthchecks_embed_valid_python():
    """容器健康检查里的 python 源码必须真的能编译。

    踩过的坑（D-12）：用 YAML 折叠标量（`>`）写多行 `python -c "..."`，折叠后会在行首留一个空格，
    python 抛 `IndentationError`，容器永远 unhealthy，worker 又因 `depends_on: service_healthy`
    永远起不来——整套 `docker compose up` 实际是坏的，而肉眼阅读 YAML 时完全看不出来。
    """
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))

    checked = 0
    for name, service in compose["services"].items():
        probe = service.get("healthcheck", {}).get("test")
        if not probe:
            continue
        command = " ".join(probe) if isinstance(probe, list) else probe

        match = re.search(r'python -c "(?P<src>.*)"', command)
        assert match, f"{name} 的健康检查不是可识别的 python -c 调用：{command!r}"
        # 语法错误（例如折叠标量留下的行首空格）在这里就会暴露，不必等容器起来才发现。
        compile(match.group("src"), f"<{name}-healthcheck>", "exec")

        # CMD-SHELL 里引用容器环境变量必须写成 $$VAR：单个 $VAR 会被 compose 在配置阶段替换掉，
        # 于是"改容器环境变量即可换端口"这个意图静默失效。
        assert not re.search(r"(?<!\$)\$(?!\$)", command), (
            f"{name} 的健康检查里的 $ 会被 compose 提前替换，容器内环境变量将不再生效：{command!r}"
        )
        checked += 1

    assert checked >= 2, "web / worker 都应有健康检查"


def test_exactly_one_service_schedules():
    """调度者必须唯一，而且必须写在 compose 里而不是靠 .env。

    踩过的坑（D-13）：worker 只写 `env_file: .env`，而 `.env` 是不入库的本机配置（默认
    `SCHEDULER_ENABLED=false`）——新克隆执行 `docker compose up` 必然得到 worker 无限重启；
    反过来若本机 .env 开了 true，web 与 worker 会**同时**调度，定时报告发两遍。
    """
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]

    schedulers = {
        name
        for name, service in compose.items()
        if str(service.get("environment", {}).get("SCHEDULER_ENABLED", "")).lower() == "true"
    }

    assert schedulers == {"worker"}, f"调度者必须恰好是 worker，实际：{sorted(schedulers) or '无'}"
    assert compose["web"]["environment"]["SCHEDULER_ENABLED"] == "false", "web 不得调度"


def test_compose_exposes_observability_as_an_opt_in_profile():
    """默认不启动：没有 profile 的话，只想跑 web+worker 的人会平白多两个容器。"""
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))

    for service in ("prometheus", "grafana"):
        assert compose["services"][service]["profiles"] == ["observability"], service
    assert "observability" in compose["services"]["grafana"]["depends_on"] or (
        compose["services"]["grafana"]["depends_on"] == ["prometheus"]
    )
