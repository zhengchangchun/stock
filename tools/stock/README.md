# tools/stock —— AI 操盘手的仓内工具

**2026-09-27 起与代码同仓纳管。** 在那之前，这些文件只存在于运行环境
（`~/.nanobot/workspace/tools/stock/`），仓库里一个都没有 —— 后果是台账
`paper_agent_decisions.prompt_sha256` 记下的指纹**对外无法验证**、AI 臂的任一决策也**无法复现**：
别人把代码拉下来，是跑不起来这个操盘手的。

## 内容

| 文件 | 作用 |
| --- | --- |
| `prompts/trader-v1.txt` / `-v2.txt` / `-v3.txt` / `-v4.txt` | AI 操盘手的 system prompt。**`prompt_sha256` 的真源**：v1 `231c12ad…` 已退役 / v2 `e3c50bc2…` 已退役 / **v3 `66200c1d…` 对照臂（无复盘）** / **v4 `0a154fc5…` 现役（有复盘）** |
| `check-prompt-v4-superset.py` | G1 自检：把 v4 的四处复盘差异逆向剥离后，必须与 `trader-v3.txt` **逐字节相同**（对照臂只能有一个自变量） |
| `ai-trader.py` | 日更决策生成器：取 PIT 上下文 → 调网关模型出决策 JSON → `paper agent review`（P86，**先复盘**）→ `paper agent decide` → `paper agent run`。**唯一联网点** |
| `paper-chart.py` | 净值曲线 + 买卖点图（只读真库、零写入） |
| `ai_pipeline_diagram.py` | AI 流水线示意图生成 |
| `arm-names.json` | 各臂显示名（**真源**是项目内 `stocklab/paper/arm_names.py`，本文件是镜像） |

## 运行位置（软链，不复制）

运行环境里 `~/.nanobot/workspace/tools/stock` 是指向**本目录**的软链：

```bash
ls -l ~/.nanobot/workspace/tools/stock
# … stock -> /Users/<you>/Documents/Workspace/stock/tools/stock
```

所以日更 cron（`c3a99ae8`，工作日 18:30）走的绝对路径一字未改、提示词只有**一份**，
不会出现「仓内改了、线上还是旧的」这种漂移。**改提示词＝改本目录的文件，
不要在线上的软链另一端另存一份。**

## 不在这里的东西（也不进仓）

- `~/.nanobot/workspace/.secrets/stock-agent.env` —— 网关凭据（600，**绝不提交**）
- `~/.nanobot/workspace/artifacts/stock/` —— 运行产物（决策回执、图）
- `data/stocklab.db` —— 真库（约 2 GB，已在 `.gitignore`）

## 「项目外」是什么意思

文档里说的「项目外的生成器」指的是**运行期隔离**，不是文件位置：它不在 `stocklab/` 包内、
不被任何模块 import、不在收盘链里跑 ⇒ **项目内仍然零 API key、零联网、零模型调用**
（判据见 `tests/test_paper_discipline_guard.py` 系列）。纳管进仓不改变这条性质。

## 换模型 / 换提示词的纪律（D-48）

换提示词或换模型 = **换口径** ⇒ 必须 `paper agent enroll --arm-name arm-agent-<版本>`
开**新账户**（旧账户保留不删）。`paper agent decide` 会拿载荷里的
`--model-id / --prompt-sha256` 与账户行逐字段比对，不一致就 exit 2、零写入。

**臂 ↔ 提示词必须成对**（`--arm` 决定用哪套 prompt；未登记的臂回退 v3）：

| 臂 | 提示词 | 状态 |
| --- | --- | --- |
| `arm-agent-ds-v1` | `trader-v1.txt` | 已退役（留档） |
| `arm-agent-ds-v2` | `trader-v2.txt` | 已退役（`live=true` ⇒ **不点名就会被记缺决策**） |
| `arm-agent-ds-v3` | `trader-v3.txt` | **对照臂**（同日与 v4 各跑一次，唯一自变量＝有没有复盘） |
| `arm-agent-ds-v4` | `trader-v4.txt` | **现役**（P86，先复盘再决策；预注册见 `docs/experiments/2026-09-27-review-arm-v4-vs-v3.md`） |

**日更两条臂的姿势**（P86 / T4）：两条臂**同日各跑一次**，第二轮 ⑥ 一次点名到齐：

```bash
# 第一轮：对照臂（不写复盘；--no-run ⇒ 日终留给第二轮）
ai-trader.py --asof <D> --arm arm-agent-ds-v3 --no-run
# 第二轮：现役臂（先复盘再决策），日终一次认领三条臂
ai-trader.py --asof <D> --arm arm-agent-ds-v4 \
    --run-arm arm-agent-ds-v3 --run-arm arm-agent-ds-v4 --run-arm arm-agent-random
```

`--run-arm` 不写时的默认值是「本臂 ＋ `arm-agent-random`」；**多臂日更必须显式点名全部**，
否则没跑到的臂会被 `paper agent run` 记成 `missing_decision` ⇒ 退出码恒 1（假警，见 P69/T1）。

## 演练 / 副本姿势（P86）

`--db <路径>` 会逐条透传给项目 CLI（`paper agent review/decide/context/random/run` 都接 `--db`）
⇒ 端到端演练请在 APFS 副本上跑，**别在真库上试写**：

```bash
cp -c data/stocklab.db /tmp/p86-copy.db
/opt/homebrew/bin/python3.12 tools/stock/ai-trader.py --asof 2026-09-24 \
    --arm arm-agent-ds-v4 --db /tmp/p86-copy.db --no-run
```
