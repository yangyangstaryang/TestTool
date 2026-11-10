# Embodied Eval Kit —— 具身智能任务评测与轨迹回放工具

---

## 一、先跑起来（3 分钟）

打开终端（VSCode 里按 `` Ctrl + ` `` 也行），逐行执行：

```bash
cd G:/MY/research/embodied-ai-starter
python evaluate.py --self-test
python evaluate.py --input sample_data --output report.html
```

第一条会打印 `[OK] 全部自测通过...`；第二条会打印成功率和 P95 延迟，并在当前目录生成 `report.html`。

**双击打开 `report.html`**，你会看到：整体成功率、按任务/策略分组的对比、失败归因、延迟 P50/P95/P99、自动生成的结论、逐条 episode 明细。

再试一下按策略分组（看不同模型的表现差异）：

```bash
python evaluate.py --input sample_data --group-by policy --output by_policy.html
```

> 如果 `python` 命令无效，试 `py` 或 `python3`。需要 Python 3.7+。

---

## 二、这个工具在解决什么真实问题

机器人跑一次任务（抓个杯子、插个零件、走一段走廊）会留下一条日志。公司里真实的痛点是：

- 一次跑 200 个 episode，人工翻日志根本看不过来
- "模型成功率 60%" 太笼统——**到底失败在哪一步？是看错了、规划错了、还是手滑了？**
- 平均延迟正常，但偶发一次 200ms 卡顿，真机上就可能撞东西

这个工具就是干这个的：把日志变成一份能直接拿去开会的评测报告。这也是为什么 JD 里反复出现"**设计评测方案、日志分析、数据回放、问题定位、自动化测试**"。

---

## 三、数据格式

打开 `sample_data/episodes.json`，它是一个数组，每个元素是一次任务记录：

| 字段                   | 含义                 | 示例                                              |
| ---------------------- | -------------------- | ------------------------------------------------- |
| `episode_id`           | 编号                 | `"pp_001"`                                        |
| `task`                 | 任务类型             | `pick_place` / `insertion` / `navigation`         |
| `scene`                | 场景                 | `table_A` / `corridor`                            |
| `policy`               | 用的模型/策略版本    | `vla_v2` / `bc_baseline`                          |
| `success`              | 是否成功             | `true` / `false`                                  |
| `steps` / `duration_s` | 步数 / 耗时          | `38` / `7.6`                                      |
| `failure_stage`        | 失败发生在哪个阶段   | `perception` / `planning` / `control` / `timeout` |
| `failure_reason`       | 具体失败原因         | `grasp_slip` / `perception_miss` / `collision`    |
| `latency_ms`           | 每步控制延迟（毫秒） | `[11,12,13,...]`                                  |

**改数据不用改代码**：往数组里加几条 episode，重新跑一次命令，报告就变了。这就是"数据驱动评测"最朴素的样子。

---
