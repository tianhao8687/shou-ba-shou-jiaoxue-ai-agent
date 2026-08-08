# 本地 Qwen3 Embedding Sidecar

该服务把官方 OpenVINO/Qwen3-Embedding-0.6B-int4-cw-ov 暴露为
OpenAI-compatible /v1/embeddings。模型使用 last-token pooling 和 L2
归一化，输出 1024 维向量；查询指令由 Harbor 检索客户端添加，知识文档不添加指令。

模型文件不进入源码包。先下载到本机目录，再执行：

    & "D:\product-evidence-guard\.venv\Scripts\python.exe" .\local_embedding_service\server.py --model-dir "C:\path\to\Qwen3-Embedding-0.6B-int4-cw-ov" --port 8093 --token "harbor-local-embedding-token"

Harbor 配置：

    EMBEDDING_BACKEND=openai-compatible
    EMBEDDING_ENDPOINT=http://127.0.0.1:8093/v1
    EMBEDDING_MODEL=OpenVINO/Qwen3-Embedding-0.6B-int4-cw-ov
    EMBEDDING_API_KEY=harbor-local-embedding-token

服务拒绝空输入、超长输入、超过 64 条的批次、错误模型名和未授权请求。
调用方还会检查批次索引、向量维度漂移、NaN/Inf、零向量并进行缓存。
