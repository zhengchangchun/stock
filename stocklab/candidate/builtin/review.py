"""插桩5 v1.1.0 的源文本（候选，**停在代码里**，真库 `submit` 由人工侧执行）。

## 这是「一版真算脚本」，不是策略

它算的三样全部来自 `ctx`（服务层 `stocklab/plugin_review/inputs.py` 已按 PIT 装配好）：
① 因子胜率（按池别与 `adj_score` 五分桶）② 回测指标读数（`plugin_backtests` 的已落库
条目，**不重跑回放**）③ `bad_case_list`（模块2 回流，原样转交）。

## 为什么与 `p5_review.py` 并存

`p5_review.py` 是**现役** `v1.0.0` 桩（返回 `{"status": "not_implemented"}`），
它照旧是「首次 submit 用的初始版本」。本文件是**下一版候选**，要走
`plugin submit` → 沙盒 → `pending_review` → **人工 `approve`**（D-24）。
把候选版直接并进 `builtin/__init__.py` 的 `BUILTIN_PLUGINS` 会让「新库拿到哪一版」
这句话说不清 —— 那份映射的语义是「初始版本」。

## `BUILTIN_PLUGINS` 形状的映射

`{"5": SOURCE}` —— 与 `builtin/__init__.py` 同形，让测试能像验其他内置脚本那样
`runtime.load_script(BUILTIN_PLUGINS["5"], plugin_id="5")` 直接验它。
"""

VERSION = "v1.1.0"

SOURCE = '''
"""插桩5 v1.1.0：定期复盘分析（真算因子胜率与回测指标）。

三样读数全部来自 `ctx`，本脚本不 import、不做 IO、不取墙上时钟、不用随机数 ——
同一份 `ctx` 跑两遍必须逐字节相同。

## 样本不足给 None，不给 0.0

`win_rate = 0.0` 是一句**结论**（「这批候选一个都没涨」），与「样本不够，
说不出话」是两件事。低于本文件写死的门槛时一律 `None` ＋ `na_reasons`
（照 m2 既有做法；需求书 §5 A5「同口径才可比」）。

## 门槛写死在这里，**不做成配置**

`MIN_SAMPLES` / `MIN_BACKTESTS` 是常量：能配就能在样本不够时把闸门调低，
而调低之后报告里那句「样本不足」会消失 —— 那不是配置，是改口径。
"""

#: 因子胜率的分母下限（低于它不给胜率，给 None ＋ 原因）。
MIN_SAMPLES = 5

#: 回测胜率的可判定样本下限（只数 WIN / LOSE；INCONCLUSIVE 不进分母）。
MIN_BACKTESTS = 3

#: 池别（文档 01 的三池）。顺序即报告里的顺序，**固定**。
POOLS = ("short", "mid", "long")

#: `adj_score` 的五分桶，左闭右开，最后一桶闭区间。
SCORE_BUCKETS = ((0.0, 20.0), (20.0, 40.0), (40.0, 60.0), (60.0, 80.0), (80.0, 100.0))

#: 口径原文：它会随读数一起落库，报告与台账因此能回答「这个数是怎么算的」。
DEFINITION = ("命中判据 = 前视窗口收益 ret_pct > 0（0 与负都不算命中）；"
              "样本 = asof <= 目标日的候选快照成员；"
              "前视窗口 = 其后 n_days 个交易日（窗口走不完的样本不进入）；"
              "价格口径 = 复权链（ADR-004，算不出的标的剔除）；"
              "分桶 = 池别 x adj_score 五分桶")


def _num(value, default=0.0):
    """宽松取数：`None` 与非数值一律退到 default（**不抛** —— 一条脏样本不该让整份读数消失）。"""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _rate(rows):
    """一组样本 → 胜率读数；样本不足 ⇒ `win_rate=None` ＋ `na_reasons`。"""
    n = len(rows)
    if n < MIN_SAMPLES:
        return {"n": n, "n_hit": None, "n_miss": None, "win_rate": None,
                "na_reasons": ["样本不足：n=" + str(n)
                               + " < MIN_SAMPLES=" + str(MIN_SAMPLES)]}
    hits = 0
    for row in rows:
        if _num(row.get("ret_pct")) > 0.0:
            hits += 1
    return {"n": n, "n_hit": hits, "n_miss": n - hits,
            "win_rate": round(float(hits) / float(n), 4), "na_reasons": []}


def _bucket_label(index):
    lo = int(SCORE_BUCKETS[index][0])
    hi = int(SCORE_BUCKETS[index][1])
    if index == len(SCORE_BUCKETS) - 1:
        return "[" + str(lo) + "," + str(hi) + "]"
    return "[" + str(lo) + "," + str(hi) + ")"


def _bucket_index(score):
    last = len(SCORE_BUCKETS) - 1
    for i in range(len(SCORE_BUCKETS)):
        lo = SCORE_BUCKETS[i][0]
        hi = SCORE_BUCKETS[i][1]
        if score >= lo and (score < hi or (i == last and score <= hi)):
            return i
    return -1


def _backtest_readings(backtests):
    """`plugin_backtests` 的已落库条目 → 回测读数（**不重跑回放**）。"""
    out = {"n": len(backtests), "by_pool": {}, "verdicts": {},
           "n_overfit_suspected": 0, "n_decided": 0,
           "window": None, "win_rate": None, "na_reasons": []}
    if not backtests:
        out["na_reasons"].append(
            "回测台账里一条读数都没有（还没跑过回放并落库）")
        return out
    starts = []
    ends = []
    for row in backtests:
        pool = str(row.get("pool"))
        bucket = out["by_pool"].get(pool)
        if bucket is None:
            bucket = {"n": 0, "win": 0, "lose": 0, "inconclusive": 0}
            out["by_pool"][pool] = bucket
        bucket["n"] += 1
        verdict = str(row.get("verdict"))
        out["verdicts"][verdict] = out["verdicts"].get(verdict, 0) + 1
        if verdict == "WIN":
            bucket["win"] += 1
        elif verdict == "LOSE":
            bucket["lose"] += 1
        else:
            bucket["inconclusive"] += 1
        if row.get("overfit_flag") == "suspected":
            out["n_overfit_suspected"] += 1
        if row.get("window_start"):
            starts.append(str(row["window_start"]))
        if row.get("window_end"):
            ends.append(str(row["window_end"]))
    decided = out["verdicts"].get("WIN", 0) + out["verdicts"].get("LOSE", 0)
    out["n_decided"] = decided
    if decided < MIN_BACKTESTS:
        out["na_reasons"].append(
            "可判定的回测不足：WIN+LOSE=" + str(decided)
            + " < MIN_BACKTESTS=" + str(MIN_BACKTESTS))
    else:
        out["win_rate"] = round(float(out["verdicts"].get("WIN", 0))
                                / float(decided), 4)
    if starts and ends:
        out["window"] = [min(starts), max(ends)]
    return out


def run(ctx):
    samples = list(ctx.get("samples") or [])
    n_days = int(_num(ctx.get("n_days"), 0.0))
    na_reasons = []

    by_pool = {}
    for pool in POOLS:
        rows = [s for s in samples if str(s.get("pool")) == pool]
        reading = _rate(rows)
        by_pool[pool] = reading
        if reading["na_reasons"]:
            na_reasons.append("池别 " + pool + "：" + reading["na_reasons"][0])

    grouped = {}
    out_of_range = []
    for sample in samples:
        index = _bucket_index(_num(sample.get("adj_score"), -1.0))
        if index < 0:
            out_of_range.append(str(sample.get("code")))
            continue
        label = _bucket_label(index)
        if label not in grouped:
            grouped[label] = []
        grouped[label].append(sample)
    by_bucket = {}
    for i in range(len(SCORE_BUCKETS)):
        label = _bucket_label(i)
        reading = _rate(grouped.get(label) or [])
        by_bucket[label] = reading
        if reading["na_reasons"]:
            na_reasons.append("分数桶 " + label + "：" + reading["na_reasons"][0])
    if out_of_range:
        na_reasons.append(
            "adj_score 越界（不在 [0,100]）的样本 " + str(len(out_of_range))
            + " 条，未进任何桶：" + ",".join(sorted(set(out_of_range))))

    stray = sorted(set(str(s.get("pool")) for s in samples
                       if str(s.get("pool")) not in POOLS))
    if stray:
        na_reasons.append("池别不在 " + str(list(POOLS)) + " 的样本未进池别读数："
                          + ",".join(stray))

    overall = _rate(samples)
    for why in overall["na_reasons"]:
        na_reasons.append("全样本：" + why)

    bad_cases = ctx.get("bad_cases") or {}
    bad_case_list = []
    for case in list(bad_cases.get("cases") or []):
        bad_case_list.append(dict(case))

    analysis_result = {
        "status": "ok",
        "asof": str(ctx.get("asof") or ""),
        "n_days": n_days,
        "min_samples": MIN_SAMPLES,
        "min_backtests": MIN_BACKTESTS,
        "definition": DEFINITION,
        "n_samples": len(samples),
        "overall": overall,
        "by_pool": by_pool,
        "by_score_bucket": by_bucket,
        "backtests": _backtest_readings(list(ctx.get("backtests") or [])),
        "bad_case_summary": {
            "n_cases": len(bad_case_list),
            "n_miss_total": int(_num(bad_cases.get("n_miss_total"), 0.0)),
            "n_scored": int(_num(bad_cases.get("n_scored"), 0.0)),
            "limit": int(_num(bad_cases.get("limit"), 0.0)),
            "empty_reason": bad_cases.get("empty_reason"),
        },
        "na_reasons": na_reasons,
    }
    return {"analysis_result": analysis_result, "bad_case_list": bad_case_list}
'''.lstrip()

#: 与 `candidate/builtin/__init__.py::BUILTIN_PLUGINS` **同形**的映射：
#: `plugin_id -> 源文本`。它是**候选版**，故意不并进那份映射（见模块 docstring）。
BUILTIN_PLUGINS: dict[str, str] = {"5": SOURCE}
