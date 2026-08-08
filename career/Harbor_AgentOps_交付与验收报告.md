# Harbor AgentOps 3.2 交付与验收报告

**交付日期：** 2026-08-08  
**项目目录：** 仓库根目录  
**状态：** 求职作品与生产验证实验室可交付；不声称可直接接管企业生产系统  

## 1. 交付内容

| 交付物 | 用途 |
|---|---|
| 仓库根目录 | 完整源码、数据、测试、部署与文档 |
| `厦门_AI_Agent_20K以上岗位与Harbor_AgentOps_3.2最终报告_2026-08-08.md` | 26家公司/28岗位、需求深挖、项目映射和20K判断 |
| `厦门_AI_Agent_20K以上岗位明细_2026-08-08.csv` | 可筛选招聘证据 |
| `Harbor_AgentOps_3.2零基础教学清单与面试题解.md` | 四阶零基础课程、6 组公开面经信号、12 道项目题、21 天练习和 10 分钟演示脚本 |
| `Harbor_AgentOps_交付与验收报告.md` | 本文件；验收范围和限制 |

## 2. 已实现的核心闭环

```text
开放事件/密封故障
→ 持久 Run + Job
→ worker lease/heartbeat/fencing
→ PostgreSQL advisory model admission
→ BM25 + Qwen3 Embedding + adaptive RRF
→ 指标/日志/实例 observation
→ 本地 Qwen CompactDraft
→ 服务端 Plan IR 物化
→ 策略编译 + 不可变 plan hash
→ tenant isolation + requester separation + 四眼 quorum
→ tenant-bound 短效 capability
→ 远程持久工具副作用
→ 独立结果验证
→ 自动补偿或人工接管
→ Trace/Audit/Evidence/Eval
```

## 3. 最终机器验收

| 项目 | 结果 |
|---|---|
| Python 编译 | 通过 |
| 后端测试 | 38/38；专用 test stage，真实 PostgreSQL 与远程 fault lab |
| 后端语句覆盖率 | 81% |
| 前端测试 | 16/16 |
| TypeScript | 通过 |
| Vite 生产构建 | 通过；JS 272.59KB / gzip 85.25KB |
| 真实 Embedding | Qwen3 0.6B INT4 / CPU / 1024d；完整 API Run 通过 |
| 检索 calibration | 15条；semantic R@1/R@3/MRR = 1/1/1 |
| 检索 holdout | 15条；semantic R@1 0.9333、R@3 1、MRR 0.9667，与 lexical 持平 |
| 四眼审批 | requester 403；首票不执行；重复票409；第二主体后执行 |
| 租户隔离 | 跨租户 API 404；observer start/cancel 403；工具 tenant 篡改403 |
| 15 例密封夹具 | `EVAL-02E4B19199`，15/15，100 分；危险越权率 0；P95 35ms |
| 真实生成 Qwen | `EVAL-EA80AAEAF4`，3/3，100 分；baseline/Prompt Injection/噪声；P95 77,019ms |
| 浏览器 E2E | `RUN-841A750FB147`、`RUN-E8C59C2EBDFB` 完成；停掉 worker 时新 Job 正确显示 queued/unclaimed |
| 浏览器 console | 0 error / 0 warning |
| 手机 390×844 | 无页面级横向溢出 |
| 并发任务认领 | SQLite 32 线程与 PostgreSQL 16 连接抢 1 job，均仅 1 个成功 |
| lease 恢复 | 实际停止已 claim 的 worker；attempt 2 / fencing #2 接管，旧 token 写入被拒绝 |
| 响应丢失 | commit 后 503；同 key 重放 skipped；副作用 1 次 |
| 伪成功 | 验证失败后补偿至原 2 副本并独立确认 |
| 本机压测 | 2,000 请求/64并发，100%，214.93 RPS，P95 809.92ms |
| 模型容量 | 修复前并发 3 条完成 2 条、1 条 180s timeout；修复后 3/3，排队与推理延迟分离 |
| Compose | 8 容器实启健康；PostgreSQL/pgvector、fault lab、API、3 worker、Nginx、Prometheus |
| Prometheus | backend、宿主模型、fault lab、3 worker 共 6/6 targets up |
| 容器安全 | 自研运行容器全非 root、只读 rootfs、cap_drop ALL、no-new-privileges；生产 API 无 pytest |
| 浏览器安全 | CSP/COOP/CORP 生效；390×844 通过；console 0 error/warning；证据泄漏扫描通过 |

机器可读明细：`docs/validation-evidence-v3.2-2026-08-08.json`、`coverage-v3.2.json`、`retrieval-evidence-v3.1.json`、`semantic-runtime-evidence-v3.1.json`。

## 4. 验收分层

### 已验证

- SQLite 与 PostgreSQL 持久 Run/Job、CAS、lease、`SKIP LOCKED` 与 fencing；
- 远程 HTTP fault lab 的 capability、事务幂等与 oracle 隔离；
- 浏览器完整操作链；
- 新身份渲染前清空旧租户 protected state；跨租户切换不显示旧 Run，也不触发残留任务请求；
- fixture 15 例安全回归；
- 真实 Qwen3 Embedding 的本地 sidecar、完整 API 集成和 15+15 检索评测；
- tenant API 隔离、observer 只读、高风险双主体审批与工具租户绑定；
- V3.2 上 3 个真实生成 Qwen 密封抽样；
- 扩容补偿的执行与独立复查；
- 本机有界混合读压测；
- WSL2/Docker 完整 Compose、3 worker、Prometheus DNS SD、PostgreSQL advisory model slot；
- 非 root/只读容器、生产与测试依赖分层、基础镜像 digest 固定和 Nginx 安全头；
- React 跨运行异步状态隔离及无 worker 浏览器验证。

### 明确不包含

- 企业 OIDC/SSO/SCIM/JIT（已有演示身份下的真实租户隔离和双人审批规则）；
- 真实 Kubernetes/MES/云平台写工具；
- mTLS、Vault/KMS、WORM 审计；
- 大规模领域检索标注、hard negatives、reranker 与在线 A/B；
- GPU 在线推理集群；
- 真实生产事故准确率；
- 跨主机 HA、备份恢复演练、长时间 soak 和完整混沌矩阵；
- 已通过的 SBOM/CVE 发布门禁：Docker Scout 因本机未登录未完成扫描。

## 5. 验收命令

```powershell
# 后端：从 harbor-agentops 根目录构建专用测试 stage
docker build --target test -t harbor-agentops-backend-test -f backend/Dockerfile .
# 运行参数见 README；测试结果为 38/38

# 前端
cd frontend
pnpm test
pnpm typecheck
pnpm build

# 部署配置和实启
cd ..
docker compose config --quiet
docker compose up -d --build --scale worker=3
docker compose ps

# 真实 Embedding 与检索门禁
python scripts\evaluate-retrieval.py
python scripts\validate-semantic-runtime.py

# 真实模型抽样
python scripts\validate-live-model.py --case-limit 2 --case-offset 0

# 本机 API 压测
python scripts\benchmark-api.py --requests 2000 --concurrency 64
```

## 6. 安全提示

Compose 和本机启动脚本包含明确的演示 secret。把服务暴露到非 localhost 之前必须：

1. 替换全部 secret 和演示密码；
2. 关闭 demo mode 和 fault injection；
3. 接入企业身份、mTLS、网络策略和 secrets manager；
4. 把工具账户限制到最小资源范围；
5. 完成备份恢复、跨主机、多 worker soak、SBOM/CVE 与签名发布验收。

## 7. 教学文档结构验收

- 第一阶第 1–5 课只操作、观察和复述业务，显式新术语为 0，也不要求读代码。
- 第二、三阶第 6–31 课各有且只有 1 条显式新术语，共 26 条；每课只增加一个认知点。
- 每一阶都有通关门；未通过时不进入下一阶，专业面试回答全部后置到第四阶。
- 选学区共 10 张卡，每张只新增 1 个高级词；不要求与核心课程同时学习。
- 面试部分只组合已经学过的内容，提供 12 道由浅到深项目题、21 天安排和 10 分钟演示脚本。
- 11 个项目相对链接均已检查，缺失 0；14 个资料引用均有定义，UTF-8 乱码扫描通过。

## 8. 最终交付判断

本交付已经达到“可运行、可测试、可解释、可失败、可恢复、可审计”的求职主项目标准。它最适合证明 20–30K Agent 应用/AgentOps 岗位所需的工程能力；对训练、强化学习、CUDA 和推理内核岗位仍需另一份专门项目。

教学题库不把候选人公开面经冒充企业官方题库。它以重复考察方向为筛选信号，所有项目化回答要么指向当前代码与机器证据，要么明确标成尚未实跑的生产升级设计。
