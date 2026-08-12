# 本地 OpenVINO 模型网关

该 sidecar 将本机已有的 `OpenVINO/Qwen3-VL-8B-Instruct-int4-ov` 以文本模式暴露为精简的 OpenAI-compatible `/v1/chat/completions` 接口。

设计约束：

- 权重不复制进项目或 Docker 镜像；
- 默认仅监听 `127.0.0.1:8091`；
- Bearer token 鉴权；
- 请求体最大 2 MiB，输出 token 有硬上限；
- `VLMPipeline` 懒加载并常驻；
- 单执行槽串行推理，避免 CPU/RAM 被并发请求击穿；
- 等待队列有明确上限和超时：队列满返回 429，等待超时返回 503，并带 `Retry-After`；
- 日志只记录方法与状态，不记录 Prompt 和输出；
- `/health` 只表示网关进程存活，模型真正加载后 `/ready` 才返回 200；
- `/metrics` 暴露 active、queued、reject、queue timeout、排队与生成耗时。

模型文件不进入 Git。推荐放在仓库的 `.models/Qwen3-VL-8B-Instruct-int4-ov`，Python 依赖推荐安装到仓库自己的 `.venv`。这两个目录都不会被提交。

PowerShell 启动：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r local_model_service\requirements.txt
.\scripts\start-local-model.ps1 `
  -Python ".venv\Scripts\python.exe" `
  -ModelPath ".models\Qwen3-VL-8B-Instruct-int4-ov"
```

脚本接受绝对路径，也接受相对仓库根目录的路径；所以从其他工作目录调用时结果不变。也可以设置 `HARBOR_PYTHON`、`HARBOR_LOCAL_MODEL_PATH`、`HARBOR_LOCAL_MODEL_DEVICE`、`HARBOR_MODEL_GATEWAY_TOKEN`、`HARBOR_MODEL_MAX_PENDING_REQUESTS` 和 `HARBOR_MODEL_QUEUE_TIMEOUT_SECONDS`。先用 `-ValidateOnly` 可只检查运行资产而不启动服务。

如果 Windows 执行策略阻止 `.ps1`，可用下面的单次进程命令；它不会修改机器的全局策略：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-local-model.ps1 -ValidateOnly
```

Linux/macOS 或不使用 PowerShell时，可直接运行：

```bash
python local_model_service/server.py --model-path /path/to/Qwen3-VL-8B-Instruct-int4-ov
```

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8091/health
Invoke-RestMethod http://127.0.0.1:8091/ready
Invoke-RestMethod http://127.0.0.1:8091/metrics
```

默认容量是“1 个正在生成 + 最多 2 个等待”，等待预算 30 秒。它的目标是让上游尽快看到过载，而不是让 HTTP 线程无限堆积；这不会提升模型吞吐。
