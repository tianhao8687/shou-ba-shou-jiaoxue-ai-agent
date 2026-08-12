# 手把手教学 AI Agent：Harbor AgentOps 3.6

> **先看项目完整复盘：** [开发问题全记录与工程复盘](./docs/开发问题全记录与工程复盘.md)——83 条可追溯问题，以及对应的根因、修复、回归证据和未解决边界。

这是一个从零基础教程逐步走到生产型 AI Agent 工程实践的完整求职项目。建议第一次访问先阅读 [由浅入深学习指南](./LEARNING_GUIDE.md)，再运行应用和查看架构。

## 阅读入口

- [零基础四阶学习指南](./LEARNING_GUIDE.md)：前五课不讲术语，随后每课只引入一个新词。
- [系统架构与设计原理](./docs/architecture.md)：状态机、RAG、工具治理、恢复、评测与可观测性。
- [厦门 AI Agent 20K+ 岗位与项目报告](./career/厦门_AI_Agent_20K以上岗位与Harbor_AgentOps_3.2最终报告_2026-08-08.md)：26 家公司、28 个岗位和项目能力映射。
- [v3.4 交付与验收报告](./docs/项目成熟度审计与v3.4交付报告_2026-08-10.md)：已实测内容、机器证据和未完成边界。
- [外部数据验证报告](./docs/外部数据验证报告_2026-08-10.md)：Loghub、NAB、AIOps Challenge 2025 的固定版本、留出方法、真实结果和生产边界。
- [v3.5 成熟度复评](./docs/项目成熟度审计与v3.5外部验证交付报告_2026-08-10.md)：外部验证加入后的重新评分、招聘映射和下一阶段差距。
- [完整遥测 RCA 验证报告](./docs/完整遥测RCA验证报告_2026-08-11.md)：四天、54,426,202 行公开日志/指标/调用链的三阶段隔离、失败复盘与最终盲测。
- [v3.6 可交付复评](./docs/项目成熟度审计与v3.6完整遥测交付报告_2026-08-11.md)：基于实现、测试和真实数据重新评分，并映射 20K+ 招聘要求。
- [v3.6 生产加固整改报告](./docs/v3.6-production-hardening-report.md)：Fresh Observation、Saga、生产密钥、连接池、故障注入和最新验收边界。
- [v3.6 机器发布证据](./docs/release-evidence-v3.6.json)：从 GitHub Actions API 生成的提交、Run、六个 Job 与时间戳，不手填绿色状态。
- [v3.3 成熟度审计与整改报告](./docs/项目成熟度审计与v3.3整改报告_2026-08-10.md)：按源码、容器、数据库、故障和浏览器行为复评，不用 Markdown 代替证据。
- [v3.4 1–4 项交付与复评报告](./docs/项目成熟度审计与v3.4交付报告_2026-08-10.md)：真实 kind staging、迁移、备份恢复、可靠性和 105 项对抗评测。
- [岗位明细 CSV](./career/厦门_AI_Agent_20K以上岗位明细_2026-08-08.csv)：逐条招聘来源和需求证据。

第一次体验可以使用下方的可复现夹具模式，不要求安装本地大模型。完整模式再接入本机 OpenVINO Qwen。

Harbor AgentOps 是一个本地优先、证据约束、可恢复的 AIOps Agent 控制面与密封故障实验室。它不是聊天框：事故进入持久任务队列后，系统先用真实 Qwen3 Embedding 检索手册并采集指标、日志和实例状态，再让本地 Qwen 生成紧凑候选计划；确定性策略编译器补齐并校验 Plan IR，高风险动作必须由两个独立主体审批，工具服务在独立网络边界验证租户绑定的短效 capability，执行后回到原始观测通道验证结果，失败时按已审批合同补偿或转人工。

项目面向厦门 20–30K 的 AI Agent 应用、AgentOps、AIOps 与企业 AI 平台岗位。它不使用 Elasticsearch，也不使用 Electron。

## 为什么不是普通 Agent Demo

- 运行输入没有 `expected_cause`、标准计划或预期答案。
- 105 个密封评测 case 每次创建新实验和数据库；oracle 只在运行结束后由评测器读取。
- 外部套件实际下载第三方日志、真实时序和 AIOps Challenge 2025 完整遥测；提交号、字节数与 SHA-256 固定，约 1.90 GB 原始归档不进入 Git。
- 评测页面把内部 fixture、轻量外部证据和完整遥测 RCA 分成三个标签；原始未见日盲测与修复后两次重放都通过 10/10 门槛，并把严格根因从盲测快照 29.17% 到可复现运行线 25.00% 的变化直接展示，不包装成生产满分。
- 模型只输出小型 `CompactDraftProposal`，不能签发权限或直接调用工具。
- 服务端根据真实观察物化前置条件、成功条件、风险、容量目标和回滚。
- 所有 Run 都绑定控制面租户；列表、详情、任务、指标、证据和动作接口按租户隔离。
- 高风险需要 2 个独立审批主体，中风险需要 1 个；申请人不能自批，同一主体不能重复投票。
- 每一票都绑定同一个 SHA-256 计划哈希；参数变化会使旧审批失效。
- capability 绑定 tenant、run、plan hash、step、tool、payload hash、角色、有效期和单次使用次数。
- API 只入队；worker 使用数据库 lease、heartbeat 与 fencing token，崩溃后可以恢复。
- Run 状态变化与 durable Job 入队在同一数据库事务提交；进程崩溃不会留下“显示排队但没有任务”的孤儿状态。
- lease 动态失效会在节点、模型和工具边界主动中止；远程工具还会拒绝较旧 fencing token。
- 普通 production 事件只用服务端固定 PromQL 模板读取 Prometheus；空数据、401 或超时均在模型和写动作前停止。
- 所有写工具在审批后、动作 capability 签发前重新读取真实状态；状态漂移、超时或无效数据都失败关闭，不使用旧 Observation 兜底。
- 工具副作用与幂等记录在同一事务提交；响应丢失后复用同一 key 只返回首次结果。
- “工具返回成功”不等于事故恢复；系统独立重读指标逐条检查 success criteria。
- 多步骤中后续动作明确失败时，工具根据写前真实状态生成并逆序执行 Saga 补偿；Unknown Outcome 不自动重试或补偿。
- 自动补偿也经过 Schema、角色、实验绑定、计划哈希、capability、幂等和独立复查。

## 架构

```mermaid
flowchart TB
    UI["React / TypeScript 控制台"] --> API["FastAPI 控制面"]
    API --> DB["SQLite 或 PostgreSQL<br/>Run / Job / Audit / Evaluation"]
    WORKER["Durable Worker"] -->|"claim · lease · heartbeat · fencing"| DB
    WORKER --> RAG["Metadata Route + BM25 + Qwen3 Embedding + RRF"]
    RAG --> EMB["本机 OpenVINO Qwen3-Embedding 0.6B INT4"]
    WORKER --> MODEL["OpenAI-compatible Model Adapter"]
    MODEL --> QWEN["本机 OpenVINO Qwen3-VL-8B INT4"]
    WORKER -->|"固定模板，只读"| PROM["Production Prometheus"]
    WORKER --> POLICY["Plan IR + Deterministic Policy Compiler"]
    POLICY --> GATE["Tenant-bound Four-eyes HITL"]
    GATE -->|"signed capability"| LAB["独立 Sealed Fault Lab"]
    GATE -->|"medium-risk capability"| K8S["kind staging connector<br/>namespace RBAC + Scale only"]
    LAB --> STATE["持久状态 + 工具幂等 + capability usage"]
    EVAL["Evaluator / Hidden Oracle"] -. "运行结束后核对" .-> LAB
    API --> OBS["Trace · Audit · Prometheus · Evidence Bundle"]
```

一次完整运行是：

`intake → retrieve → investigate → observe → diagnose → policy → gate → approval → execute → verify → finalize`

基础取证是确定性的只读最小集，LLM 不浪费一次调用去决定“是否先看指标”。模型在 `diagnose` 阶段只收到类型化事件字段、路由手册和工具观察；自由文本摘要不会跨入写动作规划边界。

## 技术栈

| 层 | 实现 |
|---|---|
| Web | React 19、TypeScript、Vite、Vitest、Testing Library、Playwright、Axe |
| API | Python 3.12、FastAPI、Pydantic v2、OpenAPI |
| 模型 | 本机 Qwen3-VL-8B-Instruct INT4、OpenVINO GenAI、OpenAI-compatible sidecar |
| Agent | 显式状态机、checkpoint、HITL、Plan IR、确定性策略编译 |
| 数据 | SQLite 开发模式；PostgreSQL 16 + pgvector 部署模式；DuckDB 直接扫描外部 Parquet |
| 检索 | 文档切块、metadata route、BM25、Qwen3 Embedding 1024d、adaptive RRF、pgvector HNSW |
| 执行 | 独立 FastAPI fault lab、HMAC capability、持久幂等 |
| 可靠性 | durable jobs、job lease、Worker registry heartbeat、CAS、fencing、补偿动作 |
| 可观测 | Trace、Audit、Evidence Bundle、Prometheus |
| 交付 | Docker Compose、kind Kubernetes、Nginx、独立 API/worker、GitHub Actions |

V3.3 已在本机真实运行 `OpenVINO/Qwen3-Embedding-0.6B-int4-cw-ov`：last-token pooling、L2 归一化、1024 维，OpenAI-compatible sidecar 只监听本地端口。默认配置仍保留可复现的 256 维 Feature Hashing fallback，且健康接口会如实区分 `semantic` 与 `lexical-feature-baseline`。

## 3.4 生产验证增量

- Run + Job 原子提交、活动任务唯一约束和 orphan reconciler 已覆盖 SQLite 与真实 PostgreSQL 回归。
- heartbeat 连续失败会主动取消当前 lease；节点、模型、工具前后检查动态租约，远端工具持久记录并拒绝旧 fencing token。
- `/api/health` 只表示进程存活，`/api/ready` 检查数据库、检索、模型合同和工具边界。实测停止 Prometheus 时分别返回 200 / 503，恢复后 readiness 回到 200。
- production / staging 自由事件通过固定 PromQL 模板读取请求率、错误率、P95 与 `up`，记录来源 URI；没有生产写连接器时只诊断并转人工。
- 本地模型网关现在只有一个执行槽和有界等待队列；队列满返回 429，排队超时返回 503，并暴露 active、queued、reject 和 timeout 指标。
- Evaluation 按 tenant 持久化和查询；Run 列表的 tenant filter 与 limit 下推到 SQLite / PostgreSQL，并建立表达式索引。
- Playwright 在桌面 Chromium 与 Pixel 7 两种视口验证登录、键盘焦点、响应式、Prometheus 只读取证、低风险闭环和高风险 2/2 审批；Axe WCAG 2 A/AA 门禁同步执行。
- 当前容器回归使用 2 个独立 Worker；两者都处理过真实 Job。CI 同样启动 2 Worker，并执行 HTTP smoke 与浏览器 E2E。
- kind staging connector 使用命名空间 ServiceAccount；集群只允许读取命名工作负载并更新 `deployments/scale`，拒绝 Secret、Pod 创建/exec、模板更新和 RBAC 修改。
- API / Worker 通过 kind 的私有 Docker network 访问 control-plane NodePort；宿主机验证端口只监听 `127.0.0.1`，不为 Linux 兼容性把 connector 暴露到局域网。
- staging 真实链路已完成“1 副本调查 → medium 风险审批 → 4/4 ready → 独立验证 → 实验复位”；connector 以 Kubernetes Lease 保存最大 fencing token 和幂等结果。
- 数据库使用带 SHA-256 漂移检查的 3 版 migration；PostgreSQL advisory lock 防多个 API / Worker 同时升级。
- pgvector 冷启动把扩展、派生表、知识种子与 HNSW 初始化放在另一把事务级 advisory lock 内；并发回归会让两个初始化线程同时起跑，防止新库上的 `CREATE EXTENSION` 竞态。
- Worker 即使空闲也写数据库进程心跳，`/api/status` 能证明当前 fleet 的在线副本，不再固定返回 `verified=false`。
- PostgreSQL custom-format 备份已真实恢复到隔离临时库，并核对迁移版本、核心表行数与孤儿 Job 后删除临时库。
- v4 评测为 5 类故障 × 21 个变体 = 105 case，加入工具输出注入、长上下文、Unicode 和结构化诱导，并报告 95% Wilson 区间。

## 3.6 生产加固增量

- Runtime 镜像明确包含 `/app/config/retrieval.json`，CI 以真实 `docker build` + `docker run` 验证加载合同。
- 写工具在审批后执行轻量 Fresh Observation，重新验证真实前置条件；失败、超时、缺字段和类型错误全部转人工。
- `scale_workers` 与 kind staging `scale_kubernetes_deployment` 支持工具自生成 Saga 逆操作；补偿元数据先于副作用持久化，补偿后走独立只读复验。
- `DEMO_MODE=false` 或 `APP_ENV=production` 时，演示密码、认证签名、capability 签名和 oracle token 若为空、过短或带默认标记会直接拒绝启动。
- PostgreSQL 使用官方 `psycopg_pool`；每个进程有有界连接池，模型 advisory lock 在同一 checkout 内加锁和解锁。
- 可靠性实验覆盖“Job 已 claim、首次读取 Run 前数据库崩溃”的精确窗口，并验证新 fencing token 接管同一 Run。

## 3.6 完整遥测盲测增量

- 数据清单固定 AIOps Challenge 2025 官方仓库提交、四个日期归档、精确字节数、SHA-256 与发布者 MD5；下载只允许指定 HTTPS 域名并限制大小，解压拒绝路径穿越、符号链接和设备文件。
- 四天共扫描 54,426,202 行：27,069,861 行日志、4,162,034 行指标和 23,194,307 行调用链；DuckDB 直接过滤 Parquet 的事故时间窗，不把全量数据塞入 Python 内存。
- 6 月 9 日 16 案例用于校准；6 月 17/18 日各 24 案例用于两轮验证并保留 7/10、8/10 的失败记录；修复数据语义和融合缺陷后冻结 `deterministic-rca-v3`。
- 6 月 19 日 24 案例此前未参与开发。预测器无 oracle 参数，预测先原子落盘并固定 SHA-256，评分器随后才打开标签；最终 0 次 oracle 泄漏、0 次危险写操作。
- 原始盲测快照为故障 Top-3 79.17%、实体 Top-3 58.33%、严格根因 Top-1 29.17%、证据模态召回 86.11%、多模态覆盖 75%、P95 61 ms，10/10 预注册质量门通过；冻结预测文件保持不变。
- 交付前第二次断网重放暴露 DuckDB 并行聚合与同分候选缺少二级排序，结果会随无序行先后漂移。实现改为单线程确定聚合、稳定文件选择和显式字典序裁决；没有用已打开的留出标签调回高分。
- 修复后两次独立重放的语义 SHA-256 完全一致，运行线为故障 Top-3 66.67%、实体 Top-3 58.33%、严格根因 Top-1 25.00%、证据模态召回 68.06%、多模态覆盖 50%，仍为 10/10 门槛。它是“答案已打开后的复现审计”，不是第二次盲测。
- 后端新增只读聚合 API，前端新增“完整遥测 RCA”面板；原始遥测保持 Git 忽略，页面不会暴露逐案 oracle。

## 快速启动

### 0. 一键启动并自动验收（推荐）

只需要 Python 3.12+、Docker Desktop 或 Docker Engine。仓库根目录执行：

```powershell
python scripts/quickstart.py up
```

它会构建并启动 PostgreSQL、fault lab、API、3 个 worker、Nginx 和 Prometheus，等待服务就绪，再通过真实 HTTP 完成一次“创建缓存故障 → 入队 → worker 领取 → 检索与规划 → 受控执行 → 独立验证 → 证据检查”。成功后打开 <http://127.0.0.1:8080>。

```powershell
# 查看状态或重新执行验收
python scripts/quickstart.py status
python scripts/quickstart.py smoke

# 停止服务；默认保留演示数据
python scripts/quickstart.py down
```

默认使用无需权重的 fixture 模式。只有明确配置 `MODEL_ENABLED=true`、`MODEL_FIXTURE_MODE=false` 时才连接真实 Qwen。Docker 没有运行、端口被占用或验收失败时，启动器会返回非零退出码并打印容器状态与有界日志。

### 1. 可复现夹具模式

复制 `.env.example` 为 `.env`。它已经是无需模型权重的可移植默认值：

```dotenv
MODEL_ENABLED=false
MODEL_FIXTURE_MODE=true
TOOL_MODE=inprocess
DATABASE_URL=sqlite:///./harbor.db
VECTOR_BACKEND=memory
```

后端：

```powershell
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

前端：

```powershell
cd frontend
pnpm install
pnpm dev
```

打开 <http://localhost:5173>。演示密码来自 `DEMO_PASSWORD`，默认本地值为 `harbor-demo-2026`。

### 2. 本机真实 Qwen

模型权重不会进入 Git。启动脚本按下面的优先级寻找运行资产：

1. `-Python` / `-ModelPath` 显式参数；
2. `HARBOR_PYTHON` / `HARBOR_LOCAL_MODEL_PATH` 环境变量；
3. 当前仓库的 `.models/Qwen3-VL-8B-Instruct-int4-ov` 目录，以及 `PATH` 中的 `python`。

相对路径始终按仓库根目录解析，因此可以从任意工作目录调用脚本。推荐把模型放进已被 Git 忽略的 `.models/`，把 OpenVINO 依赖安装到仓库自己的 `.venv`。

启动：

```powershell
.\scripts\start-local-model.ps1 `
  -Python ".venv\Scripts\python.exe" `
  -ModelPath ".models\Qwen3-VL-8B-Instruct-int4-ov"
```

首次迁移到另一台机器时可先加入 `-ValidateOnly`，只校验解释器、模型目录和参数，不加载模型。Linux/macOS 可直接执行 `python local_model_service/server.py --model-path /path/to/model`。

如果 Windows 执行策略阻止 `.ps1`，不要修改全局策略；只对本次进程执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-local-model.ps1 -ValidateOnly
```

`.env`：

```dotenv
MODEL_ENABLED=true
MODEL_FIXTURE_MODE=false
MODEL_ENDPOINT=http://127.0.0.1:8091/v1
MODEL_NAME=OpenVINO/Qwen3-VL-8B-Instruct-int4-ov
MODEL_API_KEY=harbor-local-model-token
MODEL_TIMEOUT_SECONDS=240
```

网关默认只监听 `127.0.0.1`，限制请求大小与输出 token，串行推理保护 CPU/RAM，不记录 Prompt 或模型输出。

### 2.1 本机真实 Qwen3 Embedding

模型同样不进入 Git。脚本依次读取 `-ModelDir`、`HARBOR_LOCAL_EMBEDDING_PATH`，最后检查仓库内的 `.models/Qwen3-Embedding-0.6B-int4-cw-ov`。验证环境使用 CPU / OpenVINO / INT4，输出为 1024 维、last-token pooling、L2 normalized。

启动：

```powershell
.\scripts\start-local-embedding.ps1 `
  -Python ".venv\Scripts\python.exe" `
  -ModelDir ".models\Qwen3-Embedding-0.6B-int4-cw-ov"
```

也可先追加 `-ValidateOnly` 做无推理校验；Linux/macOS 可直接执行 `python local_embedding_service/server.py --model-dir /path/to/model`。

`.env`：

```dotenv
EMBEDDING_BACKEND=openai-compatible
EMBEDDING_ENDPOINT=http://127.0.0.1:8093/v1
EMBEDDING_MODEL=OpenVINO/Qwen3-Embedding-0.6B-int4-cw-ov
EMBEDDING_API_KEY=harbor-local-embedding-token
```

离线评测把 15 条校准集与 15 条独立留出集分开。留出集 Recall@3=1.0、MRR=0.9667，与词法基线持平；这证明切换没有回归，不声称小样本上存在泛化提升。

### 3. Compose 基础设施

Compose 默认使用夹具模型和词法特征基线，克隆后不依赖本机权重。如需切换到宿主机的真实生成模型和 Embedding 网关，先启动两个网关，再设置：

```powershell
$env:MODEL_ENABLED="true"
$env:MODEL_FIXTURE_MODE="false"
$env:COMPOSE_MODEL_ENDPOINT="http://host.docker.internal:8091/v1"
$env:EMBEDDING_BACKEND="openai-compatible"
$env:COMPOSE_EMBEDDING_ENDPOINT="http://host.docker.internal:8093/v1"
```

然后执行：

```powershell
docker compose up -d --build --scale worker=3
```

Compose 包含 PostgreSQL/pgvector、持久 fault lab、API、独立 worker、前端 Nginx 和 Prometheus。默认端口：

| 服务 | 地址 |
|---|---|
| Web | <http://localhost:8080> |
| OpenAPI | <http://localhost:8000/docs> |
| 模型网关 | <http://localhost:8091/health> |
| fault lab | <http://localhost:8092/health> |
| Embedding 网关 | <http://localhost:8093/v1/health> |
| Prometheus | <http://localhost:9090> |

暴露到非本机网络前必须替换所有示例 secret、演示密码，并接入企业身份系统。

### 4. kind staging 安全实验

脚本会下载固定到 v0.32.0 且校验 SHA-256 的 kind，不需要全局安装。Docker 和 `kubectl` 可用时执行：

```powershell
python scripts/kind-lab.py up

docker compose `
  -f docker-compose.yml `
  -f kubernetes/kind/docker-compose.kind.yml `
  up -d --build --scale worker=2

python scripts/verify-kind-agent.py
```

最后一条命令验证审批前无写入、审批后真实扩到 4/4 ready、独立检查和实验复位。只复查 RBAC 与 connector 健康可执行 `python scripts/kind-lab.py verify`；删除实验集群执行 `python scripts/kind-lab.py down`。

### 5. 数据恢复、可靠性与内部/外部评测

```powershell
# 生成带 SHA-256 manifest 的事务一致备份，并恢复到隔离临时库验证
python scripts/database-ops.py backup --output backups/harbor.dump
python scripts/database-ops.py verify backups/harbor.dump

# 快速故障矩阵；长时间 soak 可把 duration 改为 7200–28800
python scripts/reliability-lab.py `
  --duration-seconds 0 --min-runs 8 `
  --worker-restart-every 2 --database-restart-every 4 `
  --prometheus-outage-once

# fixture 全量密封评测；真实模型在线时将 mode 改为 live
python scripts/run-evaluation-suite.py --mode fixture

# 首次联网获取 8 个固定版本外部文件、校验哈希并运行留出评测
python scripts/run-external-validation.py

# 使用已经验真的缓存断网复跑
python scripts/run-external-validation.py --offline

# 首次联网下载四天完整遥测并执行三阶段 RCA 验证（约 1.90 GB）
python scripts/run-telemetry-validation.py --require-gates `
  --predictions .runtime/telemetry-online.json `
  --output .runtime/telemetry-online-evidence.json

# 原始归档已验真时，断网复算；质量门和语义预测都必须相同
python scripts/run-telemetry-validation.py --offline --require-gates `
  --assert-semantic-equals .runtime/telemetry-online.json `
  --predictions .runtime/telemetry-offline.json `
  --output .runtime/telemetry-offline-evidence.json
```

## 演示身份

| 账号 | 角色边界 |
|---|---|
| `viewer@harbor.local` | observer；只读查看本租户运行与证据，不能创建或取消任务 |
| `operator@harbor.local` | observer + operator；可执行低风险精确动作 |
| `lead@harbor.local` | 增加 on-call-lead；可审批扩容/重启 |
| `security@harbor.local` | 增加 security-on-call；可审批凭据轮换 |
| `approver@harbor.local` | 独立双角色审批人；用于完成高风险第二票 |
| `admin@harbor.local` | 全部演示角色 + 故障实验和评测 |
| `other@harbor.local` | `other-tenant` 只读身份；验证跨租户不可见 |

身份令牌和 capability 都是 HMAC 签名且有过期时间、租户声明；高风险四眼审批和职责分离是真实服务端规则，但账号仍是本地演示数据，不等同于企业 OIDC/SSO/JIT。

## 测试与验证

```powershell
# 后端测试镜像：131 项 + 分层覆盖率门禁；生产镜像不携带 pytest / DuckDB
# 以下命令从仓库根目录执行
docker build --target test -t harbor-agentops-backend-test -f backend/Dockerfile .
docker run --rm --network harbor-agentops_default `
  -e HARBOR_TEST_POSTGRES_URL=postgresql://harbor:harbor-local-db@database:5432/harbor_test `
  -e HARBOR_TEST_OPS_URL=http://ops-sandbox:8092 `
  -e HARBOR_TEST_OPS_ORACLE_TOKEN=harbor-local-oracle-token-change-me `
  -e HARBOR_TEST_CAPABILITY_SECRET=harbor-local-capability-secret-change-me `
  harbor-agentops-backend-test sh -lc `
  "pytest --cov=app --cov-report=json:/tmp/coverage.json --cov-fail-under=82 && python /app/scripts/check-coverage.py /tmp/coverage.json"

# 或在本地开发环境运行
cd backend
pip install -r requirements-dev.txt
pytest --cov=app --cov-report=term
pytest tests/test_telemetry_validation.py `
  --cov=app.telemetry_validation `
  --cov-config=.coveragerc-telemetry --cov-fail-under=50

# 前端 21 项、类型、生产构建和桌面/移动真实浏览器 E2E
cd ../frontend
pnpm test
pnpm typecheck
pnpm build
pnpm exec playwright install chromium
pnpm e2e

# Compose 配置与 2 worker 实启
cd ..
docker compose config --quiet
python scripts/test-runtime-image.py
python scripts/quickstart.py up --workers 2
docker compose ps

# 真实 Embedding：校准集 + 独立留出集
python scripts/evaluate-retrieval.py
python scripts/validate-semantic-runtime.py

# 本机 Qwen 密封抽样
python scripts/validate-live-model.py --case-limit 2 --case-offset 0

# 有界混合读压测
python scripts/benchmark-api.py --requests 2000 --concurrency 64

# 防止个人磁盘路径再次进入公开仓库
python scripts/check-portability.py
```

GitHub Actions 对每个 PR 和 `main` 提交执行六个稳定检查：`Quality gates`、`Pinned external data validation`、`Backend tests`、`Frontend tests`、`Compose smoke and browser E2E`、`kind least-privilege staging E2E`。外部轻量数据 job 先联网核验固定字节，再断网复跑；Compose job 执行备份恢复和依赖故障矩阵；kind job 验证真实 RBAC、审批、Scale 与独立就绪证据。约 1.90 GB 的完整遥测另设手动 workflow，并缓存验真原始文件，避免每个小改动浪费带宽和 CI 时间。

最新生产加固代码提交 `14a0ec538901e4817cc047b7a8315492cec3a7a3` 已由 GitHub Actions Run [`31592619182`](https://github.com/tianhao8687/shou-ba-shou-jiaoxue-ai-agent/actions/runs/31592619182) 验证：六个必需 Job 6/6 passed，验证完成时间为 2026-08-12 11:39:19 UTC。机器证据由 `scripts/generate-release-evidence.py` 从 GitHub API 生成；脚本会拒绝失败、取消、运行中或缺 Job 的 Run。

2026-08-12 v3.6 生产加固本机验证：

| 证据 | 结果 |
|---|---|
| 后端 | Linux 测试镜像连接独立 PostgreSQL/Fault Lab：131/131；总覆盖率 86.33%；遥测验证器独立 9/9、覆盖率 62.53% |
| 前端 | 21/21 组件测试；TypeScript 与生产构建通过 |
| 浏览器 | desktop Chromium 11/11 + Pixel 7 11/11，共 22/22；包含 Axe WCAG 2 A/AA、只读生产事件、低风险闭环和双主体审批 |
| 完整启动入口 | 中文目录直接运行 `quickstart.py up --workers 2`，逐镜像构建、就绪等待和 HTTP smoke 全通过 |
| Runtime 合同 | 真实 runtime image 成功加载 `/app/config/retrieval.json`，配置哈希 `baf4d50e…` |
| 双 Worker | 2 个独立 Worker 均为 healthy，数据库心跳注册 2/2；PostgreSQL 连接竞争单 Job 仍只领取一次 |
| TOCTOU | 审批期间副本漂移、状态不变、观测超时、无效数据四类回归通过；三个失败路径均 0 次写入 |
| Saga | 双成功、明确失败后补偿成功、补偿失败、Unknown、只读前序五类回归通过 |
| 生产密钥 | demo 允许；非 demo 默认值与 production `change-me` 均 fail-fast；独立随机密钥允许 |
| PostgreSQL Pool | 官方 `psycopg_pool` 生效；advisory lock/unlock 只 checkout 同一连接一次 |
| 生产观测 | 正常 Prometheus 模拟返回 4/4 指标后才调用模型；空数据、401、超时均无模型调用、无写动作并转人工 |
| readiness | 停止 Prometheus：liveness 200、readiness 503；恢复后 readiness 200 |
| 一致性与隔离 | Run + Job 故障回滚、orphan 修复、活动 Job 去重、租约主动失效、远程 stale fence 拒绝均有回归 |
| 租户 | Run 查询和 Evaluation 均按 tenant 在数据库层隔离；API 跨租户仍返回 404 / 空结果 |
| 模型背压 | 单执行槽 + 有界队列；队列满与排队超时测试通过，且 429 / 503 语义和指标已实现 |
| 历史真实模型证据 | v3.2 的 3 条真实 Qwen 抽样仍保留；本轮未把旧样本伪装成新的生产准确率测试 |
| kind staging | 从零创建固定 Kind 集群，RBAC 矩阵通过；审批前 1 副本，审批后真实 4/4 ready，独立验证后复位 |
| 数据恢复 | 26,780-byte custom-format 备份已隔离恢复；3 个 migration、核心表行数和 0 orphan Job 一致，临时库已删除 |
| 可靠性 | 4 个 Run 全完成、0 失败；含 claim 后 DB 崩溃恢复、Worker 重启、DB 重启、Prometheus 停机；最终 Worker 2/2 |
| 密封评测 | v4 105/105 fixture 通过、0 unsafe action；95% Wilson 区间 96.47%–100% |
| 外部数据 | 3 个独立来源、8 个文件哈希匹配、34,984 条记录、29,125 条留出；9/9 基础门槛通过，外部自动故障覆盖 0% |
| 完整遥测 RCA | 4 个日期归档哈希匹配、54,426,202 行；原始盲测 79.17%/29.17%，确定排序后的两次重放语义一致并为 66.67%/25.00%；两者均 10/10 门槛、0 oracle 运行时泄漏、0 危险写入 |
| 恢复与可移植性回归 | pgvector 扩展卸载后留下缺失向量列的表壳，两个并发副本可自动修复；覆盖率门禁同时接受 Windows `\` 与 Linux `/` 路径，并对缺失模块失败关闭 |

本轮工程基线见 `docs/项目成熟度审计与v3.6完整遥测交付报告_2026-08-11.md`，完整方法和失败账本见 `docs/完整遥测RCA验证报告_2026-08-11.md`；机器可读结果分别为 `docs/evaluation-evidence-v4.json`、`data/external/evidence-v1.json` 和 `data/external/telemetry-evidence-v4.json`。fixture、公开外部数据和历史真实 Qwen 抽样都不能外推成目标公司的生产准确率。

## 目录

```text
backend/app/
  agent/            状态机节点、审批、Fresh Observation、执行守卫、Saga 补偿
  model/            Qwen 协议、结构校验、Plan IR 提案与路由
  policy.py         工具/证据/风险/回滚的确定性编译
  retrieval.py      切块、路由、BM25/RRF、Embedding adapter
  store/            SQLite/PostgreSQL、连接池、CAS、job lease、fencing
  migrations.py     版本化 schema、校验和、SQLite/PostgreSQL 升级锁
  tools/            工具合同、注册表、capability、幂等与执行边界
  registry/         服务、工具和 runbook 注册边界
  evaluation.py     密封实验、隐藏 oracle、逐项评分
  external_validation.py  外部来源校验、隔离切分、检测与安全评分
  telemetry_validation.py 完整遥测安全获取、Parquet 窗口分析、多模态融合与盲评分
ops_sandbox/        独立持久故障与工具服务
kubernetes_connector/命名空间最小权限 Kubernetes 工具边界
kubernetes/kind/    固定版本 kind 集群、RBAC、演示工作负载与 Compose override
local_model_service/本地 OpenVINO Qwen sidecar
local_embedding_service/本地 OpenVINO Qwen3 Embedding sidecar
frontend/           运行、评测、知识、策略控制台
data/               手册、检索集、V4 密封用例与外部来源/证据清单
observability/      Prometheus 配置
scripts/            启动、真实模型/Embedding、外部数据、检索评测与压测
docs/               架构和机器可读证据
```

## 已知边界

- fault lab 是可变、持久、隔离的测试边界；另有真实 kind staging Scale connector，但它不是企业 production、MES、云平台或数据库连接器。
- production connector 当前仍只有 Prometheus 固定模板只读适配器；Kubernetes 写权限只存在于隔离 staging 命名空间，因此普通 production 事件必然转人工。
- 默认账户仍是演示 RBAC；已有租户 claims、职责分离和双人审批，但生产仍需要 OIDC/SSO、SCIM/JIT、离职回收和组织目录。
- PostgreSQL 逻辑备份与隔离恢复已实测；尚未做 WAL/PITR、跨主机集群、跨区域恢复和 2–8 小时 soak。
- API `/api/ready` 只判断能否安全接收入持久队列；`/api/status` 通过数据库心跳报告 Worker fleet，但单个 API 200 仍不能冒充跨主机任务交付 SLO。
- 真实 Qwen3 Embedding 已接入并完成 15+15 小型评测，但尚无大规模领域标注集、hard negatives、reranker 或在线 A/B。
- 自动 Saga 目前仅白名单允许 `scale_workers` 与隔离 staging 的 `scale_kubernetes_deployment`；重启、凭据和缓存回滚保持人工接管，避免假装存在安全的通用逆操作。
- 本地 Qwen CPU 单次推理历史样本约 68–77 秒，不适合高并发在线决策；数据库 advisory lock 与网关有界队列能保护容量，但不能提高吞吐，生产仍需 GPU、批处理、模型路由与容量 SLO。
- 审计可查询但不是 WORM；生产还需不可篡改存储、集中 DLP、mTLS、Vault/KMS 与安全运营接入。
- 公开遥测 79.17%/29.17% 是原始盲测快照，不是当前可复现运行线；修复无序同分裁决后的诚实基线是故障 Top-3 66.67%、严格根因 Top-1 25.00%。两组都只代表固定日期离线证据；目标公司影子流量、漂移监测、本地 Qwen reranker A/B、真实 production connector 与长期 soak 仍必须单独验证。
- 测试覆盖率不等于业务质量；真实 production connector、跨主机故障、企业身份和 105 项真实模型重复试验仍必须单独验证。
- Docker Scout 因本机未登录 Docker ID 没有完成 CVE 数据库扫描；交付只声称镜像 digest 固定和运行权限加固，不声称漏洞扫描通过。

完整原理见 [docs/architecture.md](docs/architecture.md)。
