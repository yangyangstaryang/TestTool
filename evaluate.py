#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Embodied Eval Kit - 具身智能任务评测与轨迹回放工具

零依赖、零硬件、零 GPU：只用 Python 标准库，Windows / Linux / macOS 直接跑。

用法：
    python evaluate.py --input sample_data --output report.html
    python evaluate.py --self-test
    python evaluate.py --input sample_data --group-by policy --output by_policy.html

数据格式（一个 JSON 文件里放一个 episode 数组）：
    [
      {
        "episode_id": "pp_001",
        "task": "pick_place",
        "scene": "table_A",
        "policy": "vla_v1",
        "success": true,
        "steps": 42,
        "duration_s": 8.4,
        "failure_stage": null,        # perception / planning / control / timeout
        "failure_reason": null,       # grasp_slip / perception_miss / ...
        "latency_ms": [12.1, 13.4, ...]
      }
    ]

设计分层：load（数据加载） -> metrics（指标计算） -> render（报告渲染）
这样分层是为了以后能把 load 层换成 ROS2 bag / LeRobot dataset 的适配器，
而指标和报告逻辑完全不用改。面试时这一点值得主动讲。
"""

import argparse
import html
import json
import math
import statistics
import sys
from datetime import datetime
from pathlib import Path

# Windows 控制台默认编码可能不是 UTF-8，尽量修正；失败也不影响主流程
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

FAILURE_STAGES = ("perception", "planning", "control", "timeout")


# ---------------------------------------------------------------- load 层

def load_episodes(path: Path):
    """从文件或目录加载 episode 列表，并做最基本的字段校验。"""
    files = []
    if path.is_dir():
        files = sorted(list(path.glob("*.json")) + list(path.glob("*.jsonl")))
    elif path.is_file():
        files = [path]
    else:
        raise FileNotFoundError("找不到输入路径: %s" % path)

    if not files:
        raise ValueError("目录里没有 .json / .jsonl 文件: %s" % path)

    episodes = []
    for f in files:
        if f.suffix == ".jsonl":
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    episodes.append(json.loads(line))
        else:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data = data.get("episodes", [])
            episodes.extend(data)

    for ep in episodes:
        ep.setdefault("task", "unknown")
        ep.setdefault("scene", "unknown")
        ep.setdefault("policy", "unknown")
        ep.setdefault("success", False)
        ep.setdefault("steps", 0)
        ep.setdefault("duration_s", 0.0)
        ep.setdefault("failure_stage", None)
        ep.setdefault("failure_reason", None)
        ep.setdefault("latency_ms", [])
        ep["_source"] = f.name if len(files) == 1 else "multiple"

    return episodes, files


# ------------------------------------------------------------ metrics 层

def percentile(values, p):
    """最近秩法分位数。p 取 0.5 / 0.95 / 0.99。

    为什么报告 P95/P99 而不是只看平均值：机器人控制是实时系统，
    平均延迟正常但长尾偶发卡顿，真机上就会撞东西。面试常问这一题。
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, math.ceil(p * len(ordered)) - 1))
    return round(ordered[idx], 2)


def summarize(episodes):
    """把一个 episode 集合压成一组指标。"""
    n = len(episodes)
    ok = [e for e in episodes if e["success"]]
    latency = [v for e in episodes for v in e["latency_ms"]]
    return {
        "count": n,
        "success_count": len(ok),
        "success_rate": (len(ok) / n) if n else 0.0,
        "avg_steps": round(statistics.mean([e["steps"] for e in episodes]), 1) if n else 0.0,
        "avg_duration": round(statistics.mean([e["duration_s"] for e in episodes]), 2) if n else 0.0,
        "p50": percentile(latency, 0.50),
        "p95": percentile(latency, 0.95),
        "p99": percentile(latency, 0.99),
        "latency_samples": len(latency),
    }


def group_by(episodes, key):
    groups = {}
    for ep in episodes:
        groups.setdefault(str(ep.get(key, "unknown")), []).append(ep)
    return {k: summarize(v) for k, v in sorted(groups.items())}


def failure_breakdown(episodes):
    reasons, stages = {}, {}
    for ep in episodes:
        if ep["success"]:
            continue
        r = ep.get("failure_reason") or "unspecified"
        s = ep.get("failure_stage") or "unspecified"
        reasons[r] = reasons.get(r, 0) + 1
        stages[s] = stages.get(s, 0) + 1
    return dict(sorted(reasons.items(), key=lambda x: -x[1])), dict(sorted(stages.items(), key=lambda x: -x[1]))


def build_insights(episodes, overall, by_group, reasons, stages, group_key):
    """自动生成结论。让工具不只是画表，而是能给出可行动的发现。"""
    out = []
    if stages:
        top_stage, cnt = next(iter(stages.items()))
        out.append("失败最集中的阶段是 <b>%s</b>（%d 次），优先排查该阶段的日志与数据。"
                   % (html.escape(top_stage), cnt))
    if reasons:
        top_reason, cnt = next(iter(reasons.items()))
        out.append("首要失败原因是 <b>%s</b>（%d 次），可作为下一轮数据采集或仿真场景补充的重点。"
                   % (html.escape(top_reason), cnt))
    if overall["p50"] and overall["p95"]:
        ratio = overall["p99"] / overall["p50"] if overall["p50"] else 0
        if ratio > 2:
            out.append("P99/P50 = %.1f，长尾延迟明显：平均延迟正常不代表真机控制安全，建议检查偶发阻塞与 GC/调度抖动。"
                       % ratio)
        else:
            out.append("延迟分布较平稳（P99/P50 = %.1f），控制链路暂无明显长尾。" % ratio)
    if len(by_group) > 1:
        ranked = sorted(by_group.items(), key=lambda x: -x[1]["success_rate"])
        best, worst = ranked[0], ranked[-1]
        gap = best[1]["success_rate"] - worst[1]["success_rate"]
        if gap >= 0.15 and best[1]["count"] >= 3 and worst[1]["count"] >= 3:
            out.append("分组对比：<b>%s</b> 成功率 %.0f%%，<b>%s</b> 仅 %.0f%%，差异达 %.0f 个百分点，值得单独做消融验证。"
                       % (html.escape(best[0]), best[1]["success_rate"] * 100,
                          html.escape(worst[0]), worst[1]["success_rate"] * 100, gap * 100))
        else:
            out.append("按 <b>%s</b> 分组的成功率差异较小（最高 %.0f%% / 最低 %.0f%%，样本量 %d），"
                       "当前证据不足以判断优劣，建议增加 episode 数量或改用 <code>--group-by policy</code> 对比不同策略。"
                       % (html.escape(group_key), best[1]["success_rate"] * 100,
                          worst[1]["success_rate"] * 100, overall["count"]))
    if not out:
        out.append("样本量偏小，建议补充 episode 后再看结论。")
    return out


# ------------------------------------------------------------- render 层

CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; padding: 32px; background: #fafaf8; color: #2c2c2a;
       font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
       font-size: 14px; line-height: 1.6; }
.wrap { max-width: 1040px; margin: 0 auto; }
h1 { font-size: 22px; font-weight: 600; margin: 0 0 4px; }
h2 { font-size: 16px; font-weight: 600; margin: 32px 0 12px; }
.sub { color: #5f5e5a; font-size: 13px; margin-bottom: 24px; }
.cards { display: flex; gap: 12px; flex-wrap: wrap; }
.card { flex: 1 1 180px; background: #fff; border: 1px solid #e3e1d9;
        border-radius: 12px; padding: 16px; }
.card .k { color: #5f5e5a; font-size: 12px; }
.card .v { font-size: 26px; font-weight: 600; margin-top: 4px; }
table { width: 100%; border-collapse: collapse; background: #fff;
        border: 1px solid #e3e1d9; border-radius: 12px; overflow: hidden; }
th, td { padding: 9px 12px; text-align: left; border-bottom: 1px solid #f0efe9; font-size: 13px; }
th { background: #f5f4ee; font-weight: 600; color: #444441; }
tr:last-child td { border-bottom: none; }
.bar { display: inline-block; height: 8px; background: #1d9e75; border-radius: 4px;
       vertical-align: middle; margin-right: 6px; }
.bar.bad { background: #d85a30; }
.insights { background: #e1f5ee; border: 1px solid #9fe1cb; border-radius: 12px;
            padding: 14px 18px; }
.insights li { margin: 6px 0; }
footer { margin-top: 36px; color: #888780; font-size: 12px; }
code { background: #f1efe8; padding: 1px 5px; border-radius: 4px; font-size: 12px; }
"""


def pct(v):
    return "%.0f%%" % (v * 100)


def bar(v):
    color = "" if v >= 0.6 else " bad"
    return '<span class="bar%s" style="width:%dpx"></span>%s' % (color, int(v * 90), pct(v))


def render(episodes, files, group_key, output: Path):
    overall = summarize(episodes)
    by_group = group_by(episodes, group_key)
    reasons, stages = failure_breakdown(episodes)
    insights = build_insights(episodes, overall, by_group, reasons, stages, group_key)

    p = []
    p.append("<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>")
    p.append("<title>具身智能任务评测报告</title><style>%s</style></head><body><div class='wrap'>" % CSS)
    p.append("<h1>具身智能任务评测报告</h1>")
    p.append("<div class='sub'>生成时间 %s ｜ 数据来源 %s ｜ 共 %d 个 episode ｜ 分组维度 %s</div>"
             % (datetime.now().strftime("%Y-%m-%d %H:%M"),
                html.escape(", ".join(f.name for f in files)), len(episodes), html.escape(group_key)))

    p.append("<div class='cards'>")
    for k, v in (("Episode 总数", str(overall["count"])),
                 ("整体成功率", pct(overall["success_rate"])),
                 ("平均步数", str(overall["avg_steps"])),
                 ("控制延迟 P95", "%.1f ms" % overall["p95"]),
                 ("P99", "%.1f ms" % overall["p99"])):
        p.append("<div class='card'><div class='k'>%s</div><div class='v'>%s</div></div>" % (k, v))
    p.append("</div>")

    p.append("<h2>自动结论</h2><div class='insights'><ul>")
    for s in insights:
        p.append("<li>%s</li>" % s)
    p.append("</ul></div>")

    p.append("<h2>按 %s 分组</h2><table><tr><th>%s</th><th>Episodes</th><th>成功率</th>"
             "<th>平均步数</th><th>平均时长</th><th>P50</th><th>P95</th><th>P99</th></tr>"
             % (html.escape(group_key), html.escape(group_key)))
    for name, m in by_group.items():
        p.append("<tr><td><b>%s</b></td><td>%d</td><td>%s</td><td>%.1f</td><td>%.2fs</td>"
                 "<td>%.1f</td><td>%.1f</td><td>%.1f</td></tr>"
                 % (html.escape(name), m["count"], bar(m["success_rate"]),
                    m["avg_steps"], m["avg_duration"], m["p50"], m["p95"], m["p99"]))
    p.append("</table>")

    p.append("<h2>失败归因</h2><table><tr><th>失败原因</th><th>次数</th><th>占比</th><th>失败阶段</th><th>次数</th></tr>")
    total_fail = max(1, sum(reasons.values()))
    stage_items = list(stages.items())
    for i, (reason, cnt) in enumerate(reasons.items()):
        stage_txt = "%s (%d)" % (html.escape(stage_items[i][0]), stage_items[i][1]) if i < len(stage_items) else ""
        p.append("<tr><td>%s</td><td>%d</td><td>%s</td><td>%s</td><td></td></tr>"
                 % (html.escape(reason), cnt, pct(cnt / total_fail), stage_txt))
    if not reasons:
        p.append("<tr><td colspan='5'>全部成功，无失败样本</td></tr>")
    p.append("</table>")

    p.append("<h2>Episode 明细</h2><table><tr><th>ID</th><th>任务</th><th>场景</th><th>策略</th>"
             "<th>结果</th><th>步数</th><th>失败阶段</th><th>失败原因</th></tr>")
    for ep in episodes:
        status = "成功" if ep["success"] else "<span style='color:#a32d2d'>失败</span>"
        p.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                 % (html.escape(str(ep.get("episode_id", "-"))),
                    html.escape(str(ep["task"])), html.escape(str(ep["scene"])),
                    html.escape(str(ep["policy"])), status, ep["steps"],
                    html.escape(str(ep.get("failure_stage") or "-")),
                    html.escape(str(ep.get("failure_reason") or "-"))))
    p.append("</table>")

    p.append("<footer>Embodied Eval Kit · 指标口径：最近秩分位数 · "
             "下一步可接入 <code>ROS2 bag</code> / <code>LeRobot dataset</code> / 仿真轨迹导出。"
             "本工具只覆盖离线日志，不替代真机在线监控。</footer>")
    p.append("</div></body></html>")

    output.write_text("\n".join(p), encoding="utf-8")
    return overall


# ------------------------------------------------------------- self test

def self_test():
    sample = [
        {"episode_id": "t1", "task": "a", "policy": "p1", "success": True, "steps": 10,
         "duration_s": 1.0, "latency_ms": [10, 10, 10, 10]},
        {"episode_id": "t2", "task": "a", "policy": "p1", "success": False, "steps": 20,
         "duration_s": 2.0, "failure_stage": "control", "failure_reason": "grasp_slip",
         "latency_ms": [10, 10, 10, 200]},
    ]
    m = summarize(sample)
    assert m["count"] == 2, "count 错误"
    assert abs(m["success_rate"] - 0.5) < 1e-9, "success_rate 错误"
    assert m["avg_steps"] == 15.0, "avg_steps 错误"
    assert m["p50"] == 10.0, "p50 错误"
    assert m["p95"] == 200.0, "p95 应捕获长尾"
    reasons, stages = failure_breakdown(sample)
    assert reasons == {"grasp_slip": 1}, "失败原因统计错误"
    assert stages == {"control": 1}, "失败阶段统计错误"
    assert percentile([], 0.95) == 0.0, "空列表处理错误"
    print("[OK] 全部自测通过：指标计算、失败归因、长尾延迟捕获、空值处理均正常")


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description="具身智能任务评测与轨迹回放工具")
    ap.add_argument("--input", default="sample_data", help="JSON/JSONL 文件或目录")
    ap.add_argument("--output", default="report.html", help="输出 HTML 报告路径")
    ap.add_argument("--group-by", default="task", choices=["task", "policy", "scene"],
                    help="分组维度，默认 task")
    ap.add_argument("--self-test", action="store_true", help="运行内置自测")
    args = ap.parse_args()

    if args.self_test:
        self_test()
        return

    episodes, files = load_episodes(Path(args.input))
    if not episodes:
        print("[ERROR] 没有读到任何 episode")
        sys.exit(1)
    overall = render(episodes, files, args.group_by, Path(args.output))
    print("[OK] 已分析 %d 个 episode，成功率 %s，P95 延迟 %.1f ms"
          % (overall["count"], pct(overall["success_rate"]), overall["p95"]))
    print("     报告已生成：%s" % Path(args.output).resolve())


if __name__ == "__main__":
    main()
