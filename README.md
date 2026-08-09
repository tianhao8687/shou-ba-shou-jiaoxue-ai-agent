# 手把手教学 AI Agent：Harbor AgentOps 3.2

这是一个从零基础教程逐步走到生产型 AI Agent 工程实践的完整求职项目。建议第一次访问先阅读 [由浅入深学习指南](./LEARNING_GUIDE.md)，再运行应用和查看架构。

## 阅读入口

- [零基础四阶学习指南](./LEARNING_GUIDE.md)：前五课不讲术语，随后每课只引入一个新词。
- [系统架构与设计原理](./docs/architecture.md)：状态机、RAG、工具治理、恢复、评测与可观测性。
- [厦门 AI Agent 20K+ 岗位与项目报告](./career/厦门_AI_Agent_20K以上岗位与Harbor_AgentOps_3.2最终报告_2026-08-08.md)：26 家公司、28 个岗位和项目能力映射。
- [交付与验收报告](./career/Harbor_AgentOps_交付与验收报告.md)：已实测内容、机器证据和未完成边界。
- [岗位明细 CSV](./career/厦门_AI_Agent_20K以上岗位明细_2026-08-08.csv)：逐条招聘来源和需求证据。

第一次体验可以使用下方的可复现夹具模式，不要求安装本地大模型。完整模式再接入本机 OpenVINO Qwen。

Harbor AgentOps 是一个本地优先、证据约束、可恢复的 AIOps Agent 控制面与密封故障实验室。它不是聊天框：事故进入持久任务队列后，系统先用真实 Qwen3 Embedding 检索手册并采集指标、日志和实例状态，再让本地 Qwen 生成紧凑候选计划；确定性策略编译器补齐并校验 Plan IR，高风险动作必须由两个独立主体审批，工具服务在独立网络边界验证租户绑定的短效 capability，执行后回到原始观测通道验证结果，失败时按已审批合同补偿或转人工。

项目面向厦门 20–30K 的 AI Agent 应用、AgentOps、AIOps 与企业 AI 平台岗位。它不使用 Elasticsearch，也不使用 Electron。

## 为什么不是普通 Agent Demo

- 运行输入没有 `expected_cause`、标准计划或预期答案。
- 15 个密封评测用例每次创建新实验；oracle 只在运行结束后由评测器读取。
- 模型只输出小型 `CompactDraftProposal`，不能签发权限或直接调用工具。
- 服务端根据真实观察物化前置条件、成功条件、风险、容量目标和回滚。
- 所有 Run 都绑定控制面租户；列表、详情、任务、指标、证据和动作接口按租户隔离。
- 高风险需要 2 个独立审批主体，中风险需要 1 个；申请人不能自批，同一主体不能重复投票。
- 每一票都绑定同一个 SHA-256 计划哈希；参数变化会使旧审批失效。
- capability 绑定 tenant、run、plan hash、step、tool、payload hash、角色、有效期和单次使用次数。
- API 只入队；worker 使用数据库 lease、heartbeat 与 fencing token，崩溃后可以恢复。
- 工具副作用与幂等记录在同一事务提交；响应丢失后复用同一 key 只返回首次结果。
- “工具返回成功”不等于事故恢复；系统独立重读指标逐条检查 success criteria。
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
    WORKER --> POLICY["Plan IR + Deterministic Policy Compiler"]
    POLICY --> GATE["Tenant-bound Four-eyes HITL"]
    GATE -->|"signed capability"| LAB["独立 Sealed Fault Lab"]
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
| Web | React 19、TypeScript、Vite、Vitest、Testing Library |
| API | Python 3.12、FastAPI、Pydantic v2、OpenAPI |
| 模型 | 本机 Qwen3-VL-8B-Instruct INT4、OpenVINO GenAI、OpenAI-compatible sidecar |
| Agent | 显式状态机、checkpoint、HITL、Plan IR、确定性策略编译 |
| 数据 | SQLite 开发模式；PostgreSQL 16 + pgvector 部署模式 |
| 检索 | 文档切块、metadata route、BM25、Qwen3 Embedding 1024d、adaptive RRF、pgvector HNSW |
| 执行 | 独立 FastAPI fault lab、HMAC capability、持久幂等 |
| 可靠性 | durable jobs、lease、heartbeat、CAS、fencing、补偿动作 |
| 可观测 | Trace、Audit、Evidence Bundle、Prometheus |
| 交付 | Docker Compose、Nginx、独立 API/worker、健康检查 |

V3.2 已在本机真实运行 `OpenVINO/Qwen3-Embedding-0.6B-int4-cw-ov`：last-token pooling、L2 归一化、1024 维，OpenAI-compatible sidecar 只监听本地端口。默认配置仍保留可复现的 256 维 Feature Hashing fallback，且健康接口会如实区分 `semantic` 与 `lexical-feature-baseline`。

## 3.2 生产验证增量

- Docker Desktop 29.6.1 + WSL 2.7.11 上实启 PostgreSQL/pgvector、持久 fault lab、API、3 个独立 worker、Nginx 和 Prometheus，不再把静态 Compose 校验冒充容器联调。
- 3 个 worker 竞争真实 PostgreSQL job；数据库 claim 使用 `FOR UPDATE SKIP LOCKED`，模型侧用 PostgreSQL advisory lock 做跨进程 admission control。
- 修复了并发模型调用造成的容量型假超时：修复前第 3 条任务在 180 秒超时并 handoff；修复后 3/3 完成，并把排队时间与推理时间分开记录。
- Prometheus 通过 DNS service discovery 抓取每个 worker 的 `:9101/metrics`；实测 backend、模型、fault lab 与 3 个 worker 共 6/6 targets 为 up。
- API、worker、fault lab 使用 UID 10001，Nginx 使用 UID 101；四类自研容器均为只读根文件系统、`cap_drop: ALL`、`no-new-privileges`，仅 `/tmp` 和明确数据卷可写。
- 生产镜像不再安装 pytest；测试依赖进入独立 multi-stage test image。Python、Node、Nginx、pgvector 和 Prometheus 基础镜像均固定 digest。
- 修复 React 异步竞态：Job 状态按 `run_id` 缓存，旧请求不能把上一运行的 lease/fencing 数据显示到新运行。
- Nginx 增加 CSP、COOP、CORP 并隐藏版本；真实浏览器在 CSP 下 console 仍为 0 error / 0 warning。

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
# 后端测试镜像：38 项 + 覆盖率；生产镜像不携带 pytest
# 以下命令从仓库根目录执行
docker build --target test -t harbor-agentops-backend-test -f backend/Dockerfile .
docker run --rm --network harbor-agentops_default `
  -e HARBOR_TEST_POSTGRES_URL=postgresql://harbor:harbor-local-db@database:5432/harbor_test `
  -e HARBOR_TEST_OPS_URL=http://ops-sandbox:8092 `
  -e HARBOR_TEST_OPS_ORACLE_TOKEN=harbor-local-oracle-token-change-me `
  -e HARBOR_TEST_CAPABILITY_SECRET=harbor-local-capability-secret-change-me `
  harbor-agentops-backend-test pytest --cov=app --cov-report=term

# 或在本地开发环境运行
cd backend
pip install -r requirements-dev.txt
pytest --cov=app --cov-report=term

# 前端 16 项、类型与生产构建
cd ../frontend
pnpm test
pnpm typecheck
pnpm build

# Compose 配置与 3 worker 实启
cd ..
docker compose config --quiet
docker compose up -d --build --scale worker=3
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

GitHub Actions 对每个 PR 和 `main` 提交执行四个稳定检查：`Quality gates`、`Backend tests`、`Frontend tests`、`Compose smoke`。最后一项调用同一个 `quickstart.py`，因此公开徽章和本地一键启动验证的是同一条交付路径。

2026-08-08 最终验证：

| 证据 | 结果 |
|---|---|
| 后端 | 38/38；总语句覆盖率 81%；包含真实 PostgreSQL 和远程 fault lab 集成测试 |
| 前端 | 16/16；类型检查、生产构建通过；新增跨运行异步竞态回归 |
| 真实语义运行 | Qwen3 Embedding 1024d；完整 API 事故运行通过，健康状态 `semantic` |
| RAG 留出集 | 15 条；Recall@1 0.9333、Recall@3 1.0、MRR 0.9667；相对词法基线不回归 |
| 四眼与租户 | 自批/重复票/越权角色/跨租户/工具租户伪造均被拒绝；第二主体后才执行 |
| 15 例密封夹具 | `EVAL-02E4B19199`，15/15，100 分，危险越权率 0，P95 35ms |
| 真实 Qwen 密封抽样 | `EVAL-EA80AAEAF4`，3/3，100 分，P95 77,019ms；含 baseline、Prompt Injection、无关噪声 |
| 浏览器真实运行 | `RUN-841A750FB147` 与 `RUN-E8C59C2EBDFB` 均完成；后者在停掉全部 worker 时先显示正确的 queued/unclaimed，新 worker 恢复后完成 |
| 响应丢失 | 提交后 503；同 key 重放为 `skipped`，副作用计数仍为 1 |
| worker 竞争 | PostgreSQL 下 16 个并发 claimant 同抢 1 个 job，仅 1 个成功；3 个真实容器 owner 唯一 |
| 崩溃恢复 | 过期 lease 被 fencing #2 恢复，旧 owner 写入被拒绝 |
| 补偿测试 | 扩容“伪成功”后验证失败；回滚至 2 副本并独立确认 |
| HTTP 压测 | 2,000 请求 / 64 并发，100% 成功，214.93 RPS，P95 809.92ms |
| 模型容量故障 | 修复前并发 3 条中第 3 条 180s timeout；advisory admission control 后 3/3 完成，排队 0/67/135s、推理约 67–70s |
| 浏览器 | 390×844 适配通过；UTF-8 正常；CSP 下 console 0 warning / 0 error；证据泄漏扫描通过 |
| Compose | 8 个容器健康运行：PostgreSQL、fault lab、API、3 worker、Nginx、Prometheus；6/6 scrape targets up |
| 容器安全 | 自研运行容器全为非 root、只读根文件系统、capabilities 全丢弃；生产后端无 pytest |

详细机器证据见 `docs/validation-evidence-v3.2-2026-08-08.json`。夹具 100 分只证明编排、安全和回归；真实 Qwen 仅抽样 3 个用例，不能外推成 15/15 或生产准确率。

## 目录

```text
backend/app/
  agent.py          状态机、审批、执行、验证、补偿
  model_adapter.py  Qwen 协议、结构校验、Plan IR 物化、熔断
  policy.py         工具/证据/风险/回滚的确定性编译
  retrieval.py      切块、路由、BM25/RRF、Embedding adapter
  store.py          SQLite/PostgreSQL、CAS、job lease、fencing
  tools.py          工具注册表、capability、幂等客户端
  evaluation.py     密封实验、隐藏 oracle、逐项评分
ops_sandbox/        独立持久故障与工具服务
local_model_service/本地 OpenVINO Qwen sidecar
local_embedding_service/本地 OpenVINO Qwen3 Embedding sidecar
frontend/           运行、评测、知识、策略控制台
data/               V3 手册与密封用例
observability/      Prometheus 配置
scripts/            启动、真实模型/Embedding 验证、检索评测、压测
docs/               架构和机器可读证据
```

## 已知边界

- fault lab 是可变、持久、隔离的真实测试边界，但不连接真实 Kubernetes、MES、云平台或数据库。
- 默认账户仍是演示 RBAC；已有租户 claims、职责分离和双人审批，但生产仍需要 OIDC/SSO、SCIM/JIT、离职回收和组织目录。
- PostgreSQL/pgvector、3 个 worker 与 Prometheus 已完成本机容器联调；但尚未做跨主机集群、备份恢复演练和长时间 soak test。
- 真实 Qwen3 Embedding 已接入并完成 15+15 小型评测，但尚无大规模领域标注集、hard negatives、reranker 或在线 A/B。
- 自动补偿目前仅白名单允许 `scale_workers`；重启、凭据和缓存回滚保持人工接管，避免假装存在安全的通用逆操作。
- 本地 Qwen CPU 单次推理约 68–77 秒，不适合高并发在线决策；当前用数据库 advisory lock 防止容量型假失败，生产仍需 GPU、批处理、模型路由与容量 SLO。
- 审计可查询但不是 WORM；生产还需不可篡改存储、集中 DLP、mTLS、Vault/KMS 与安全运营接入。
- 81% 覆盖率不等于 81% 质量；未覆盖主要集中在异常分支、取消/重试和 worker 进程生命周期，应在 CI 容器矩阵继续补。
- Docker Scout 因本机未登录 Docker ID 没有完成 CVE 数据库扫描；交付只声称镜像 digest 固定和运行权限加固，不声称漏洞扫描通过。

完整原理见 [docs/architecture.md](docs/architecture.md)。
