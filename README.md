# Work Assistant / 本地工作助理

复用已安装的 DeepSeek Harness、Claude Code 和 Codex，把任务、执行记录、证据、更正和日报留在本机。Python 3.9+，运行时只有标准库，不需要 pip。MIT 开源。

Local-first personal assistant with two-stage routing, durable task history, attributed evidence, offline reports, and explicitly authorized Feishu publication.

## 五分钟无凭证离线运行

在仓库根目录执行：

```sh
python3 -m work_assistant demo --output out/demo
python3 -m unittest discover -s tests -v
python3 -m compileall -q work_assistant
```

demo 输出 JSON 内的 `report_path` 指向报告目录。用浏览器打开其中 `index.html`，查看五部分 Daily Sync、源数据柱图与证据明细；`card-preview.html` 是飞书原生文字卡片的本地预览。全部文件自包含，无 CDN。demo 的模型和观测均是确定性合成 fixture，不联网、不验证真实提供商、不发消息。重复使用完整 demo 目录会返回原结果；不完整目录请换一个输出位置。

交付文件：`index.html`、`daily-sync.md`、`evidence.html`、`evidence.json`、`card.json`、`document.json`、`card-preview.html`。在不同目录新跑 demo，报告内容哈希相同。状态数据库、工作区和源观测留在输出目录，不提交 Git。

从源码路径只需要 Python 3.9+：克隆本仓库、进入目录，直接执行上述 `python3 -m work_assistant`，不需要虚拟环境、模型凭证或第三方包。

正常安装路径可在隔离虚拟环境中执行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/wa demo --output out/installed-demo
```

安装过程需要 setuptools 构建工具；安装后的运行时仍无第三方依赖。开发者可改用 `pip install -e .`。源码路径在断网机器可直接运行，安装路径是否需联网取决于本机已有构建工具。

提供商均通过已有本机 CLI 选择使用，demo、证据、报告、dry-run 均不需要安装任何模型 CLI。DSH 可选：缺失/不可运行时记录原因并升级到 Codex GPT planner；要在没有 DSH 时使用真实 route，需要已安装并登录的 Codex。Claude 是可选执行器；只安装 Codex 时可固定任务 `--provider codex` 执行，但独立的相反提供商 review 需要 Claude。DSH+Claude 可处理直接路由给 Claude 的任务；需要 GPT 升级而 Codex 不可用时会诚实进入 needs_user。Paseo 完全可选，不是 Python 包依赖，也不需要其 CLI。

## 配置和任务

最短真实使用路径（先安装并登录自己要用的提供商；DSH 可选）：

```sh
python3 -m work_assistant init
python3 -m work_assistant project add my-project --workspace .
python3 -m work_assistant ask my-project '只读检查 README 与当前实现的一致性' --provider codex
```

`ask PROJECT GOAL` 创建一个新任务，复用 task → route → run；输出始终包含已创建的 task_id，遇到 failed/unknown/needs_user 就停止，后续可用 `task show`、`reconcile` 或显式 `run --followup` 继续处理。不会自动发布。需要独立审查时加 `--review`，需要报告时加 `--report-date 2026-01-15`；它们都是可选步骤，前面的步骤不成功就不继续。写入必须显式给出 `--intent write_workspace`，且项目已有对应 grant，ask 不会创建授权。每次 ask 是一次新的用户动作，不会自动重放先前未知运行。

例如完整只读旅程：

```sh
python3 -m work_assistant ask my-project '检查当前文档并报告可复现的问题' --provider codex --review --report-date 2026-01-15
```

下面的底层命令仍可逐步使用，便于检查或续接指定任务。

```sh
python3 -m work_assistant init
python3 -m work_assistant doctor --json
python3 -m work_assistant project add my-project --workspace .
python3 -m work_assistant project list
python3 -m work_assistant task add my-project '只读检查 README 与当前实现的一致性'
python3 -m work_assistant task list --project my-project
```

复制输出中的 task ID，依次调用：

```sh
python3 -m work_assistant route TASK_ID
python3 -m work_assistant run TASK_ID
python3 -m work_assistant review TASK_ID
python3 -m work_assistant task show TASK_ID
python3 -m work_assistant run TASK_ID --followup '再检查刚才结果中的一个具体问题'
```

把 `TASK_ID` 替换为实际 ID。`route` 仅分类，DSH 超时、报错、低置信度、复杂任务、无效 JSON 或调用工具时最多升级一次到 Codex planner；planner 失败进入 needs_user。有效 planner 可执行复杂任务，不能扩大用户授权。默认执行器建议架构/审查交 Claude、实现交 Codex。任务可用 `--provider codex|claude_code` 固定执行器，也仍受项目 grants 约束。

要授权一个独立可写项目，创建时显式给出两个 grant，任务也要声明写 intent：

```sh
python3 -m work_assistant project add writable-project --workspace . --allow read --allow write_workspace
python3 -m work_assistant task add writable-project '在工作区添加合成示例文件' --intent write_workspace
```

编码进程必须有终态成功才标记 completed。写任务完成时仍未 verified；`review` 用相反提供商以只读权限检查实际工作区、结果和受锁保护的 git diff，保留 host git 观测与模型判断。只有结构化 pass 才设置 independently reviewed；这不是科学复现证明。进程仅启动、exit=0 报错、权限拒绝、没有最终结果都不算成功。CLI 的错误、failed、needs_user、unknown 返回 2，中断返回 130。

`protocol_status=completed` 仅说明提供商协议成功结束；`goal_status=unverified` 表示用户目标尚未独立核验。task 的 completed 是执行阶段状态，模型可能只报告“无法读取文件”，仍必须核查实际产物/运行 review，不能据此声称目标已实现。报告会明确显示 `protocol completed; goal unverified`。review 未通过返回 2；SIGTERM 返回 143。Claude 写任务仅允许 Read/Glob/Grep/Edit/Write，不开放 Bash。两个可写项目不能注册相同或互为祖先的工作区。

配置默认 `config.local.json`，状态默认 `.wa/`，均被忽略。`--config PATH` 放在子命令之前。所有配置相对路径以配置文件所在目录为基准，支持 `~`；`project --workspace` 同样遵循该规则。证据命令中的源路径相对当前工作目录。参考 [合成配置](examples/config.example.json)。可配置每个提供商的 argv 数组、env、timeout、model，Codex 的 planner_effort/executor effort；不拼 shell，不自动切账号。显式本地 `CODEX_HOME` 可绑定已登录账号，仓库不包含个人路径或账号设置。

`doctor` 检测安装与本机 help 能力，不测试登录、不消费模型。macOS 自动发现标准 Applications 下的 DSH。实际协议为 DSH headless JSON、CC print JSON、Codex exec JSONL；实现依据本机 help，详见 [设计规范](PLAN.md)。DSH 分类设置 `DSH_PERMISSION_MODE=read-only`，没有向旧版本传不存在的权限 flags。classifier/planner 使用临时空工作目录，仅接收任务分类输入；真实项目路径只交给执行器。具体登录/profile overlay 由本机用户维护，不打印完整配置。安装版 DSH 0.2.1-rc.3 源码确认 session 的 `sessionId`、`status.phase=turn_end` 的 `reason.kind=completed` 和最后 `final.text`；三者中终态成功、非空 final 与 exit 0 缺一即拒绝，失败/截断状态不可忽略。成功 fixture 来自安装包协议源码；已于 2026-10-29 在 macOS 的 DSH 0.2.1-rc.3 桌面账号登录下完成真实路由验证。其他安装环境仍需自行检查认证与版本兼容性。

该安装版可能将一行 `[llm-deepseek-account] model listing from https://api.deepseek.com/models loaded N model(s), skipped N row(s)` 诊断写入 stdout。适配器只过滤这个已实证格式；未知非 JSON 行和损坏 JSON 都拒绝，脱敏原始日志仍保留。Codex 的 item 级开发功能 warning 不等于 turn.failed。

若已登录 DSH 桌面账号，headless profile 却没有凭证服务，可自行创建忽略提交的 `.wa/dsh-desktop-account.yml`：

```yaml
- id: credentials
  disabled: false
- id: authorization
  disabled: false
- id: deepseek-account
  disabled: false
- id: agent-default-model
  config:
    provider: deepseek-account
    model: deepseek-flash
```

这是已安装 0.2.1-rc.3 的桌面账号 overlay 示例；`model` 应与自己的桌面选择一致，示例别名不是应用默认值。只在本地 `config.local.json` 的 `providers.dsh` 中添加 `"patches": [".wa/dsh-desktop-account.yml"]`。patch 路径相对配置文件解析；启动时仅通过 `--patch` 临时覆盖，不更改 DSH 用户配置、不附带账号或 API key。不同安装版本需核对本机 help/服务配置，未登录时仍会明确失败并升级 planner。

进程环境保留用户配置与凭证。若继承的 `PASEO_*` 元数据含非 UTF-8 值，会移除这一无效元数据键并记录 diagnostics，避免 Rust CLI 在枚举环境时 panic；其他无效用户环境会在启动前明确报错，不静默改变授权或凭证。

所有 Codex 调用（分类、执行、跟进）都先以相同 argv 基础配置、环境和 cwd 运行有界的 `mcp list --json`。仅接受 `[A-Za-z0-9_-]+` 名称，按安全的 `transport.type` 使用无引号路径和完整 inline table：stdio 使用 `{command="wa-mcp-disabled",enabled=false}`，streamable_http 使用 `{url="http://127.0.0.1:9/wa-disabled",enabled=false}`；未知类型拒绝。再带覆盖参数执行一次 list，名称集合与 transport 类型必须一致且全部 disabled。doctor 也走相同的复核路径。无此命令、超时、截断、畸形输出、不合规名字或复核失败都阻止 exec，且 `launched=false`；不降级绕过。预检两次合计最多 10 秒、每路输出 128 KiB、512 个服务器；仅提取 enabled 与 transport.type，不保存原 command/url/env/auth，不改全局配置。检测到本机 Codex 时，离线测试会用隔离的合成 stdio/HTTP 配置运行真实 list 验证覆盖语法，不调用模型或 MCP 工具。

每次 Codex 调用同时覆盖 `features.apps=false`、`web_search="disabled"`、`sandbox_workspace_write.network_access=false`，不修改全局配置，保留模型自身连接和订阅认证。V1 暂不提供外部连接器调用，真实业务研究需要用户后续明确扩展权限；read/write grant 不隐含外部应用写入。配置键参考 [官方配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)。

## 证据、更正与报告

```sh
python3 -m work_assistant evidence add my-project examples/sample-observation.json --scope 'synthetic local example' --origin 'operator fixture'
python3 -m work_assistant claim add my-project '样本数为 10' --evidence EVIDENCE_ID --attribution 'fixture operator' --numeric '{"key":"samples","value":10,"unit":"items"}'
python3 -m work_assistant claim supersede OLD_CLAIM_ID --replacement NEW_CLAIM_ID --reason '去重后更正'
python3 -m work_assistant claim retract CLAIM_ID --reason '源观测已撤回'
python3 -m work_assistant report my-project --date 2026-01-15
python3 -m work_assistant publish REPORT_ID --dry-run
```

`EVIDENCE_ID`、`CLAIM_ID`、`REPORT_ID` 替换为输出 ID。数字明确与源 JSON `values` 的 value/unit 比对；文本中出现相同数字不等于验证。缺乏数字时不生成虚构百分比。手工等级可选 measured/reproduced/derived/code_observed/reported，并始终保留 attribution 和校验范围。derived 需要 `--derivation '公式' --input CLAIM_ID`；程序检查输入链存在、有效，不执行公式。源文件精确复制、哈希校验，原文件不变。报告先递归脱敏字符串再序列化，带引号和反斜杠的模型文本不会破坏 JSON。

`evidence ingest` 只读取配置 `ingest_paths` 中列出的精确文件，不改其他项目的源数据。报告基于有上限的一致性快照，当前实现每个快照最多 1000 条任务/结论/更正/证据，原始文件每份最多 20 MiB，观测预览最多 32 KiB。所有已知事实按新到旧排列；日期是报告归档周期，既有任务与跨周期更正继续显示，并非只截取当天事件。游标记录成功快照的收集边界；新快照保留完整的当前事实和更正，旧快照不可变。文件全部写好后才提交快照/游标；发布失败不影响收集。没有来源的责任人、截止期或承诺不会被自动添加。

## 导入已有的本地日报

已有收集器可继续生成自己的日报，使用 `import-daily` 将一个显式指定的 bundle 导入当前项目。此命令只生成普通的不可变 report，不调用模型、不联网、不发送消息，不修改源文件，也不创建或调整 schedule。下面是仓库内的纯合成示例，可直接离线运行：

```sh
python3 -m work_assistant init
python3 -m work_assistant project add imported-demo --workspace .
python3 -m work_assistant import-daily imported-demo --source-root examples/daily-bundle --date 2026-01-15 --timezone Asia/Shanghai
python3 -m work_assistant publish REPORT_ID --dry-run
```

将 `REPORT_ID` 换成导入结果的 `id`。真实使用时由用户显式指定自己的 bundle 目录和当天日期；`--timezone` 默认 `Asia/Shanghai`。历史归档可显式指定过去日期，不能把旧执行日期冒充今天。重复示例时使用已有项目即可。源 bundle 的结构见 [合成 state](examples/daily-bundle/state.json)、[合成 evidence](examples/daily-bundle/evidence/daily-2026-01-15.json) 和 [合成 HTML](examples/daily-bundle/daily/2026-01-15.html)。

bundle 根目录的 `state.json` 提供 `timezone` 以及 `daily.last_report`、`daily.last_evidence`、`daily.last_successful_execution_date`、`daily.covered_until`。两个文件路径在解析符号链接后必须仍位于 bundle 内。evidence 要求 `report_type=daily`，其 `execution_date`、state 的成功执行日期与 `--date` 三者一致；两处 timezone 与命令一致；覆盖区间起点早于终点，state/evidence 的终点字符串一致，不允许未来的终点或执行日期。区间时间戳须有时区偏移，可使用 UTC `Z`。必须提供 `validation.status=passed`，并让 `validation.report_sha256` 等于源 HTML **原始字节**的 SHA-256。缺失、过期、未通过校验、哈希不符或读取期间发生变化均明确报错，不能退回旧日报。

HTML 使用 body 中的 h1 标题、日期／覆盖说明，以及唯一的 `id="sync-summary"` 短摘要。导入器用标准库 HTMLParser 提取正文、段落、列表与折叠 details 中的可见文字，排除 head、script、style、footer、导航和隐藏元素；不执行或复制任意源 HTML、链接和属性。重建后的 HTML 自包含且转义所有文本。卡片保留源标题、证据中的覆盖区间和短摘要；文档保留全文，并附证据摘要和局限。应用模式发布时由现有发布器添加文档按钮。源陈述明确标注 reported，源提供的 verified_at 仅作为来源字段展示，不升级为独立测量。

evidence 的 `items` 每项含 `id`、`claim`、`source_ids`，引用 `sources` 的 `id`；每个 source 含 `summary`、`verified_at`；`limitations` 为文字列表。只将这些相关字段脱敏后写入七个标准产物，额外元数据保留在私有 journal，勿提交或直接分享 journal。标题最多 200 字符，短摘要最多 6000 字符；源摘要／结论／局限每项最多 2000 字符，证据摘要最多 64 KiB、共 1000 项；state 最多 1 MiB，源 HTML／evidence 各最多 20 MiB。完整文档超过 1000 个原生文字块时明确拒绝，不会静默截断详情。

同一输入字节、日期和元数据返回同一个报告 ID／哈希；已校验的同日报修订生成新快照，旧快照保持不可变。导入不收集本项目之前的任务和结论，也不推进普通 report 的收集游标。后续 `publish`／`reconcile` 继续使用现有授权、checkpoint 和未知结果规则：相同内容 no-op；同目的地＋项目＋日期的修订复用文档／消息 ID；未知结果不能因再次导入而重发。定时流程只需在原收集器结束后显式调用 import-daily，导入成功后再按既有授权发布；导入失败就回报原因。

## 飞书预览和真实发布

dry-run 不需要凭证也不联网。真实自建应用模式需要设置配置所引用的 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_RECEIVE_ID`；可选 `FEISHU_FOLDER_TOKEN` 指定应用可编辑的文件夹。应用必须拥有文档创建/编辑及向该接收者发消息的权限，机器人需在目标群/用户可见范围内，报告文档应由本工具独占编辑。秘密仅放环境变量；不要写入仓库。

```sh
python3 -m work_assistant publish REPORT_ID --confirm
```

每位使用者需要在自己的飞书租户建立自建应用或 Webhook，使用自己有权限的文档空间和目的地；仓库不附带共享应用、租户、机器人或账号。先用自己专用的测试目的地验收。没有配置时明确报错。本开发实现未向真实飞书发送；本地 HTTP server 验证 API 协议和故障状态。应用模式创建/更新 docx 文档块和 interactive 卡片，保存文档、块、消息 ID。相同目的地+项目+周期复用资源；相同内容无请求；变更内容更新已有资源。单个 CLI 对真实 API 节流，服务端应用错误仍立即失败。Webhook 设置 `publisher.mode=webhook` 并配置 `FEISHU_WEBHOOK`，只发送卡片，文档留本地；Webhook 没有消息更新能力，已发布周期的新内容会明确拒绝，需改用应用模式。

精确预配置目的地 grant 使用 `app:<sha256([app_id,receive_id_type,receive_id,folder_token])>` 或 `webhook:<sha256(webhook_url)>`，序列化规则为 UTF-8、排序 key、紧凑 JSON，与代码 `util.digest` 一致。grants 默认空；通常使用逐次 `--confirm` 即可，模型输出不能建立 grant。

官方 API 参考：[租户 token](https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal)、[创建文档](https://open.feishu.cn/document/server-docs/docs/docs/docx-v1/document/create)、[创建块](https://open.feishu.cn/document/server-docs/docs/docs/docx-v1/document-block/create)、[更新块](https://open.feishu.cn/document/server-docs/docs/docs/docx-v1/document-block/patch)、[发送消息](https://open.feishu.cn/document/server-docs/im-v1/message/create)、[更新卡片](https://open.feishu.cn/document/server-docs/im-v1/message/patch)。

### 可选：使用官方 lark-cli securestore

默认 `publisher.transport=http` 继续使用上述环境变量凭证；Webhook 行为不变。若凭证已由[官方 lark-cli](https://github.com/larksuite/cli) 管理，可使用 `lark_cli` transport（仅应用模式），无需向 Work Assistant 提供 `FEISHU_APP_SECRET` 或 tenant token。CLI 由使用者另外安装，Python 包仍没有运行时依赖。

从 [config.lark-cli.example.json](examples/config.lark-cli.example.json) 复制配置。先在自己租户按官方流程注册应用；如下隔离目录由 CLI 保存配置及 securestore 引用，请保留，不要提交到 Git：

```sh
mkdir -p private-lark-cli
chmod 700 private-lark-cli
LARKSUITE_CLI_CONFIG_DIR="$PWD/private-lark-cli" lark-cli config init --new --brand feishu --name work-assistant
LARKSUITE_CLI_CONFIG_DIR="$PWD/private-lark-cli" lark-cli --profile work-assistant config show
```

把脱敏 `config show` 的 `appId` 填入 `expected_app_id`。将示例保存为 `config.local.json`，按自己的 CLI profile 设置 `cli_profile`（可以省略以选择该隔离配置的默认应用），设置目的地 `FEISHU_RECEIVE_ID`，必要时设置自己的 `FEISHU_FOLDER_TOKEN`。应用仍须拥有文档、消息及机器人权限，且能访问目标文档空间和接收人。接收人、群及文件夹属于使用者自己的租户；本项目不提供共享机器人。

```sh
python3 -m work_assistant doctor
python3 -m work_assistant publish REPORT_ID --dry-run
python3 -m work_assistant publish REPORT_ID --confirm
```

`cli_argv` 是可信本地命令数组，支持包装命令或 CLI 的绝对路径；`cli_config_dir` 和命令首项的相对路径均相对 Work Assistant 配置文件解析。`cli_timeout` 默认 30 秒，上限 300 秒。doctor 只调用本地脱敏 `config show`，显示 CLI 是否可用、App ID 是否匹配；不代表权限或真实发布已通过。每次实际发布先再次核对选中应用和 Feishu 品牌，再以 bot 身份只读探活，随后才写入待发送请求。CLI 子进程使用完整环境白名单，排除继承的 Lark/Feishu 凭证、授权代理、profile、其他宿主 workspace，以及普通代理变量，避免误用旧机器人。需代理的部署应先自行核验网络环境；不要把 token 放进 `cli_argv`。

可选文档访问配置同时适用于 HTTP 和 lark_cli：`document_member_env` 引用一个用户明确批准的本人 open_id 环境变量（示例为 `FEISHU_DOCUMENT_MEMBER`），`document_member_perm` 默认 `edit`，也可在可信本地配置中选择 `full_access`。未配置 `document_member_env` 默认不分享；不需要分享时请删除示例中的该字段。配置后必须提供有效的 `ou_…` 用户 ID；不会给群或公开链接授权。创建文档后通过 `docs:permission.member:create` 权限添加此成员，关闭通知，并在发卡片前保存授权 checkpoint；同一成员和权限不会重复 grant。变更成员或权限会使用新的发布身份和文档，不会把旧文档静默分享给新成员，也不会自动撤回旧授权。

应用模式发送的卡片自动增加“打开 Daily Sync”原生按钮，指向 `https://www.feishu.cn/docx/{document_id}`。此标准入口采用[官方 CLI 的资源 URL 规则](https://github.com/larksuite/cli/blob/main/shortcuts/common/resource_url.go)，由飞书跳转至文档所属租户；打开后仍需具备文档访问权限。本地报告快照保持不变；Webhook 只有本地文档，不添加云文档按钮。

升级前已完成的同内容报告仍保持 no-op，不因新增按钮而自动改写旧卡片；下一次报告内容变化时再正常更新已有资源并添加按钮。

两个 transport 共用发布和 reconcile 状态机。CLI query 通过 `--params` 传入，JSON body 通过标准输入；原始 CLI 输出和凭证不会进入报告或错误记录。明确的认证、输入、权限拒绝记为 failed；写请求超时、进程中断、5xx 或损坏输出保留 unknown，禁止自动补发。认证探活失败发生在发布写入之前，可检查后显式 `--retry`。同内容仍无请求，变化内容更新已知文档和消息；换 transport 时应用与目的地必须保持一致。离线 FakeCLI 覆盖此协议和故障流程，不代表真实租户验收；live 权限和产物由使用者另行检查。

## 未知结果与恢复

```sh
python3 -m work_assistant reconcile RUN_ID
python3 -m work_assistant reconcile RUN_ID --resolution failed --reason '已人工检查工作区，准备单独重试'
python3 -m work_assistant route TASK_ID --no-cache
python3 -m work_assistant reconcile PUBLICATION_ID
python3 -m work_assistant reconcile PUBLICATION_ID --resolution retry_authorized --reason '已确认未创建卡片，并检查了文档 ID'
python3 -m work_assistant publish REPORT_ID --confirm --retry
```

先读 `task show` 的运行记录或 publish 返回的 publication_id。reconcile 不继续 agent，不补发卡片。写进程已启动但未成功结束，包括部分编辑后的权限拒绝，均进入 unknown；明确没有启动的二进制错误才可直接 failed。Codex 中间命令失败可以由该 turn 恢复，最终 `turn.completed` 成功仍有效；顶层 error/turn.failed 则拒绝。运行 unknown 只允许有理由的 completed/failed/cancelled 人工处理；该完成记录不可代替真实独立 review。中断的 routing 可用 `reconcile TASK_ID` 检查无活进程后恢复为 needs_user，随后显式 route。SIGTERM 会清理所拥有的子进程并保存未知状态；报告只有 OS 锁、整目录原子落盘，被 SIGKILL 后可重新生成，不需要人工解除持久 lease。获取飞书 token 失败发生在写请求之前，记录 failed，可显式 `--retry`；实际发送后的模糊结果仍为 unknown。发布核对读取已知远端 ID 的元信息与内容；缺失 ID 时请人工核对目标端，用 `--remote-ids '{"document_id":"...","message_id":"...","block_ids":["..."]}'` 提供已查到的对应资源，再决定 completed/retry_authorized/cancelled。不要凭没有本地 ID 推断远端没有效果。

文档分享请求结果未知时，reconcile 的文档 GET 不能证明成员权限已生效，也不会额外申请读取成员权限的 scope。必须人工在飞书核对当前配置的用户和权限。确认已授权后使用精确 JSON 布尔值 `true`，保留配置中的成员、权限和目的地一致：

```sh
python3 -m work_assistant reconcile PUBLICATION_ID --resolution retry_authorized --reason '已人工确认当前配置用户拥有指定文档的 edit 权限' --remote-ids '{"document_member_granted":true}'
python3 -m work_assistant publish REPORT_ID --confirm --retry
```

此确认会保存由可信配置计算的成员指纹和 `operator_confirmed` 来源，并审计理由；它是人工确认，不等于独立自动验证。未确认时不能继续或重发未知分享，即使先取消发布也一样。确认后继续剩余发布，后续报告更新不重复授权。仅当整条发布也已人工确认完成时才选 `completed`，必要时同时提供已查到的远端 ID；该路径同样保留成员确认，后续更新不会再次分享。

若人工确认授权未生效，可改用 `--remote-ids '{"document_member_granted":false}'`，仅配合 `retry_authorized` 和非空理由，保持原成员、权限、目的地及已知文档 ID；这会记录 `operator_confirmed_absent` 而非已授权，之后仍须显式 `publish --confirm --retry` 才重发一次授权，不能以 `completed` 或 `cancelled` 绕过。

## Paseo 集成与验证范围

不新增 scheduler/daemon，不修改现有 schedules。把已有 Paseo schedule/agent 的工作目录设为本仓库，运行上述一次性 CLI 即可。例如已有每日流程运行 `python3 -m work_assistant report my-project --date YYYY-MM-DD`，读取报告后以 dry-run 检查；若未配置精确发布 grant，则留待用户显式 publish。给调度 agent 的约束示例：“只运行指定项目 report 命令，回传 report ID/路径；发现 unknown 就回报，不自动 resume/retry，不发外部消息。”写任务由用户先建好并授权，调度 agent 只操作明确给出的 task ID。

自然语言入口可按 [Paseo 一次性调用示例](examples/paseo-entry.md) 调用 ask。这只是接入现有 agent/schedule 的操作说明，不代表本工具已接管 Paseo 的会话或调度。

离线 unittest 覆盖严格路由、工具调用升级、一次规划失败、意图/授权不扩大、三种事件解析、进程终止/输出上限、并发与 fencing、证据链和更正、确定性导出、HTML/秘密处理、HTTP 200 应用报错、分步 checkpoint、unknown 核对与 create/update/no-op。测试只监听回环地址，不联系真实服务。macOS/Linux 支持本地任务锁；Windows 暂不支持实际并发运行。真实登录、模型账号权限、安装版本差异和飞书权限需要用户在隔离合成工作区自选进行 live 冒烟，不属于离线通过的证明。

更多约束见 [SECURITY.md](SECURITY.md)、[PLAN.md](PLAN.md)。

## 已验证的实际链路

2026-10-29 在隔离合成 Git 工作区验证了：DSH 简单任务直接路由 → Codex 实际修改指定文件 → Claude Code 独立只读审查 → 本地 Daily Sync 与证据输出；由主控再次核对文件精确内容。另一个复杂任务实际触发一次 DSH → Codex GPT planner 升级并选出 Claude 执行器，该测试只分类，没有执行假设的架构任务。使用 Claude Code 2.1.303 和 Codex 0.164.0-alpha.11.1，复用本机订阅登录。真实飞书仅核验了认证与现有权限，尚未验证创建文档或发送/更新消息；离线 HTTP 测试不替代租户内验收。
