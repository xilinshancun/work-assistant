# First real task on synthetic input / 用合成项目跑第一个真实任务

Run every Work Assistant command below from the repository root with Python 3.9+.
The [practice project](first-project/README.md) contains no private data. You need
your own installed and signed-in Claude Code CLI, or Codex if selected instead.
No account is included. Nothing starts a model automatically when these files
are downloaded, imported, or counted locally.

以下 Work Assistant 命令均在仓库根目录运行。练习输入全为合成数据；需要使用者
自己已安装、已登录的 Claude Code 或 Codex，不附带账号，也不会自动启动模型。

## Installed package / 已安装的发行版

You do not need a clone for the practice files. From your assistant directory:

安装后不必克隆仓库，在自己的工作助理目录执行：

```sh
wa example --output practice-project
wa init
wa doctor --json
wa project add first-project --workspace ./practice-project
```

Use `wa` in place of `python3 -m work_assistant` in the remaining steps. The
output directory must be new. For manual verification, run
`python3 practice-project/count_records.py practice-project/records.txt`.
The source-checkout setup below is an alternative, not an additional step.

后续命令将 `python3 -m work_assistant` 替换为 `wa` 即可。输出目录必须是新的。
下方源码注册方式是另一种选择，无需重复注册。

## Set up a read-only project / 注册只读项目

```sh
python3 -m work_assistant init
python3 -m work_assistant doctor --json
python3 -m work_assistant project add first-project --workspace examples/first-project
```

Doctor checks local interfaces, not account access. Project creation defaults to
`read`; no write grant or independent-review dependency is added. Reuse the
existing project if you already registered it.

doctor 不测试登录或模型访问。项目默认只有 read 授权，不创建 write grant；
如果已经注册同名项目，直接复用即可。

## Explicitly invoke your model / 显式调用自己的模型

**The next command invokes your own model CLI and service. This is a real task
on synthetic input, not the offline evaluator.** It may send the example files
to your configured model service. Direct routing selects the named executor;
this example does not request `--review` or file edits.

**下一条会调用自己的模型服务，不是离线 eval。** direct 显式选择执行器，
只读检查合成项目，不请求独立 review，也不要求改文件。

```sh
python3 -m work_assistant ask first-project '只读核对README与实现，给出文件依据和实测限制' --routing direct --provider claude_code
```

To use Codex, replace only `--provider claude_code` with `--provider codex`.
使用 Codex 时，将最后的执行器参数替换为 `--provider codex`。

## Inspect and report / 查看原任务并生成报告

Keep the returned `task_id` and replace `TASK_ID` below. Replace `YYYY-MM-DD`
with the archive date you intend to use:

保留返回的 task_id，替换下方占位符，并填写归档日期：

```sh
python3 -m work_assistant task show TASK_ID
python3 -m work_assistant report first-project --date YYYY-MM-DD
```

The report directory is `data.path` in the report result; open its `index.html`.
The date labels a snapshot of current project facts, not a filter for that day's
events. If the task is unresolved, inspect its original ID rather than repeatedly
creating new asks. These commands do not publish messages.

报告目录见输出 data.path，打开其中的 index.html。日期用于归档当前事实，
并非仅筛选当天事件。遇到未解决状态先检查原任务，不重复 ask；这里不会发送消息。

## Human verification / 人工核验要点

- The intentional claim is **3** nonempty records. The implementation counts
  **5** total lines: three nonempty lines plus two blank lines.
  有意不一致是：文档说忽略空白行得到 **3**，实现统计全部行得到 **5**。
- Reproduce without a model from the repository root:
  `python3 examples/first-project/count_records.py examples/first-project/records.txt`.
  无需模型即可人工运行此命令复现 **5**，输入文件保持不变。
- Require references to README, `count_records.py`, and `records.txt`. If the
  model did not run the program, its result must be labeled code inference, not
  measured execution. Read-only tools may not permit Python execution.
  回答应给出文件依据；没有执行证据时应标“代码推断”，不能声称“实测通过”。
- Protocol completion is not independent goal verification. Finding this small,
  disclosed mismatch does not establish model quality or business correctness.
  协议完成不是独立核验；发现这处已声明的合成差异不证明模型质量或业务正确性。
