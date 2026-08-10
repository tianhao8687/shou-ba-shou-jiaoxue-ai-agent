# V3.1 数据边界

- `eval_cases_v4.json`：当前 105 项密封对抗评测定义。21 类共享变体覆盖事件文本、编码混淆、上下文过载和工具输出注入；隐藏 oracle 只在隔离实验结束后核对。
- `eval_cases_v3.json`：保留的 15 项 V3 基线，用于回归结果对照；运行时不再默认加载。
- `retrieval_eval_v3_1.json`：15 条检索融合校准样本，只用于调权与错误分析。
- `retrieval_eval_holdout_v3_1.json`：15 条独立留出样本，只用于最终 non-regression gate，不能反向调参。
- `knowledge/`：当前运行手册与安全/验证控制文档。

V2 的 `scenarios.json`、`eval_cases.json` 和 `eval_matrix.json` 已移除。它们把预期根因、计划或结果放在同一项目数据流里，不符合 V3 的答案隔离要求，也没有任何 V3 代码引用。
