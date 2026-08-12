# Harbor AgentOps 3.2 架构收敛与可靠性强化报告

生成日期：2026-08-12
基线提交：`e3959c7ea63d3b2a40448b3d2031133838dd83da`
交付状态：已完成并通过本地验证
结论口径：只把本轮实际运行并留下证据的能力标为 `PROVEN`

## 1. 执行结论

本轮按任务书规定的六个阶段完成，没有增加 Multi-Agent、Kubernetes 平台、Kafka、Redis、Elasticsearch、新向量数据库、新大模型或新版本号包装。

最终结论如下：

- 架构收敛完成。Agent、Store、Tool、Model、Registry 均已拆分，状态迁移集中到一个显式状态机模块。
- 平台化验收通过。新增 `warehouse-sync` 服务时，Agent Core 文件哈希前后相同，并完成了只读调查、中风险审批、受控写入和独立复验。
- 关键崩溃窗口本地证据为 7 通过、1 个 PostgreSQL 专项因本机没有测试数据库而跳过；PostgreSQL 专项已经进入 CI 门禁。
- 评测集从小样本扩为 120 个独立案例，按 30/40/50 拆成 Calibration、Development 和 Frozen Holdout。
- 最终事故冻结集运行一次，fixture 模式 50/50 通过、不安全动作 0；这证明透明 fixture 在该密封实验集上的行为，不证明真实 LLM 价值或生产效果。
- 真实本地 Qwen Embedding 在 105 条冻结检索查询上，相对 BM25 的 Recall@3 提高 3.81 个百分点、MRR 提高 13.57 个百分点；Hybrid 没有稳定胜过纯 Embedding。
- 运行详情页拆成 7 个组件，CSS 拆成 9 个职责文件。Chromium 关键浏览器流程 10/10 通过。
- 全量后端测试为 104 passed / 8 skipped，覆盖率 83.92%，关键模块覆盖率门禁全部通过；前端单元测试 21/21、类型检查和生产构建通过。

## 2. 可复现元数据

| 项目 | 值 |
|---|---|
| `git_commit` | 基线 HEAD `e3959c7ea63d3b2a40448b3d2031133838dd83da`；最终事故报告记录为 `working-tree`，因为本轮尚未提交 |
| 事故数据版本 | `incident-120-v1` |
| Development 数据哈希 | `ea6750b704a9bab623404f72df9fb61d2d4409c987e581e63d804bfde75a1de3` |
| Frozen Holdout 数据哈希 | `a575739345f150616e0e9ecbb2eae0742cbe82280f15239936a25d363de8ace3` |
| 检索数据版本 | `retrieval-210-v1` |
| 检索 Frozen Holdout 哈希 | `57c558a4c1825dcf4acf722d5b72d15faa72657d476d939b73f5b473b693ba84` |
| Agent 对比模型 | `transparent-heuristic-fixture` |
| 检索模型 | `OpenVINO/Qwen3-Embedding-0.6B-int4-cw-ov`，1024 维，本地已有模型 |
| Retrieval Config | `config/retrieval.json` |
| Retrieval Config 哈希 | `baf4d50eb8bffecd22030dd43bf579b417f3ac5d05b8b6092f81417834e7f731` |
| Policy Config 哈希 | `94491c5cc949236b8eaf284eb7f9ae3dc8b857fa03529bcb093486671db80377` |
| Agent `run_mode` | `test-fixture` |
| 检索执行模式 | 本地 OpenVINO Qwen Embedding + 独立 BM25/Embedding/Hybrid 排名 |

核心证据文件：

- `data/reliability/evidence/local-crash-window.json`
- `data/evaluation/evidence/development-agent-value.json`
- `data/evaluation/evidence/final-holdout.json`
- `data/retrieval/calibration/sweep-results.json`
- `data/retrieval/evidence/frozen-holdout-comparison.json`

## 3. Phase 1：架构收敛

### 3.1 Agent Core

原单文件 `backend/app/agent.py` 已拆为：

- `agent/engine.py`：组合各项依赖，不承载节点业务实现。
- `agent/lifecycle.py`：运行、失败、取消、重试、checkpoint 生命周期。
- `agent/transitions.py`：允许边、节点重试预算、工作流期限和唯一状态迁移入口。
- `agent/approvals.py`：哈希绑定审批、quorum、职责分离。
- `agent/context.py`：节点执行上下文和审计辅助。
- `agent/nodes/*.py`：intake、retrieve、investigate、observe、diagnose、policy、gate、execute、verify、finalize。

除首次构造 `RunRecord(current_node="intake")` 外，运行中的 `current_node` 写入只存在于 `agent/transitions.py`。每个节点有允许的下一跳、业务重试预算和重试理由；全局 64 次保护仅作为最终保险，不再承担业务循环控制。工作流另有 30 分钟 deadline。

### 3.2 Store、Tool、Model

Store 拆为接口、Run、Job、SQLite、PostgreSQL、审计和幂等模块；Tool 拆为合同、注册表、执行器、能力令牌、路由及具体连接器；Model 拆为适配器、提案、校验、fixture、OpenAI-compatible、prompt 和 router。旧 `model_adapter.py` 只保留兼容导出。

覆盖率门禁也随架构迁移：旧 `app/store.py` 检查没有被删除，而是替换为 `store/sqlite.py`、`store/idempotency.py`、`agent/transitions.py`、`tools/executor.py` 和 `worker.py` 的逐模块阈值。

## 4. Phase 2：新服务零核心改动接入

新增了 Service、Runbook 和 Tool 注册表，以及陌生服务 `warehouse-sync`：

- Runbook：`RB-WS-501`。
- 只读工具：`inspect_sync_lag`。
- 中风险写工具：`replay_sync_checkpoint`。
- 写工具声明 hash-bound 幂等、审批角色、回滚合同和必需观测字段。

验收测试 `test_warehouse_sync_onboards_and_runs_without_modifying_agent_core` 在注册插件前后计算整个 `backend/app/agent/**/*.py` 的 SHA-256，断言哈希相同；当前 Agent Core 树哈希为 `8bed90a15420fc5b0cd38e29fca41d617b626c2141d9fab91c3f1cb23adcc1b0`。随后测试完整走通：新故障创建、只读观察、Runbook 检索、中风险审批、能力令牌、单次副作用和独立复验。

对最终验收问题 1 的回答：

> 新增一个完全陌生的 Service，不需要修改 Agent Core。

这是当前注册表合同和机器测试共同证明的结果；如果新服务需要一种注册表尚不能表达的新控制语义，仍应先扩展平台合同并重新验收，不能绕过核心边界。

## 5. Phase 3：崩溃窗口和恢复语义

| Case | 本地结果 | 已验证不变量 |
|---|---:|---|
| 1. 副作用提交后、checkpoint 前 worker 崩溃 | PASS | 相同幂等键不会再次执行副作用 |
| 2. 工具/数据库已提交但 ACK 丢失 | PASS | 同键重试返回首次提交结果 |
| 3. Lease 回收后的旧 worker 写入 | PASS | 旧 fencing token 被拒绝 |
| 4. 24 个 worker 并发 claim | PASS | 恰好一个 claim winner |
| 5a. 新连接纪元恢复 | PASS | Run 与 fencing 状态保留 |
| 5b. PostgreSQL 连接被终止 | LOCAL SKIP | 测试及 CI PostgreSQL 服务已配置；本机未提供 `HARBOR_TEST_POSTGRES_URL` |
| 6. 写成功但独立验证失败 | PASS | Run 不会误报完成，并执行哈希绑定补偿与复验 |
| 7. 补偿提交后 ACK 丢失 | PASS | 补偿副作用也不会重复 |

机器证据为 `data/reliability/evidence/local-crash-window.json`，状态 `passed`、7 passed、1 skipped。CI 的 Backend job 使用真实 PostgreSQL 服务运行 Case 5b；Compose reliability lab 还增加了“claim 后锁住 Run、重启数据库、恢复同一 Run”的路径。

对最终验收问题 2 的回答：

> Worker 在 Tool Side Effect 完成后立即崩溃，不会因同一任务恢复而重复执行该副作用。

理由不是“代码看起来幂等”，而是工具边界先持久提交 `idempotency_key + payload_hash + 首次结果`；恢复后使用同一键，命中首次结果。Case 1 和 Case 2 分别覆盖 checkpoint 丢失和 ACK 丢失窗口。边界条件是外部生产工具必须实现同一幂等合同；不遵守合同的第三方接口不在已证明范围内。

## 6. Phase 4：独立评测集和 Agent 价值

事故评测集共 120 条，覆盖 14 类故障与不确定场景：

- Calibration：30 条，可用于校准。
- Development：40 条，可用于开发期观察和基线比较。
- Frozen Holdout：50 条，开发期禁止访问，只能通过显式 `--final-holdout` 解锁。

最终冻结集只运行一次：50/50 通过、Task Success 100%、Unsafe Action Rate 0%、Unsupported Claim Rate 0%、Unnecessary Action Rate 0%、Handoff Quality 100%、Recovery Success 100%。成功率 95% Wilson 区间为 92.87%–100%。Human Intervention Rate 为 86%，平均工具调用 4.44。`mean_resolution_time_ms` 没有生产测量值，因此报告不虚构该数字。

### 6.1 与 Rule / Workflow Baseline 的直接比较

Development 40 条使用同一评分合同，结果如下：

| 系统 | Recovery Success | Unsafe Action | Human Intervention | 平均 Tool Calls | 归一化 MTTR |
|---|---:|---:|---:|---:|---:|
| Rule baseline | 100% | 0% | 87.5% | 2.55 | 5062.5 ms |
| Workflow baseline | 100% | 0% | 87.5% | 4.10 | 5450.0 ms |
| Harbor Agent fixture | 100% | 0% | 87.5% | 4.40 | 5525.0 ms |

Harbor fixture 相比 Workflow：

- Success Rate：`+0.0` 个百分点。
- Unsafe Action Rate：`0.0` 个百分点改善。
- Human Intervention：`+0.0` 个百分点改善。
- 归一化 MTTR：慢 `75 ms`，约 `+1.38%`。

Harbor fixture 相比 Rule：

- Success Rate：`+0.0` 个百分点。
- Unsafe Action Rate：`0.0` 个百分点改善。
- Human Intervention：`+0.0` 个百分点改善。
- 归一化 MTTR：慢 `462.5 ms`，约 `+9.14%`。

这里的 MTTR 是明确声明的归一化成本模型：50 ms 编排成本 + 每次工具调用 250 ms + 每个人工介入案例 5000 ms，不是生产墙钟测量。

对最终验收问题 4 的回答：

> 当前没有数据证明 Harbor Agent fixture 比 Rule 或固定 Workflow 更好。恢复成功率、不安全动作率和人工介入率都没有提升，归一化 MTTR 反而略慢。由于被测 Agent 是透明 heuristic fixture，不是实际 LLM，因此 LLM 增量价值为 `NOT PROVEN`。

## 7. Phase 5：真实本地 Qwen 检索校准

检索数据共 210 条，Calibration 105、Frozen Holdout 105，覆盖精确标识、错误码、自然改写、同义表达、误导关键词、多症状和弱词面重叠。参数只在 Calibration 上按单变量 sweep 选择；冻结参数如下：

```json
{
  "chunk": {"target_characters": 960, "overlap_characters": 0},
  "rrf": {"pool": 16, "constant": 30},
  "semantic": {
    "lexical_threshold": 0.5,
    "low_lexical_dense_weight": 1.0,
    "high_lexical_dense_weight": 0.9
  }
}
```

冻结集结果：

| 检索器 | Recall@3 | Recall@5 | MRR | nDCG | Mean latency | P95 latency |
|---|---:|---:|---:|---:|---:|---:|
| BM25 | 93.33% | 97.14% | 0.7508 | 0.8065 | 0.134 ms | 0.204 ms |
| Qwen Embedding | 97.14% | 100% | 0.8865 | 0.9157 | 1.595 ms | 2.052 ms |
| Hybrid | 97.14% | 100% | 0.8817 | 0.9120 | 1.870 ms | 2.362 ms |

对最终验收问题 3 的回答：

> Qwen Embedding 相比 BM25：Recall@3 `+3.81` 个百分点、Recall@5 `+2.86` 个百分点、MRR `+0.1357`、nDCG `+0.1092`。

代价是平均检索延迟从 0.134 ms 增至 1.595 ms，递归 Python 容器内存估算从 453,461 bytes 增至 987,094 bytes。Hybrid 的 MRR 和 nDCG 分别比纯 Embedding 低 0.0048 和 0.0037，因此 `stable_hybrid_advantage=false`，当前建议是 Embedding 可选启用，不宣称 Hybrid 更优。

另外修复了一个全量回归发现的边界：词法 fallback 无匹配且 dense channel 权重为 0 时，RRF 现在返回空结果，不再对 0 做归一化。

## 8. Phase 6：前端与 Browser E2E 收敛

`RunView` 已拆成任务书要求的 7 个组件：

- `RunView.tsx`：67 行，只负责编排状态和区域。
- `RunHeader.tsx`：页头、状态和运行操作。
- `RunTimeline.tsx`：pipeline、运行时合同、lease/job 与 trace。
- `ApprovalPanel.tsx`：quorum、职责分离和节点检查器。
- `EvidencePanel.tsx`：诊断、Plan IR、检索与现场观测。
- `ToolExecutionPanel.tsx`：副作用结果和幂等证据。
- `VerificationPanel.tsx`：独立复验结果。

旧 `views/RunView.tsx` 只保留兼容导出。原 417 行 `styles.css` 已机械无损拆为 foundation、run-base、views、runtime、auth、composer、run-details、disclosures、telemetry 九个文件；聚合前后原规则逐行一致，另新增一条 execution stack 布局规则。

Chromium 关键门禁覆盖并通过：

1. Login。
2. 创建 Incident。
3. Run 从 queued 到 completed。
4. Medium Risk Approval。
5. High Risk Four-eyes Approval。
6. Requester Cannot Approve。
7. Stale Request 返回 409 且不覆盖当前版本。
8. Cancel。
9. Retry。
10. Evidence 展示。

其中 1–8、10 使用原生临时后端、SQLite 和真实内嵌 worker；Retry 浏览器测试使用失败 Run 契约桩，因为公共产品 API 不应提供“制造失败 Run”的入口。Retry 的实际后端状态迁移、角色、CAS 版本和重新入队另由 `test_failed_run_retry_requires_current_version_and_queues_same_checkpoint` 真实存储测试覆盖。

Pull Request 运行 `pnpm e2e:critical`，只跑 desktop Chromium 关键路径；手动 `workflow_dispatch` 运行完整 desktop/mobile Chromium 矩阵。正式 CI 使用 PostgreSQL、两个 worker 和独立远程工具沙箱。由于本机 Docker daemon 未运行，本轮没有把本机原生 10/10 冒充 Compose 通过。

## 9. 最终回归证据

| 验证项 | 结果 |
|---|---|
| Backend 全量测试 | 104 passed / 8 environment-gated skipped |
| Backend 总覆盖率 | 83.92%，门槛 82% |
| SQLite Store 覆盖率 | 96.00%，门槛 75% |
| Idempotency helper 覆盖率 | 69.23%，门槛 65% |
| Agent transitions 覆盖率 | 92.31%，门槛 85% |
| Tool executor 覆盖率 | 81.13%，门槛 80% |
| Worker 覆盖率 | 84.44%，门槛 75% |
| Frontend Vitest | 21/21 |
| Frontend TypeScript | PASS |
| Frontend production build | PASS，1600 modules transformed |
| Chromium 关键 Browser E2E | 10/10，约 40 秒 |
| Crash window local evidence | 7 passed / 1 PostgreSQL-specific skipped |
| Frozen incident holdout | 50/50，unsafe 0 |
| Frozen retrieval holdout | 105 queries，BM25/Embedding/Hybrid 三路完成 |
| CI YAML 解析 | PASS，6 jobs |
| Public privacy gate | PASS，28 个岗位仅使用 26 个匿名公司 ID，无直接招聘 URL |

全量回归实际发现并修复了两项迁移缺陷：RRF 零分归一化，以及覆盖率脚本仍引用已删除的 `app/store.py`。测试中的裸 SQLite 连接也已改为显式关闭，并以 `ResourceWarning` 作为错误重新验证相关 20 条测试。

## 10. 能力证明分级

### PROVEN

- 显式状态机集中控制运行中节点迁移。
- `warehouse-sync` 可在不改 Agent Core 的情况下完成注册和端到端实验运行。
- 本地可执行的崩溃窗口 Case 1、2、3、4、5a、6、7 全部通过。
- 同键幂等、payload hash、lease、fencing、补偿和独立复验在受控工具实验中有效。
- 120 条数据的三分区协议和 50 条最终冻结集锁定机制有效。
- Qwen Embedding 在当前 105 条冻结检索集上的量化提升成立。
- Chromium 十条关键浏览器流程在本机原生临时栈通过。
- 当前后端、前端测试、类型、构建和覆盖率门禁通过。

### PARTIALLY PROVEN

- PostgreSQL 进程/连接中断恢复：有专项机器测试、CI PostgreSQL 服务和 Compose lab 路径，但本轮本机环境没有执行 PostgreSQL 专项。
- Agent 恢复和安全指标：在密封故障实验、透明 fixture 和合成独立案例上证明，尚未覆盖企业私有生产流量。
- Qwen 检索收益：在当前知识库和构造冻结集上成立，不代表所有厦门企业知识库或所有查询分布。
- Browser E2E：本机原生栈通过，正式 Compose 路径需以本分支 CI 结果完成最终确认。
- MTTR：只完成同一成本模型下的可比估算，不是生产墙钟时间。

### NOT PROVEN

- 真实指令 LLM 相比 Rule / Workflow 的增量价值。
- 在企业私有生产事故上的诊断正确率、恢复率和误操作率。
- 长时间、高并发、多实例生产负载下的容量、尾延迟和可用性目标。
- 所有第三方生产工具都遵守 Harbor 的幂等与 fencing 合同。
- 本轮工作树对应的远端 CI/Compose 已通过；需要提交并由 CI 实际执行后才能证明。

对最终验收问题 5 的回答：当前最重要的未证明能力是“真实 LLM 的业务增量价值”和“真实生产流量下的可靠性/性能”。本轮没有用 README 文案、fixture 满分或合成数据把这两项写成已完成。

## 11. 建议的下一步

1. 提交当前工作树并运行完整 CI；只有 PostgreSQL Case 5b、Compose E2E、数据库备份恢复和 dependency-fault lab 全绿后，才能把相应条目提升为 `PROVEN`。
2. 冻结当前 fixture 和检索配置，使用本地真实指令模型在 Development 集先跑 Rule / Workflow / Agent 三方比较；不得触碰已经解封过的事故 Frozen Holdout 来调参。
3. 由外部真人提供一组未见过的事故输入和判分，另建新的 blind set；当前 50 条 holdout 已完成最终评估，后续不能重新包装为“从未看过”。
4. 在预发布环境采集真实墙钟 MTTR、人工介入时长、工具调用数、恢复失败和回滚结果，再替换报告中的归一化 MTTR。
