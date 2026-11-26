# 来源

每条来源注明支持正文哪一句，以及可信度：论文、官方文档、公司自报或公开讨论。2026-11-26 核对。

## 方法

| 来源 | 支持的内容 | 可信度 |
| --- | --- | --- |
| Brendan Gregg, [The USE Method](https://www.brendangregg.com/usemethod.html)；[Performance Analysis Methodology](https://www.brendangregg.com/methodology.html) | 按资源检查利用率、饱和度和错误；随机改动、路灯法、归咎他人、被动基准等反面方法 | 作者本人的方法页 |
| Horace He, [Making Deep Learning Go Brrrr From First Principles](https://horace.io/brrr_intro.html) | 深度学习的耗时分为计算、显存带宽和开销三类 | 作者文章 |
| Jeff Dean, Sanjay Ghemawat, [Performance Hints](https://abseil.io/fast/hints.html) | 动手前用量级估算比较候选、淘汰弱方案；原文范围是单个程序，不含分布式与 ML 硬件 | 作者文档 |
| [Holistic Trace Analysis 教程](https://docs.pytorch.org/tutorials/beginner/hta_intro_tutorial.html) | GPU 空闲分为主机等待、kernel 等待和其他等待；通信与计算重叠比例的分母是通信时间 | 官方文档 |
| Dean, Barroso, [The Tail at Scale](https://research.google/pubs/pub40801/)（CACM 2013） | 扇出放大尾延迟；对冲请求等缓解办法 | 论文 |
| DeepMind, [AlphaEvolve 白皮书](https://storage.googleapis.com/deepmind-media/DeepMind.com/Blog/alphaevolve-a-gemini-powered-coding-agent-for-designing-advanced-algorithms/AlphaEvolve.pdf) | 一组 kernel 平均提速 23%，对应 Gemini 总训练时间缩短 1% | 公司自报 |
| Lin 等, [ECO](https://arxiv.org/html/2503.15669v1)；[OSDI 2026 页面](https://www.usenix.org/conference/osdi26/presentation/lin-hannah) | 从历史提交挖掘反模式，借全机群 profiling 定位，经测试、自审、评审后上线监测；已落地 6,400 多个提交 | 论文，作者为 Google 员工 |
| Meta, [FBDetect](https://tangchq74.github.io/FBDetect-SOSP24.pdf)（SOSP 2024） | 生产环境中检出低至 0.005% 的回归 | 论文，作者在 Meta 生产环境测得 |
| Meta, [Capacity Efficiency at Meta](https://engineering.fb.com/2026/04/16/developer-tools/capacity-efficiency-at-meta-how-unified-ai-agents-optimize-performance-at-hyperscale/)（2026-04-16） | "进攻"和"防守"共用工具，只有 skill 层不同；AI 生成修复 PR，由引入回归的作者评审 | 公司自报 |

## 测量

| 来源 | 支持的内容 | 可信度 |
| --- | --- | --- |
| Mytkowicz, Diwan, Hauswirth, Sweeney, [Producing Wrong Data Without Doing Anything Obviously Wrong!](https://gwern.net/doc/cs/algorithm/2009-mytkowicz.pdf)（ASPLOS 2009） | 环境变量大小和链接顺序会造成测量偏差；用随机化设置避免偏差，用因果分析检出偏差 | 论文 |
| Kalibera, Jones, [Rigorous Benchmarking in Reasonable Time](https://kar.kent.ac.uk/33611/45/p63-kaliber.pdf)（ISMM 2013） | 不确定性最大的层级最需要重复；用效应量置信区间报告结果 | 论文 |
| Gil Tene, [Coordinated Omission](https://groups.google.com/g/mechanical-sympathy/c/icNZJejUHfE) | 闭环压测在慢响应期间漏掉样本，导致分位数偏乐观；可按期望间隔校正 | 公开邮件列表讨论 |

## AI 自动优化

| 来源 | 支持的内容 | 可信度 |
| --- | --- | --- |
| Ouyang 等, [KernelBench](https://arxiv.org/abs/2502.10517) | 以 fast_p 计分：功能正确，且加速超过阈值 p 的题目所占比例 | 论文 |
| [KernelBench-Verified](https://arxiv.org/html/2607.16241v1)；[代码库](https://github.com/facebookresearch/kernel_bench_verified) | 某道 ReLU 题里，恒等函数通过标准检查，报出 374×；同一模型的汇总在标准协议下为 1.43×，验证协议下为 0.88×，两者统计层级不同 | 论文 |
| Sakana AI, [AI CUDA Engineer 复盘](https://sakana.ai/ai-cuda-engineer-post-mortem/)；[TechCrunch 报道](https://techcrunch.com/2025/02/21/sakana-walks-back-claims-that-its-ai-can-dramatically-speed-up-model-training/) | 评测没能阻止候选访问评测内存，部分候选省略了计算；公司修订论文，收回原先的最高加速数字 | 公司复盘与媒体报道 |
| METR, [Recent Frontier Models Are Reward Hacking](https://metr.org/blog/2025-06-05-recent-reward-hacking/)（2025-06-05） | 前沿模型通过修改测试或计分代码、获取现成答案来拿高分 | 评测机构报告 |
| He 等, [GSO](https://proceedings.neurips.cc/paper_files/paper/2025/hash/ddd98389004161943e646322a16348c3-Abstract-Datasets_and_Benchmarks_Track.html)（NeurIPS 2025） | 102 项真实仓库优化任务中，领先 agent 成功率低于 5%；成功要求补丁正确，并达到人类参考提交的性能目标；轨迹分析显示 agent 难以定位瓶颈 | 论文 |

## 多 agent 协作

| 来源 | 支持的内容 | 可信度 |
| --- | --- | --- |
| Anthropic, [How we built our multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system) | 子任务要有目标、输出格式、工具与来源和边界；按任务规模给预算；token 约为普通对话的 15 倍；共享上下文多、依赖多的任务不宜拆分 | 公司自报 |
| Anthropic, [Building effective agents](https://www.anthropic.com/research/building-effective-agents) | 编排者与 worker、评测者与优化者两种模式，以及各自的适用条件 | 公司文章 |
| Anthropic, [Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) | 用进度文件和 git 历史让新会话接手 | 公司文章 |
