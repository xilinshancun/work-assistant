---
name: work-assistant-feishu
description: 通过本地 Work Assistant 在飞书发送文字或展示卡片、交付固定文档日报、查看本人私聊或群内 @ 助理的处理状态。适用于有明确收件人、需要核对发送身份和回执的任务。
---

# 工作助理飞书分身

使用用户已配置的 Work Assistant、同一个私有配置文件和同一份 journal。下文的 `WA_CONFIG` 表示该配置文件的绝对路径。保留应用、本人、固定文档和群的现有绑定。只要求拟稿、润色或在当前会话预览时，直接交付草稿。

| 用户请求 | 入口 |
| --- | --- |
| 给具体对象发文字、卡片或引用回复，检查已发消息 | [消息、身份与回执](references/messages.md) |
| 同步固定日报文档，发测试卡或已确认的正式卡 | [日报交付](references/daily.md) |
| 启用或检查本人私聊和测试群里的 @，查看深度任务的答复 | [入站与验收](references/inbound.md) |

发送前确定身份、目标和精确内容。每个逻辑请求只分配一次 request-id。跨会话交接时传递原 request-id、action/publication ID、目标和内容哈希，从共享 journal 继续。报告结果时区分服务端返回的 message_id、GET 核对的内容和接收人已读。
