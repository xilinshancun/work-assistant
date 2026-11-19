# 消息、身份与回执

默认用 bot。用户明确要求以本人身份发送时，使用已绑定本人的 `--identity user` 和官方 Lark CLI 配置。群目标使用已核实的 chat_id，私聊目标使用 open_id。`chats` 列出 bot 可见的群，本人可访问的群可能不同。文字或卡片保存为一个私有 UTF-8 文件，最大 16 KiB。

以下命令中的变量由调用会话从已核实的请求填写。已有发送授权时，将同一条命令的 `--dry-run` 换为 `--confirm`。要发文字，将 `--card-file` 换为 `--text-file`。

```sh
python3 -m work_assistant.messages --config "$WA_CONFIG" send \
  --identity bot --request-id "$REQUEST_ID" --receive-id-type chat_id \
  --receive-id "$CHAT_ID" --card-file "$CARD_FILE" --dry-run
python3 -m work_assistant.messages --config "$WA_CONFIG" inspect "$ACTION_ID"
```

- **引用回复**使用 bot 身份、原 chat_id 和 `--reply-to-message-id`。用户指定话题时加 `--reply-in-thread`。以回执里的实际位置为准，位置差异不会触发补发。
- **更新已发 bot 卡片**使用 `update-card` 和已核验的原 action ID。本人发送的卡片如需发修订稿，是发往明确目标的新消息。
- **回读**核对发送身份、目标与文字或卡片可读内容。GET 可能拆分 Markdown 文本，按钮 URL 和视觉样式以冻结的请求、单独的界面核验为准。
- **恢复**时保留原请求。已有 message_id 的 `verification_pending` 使用 `inspect`。结果未知且缺少 ID 时，记录阻塞，不能换键补发。只有明确记录为发送前认证失败、请求从未发出的原 `send`，才可带依据使用 `--retry-preflight --reason` 预览并显式确认。

支持的卡片格式、表情、更新窗口和人工解除条件见[消息命令](https://github.com/xilinshancun/work-assistant/blob/main/docs/messages.md)。
