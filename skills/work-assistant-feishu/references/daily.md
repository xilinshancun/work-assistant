# 固定文档日报

沿用项目已核实的来源、日期、时区、固定文档、发送身份和收件群。使用已有调度时，从本次运行或交接记录取得原始请求键。仅同步文档或准备草稿时，完成相应步骤即可。

1. 用 `report` 或 `import-daily` 取得当天的不可变报告，核对日期和源哈希。源事实变更时从校验通过的源重新导入。保留工作进展中的限定和未完成事项。
2. 在已绑定的 app timeline 上用 `--document-only` 预览和同步。检查回执，再用 `reconcile` 回读固定文档。核对当天章节和完整正文。遇到历史节点差异或未知结果，沿用原 publication 排查。
3. 文档回读通过后再生成带文档按钮的卡片。随 skill 提供的脚本读取本地 journal 快照，核对报告、项目、绑定和文档 ID，输出 card.json、SHA-256 和清单。它不调用飞书。已有单独定稿的卡片时，核对其文档按钮并保留原始字节。
4. 使用已授权身份向测试群发送，保存 action、message_id 和回读结果。采用逐稿确认流程时，正式发送须有对当天版本和目标的明确确认。使用相同的卡片字节和属于正式目标的独立请求键。正文或按钮发生变化后，按新版本核对确认范围。

```sh
python3 -m work_assistant --config "$WA_CONFIG" publish "$REPORT_ID" --document-only --dry-run
# 已获文档更新授权时执行
python3 -m work_assistant --config "$WA_CONFIG" publish "$REPORT_ID" --document-only --confirm
python3 -m work_assistant --config "$WA_CONFIG" reconcile "$BINDING_ID"
```

用已安装当前仓库源码的 Python 运行；`SKILL_DIR` 指向含 SKILL.md 的目录。仅从源码运行时，在仓库根目录为该命令设置 `PYTHONPATH=.`。

```sh
python3 "$SKILL_DIR/scripts/prepare_daily_card.py" --config "$WA_CONFIG" \
  --binding-id "$BINDING_ID" --document-id "$DOCUMENT_ID" "$REPORT_ID"
```

文档未同步时保留草稿，不发带占位按钮的测试卡或正式卡。固定文档与卡片发送使用独立回执。[日报导入](https://github.com/xilinshancun/work-assistant/blob/main/docs/guide.md) · [文档树与恢复](https://github.com/xilinshancun/work-assistant/blob/main/docs/timeline-trees.md)
