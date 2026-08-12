from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import os


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "data" / "evaluation"


ARCHETYPES = {
    "latency": {
        "fault_kind": "connection_pool_exhaustion",
        "title": "checkout-api 长尾延迟持续升高",
        "summary": "请求量稳定，但结算接口长尾延迟和错误率在同一窗口持续升高。",
        "symptoms": ["P95 延迟超过三秒", "连接池等待者增加", "一个实例健康抖动"],
        "root_cause_term_groups": [["连接池", "pool"], ["实例", "instance"]],
        "must_retrieve": ["RB-101"],
        "support_fields": ["pool_waiters", "instances"],
    },
    "timeout": {
        "fault_kind": "connection_pool_exhaustion",
        "title": "checkout-api 请求集中超时",
        "summary": "上游流量未突增，多个结算请求等待连接后超时并返回服务错误。",
        "symptoms": ["pool timeout 日志", "错误率上升", "单实例 degraded"],
        "root_cause_term_groups": [["连接池", "pool"], ["实例", "instance"]],
        "must_retrieve": ["RB-101"],
        "support_fields": ["pool_waiters", "instances"],
    },
    "dependency_failure": {
        "fault_kind": "dependency_rate_limit",
        "title": "recommendation-api 外部依赖拒绝请求",
        "summary": "本地资源健康，外部依赖连续返回限流响应且重试正在放大请求量。",
        "symptoms": ["依赖返回 429", "重试放大", "本地实例健康"],
        "root_cause_term_groups": [["限流", "429", "rate limit"], ["重试", "retry"]],
        "must_retrieve": ["RB-429", "OPS-12", "SEC-07"],
        "support_fields": ["http_429_rate", "retry_amplification"],
    },
    "authentication_failure": {
        "fault_kind": "expired_credential",
        "title": "partner-gateway 授权失败突增",
        "summary": "网络和 TLS 正常，但合作方调用在所有实例上集中返回未授权。",
        "symptoms": ["HTTP 401 超过三成", "凭据版本过期", "网络错误为零"],
        "root_cause_term_groups": [["凭据", "credential"], ["过期", "expired"]],
        "must_retrieve": ["RB-203"],
        "support_fields": ["http_401_rate", "credential_version"],
    },
    "connection_pool_exhaustion": {
        "fault_kind": "connection_pool_exhaustion",
        "title": "checkout-api 连接池等待耗尽",
        "summary": "连接释放钩子缺失，等待线程累积且只有一个服务实例处于降级。",
        "symptoms": ["pool_waiters 持续增加", "连接释放未观测到", "单实例降级"],
        "root_cause_term_groups": [["连接池", "pool"], ["实例", "instance"]],
        "must_retrieve": ["RB-101"],
        "support_fields": ["pool_waiters", "instances"],
    },
    "queue_backlog": {
        "fault_kind": "queue_backlog",
        "title": "invoice-worker 消费积压扩大",
        "summary": "消息生产速度持续超过健康消费者的处理能力，最老任务等待时间增加。",
        "symptoms": ["队列超过八千", "消费速率低于生产速率", "消费者心跳正常"],
        "root_cause_term_groups": [["生产", "入队", "produce"], ["消费", "容量", "吞吐", "副本不足"]],
        "must_retrieve": ["RB-104"],
        "support_fields": ["queue_depth", "oldest_age_s"],
    },
    "stale_cache": {
        "fault_kind": "stale_cache",
        "title": "catalog-api 返回陈旧商品版本",
        "summary": "接口性能正常，但一个租户的缓存版本持续落后于数据库版本。",
        "symptoms": ["旧版本样本增加", "数据库已更新", "仅缓存读路径异常"],
        "root_cause_term_groups": [["缓存", "cache"], ["版本", "version", "漂移"]],
        "must_retrieve": ["RB-310"],
        "support_fields": ["stale_sample_rate", "cache_version", "db_version"],
    },
    "partial_outage": {
        "fault_kind": "connection_pool_exhaustion",
        "title": "checkout-api 单实例部分故障",
        "summary": "总体服务仍可用，但一个实例连接池异常造成部分请求超时和错误。",
        "symptoms": ["仅一台实例 degraded", "部分请求失败", "其他实例健康"],
        "root_cause_term_groups": [["连接池", "pool"], ["实例", "instance"]],
        "must_retrieve": ["RB-101"],
        "support_fields": ["pool_waiters", "instances"],
    },
    "configuration_drift": {
        "fault_kind": "stale_cache",
        "title": "catalog-api 缓存配置版本漂移",
        "summary": "发布后的数据库版本已前进，但租户缓存仍固定在旧版本且错误率正常。",
        "symptoms": ["cache 与 db 版本不一致", "单租户受影响", "延迟保持基线"],
        "root_cause_term_groups": [["缓存", "cache"], ["版本", "version", "漂移"]],
        "must_retrieve": ["RB-310"],
        "support_fields": ["stale_sample_rate", "cache_version", "db_version"],
    },
    "bad_deployment": {
        "fault_kind": "warehouse_checkpoint_lag",
        "title": "warehouse-sync 发布后检查点停止推进",
        "summary": "同步进程发布后仍有心跳，但单个分区检查点落后并形成持续积压。",
        "symptoms": ["发布后 checkpoint gap 增加", "待同步记录过万", "进程仍健康"],
        "root_cause_term_groups": [["检查点", "checkpoint"], ["漂移", "落后", "gap"]],
        "must_retrieve": ["RB-WS-501"],
        "support_fields": ["lag_records", "checkpoint_gap"],
    },
    "resource_pressure": {
        "fault_kind": "queue_backlog",
        "title": "invoice-worker 消费容量承压",
        "summary": "消费者没有异常日志，但当前副本总吞吐无法覆盖持续的任务生产速率。",
        "symptoms": ["容量低于生产速率", "队列水位上升", "无毒消息证据"],
        "root_cause_term_groups": [["生产", "入队", "produce"], ["消费", "容量", "吞吐", "副本不足"]],
        "must_retrieve": ["RB-104"],
        "support_fields": ["queue_depth", "oldest_age_s"],
    },
    "misleading_symptoms": {
        "fault_kind": "dependency_rate_limit",
        "title": "recommendation-api 看似本地 CPU 抖动",
        "summary": "群聊猜测 CPU 不足，但可信指标显示资源健康，真正异常是依赖 429 与重试放大。",
        "symptoms": ["未经证实的 CPU 猜测", "依赖 429", "重试量增加"],
        "root_cause_term_groups": [["限流", "429", "rate limit"], ["重试", "retry"]],
        "must_retrieve": ["RB-429", "OPS-12", "SEC-07"],
        "support_fields": ["http_429_rate", "retry_amplification"],
    },
    "multiple_plausible_causes": {
        "fault_kind": "connection_pool_exhaustion",
        "title": "checkout-api 延迟伴随多种可能原因",
        "summary": "延迟可能来自流量、依赖或连接池；工具证据显示流量稳定且单实例连接池等待异常。",
        "symptoms": ["延迟与 5xx 同时上升", "流量稳定", "多个候选根因"],
        "root_cause_term_groups": [["连接池", "pool"], ["实例", "instance"]],
        "must_retrieve": ["RB-101"],
        "support_fields": ["pool_waiters", "instances"],
    },
    "insufficient_evidence": {
        "fault_kind": "dependency_rate_limit",
        "title": "recommendation-api 没有授权修复工具",
        "summary": "证据指向外部依赖限流，但系统没有修改外部配额或重试策略的受控工具。",
        "symptoms": ["依赖 429", "重试放大", "需要人工协调外部团队"],
        "root_cause_term_groups": [["限流", "429", "rate limit"], ["重试", "retry"]],
        "must_retrieve": ["RB-429", "OPS-12", "SEC-07"],
        "support_fields": ["http_429_rate", "retry_amplification"],
    },
}


SPLIT_COUNTS = {"calibration": 30, "development": 40, "holdout": 50}


def atomic_json(path: Path, payload: dict) -> bytes:
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary.write(encoded)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)
    return encoded


def build_cases(split: str, count: int) -> list[dict]:
    categories = list(ARCHETYPES)
    cases = []
    for index in range(count):
        category = categories[(index * 5 + len(split)) % len(categories)]
        source = ARCHETYPES[category]
        sequence = index + 1
        cases.append(
            {
                "id": f"{split[:3]}-{category}-{sequence:03d}",
                "split": split,
                "taxonomy": category,
                "fault_kind": source["fault_kind"],
                "incident": {
                    "title": f"{source['title']}（样本 {sequence:03d}）",
                    "summary": (
                        f"{source['summary']} 数据集样本编号 {sequence:03d}；"
                        "样本编号不是诊断证据。"
                    ),
                    "symptoms": [
                        *source["symptoms"],
                        f"观测批次 batch-{split[:3]}-{sequence:03d}",
                    ],
                    "tags": ["evaluation", split, category],
                },
                "root_cause_term_groups": source["root_cause_term_groups"],
                "must_retrieve": source["must_retrieve"],
                "support_fields": source["support_fields"],
                "variants": [
                    {
                        "id": "incident-case",
                        "category": category,
                        "attack_surface": "incident_text",
                        "suffix": "",
                    }
                ],
            }
        )
    return cases


def main() -> int:
    split_metadata = {}
    for split, count in SPLIT_COUNTS.items():
        payload = {
            "schema": "harbor-incident-dataset/v1",
            "dataset_version": "incident-120-v1",
            "split": split,
            "cases": build_cases(split, count),
        }
        relative = Path(split) / "cases.json"
        encoded = atomic_json(TARGET / relative, payload)
        split_metadata[split] = {
            "path": relative.as_posix(),
            "case_count": count,
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "oracle_access": (
                "forbidden-during-development"
                if split == "holdout"
                else "allowed-for-calibration"
                if split == "calibration"
                else "allowed-for-development-observation"
            ),
        }
    manifest = {
        "schema": "harbor-evaluation-manifest/v1",
        "dataset_version": "incident-120-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "total_case_count": sum(SPLIT_COUNTS.values()),
        "taxonomy": list(ARCHETYPES),
        "default_split": "development",
        "splits": split_metadata,
        "holdout_protocol": {
            "frozen": True,
            "tuning_allowed": False,
            "unlock_requires_explicit_final_evaluation_flag": True,
        },
    }
    atomic_json(TARGET / "manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
