# Synthetic first project / 合成练习项目

This tiny project contains only made-up records and an **intentional mismatch**
between documentation and implementation. It is a read-only inspection exercise,
not business data or a model-quality benchmark.

这个极小项目只有虚构记录，并且**有意保留一处文档与实现不一致**，用于只读核对。

## Documented behavior / 待核对的文档说明

**Intentionally incorrect claim / 有意写错的说明：**
`count_records` ignores blank and whitespace-only lines. The supplied
`records.txt` has three nonempty records, so its documented count is **3**.

`count_records` 会忽略空白行；`records.txt` 有三条非空记录，因此按这段文档应输出 **3**。
请检查代码后再判断这项说明，不要将它当作实测结果。

## Reproduce locally / 人工复现

From this directory, using Python 3.9+ (`python` below means that interpreter):

在此目录使用 Python 3.9+ 执行：

```sh
python count_records.py records.txt
```

The implementation actually counts **all five lines**, including two blank
lines, and prints **5**. It opens the UTF-8 input for reading only. Importing
`count_records.py` does not open files or run its command-line entry point.

实现实际统计全部 **5** 行，其中含 **2** 个空白行，所以输出 **5**。函数只读输入，
导入模块不会读取文件或启动命令。此练习先保留差异，不要求修改文件。

For the Work Assistant task commands, see [first-task.md](../first-task.md).
