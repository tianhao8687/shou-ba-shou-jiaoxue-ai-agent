# 本地 Qwen3 Embedding Sidecar

该服务把官方 OpenVINO/Qwen3-Embedding-0.6B-int4-cw-ov 暴露为
OpenAI-compatible /v1/embeddings。模型使用 last-token pooling 和 L2
归一化，输出 1024 维向量；查询指令由 Harbor 检索客户端添加，知识文档不添加指令。

模型文件不进入源码包。推荐放在仓库的
`.models/Qwen3-Embedding-0.6B-int4-cw-ov`，再执行：

```powershell
.\scripts\start-local-embedding.ps1 `
  -Python ".venv\Scripts\python.exe" `
  -ModelDir ".models\Qwen3-Embedding-0.6B-int4-cw-ov"
```

脚本也支持 `HARBOR_PYTHON`、`HARBOR_LOCAL_EMBEDDING_PATH`、
`HARBOR_LOCAL_EMBEDDING_DEVICE` 和 `HARBOR_EMBEDDING_GATEWAY_TOKEN`。
相对路径按仓库根目录解析，`-ValidateOnly` 可在不加载模型时验证配置。
Linux/macOS 可直接执行：

```bash
python local_embedding_service/server.py --model-dir /path/to/Qwen3-Embedding-0.6B-int4-cw-ov
```

Windows 执行策略阻止 `.ps1` 时，可使用单次进程方式，不修改全局策略：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-local-embedding.ps1 -ValidateOnly
```

Harbor 配置：

```dotenv
EMBEDDING_BACKEND=openai-compatible
EMBEDDING_ENDPOINT=http://127.0.0.1:8093/v1
EMBEDDING_MODEL=OpenVINO/Qwen3-Embedding-0.6B-int4-cw-ov
EMBEDDING_API_KEY=harbor-local-embedding-token
```

服务拒绝空输入、超长输入、超过 64 条的批次、错误模型名和未授权请求。
调用方还会检查批次索引、向量维度漂移、NaN/Inf、零向量并进行缓存。
