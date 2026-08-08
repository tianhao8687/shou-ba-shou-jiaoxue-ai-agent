# V3.1 数据边界

- `eval_cases_v3.json`：当前密封评测定义。运行时只读取事件变体；隐藏 oracle 由评测器在隔离实验结束后核对。
- `retrieval_eval_v3_1.json`：15 条检索融合校准样本，只用于调权与错误分析。
- `retrieval_eval_holdout_v3_1.json`：15 条独立留出样本，只用于最终 non-regression gate，不能反向调参。
- `knowledge/`：当前运行手册与安全/验证控制文档。

V2 的 `scenarios.json`、`eval_cases.json` 和 `eval_matrix.json` 已移除。它们把预期根因、计划或结果放在同一项目数据流里，不符合 V3 的答案隔离要求，也没有任何 V3 代码引用。
