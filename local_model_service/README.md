# 本地 OpenVINO 模型网关

该 sidecar 将本机已有的 `OpenVINO/Qwen3-VL-8B-Instruct-int4-ov` 以文本模式暴露为精简的 OpenAI-compatible `/v1/chat/completions` 接口。

设计约束：

- 权重不复制进项目或 Docker 镜像；
- 默认仅监听 `127.0.0.1:8091`；
- Bearer token 鉴权；
- 请求体最大 2 MiB，输出 token 有硬上限；
- `VLMPipeline` 懒加载并常驻；
- 串行推理，避免 CPU/RAM 被并发请求击穿；
- 日志只记录方法与状态，不记录 Prompt 和输出；
- `/health` 与 `/metrics` 可观测。

当前机器默认使用：

```text
Python: D:\product-evidence-guard\.venv\Scripts\python.exe
Model:  D:\product-evidence-guard\.models\Qwen3-VL-8B-Instruct-int4-ov
Device: CPU
```

启动：

```powershell
.\scripts\start-local-model.ps1
```

换机器时，安装 `requirements.txt`，再通过脚本参数或 `HARBOR_LOCAL_MODEL_PATH` 指定权重目录。健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8091/health
```
