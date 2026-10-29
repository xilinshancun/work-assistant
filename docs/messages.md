# 各会话直接调用的普通文字消息命令

`python3 -m work_assistant.messages` 是独立的出站命令，复用 Work Assistant 的配置、Store 锁／journal 以及 HTTP／LarkCLI transport。各 Paseo session 可以直接通过共享 Skill 调用，无需主控代转，也无需自行编写 UUID、锁和发送脚本。运行时只用 Python 3.9+ 标准库。此命令不接收用户消息，不创建入站服务、调度器或后台进程；本机发送锁采用既有 POSIX Store 实现。

## 配置与授权

各 session 必须使用同一份明确指定的配置文件及其 `state_dir`，才能共享 request-id 去重和未知结果记录。独立复制 journal 不能提供跨副本去重。配置沿用 `publisher.mode=app`；HTTP 和 `lark_cli` 均支持，Webhook 不支持。LarkCLI 沿用固定 App ID、隔离配置目录、bot 身份和环境白名单，凭证留在 CLI securestore。HTTP 凭证继续使用配置中的环境变量引用。本工具不导出或显示凭证，不改写任何共享接收者／“发给我”绑定。

`send` 必须显式提供 `--receive-id-type chat_id|open_id` 和对应的 `oc_…`／`ou_…` ID。不会从共享 `receive_id_env` 选择收件人，也不会按相似群名推断目标。发布日报的 grants 不授予普通消息自动发送权限：普通消息仍需调用者已有用户授权并显式 `--confirm`。

共享 Skill 应先确认当前用户明确授权了这段文字和该收件人，再调用命令。`--request-id` 必须来自**原始会话与原始逻辑用户请求**，例如原始 session UUID + 用户请求事件 ID。接力、换 worker、重复执行时原样沿用；不得从模型消息正文生成权限、自动换 ID、按当前时间生成新请求来规避 unknown。

下面的命令、ID 和正文均为合成示例。先由调用会话把准备发送的确切 UTF-8 文字保存为私有文件；本工具不会调用模型润色、删改或重新总结正文。确认文件适合交给目标接收者，不要写入凭证。

```sh
python3 -m work_assistant.messages --config private/config.local.json chats

python3 -m work_assistant.messages --config private/config.local.json send \
  --request-id paseo:synthetic-session:original-request-001 \
  --receive-id-type chat_id --receive-id oc_synthetic_group \
  --text-file private/synthetic-message.txt --dry-run

python3 -m work_assistant.messages --config private/config.local.json send \
  --request-id paseo:synthetic-session:original-request-001 \
  --receive-id-type chat_id --receive-id oc_synthetic_group \
  --text-file private/synthetic-message.txt --confirm

python3 -m work_assistant.messages --config private/config.local.json inspect ACTION_ID
```

省略 `--confirm` 即为 dry-run：展示精确目标、原文、哈希和已有状态，不创建网络客户端，不访问认证、不发送，也不创建持久消息动作或私有副本。dry-run 成功不代表真实账号权限或收件人已经验证。私聊时替换为 `--receive-id-type open_id --receive-id ou_synthetic_user`，其余流程相同。

## 去重、回执与恢复

request-id 在共享 journal 内全局绑定原始目标类型／ID 和精确内容哈希，不按项目、日期或 worker 分开。相同 request-id 改目标或正文会拒绝；相同请求已完成则返回原 action_id、message_id 和核验结果，零发送请求。每个动作在任何发送之前生成并保存一个稳定 UUID（36 字符）、模式 0600 的原文字节副本及目标／内容哈希。不会把模型输出字段当成授权。

发送在 request 锁及相同目标／内容的锁内执行：先认证，再持久写入 `sending`，最后才 POST。HTTP／CLI 的明确拒绝为 `failed`；超时、断连、损坏响应、缺 message_id 或发送后的进程中断为 `outcome_unknown`。重启看见残留 `sending` 只恢复成 unknown，不重发。认证前后尚未进入 sending 的失败为 definite failed，因为消息 POST 尚未开始。没有 V1 retry 命令：同一个 failed 请求也不会因重复 confirm 自动重试。

不同 request-id 若目标和内容与已有 preparing／sending／unknown／verification_pending 动作一致，会被阻止，以免自动替换 ID 绕过未知状态。不同内容的新用户请求不能代替对原 unknown 的处理；Skill 必须持续沿用原逻辑请求 ID。用户对一个**已明确未发送的失败**作新授权时，由原始会话单独处理新的逻辑请求，工具不会自动建立它。

POST 返回的 message_id 先落盘，然后 GET 此 ID，只在以下检查全部通过时返回 `completed` + `verification_status=verified`：

- GET 正好包含该 message_id，消息类型为 text，且未被撤回。
- sender 类型为 app、id_type 为 app_id，ID 与原配置应用一致。
- chat_id 与显式 chat_id 收件人一致；open_id 私聊则与 **POST 回执给出的 chat_id** 一致，不从 GET 猜测／补认。
- `body.content` 的 JSON 文字与私有副本逐字一致，包括换行、空格和 Unicode。

这验证的是应用发送者、chat 和消息正文，不表示接收人已读，更不验证文字陈述的业务真实性。open_id 回执缺失 chat_id 时保留已知 message_id 和 verification_pending；不会从 GET 反向“认领”目标。

GET 不可用、字段不符或私有副本被改动时，返回 `verification_pending`，保留真实已知 message_id，绝不重发。使用 `inspect ACTION_ID` 再次只读核对；读取所用应用必须与原动作一致。缺 message_id 的 unknown 保持 unresolved，inspect 不列消息猜测、不补发、不启动 agent。人工调查／新授权应在原会话中单独处理，不应直接修改 journal 来伪造完成。每次读回另存核验观测，当前结果与历史观测可区分。

## 群列表、输出与限值

`chats` 仅分页读取机器人可访问的群，返回精确 chat_id／name；重名群保持不同 ID。不会选群、加入群、发送消息或读取群消息内容。请求采用创建时间升序，重复 ID、缺失／重复分页 token 或超出上限明确报错，不把不完整列表当成完整结果。认证 token 请求／LarkCLI 的 bot 探活仍是所选 transport 的必要前置步骤。

CLI 输出 JSON。退出码：0 为 dry-run、已验证完成或群列表完成；2 为参数／本地状态错误或 definite failed；3 为 unknown／verification_pending；130／143 为人工中断／终止。结果包含 action_id、request_id、UUID、原目标与哈希、真实 message_id（若已知）、chat_id 和 verification_status，不返回原始服务响应、访问 token 或 credential 配置。

- request-id：1～200 个 ASCII 字符，可含字母、数字、点、下划线、冒号、斜线、短横线；必须是原始逻辑请求的稳定键。
- 原文：非空 UTF-8 普通文字，最多 16 KiB，不截断；允许换行／回车／tab，拒绝其他控制字符和已识别的明显凭证。发送为 text JSON，不拼 shell 或模型命令。
- chats：每页最多 100 项，最多 100 页／10000 项。分页名称仅作来源观测，不推断同名目标。
- 原文副本存放于配置的私有 state/messages 中，文件模式 0600；运行数据库、副本、真实消息和回执均不得提交 Git。

协议核对使用[官方发送消息 API](https://open.feishu.cn/document/server-docs/im-v1/message/create)、[获取指定消息](https://open.feishu.cn/document/server-docs/im-v1/message/get)、[群列表](https://open.feishu.cn/document/server-docs/im-v1/chat/list)及[官方 SDK 模型](https://github.com/larksuite/oapi-sdk-go/blob/v3_main/service/im/v1/model.go)。飞书 UUID 的服务端去重窗口不能代替本地持久 unknown 规则。本仓库测试只用合成 HTTP／CLI fixture，包括中断与锁冲突；不代表任何真实租户或用户发送已验收。
