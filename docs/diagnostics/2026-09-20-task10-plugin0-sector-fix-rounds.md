# Task 10 插桩0 sector 优先 — fix rounds 记录

## Fix round 1

commit: `dad492d`

- 插桩0 新增 `_sector_of(ctx)` 优先读 `ctx["sector"]`，name-keyword 仅当 sector 缺失时触发。
- 新增 `test_plugin0_ctx_sector_wins_over_name_keyword`（初版）与
  `test_plugin0_unknown_sector_emits_only_one_note`。
- `if`→`elif` 防止 sector 未知时同时发两条 note（round 1 的真实行为变化）。

## Fix round 2

commit: `d1f3a81`

- re-reviewer 认为断言可能无法证伪，要求补充证伪证据。
- 测试断言改为判别性更强的形式；两个 test 保留。
- 但 docstring 仍未明确指名基线 `ab1f30e`，导致 re-reviewer 误用 `dad492d` 作为 pre-fix。

## Fix round 3

commit: 本次（见下方 git log）

### 实证方法

```bash
git show ab1f30e:stocklab/candidate/builtin/p0_industry.py > /tmp/p0_pre.py
```

从 wrapper 文件中提取 `SOURCE` 字符串，通过 `load_script(source_text, plugin_id="0")` 加载，
对 `ctx = {"name": "招商银行", "sector": "其他金融Ⅱ", ...}` 调用。

### pre-fix run() 函数（ab1f30e）

```python
def run(ctx):
    sector = _guess_sector(ctx["name"])
    risks = []
    if sector == "未知":
        risks.append("行业未能从名称推断，已按通用规则处理（非 PIT，仅为近似）")
    if sector == "银行":
        risks.append("银行业：高杠杆经营，通用排雷指标不完全适用")
    return {"pass_flag": True, "risk_note": risks}
```

特征：不读 `ctx["sector"]`，只用 `_guess_sector(ctx["name"])`。

### 实测 pre-fix 输出

- `ctx = {"name": "招商银行", "sector": "其他金融Ⅱ", ...}`
  → `risk_note: ['银行业：高杠杆经营，通用排雷指标不完全适用']`（含"银行"）

- 断言 `not any("银行" in r …)` 对 ab1f30e **会红**（已实测）。

### 判定：test_plugin0_ctx_sector_wins_over_name_keyword 可证伪

yes。ab1f30e 输出含"银行"，当前代码不含"银行" → 断言区分两个实现。

### test_plugin0_unknown_sector_emits_only_one_note 对 ab1f30e 也会红

ab1f30e 对 `name="某未知标的XYZ", sector=None` 输出：
`['行业未能从名称推断，已按通用规则处理（非 PIT，仅为近似）']`

- `any("行业未能判定" in r …)` → False → assert 红
- 故该测试对 ab1f30e 也是可证伪的（if→elif 修复覆盖此路径）。

### 本轮变更

- `test_plugin0_ctx_sector_wins_over_name_keyword` docstring 明确写出基线 `ab1f30e`、
  pre-fix 输出（含"银行"）以及可证伪性声明；去掉日语混写。
- `test_plugin0_unknown_sector_emits_only_one_note` docstring 同样明确 ab1f30e 对两个 assert 均会红。
- 生产代码（`p0_industry.py`）及 `build_ctx` 未动。
- 全量回归：1912 passed。
