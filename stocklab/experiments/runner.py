"""实验执行器（P8）：跑一个具名变体，产出可上报的样本外结论。

## 一行都不写生产表

`predictions` / `verifications` 是 append-only 的，且 `model_version` 是预测唯一键的一列。
实验变体**全程在内存里算**：预测由 `compute_forecast(spec=...)` 产出，
打分走 P7 的 `verify.score.score_prediction`，聚合走 P7 的 `verify.report.summarize`，
呈现走 P7 的 `verify.report.render_markdown`。报告里每一个数字都由 P7 的函数算出，
唯一被替换的是「预测从哪来」—— 那本来就是要变的那个变量。

于是有两条结构性好处：

1. **口径一致是免费的**：变体与基线跑在**同一次循环**里，共享同一次
   `load_pit_bars` / `load_scoring_bars` / `index_pct_for` 取数 ——
   「同一天、同一根 bar、同一套成本」不靠事后对齐，靠构造。
2. **口径漂移可被检测**：报告顶层写死 `metric_version`，
   与基线比较前先校验（`metrics.assert_metric_version`）。

## test 段怎么被「封存」

循环**分两趟**：先只跑 `train + validate` 的交易日；判定完 validate 之后，
**只有 `WIN` 才**再跑第二趟去读 `test`。所以「test 只在晋级评审时用一次」
不是一句注释，而是**第一趟的循环范围里根本没有 test 那些日期**。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Sequence

from stocklab.backtest.benchmark import INDEX_300_SYMBOL
from stocklab.config.costs import CostModel
from stocklab.data import adjust
from stocklab.data.models import Bar
from stocklab.experiments import metrics
from stocklab.experiments.metrics import METRIC_VERSION, MIN_DAYS
from stocklab.experiments.split import (SPLIT_NAMES, SplitConfig, boundaries,
                                        split_days)
from stocklab.experiments.variants import (Variant, get_variant,
                                           load_index_direction,
                                           load_index_rv_percentile,
                                           load_mf_sign,
                                           load_val_pe_pct_sign)
from stocklab.experiments.residuals import fit_residual_distribution
from stocklab.features.pit_regime import PitFeatures, rv_percentile, volume_z
from stocklab.predict.model import (BASELINE_SPEC, DegenerateInput,
                                    compute_forecast, degenerate_strategy_mix)
from stocklab.predict.residual import ResidualDistribution
from stocklab.predict.service import (PitCache, UnusableWindow, load_pit_bars)
from stocklab.predict.version import MODEL_VERSION
from stocklab.verify.replay import session_dates
from stocklab.verify.report import render_markdown, summarize
from stocklab.verify.score import CAPITAL, score_prediction
from stocklab.verify.service import index_pct_for, load_scoring_bars


class NoReplayDays(RuntimeError):
    """区间内没有任何可回放的交易日 —— 说明 `--from/--to` 选错了，不是「全都对」。"""


def _resolve_codes(conn: sqlite3.Connection, codes: Sequence[str] | None) -> list[str]:
    if codes:
        return list(codes)
    return [r["code"] for r in conn.execute(
        "SELECT code FROM instruments WHERE active=1 AND type='stock' ORDER BY code")]


def _pit_features(hist: Sequence[Bar], asof: str, need: frozenset[str],
                  index_rv_pct: float | None) -> tuple[PitFeatures, str | None]:
    """算变体需要的那几个 PIT 特征；**数据坏了就显式降级并计数**。

    返回 `(features, err)`。`err` 非空 = 特征层**硬拒绝**了这天的输入
    （`volume` 为 NULL、价格非正）—— 那不是「变体没用」，是数据缺口，
    必须能被读出来（所以进 `skipped`），而不是静默算成 0。

    「历史不足」不走这条路：那由各特征函数返回 `None`，再由
    `compute_forecast` 抛 `DegenerateInput`（带字段名，比这里更精确）。
    """
    if not need:
        return PitFeatures(), None                 # 基线/`const`：一个特征都不算
    try:
        return PitFeatures(
            volume_z=volume_z(hist, asof) if "volume_z" in need else None,
            rv_pct=rv_percentile(hist, asof) if "rv_pct" in need else None,
            index_rv_pct=index_rv_pct if "index_rv_pct" in need else None,
        ), None
    except DegenerateInput as exc:
        return PitFeatures(), f"DegenerateInput: {exc}"


def _replay(conn: sqlite3.Connection, days: Sequence[str], sessions: Sequence[str], *,
            variant: Variant, codes: Sequence[str], costs: CostModel,
            capital: float, cache: PitCache, strategy_mix: dict,
            residuals: ResidualDistribution | None = None,
            progress=None) -> dict[str, Any]:
    """把 `days` 这些**目标日**各跑一遍：基线预测 + 变体预测 + 两侧打分。

    `sessions` 是完整的交易日轴，用来取每天的 `asof`（= 上一交易日）；
    区间起点那天没有「上一交易日」→ 跳过并记进 `skipped`。

    两侧共享同一次取数：行情、指数方向、PIT 特征都**只算一次**，
    基线侧拿到同样的 `features` 只是不读（`sigma_mode="const"`）——
    于是「变体与基线跑在同一根 bar、同一个状态上」是构造保证而非事后对齐。

    `residuals`（P10-a）同理，而且是**更强**的一条：两侧拿到**同一个**
    `ResidualDistribution`，基线侧只是不读（`dist_mode="gaussian"`）。
    于是「变体读的是 train 段拟合的形状、基线读的是高斯」这件事由 **spec** 决定，
    不由「调用方给谁传了残差」决定（有单测钉住「传了也不读」）。
    """
    index_of = {d: i for i, d in enumerate(sessions)}
    rows: dict[str, list[dict]] = {"baseline": [], "variant": []}
    skipped: dict[str, dict[str, str]] = {"baseline": {}, "variant": {}}
    index_dirs: dict[str, int | None] = {}
    pred_seq = 0
    need_index = variant.spec.mu_mode == "index_sign"
    # P34 / P29：资金流/估值符号是**按 (code, asof)** 的当日事实，与按天的指数方向不同
    need_mf = variant.spec.mu_mode == "mf_sign"
    need_val = variant.spec.mu_mode == "val_pe_pct"
    # 这个变体需要哪些 PIT 特征（`const` → 空集，连算都不算）
    need_feats = PitFeatures.required_for(variant.spec.sigma_mode)

    for n, target in enumerate(days, start=1):
        i = index_of[target]
        if i == 0:
            reason = "区间起点是本库最早的交易日，没有「上一交易日」"
            skipped["baseline"][target] = reason
            skipped["variant"][target] = reason
            continue
        asof = sessions[i - 1]
        # 指数方向**每天只算一次**（同一天所有标的共用），且只用 <= asof 的数据
        idx_dir = load_index_direction(conn, asof, cache=cache) if need_index else None
        index_dirs[asof] = idx_dir
        # 指数波动率状态同理：市场级，每天一次，只用 <= asof 的指数行
        idx_rv = (load_index_rv_percentile(conn, asof, cache=cache)
                  if "index_rv_pct" in need_feats else None)

        for code in codes:
            try:
                hist = load_pit_bars(conn, code, asof, cache=cache)
            except (adjust.AdjustError, UnusableWindow) as exc:
                reason = f"{type(exc).__name__}: {exc}"
                skipped["baseline"][f"{target}/{code}"] = reason
                skipped["variant"][f"{target}/{code}"] = reason
                continue
            if not hist or hist[-1].date != asof:
                last = hist[-1].date if hist else "无"
                reason = (f"NoBarOnAsof: {code} 在 {asof} 无 K 线（最后一根 {last}）"
                          "—— 停牌或采集缺口，拒绝用旧价冒充今日")
                skipped["baseline"][f"{target}/{code}"] = reason
                skipped["variant"][f"{target}/{code}"] = reason
                continue

            # 两侧**共享**同一次实际行情取数 → 「同一根 bar、同一套成本」是构造保证
            adjusted, raw, suspended, err = load_scoring_bars(
                conn, code, target, cache=cache)
            index_pct = index_pct_for(conn, asof, target)
            feats, feat_err = _pit_features(hist, asof, need_feats, idx_rv)
            # 资金流/估值符号是**按 (code, asof)** 的当日事实：每股每行各取一次
            mf_s = (load_mf_sign(conn, code, asof, cache=cache)
                    if need_mf else None)
            val_s = (load_val_pe_pct_sign(conn, code, asof, cache=cache)
                     if need_val else None)

            sides = (("baseline", None, None, None, None),
                     ("variant", variant.spec, idx_dir, mf_s, val_s))
            for side, spec, idir, mf_sign, val_sign in sides:
                # 特征层**硬拒绝**了这天的输入（volume NULL / 价格非正）。
                # 基线侧 `required_for("const")` 是空集 → 不受影响；
                # 需要它的变体侧拒绝该行并**计数**，不许静默当成 0。
                if feat_err is not None and PitFeatures.required_for(
                        (spec or BASELINE_SPEC).sigma_mode):
                    skipped[side][f"{target}/{code}"] = f"PitFeatureUnavailable: {feat_err}"
                    continue
                try:
                    p = compute_forecast(code=code, asof=asof, bars=hist,
                                         target_date=target, strategy_mix=strategy_mix,
                                         spec=spec, index_dir=idir, mf_sign=mf_sign,
                                         val_sign=val_sign, features=feats,
                                         residuals=residuals)
                except DegenerateInput as exc:
                    skipped[side][f"{target}/{code}"] = f"DegenerateInput: {exc}"
                    continue
                pred_seq += 1
                # 合成 id（负数）：这些预测**从不落库**，负号让它一眼区别于 pred_id
                p["pred_id"] = -pred_seq
                row = score_prediction(p, bars=adjusted, raw_bars=raw,
                                       suspended=suspended, costs=costs,
                                       capital=capital, adjust_error=err,
                                       index_pct=index_pct)
                row["code"] = code
                row["asof_date"] = asof
                row["model_version"] = (MODEL_VERSION if side == "baseline"
                                        else variant.model_tag)
                rows[side].append(row)
        if progress is not None:
            progress(target, n, len(days))

    return {"rows": rows, "skipped": skipped, "index_dirs": index_dirs,
            "n_days": len(days)}


def _summarize_split(name: str, days: Sequence[str], rows: dict, *,
                     variant: Variant, min_days: int) -> dict:
    """一个 split 的 P7 报告 + 配对日差 + gate（**全部复用 P7 的聚合口径**）。"""
    seg = set(days)
    base_rows = [r for r in rows["baseline"] if r["target_date"] in seg]
    var_rows = [r for r in rows["variant"] if r["target_date"] in seg]
    summary = summarize(base_rows + var_rows, from_date=days[0], to_date=days[-1],
                        min_days=min_days)
    paired = metrics.paired_daily_delta(base_rows, var_rows)
    g = metrics.gate(paired, n_days=paired["direction"]["n_days"], min_days=min_days)
    return {
        "metric_version": METRIC_VERSION,
        "split": name,
        "boundaries": {"first_date": days[0], "last_date": days[-1],
                       "n_days": len(days)},
        "baseline_model_version": MODEL_VERSION,
        "variant_model_tag": variant.model_tag,
        "summary": summary,
        "paired": paired,
        "gate": g,
    }


def _sealed_reason(gate_status: str, evaluate_test_on_win: bool) -> str:
    """test 段没被评估的**真实**原因。

    两件事互相独立，报告里必须都能表达：

      ① **实际 gate 状态** —— validate 可以是 `WIN` / `LOSE` / `FLAT` / `INSUFFICIENT`；
      ② **封存声明** —— 预注册是否声明「即使 WIN 也不打开 test」（`--keep-test-sealed`）。

    旧实现只看 ②：只要带了 `--keep-test-sealed`，不论 gate 是什么都写
    「validate 段 gate=WIN」—— validate 明明是 `LOSE` 的报告因此**谎报变体赢了**
    （ERROR_DIARY 2026-09-15 #16）。现在按 ① 取真实原因，再把 ② 作为附加事实补上。
    """
    sealed = not evaluate_test_on_win
    if gate_status == "WIN":
        # 走到这里必然 sealed：`test_evaluated = (gate==WIN) and evaluate_test_on_win`，
        # 若 evaluate_test_on_win 为真则 test 已打开，不会来问原因。
        return ("validate 段 gate=WIN，但本轮预注册声明封存 test"
                "（`--keep-test-sealed`）→ 封存段不打开，结论记 inconclusive，"
                "「是否开 test」留给下一轮复现评审")
    reason = (f"validate 段 gate={gate_status}（未达 WIN）→ 封存段不打开。"
              "这正是纪律要求的：validate 输了就不许再看 test，"
              "否则「用 test 挑变体」会以「我只是看一眼」的形式发生")
    if sealed:
        reason += ("另外，本轮预注册也声明了「即使 WIN 也封存 test」"
                   "（`--keep-test-sealed`）—— 但**这不是**本次封存的主因，"
                   f"主因是 gate={gate_status}。")
    return reason


def run_experiment(conn: sqlite3.Connection, *, variant_name: str,
                   from_date: str, to_date: str,
                   codes: Sequence[str] | None = None,
                   split_config: SplitConfig | None = None,
                   selection_split: str = "validate",
                   evaluate_test_on_win: bool = True,
                   costs: CostModel | None = None, capital: float = CAPITAL,
                   cache: PitCache | None = None, min_days: int = MIN_DAYS,
                   progress=None) -> dict:
    """跑一个变体，返回可直接落盘的实验报告 dict（**不含任何时间戳**）。

    流程（顺序即纪律）：

    1. 取变体 → 单变量校验（多变量的变体在这里就被拒了）；
    2. 按日期切 train / validate / test；
    3. **第一趟**只跑 train + validate；
    4. `gate(validate)`；只有 `WIN` 才**第二趟**去读 test；
    5. `decide(...)` 合成结论。

    `evaluate_test_on_win=False`（CLI `--keep-test-sealed`）把第 4 步再收紧一档：
    **即使 validate `WIN` 也不打开封存段**，结论记 `inconclusive` 并把「是否开 test」
    留给下一轮。这是一个**只会更保守**的开关 —— 它只能阻止读 `test`，不能强制读
    （默认值 `True` = 今天的行为，逐字节不变）。多变量比较那一轮的预注册常常需要它：
    3 个变体共享同一段 validate 时，任何一个「第一眼 WIN」都不足以支撑晋级评审。
    """
    variant = get_variant(variant_name)
    if selection_split == "test":
        # 提前拒：连跑都不跑，避免「跑完了才说不能用」
        raise metrics.TestSetLeak(
            "selection_split='test' 被拒绝：test 是封存段，不许用来挑选变体")
    costs = costs or CostModel()
    cache = cache or PitCache()
    codes = _resolve_codes(conn, codes)
    cfg = split_config or SplitConfig()

    sessions = session_dates(conn, codes)
    targets = [d for d in sessions if from_date <= d <= to_date]
    if not targets:
        raise NoReplayDays(
            f"[{from_date}, {to_date}] 内没有任何可回放的交易日"
            f"（本库交易日区间 {sessions[0]} ~ {sessions[-1]}）—— 检查 --from/--to")

    seg = split_days(targets, cfg)
    strat_mix = degenerate_strategy_mix()

    # ---- P10-a：形状变体需要「训练窗内标准化残差」的经验分布 ----
    # **在跑之前**按 train 段拟合一次，apply 到 validate（以及将来可能打开的 test）。
    # 拟合只喂 `seg["train"]`，所以「分位只由训练段决定」是**循环范围**保证的，
    # 不是靠实现里记得裁。基线口径（`gaussian`）不拟合 —— 它一行都不读。
    residual_fit = None
    if variant.spec.dist_mode != "gaussian":
        residual_fit = fit_residual_distribution(
            conn, train_days=seg["train"], sessions=sessions, codes=codes,
            cache=cache)
    residual_dist = residual_fit.distribution if residual_fit is not None else None

    first = seg["train"] + seg["validate"]
    first_pass = _replay(conn, first, sessions, variant=variant, codes=codes,
                         costs=costs, capital=capital, cache=cache,
                         strategy_mix=strat_mix, residuals=residual_dist,
                         progress=progress)

    splits: dict[str, dict] = {}
    for name in ("train", "validate"):
        splits[name] = _summarize_split(name, seg[name], first_pass["rows"],
                                        variant=variant, min_days=min_days)

    validate_gate = splits["validate"]["gate"]
    # **只有 validate 赢了才打开 test** —— 「test 只读一次」的结构性实现。
    # `evaluate_test_on_win=False` 时连这个条件也不满足（见函数 docstring）。
    test_evaluated = (validate_gate["status"] == "WIN") and evaluate_test_on_win
    second_pass: dict[str, Any] | None = None
    if test_evaluated:
        second_pass = _replay(conn, seg["test"], sessions, variant=variant,
                              codes=codes, costs=costs, capital=capital,
                              cache=cache, strategy_mix=strat_mix,
                              residuals=residual_dist, progress=progress)
        splits["test"] = _summarize_split("test", seg["test"], second_pass["rows"],
                                          variant=variant, min_days=min_days)
    test_gate = splits["test"]["gate"] if test_evaluated else None

    verdict = metrics.decide(validate_gate=validate_gate, test_gate=test_gate,
                             selection_split=selection_split,
                             test_evaluated=test_evaluated,
                             test_sealed_by_policy=not evaluate_test_on_win)

    skipped = first_pass["skipped"]
    if second_pass is not None:
        for side in ("baseline", "variant"):
            skipped[side] = {**skipped[side], **second_pass["skipped"][side]}

    return {
        "metric_version": METRIC_VERSION,
        "variant": {
            "name": variant.name,
            "changed_axis": variant.changed_axis,
            "spec": {"mu_mode": variant.spec.mu_mode,
                     "sigma_mode": variant.spec.sigma_mode,
                     "dist_mode": variant.spec.dist_mode},
            "changed_fields": list(variant.spec.changed_fields()),
            "hypothesis": variant.hypothesis,
            "prereg_doc": variant.prereg_doc,
            "model_tag": variant.model_tag,
        },
        "baseline_model_version": MODEL_VERSION,
        "universe": list(codes),
        "index_symbol": INDEX_300_SYMBOL,
        "range": {"from": from_date, "to": to_date, "n_days": len(targets)},
        "split_config": cfg.as_dict(),
        "split_boundaries": boundaries(seg),
        "selection_split": selection_split,
        "test_evaluated": test_evaluated,
        "test_not_evaluated_reason": (
            None if test_evaluated
            else _sealed_reason(validate_gate["status"], evaluate_test_on_win)
        ),
        "splits": splits,
        "verdict": verdict,
        "frozen": {
            "flat_band": _flat_band(),
            "flat_band_source": "stocklab.predict.model.FLAT_BAND（总纲 §8.2，全程冻结）",
            "window": _window(),
            "level_window": _level_window(),
            "cost_model": "stocklab.config.costs.CostModel（与 P7 同一份）",
            "capital": capital,
            "min_days": min_days,
            "note": ("口径冻结靠的是「变体 spec 里根本没有这些旋钮」，"
                     "不是靠自觉：`ForecastSpec` 只有 `mu_mode` / `sigma_mode` / "
                     "`dist_mode` 三个字段，"
                     "标签带、窗口、关键位口径、标的集合、区间一个都不在里面"),
        },
        "residual_fit": (residual_fit.as_report_block()
                         if residual_fit is not None else None),
        "skipped": skipped,
        "counts": {"baseline_rows": len(first_pass["rows"]["baseline"])
                   + (len(second_pass["rows"]["baseline"]) if second_pass else 0),
                   "variant_rows": len(first_pass["rows"]["variant"])
                   + (len(second_pass["rows"]["variant"]) if second_pass else 0)},
        # P34 / P29：预注册 PIT 约束要求报告**显式**给出「实际用变体的天数 vs 报告天数」。
        # 资金流/估值是按 (code, asof) 的，所以「天数」这里 = (日, 标的) 对；
        # 基线侧不因变体缺数据而缺行，故 `baseline_rows - variant_rows` 就是
        # 「报告口径里实际用了变体的对子」之外、被硬拒绝计入 skipped 的对子数。
        "coverage": {
            "variant_days_used": len(first_pass["rows"]["variant"])
                + (len(second_pass["rows"]["variant"]) if second_pass else 0),
            "variant_days_skipped": len(first_pass["rows"]["baseline"])
                + (len(second_pass["rows"]["baseline"]) if second_pass else 0)
                - (len(first_pass["rows"]["variant"])
                   + (len(second_pass["rows"]["variant"]) if second_pass else 0)),
            "reported_days": len(targets),
            "reported_pairs": len(targets) * len(codes),
            "note": ("「天」在此 = (日, 标的) 对：资金流/估值是每股每行一个符号。"
                     "`variant_days_skipped` = 基线出了数而变体被硬拒绝的对子数，"
                     "明细见 `skipped.variant`，**绝不静默回落到基线**"),
        },
        "notes": {
            "not_persisted": (
                "本报告的每一行都是**内存里的**评估结果 —— 一行都没有写进 "
                "`predictions` / `verifications`。变体不是模型版本，"
                "只有 promoted 才允许另开 ADR 与新 model_version"
            ),
            "reused_path": (
                "打分用 `verify.score.score_prediction`、聚合用 `verify.report.summarize`、"
                "呈现用 `verify.report.render_markdown` —— 与 P7 完全同一条代码路径，"
                "唯一替换的是「预测从哪来」"
            ),
            "effective_n": (
                "`effective_n` = **交易日数**；配对差值同样按日聚类。"
                "行数只作参考，不得当样本量"
            ),
            "unscorable": (
                "不可评分（无 bar / 停牌 / 复权不可用）的行结果列全空，"
                "**既不在分子也不在分母**；配对比较只取两侧都可评分的 `(日, 标的)`"
            ),
        },
    }


def _flat_band() -> float:
    from stocklab.predict.model import FLAT_BAND

    return FLAT_BAND


def _window() -> int:
    from stocklab.predict.model import WINDOW

    return WINDOW


def _level_window() -> int:
    from stocklab.predict.model import LEVEL_WINDOW

    return LEVEL_WINDOW


# ---------- 呈现 ----------

def render_experiment_markdown(rep: dict) -> str:
    """把实验报告渲染成 markdown（**确定性**：不含生成时间）。"""
    v, verdict = rep["variant"], rep["verdict"]
    L: list[str] = []
    L.append(f"# 单变量实验报告：`{v['name']}`")
    L.append("")
    L.append(f"> {v['hypothesis']}")
    L.append("")
    L.append(f"- **口径版本** `{rep['metric_version']}`"
             f"（与基线比较前会校验，不一致直接报错）")
    L.append(f"- **唯一变量** `{v['changed_axis']}`：{v['spec']}；"
             f"相对基线改掉的字段 = {v['changed_fields']}")
    L.append(f"- **基线** `{rep['baseline_model_version']}`（本报告内**同时**重算，"
             f"与变体共享同一次取数）→ 变体标签 `{v['model_tag']}`")
    L.append(f"- **标的** {rep['universe']}；**区间** {rep['range']['from']} → "
             f"{rep['range']['to']}（{rep['range']['n_days']} 个交易日）")
    L.append(f"- **预注册** `{v['prereg_doc']}`（假设与判据在跑之前写死）")
    L.append("")
    L.append("## 1. 三段切分（按日期，连续不重叠）")
    L.append("")
    L.append(f"配置：train={rep['split_config']['train']} / "
             f"validate={rep['split_config']['validate']} / "
             f"test={rep['split_config']['test']}")
    L.append("")
    L.append("| 段 | 首日 | 末日 | 交易日数 | 本次是否评估 |")
    L.append("|----|------|------|---------:|-------------|")
    for name in SPLIT_NAMES:
        b = rep["split_boundaries"][name]
        evaluated = ("是" if name in rep["splits"] else
                     "**否**（封存段不打开，原因见本节下方 `test_not_evaluated_reason`）")
        L.append(f"| {name} | {b['first_date']} | {b['last_date']} | "
                 f"{b['n_days']} | {evaluated} |")
    L.append("")
    L.append(f"- `selection_split` = **`{rep['selection_split']}`**")
    L.append(f"- `test_evaluated` = **`{rep['test_evaluated']}`**")
    if rep["test_not_evaluated_reason"]:
        L.append(f"- 原因：{rep['test_not_evaluated_reason']}")
    L.append("")
    cov = rep["coverage"]
    L.append(f"- **变体覆盖**：实际使用 **{cov['variant_days_used']}** 个 (日, 标的)，"
             f"硬拒绝跳过 **{cov['variant_days_skipped']}** 个，"
             f"报告口径 {cov['reported_days']} 天 / {cov['reported_pairs']} 个 (日, 标的)")
    L.append("")
    L.append("## 2. 逐段结果（P7 报告口径，基线 / 变体同栏）")
    L.append("")
    for name in SPLIT_NAMES:
        s = rep["splits"].get(name)
        if s is None:
            L.append(f"### `{name}` 段 —— **未评估（封存）**")
            L.append("")
            continue
        L.append(f"### `{name}` 段  {s['boundaries']['first_date']} → "
                 f"{s['boundaries']['last_date']}"
                 f"（{s['boundaries']['n_days']} 交易日）")
        L.append("")
        L.append(render_markdown(s["summary"]).rstrip())
        L.append("")
        L.append(f"#### `{name}` 段配对日差（变体 − 基线，按日聚类）")
        L.append("")
        L.append("| 指标 | Δ 均值 ± 标准误 [95% CI] | 有效交易日 |")
        L.append("|------|--------------------------|-----------:|")
        for metric, better in metrics.PAIRED_METRICS:
            d = s["paired"][metric]
            arrow = "越大越好" if better == "higher" else "越小越好"
            if d["mean"] is None:
                L.append(f"| {metric}（{arrow}） | — | 0 |")
            else:
                L.append(f"| {metric}（{arrow}） | {d['mean']:+.4f} ± {d['se']:.4f} "
                         f"[{d['ci95'][0]:+.4f}, {d['ci95'][1]:+.4f}] | "
                         f"{d['n_days']} |")
        c = s["paired"]["counts"]
        L.append("")
        L.append(f"配对计数：`n_pairs={c['n_pairs']}`，"
                 f"仅基线有 {c['keys_baseline_only']}、仅变体有 {c['keys_variant_only']}、"
                 f"任一侧不可评分 {c['unscorable_either']}（**都不进分母**）")
        L.append("")
        L.append(f"**gate = `{s['gate']['status']}`**（有效交易日 {s['gate']['n_days']}，"
                 f"门槛 {s['gate']['min_days']}）")
        for r in s["gate"]["reasons"]:
            L.append(f"- {r}")
        L.append("")
    L.append("## 3. 结论")
    L.append("")
    L.append(f"**`{verdict['status']}`**")
    L.append("")
    for r in verdict["reasons"]:
        L.append(f"- {r}")
    L.append("")
    L.append("## 4. 口径声明（读数字前必看）")
    L.append("")
    L.append(f"- 冻结项：`flat_band={rep['frozen']['flat_band']}`"
             f"（{rep['frozen']['flat_band_source']}）、"
             f"`WINDOW={rep['frozen']['window']}`、"
             f"`LEVEL_WINDOW={rep['frozen']['level_window']}`、"
             f"成本模型 {rep['frozen']['cost_model']}、本金 {rep['frozen']['capital']}")
    L.append(f"- {rep['frozen']['note']}")
    for text in rep["notes"].values():
        L.append(f"- {text}")
    L.append("- 判定判据**提前定死**：两个指标都显著变好才算 `WIN`；"
             "只有 validate `WIN` 才打开 test；"
             "「差一点」记 `FLAT` → 结论 `inconclusive`，不许挪口径、不许挪切分。")
    L.append("")
    return "\n".join(L) + "\n"


def write_report_at(rep: dict, md_path: Path) -> dict:
    """把报告写到 `md_path`（json 与之同名 `.json`），返回路径与 sha256。

    正文**不含任何时间戳** → 同参数两次运行的 sha256 天然相等（幂等的证据）。
    """
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path = md_path.with_suffix(".json")
    md = render_experiment_markdown(rep)
    blob = json.dumps(rep, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    md_path.write_text(md, encoding="utf-8")
    json_path.write_text(blob, encoding="utf-8")
    return {"markdown": str(md_path), "json": str(json_path),
            "sha256_md": hashlib.sha256(md.encode("utf-8")).hexdigest(),
            "sha256_json": hashlib.sha256(blob.encode("utf-8")).hexdigest()}


def write_report(rep: dict, report_dir: Path, date_tag: str) -> dict:
    """落盘 `reports/<date>-exp-<variant>.{md,json}`（默认落点）。"""
    return write_report_at(
        rep, report_dir / f"{date_tag}-exp-{rep['variant']['name']}.md")
