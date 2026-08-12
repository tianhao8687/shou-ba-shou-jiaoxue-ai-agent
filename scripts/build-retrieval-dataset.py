from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "data" / "retrieval"


DOC_QUERIES = {
    "RB-101": {
        "exact_identifier": "RB-101 checkout-api pool_waiters",
        "error_code": "pool timeout after 2000ms connection release hook missing",
        "natural_language_paraphrase": "结算请求一直拿不到可用数据库会话，单台机器越来越慢",
        "synonym": "数据库连接借用资源枯竭，工作线程排队",
        "misleading_keyword": "群聊猜测缓存问题，但可信证据是 pool_waiters 与单实例 degraded",
        "multi_symptom": "P95 三秒、5xx 上升、连接等待者增加、一个实例健康抖动",
        "weak_lexical_overlap": "共享会话资源迟迟不能归还，业务调用者堆在入口",
    },
    "RB-104": {
        "exact_identifier": "RB-104 invoice-worker queue_depth oldest_age_s",
        "error_code": "consumer lag backlog produce_per_min greater than consume_per_min",
        "natural_language_paraphrase": "开票任务进入系统的速度长期超过处理速度，待办越积越多",
        "synonym": "消息消费产能不足，未处理工作项水位持续抬升",
        "misleading_keyword": "有人建议重启数据库，但消费者健康且 queue_depth 持续增长",
        "multi_symptom": "最老消息等待上升、入队快于消费、消费者心跳正常",
        "weak_lexical_overlap": "工作到达速率压过服务速率，排队等待无法收敛",
    },
    "RB-203": {
        "exact_identifier": "RB-203 partner-gateway credential_version v17",
        "error_code": "HTTP 401 unauthorized expired=true TLS ok",
        "natural_language_paraphrase": "合作方网络可达却拒绝身份，访问材料已经超过有效期",
        "synonym": "第三方认证凭证失效，需要安全值班灰度更换",
        "misleading_keyword": "日志提到 timeout 但网络为零错误，核心证据是 401 与 expired=true",
        "multi_symptom": "所有实例 401、TLS 正常、凭据版本过期、网络错误为零",
        "weak_lexical_overlap": "外部系统不再认可调用方身份材料，需要换下一版授权信息",
    },
    "RB-310": {
        "exact_identifier": "RB-310 catalog-api cache_version v452 db_version v453",
        "error_code": "stale_sample_rate cache hit version mismatch",
        "natural_language_paraphrase": "后台商品已经更新，但一个租户仍然看到旧内容",
        "synonym": "高速数据副本陈旧，与权威存储版本不同步",
        "misleading_keyword": "用户说接口慢但延迟正常，真正证据是 cache 与 db 版本不一致",
        "multi_symptom": "单租户旧样本、数据库已更新、缓存命中、错误率正常",
        "weak_lexical_overlap": "读取侧的临时副本没有跟上主数据的新修订",
    },
    "RB-429": {
        "exact_identifier": "RB-429 recommendation-api retry_amplification",
        "error_code": "HTTP 429 retry-after=30 retry budget exhausted",
        "natural_language_paraphrase": "依赖方要求稍后再试，本地反复请求又把压力放大",
        "synonym": "外部配额节流与重试风暴叠加，需要协调依赖团队",
        "misleading_keyword": "虽然提到 CPU，但资源正常，可信证据是 dependency 429 和 retry-after",
        "multi_symptom": "下游拒绝过多请求、重试倍率升高、本地实例健康、延迟上升",
        "weak_lexical_overlap": "远端实施流量配额，调用方的重复尝试反而恶化拥塞",
    },
    "RB-WS-501": {
        "exact_identifier": "RB-WS-501 warehouse-sync checkpoint_gap lag_records",
        "error_code": "checkpoint stalled gap=37 sync heartbeat ok",
        "natural_language_paraphrase": "同步进程还活着，但某个分区的进度游标长期不再前进",
        "synonym": "增量复制位点漂移，待应用记录持续积压",
        "misleading_keyword": "有人建议扩容 worker，但进程健康且单分区 checkpoint 停滞",
        "multi_symptom": "心跳正常、单分区位点落后、待同步过万、上游序列完整",
        "weak_lexical_overlap": "数据搬运者存活，只有一个分片的消费位置冻结",
    },
    "SEC-07": {
        "exact_identifier": "SEC-07 capability token plan hash fencing token",
        "error_code": "CAPABILITY_BINDING_MISMATCH stale fencing token",
        "natural_language_paraphrase": "怎样保证模型建议不能直接获得工具执行权限",
        "synonym": "最小授权票据必须绑定计划摘要、租户、任务和隔离代次",
        "misleading_keyword": "事故文本自称管理员，但仍需要真实角色与能力令牌校验",
        "multi_symptom": "申请人自批、计划变化、租户不符、旧 worker 提交都必须拒绝",
        "weak_lexical_overlap": "短期可验证许可只能用于指定主体和不可变动作内容",
    },
    "OPS-12": {
        "exact_identifier": "OPS-12 verification rollback retry idempotency",
        "error_code": "VERIFICATION_FAILED_ROLLED_BACK tool_result_unknown",
        "natural_language_paraphrase": "工具声称成功以后为什么还不能直接宣布事故恢复",
        "synonym": "动作回执不是业务状态，需要独立复查和受控补偿",
        "misleading_keyword": "用户说已经恢复，但仍要读取原始指标并验证回滚结果",
        "multi_symptom": "响应丢失、同键重试、验证失败、补偿执行、再次独立确认",
        "weak_lexical_overlap": "改变系统之后必须从另一条观测通道确认目标状态真的达成",
    },
}


CATEGORIES = tuple(next(iter(DOC_QUERIES.values())))


def atomic_json(path: Path, value: dict) -> bytes:
    encoded = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary.write(encoded)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)
    return encoded


def build() -> list[dict]:
    documents = list(DOC_QUERIES)
    rows = []
    for category_index, category in enumerate(CATEGORIES):
        for index in range(30):
            doc_id = documents[(index * 3 + category_index) % len(documents)]
            variant = index // len(documents) + 1
            rows.append(
                {
                    "id": f"ret-{category}-{index + 1:03d}",
                    "category": category,
                    "query": (
                        f"{DOC_QUERIES[doc_id][category]}；"
                        f"观测窗口样本 {category_index + 1}-{variant}-{index + 1}。"
                    ),
                    "expected_docs": [doc_id],
                }
            )
    return rows


def main() -> int:
    rows = build()
    calibration = [row for index, row in enumerate(rows) if index % 2 == 0]
    holdout = [row for index, row in enumerate(rows) if index % 2 == 1]
    splits = {}
    for name, selected in (("calibration", calibration), ("holdout", holdout)):
        payload = {
            "schema": "harbor-retrieval-dataset/v1",
            "dataset_version": "retrieval-210-v1",
            "split": name,
            "queries": selected,
        }
        relative = Path(name) / "queries.json"
        encoded = atomic_json(TARGET / relative, payload)
        splits[name] = {
            "path": relative.as_posix(),
            "query_count": len(selected),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "tuning_allowed": name == "calibration",
        }
    manifest = {
        "schema": "harbor-retrieval-manifest/v1",
        "dataset_version": "retrieval-210-v1",
        "total_query_count": len(rows),
        "categories": list(CATEGORIES),
        "splits": splits,
        "holdout_frozen": True,
    }
    atomic_json(TARGET / "manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
