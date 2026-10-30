# 在现有 Paseo 入口调用 Work Assistant

Paseo 是可选入口；本工具不会安装或修改它，也不会接管其他 agent、会话或 schedules。配置好自己登录的模型 CLI、项目工作区和 grants 后，可让某个现有的、由自己选择的 Paseo agent 将本次自然语言请求交给一次性命令。

给入口 agent 的示例说明：

> 工作目录使用 Work Assistant 仓库。用户给出目标项目与本次任务后，以 argv 数组调用 `python3 -m work_assistant ask PROJECT GOAL`，GOAL 是独立参数，不拼 shell。默认只读。如果用户明确要求本次写入，再加入 `--intent write_workspace`；项目必须已经有 grant，你不能添加 grant。仅在用户要求审查/报告时使用 `--review` / `--report-date YYYY-MM-DD`。回传 task_id、status、goal_status 与存在的报告路径。发现 failed、outcome_unknown 或 needs_user 就停止，提供已保存 ID，不自动新建相同任务、resume、重试或切换账号。不发布、不发外部消息、不改其他会话和 schedules。

手动调用等价于：

```sh
python3 -m work_assistant ask my-project '只读检查现有实现，并列出有来源的结论' --routing direct --provider codex
```

带独立审查与本地报告的可选调用：

```sh
python3 -m work_assistant ask my-project '检查当前文档与实现差异' --review --report-date 2026-01-15
```

`ask` 输出的 `task_id` 是后续检查入口。`protocol_status=completed` 不能代替目标实现证明；没有独立审查时 `goal_status` 仍为 unverified。发布始终需要单独的、明确授权的 publish 操作。已有任务应使用 task show/route/run/review/report 等底层命令续接，不再调用 ask 创建第二次执行。
