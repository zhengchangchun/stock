#!/usr/bin/env python3
"""AI 操盘手日更循环的**项目外**驱动（P56 / D-48 / D-49 / D-50）。

分工（任务书 §1）：
  项目内 = 校验 → 落库 → 执行 → 计账（零 API key、零联网、零模型调用）
  本脚本 = 项目外，负责：
     ① paper agent random   （随机对照臂，项目内跑，种子固定 ⇒ 可复现）
     ② paper agent context  （PIT 输入的唯一来源）
     ③ 调网关模型产出决策 JSON（唯一的联网点）
     ③′ paper agent review  （P86：模型同一次回答里的 `review` 段 —— **先决策、后复盘**）
     ④ 注入 asof / context_sha256 落盘
     ⑤ paper agent decide   （预注册闸门 + 上下文指纹闸门）
     ⑥ paper agent run      （成交 + 写净值；P69 起**必须显式点名**在飞的臂，见下）

P86 / G1–G6（提示词 v4 ＝ v3 ＋「先复盘再决策」）：
  · **一次调用两个产物**：不拆两次调用 —— 拆了就会引入第二个自变量（上下文与温度都变了）。
  · **复盘失败不阻断决策**（G3）：`paper agent review` 非 0/1 退出 ⇒ 回执记 `review_written=false`
    ＋原因，继续走 `decide`；`decide` 失败才整条链失败。
    ⚠️ **写序与任务书 G3 一致**（复盘先写、决策后写）—— 这要求当天的复盘**不进**当天决策的
    上下文，否则 D-49 的 `context_sha256` 闸门必拒。P86 为此把 `own_history` 的复盘窗口
    由 `asof <= 决策日` 收紧为 `asof < 决策日`（P86 修订，见 `review._iter_reviews`
    与 ADR-036 的修订段）。
  · **重放不给新行**（G4）：撞 `(arm, asof, kind)` ⇒ exit 1 且零写入 ⇒ 视为「今天已经复盘过」。
  · **v3 兼容**（G5）：模型回答里没有 `review` 键 ⇒ 跳过 ③′，其余逐位不变。
  · 回执 `ai-trader-receipt.json` 只**增**三键：`review_written` / `review_id` / `review_skipped_reason`
    （`run-receipt.json` 仍是 `paper agent run` 的原始 stdout，逐字节不动）。

凭据只在项目外读：~/.nanobot/workspace/.secrets/stock-agent.env（600，不进仓）。

用法：
  python3 ai-trader.py --asof 2026-09-23                 # 全流程
  python3 ai-trader.py --asof 2026-09-23 --skip-random    # 已跑过随机臂
  python3 ai-trader.py --asof 2026-09-23 --no-run         # 只跑 ①–⑤（P62 修好前用这个）
  python3 ai-trader.py --asof 2026-09-23 --dry-run        # 只取上下文 + 出载荷，不写库

⚠️ **P62 已于 2026-09-23 修好（`b0f34ed`），第 ⑥ 步可以跑**（不再需要 `--no-run`）。
   P62 修的是「成交日的净值行被记成执行前的现金」——`arm-agent-ds-v1 / 2026-09-23` 那行
   已从 `nav 11320.0` 修回 `19553.68`（详见 `docs/plans/2026-09-23-P62-*` 验收记录）。

⚠️ **联网必须走流式**（2026-09-24 实测，见下）：非流式满量请求会让 Cloudflare 把连接
   当成「上游空闲」、约 **126 s** 返回 `524 / Upstream model provider is temporarily unavailable`；
   思维链模型一次要生成 4000–5000 token（~90–150 s）⇒ **非流式必然撞墙**。
   已改为 `stream=True`（最后一次尝试退回非流式兜底），实测 156 s 的生成全程无断。
   小请求（`max_tokens=32`）非流式只要 2.2 s，所以早期探测会误以为「网关是好的」。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

PROJ = Path.home() / "Documents" / "Workspace" / "stock"
PY = PROJ / ".venv" / "bin" / "python"
SECRETS = Path.home() / ".nanobot" / "workspace" / ".secrets" / "stock-agent.env"
PROMPTS_DIR = Path.home() / ".nanobot" / "workspace" / "tools" / "stock" / "prompts"
PROMPT_DEFAULT = PROMPTS_DIR / "trader-v3.txt"
#: 提示词与 `--arm` 必须成对（`paper agent enroll` 把 `prompt_sha256` 预注册进账户，
#: `decide` 时逐字段比对，配错即拒）：
#:   `arm-agent-ds-v1` ↔ `trader-v1.txt`（`231c12ad…`）—— 已退役
#:   `arm-agent-ds-v2` ↔ `trader-v2.txt`（`e3c50bc2…`）—— 已退役（09-23/09-24 两天历史留档）
#:   `arm-agent-ds-v3` ↔ `trader-v3.txt`（`66200c1d…`）—— **对照臂（无复盘）**；v3 不再让模型心算一手成本，
#:     直接读 P81 的 `tradability`（`tradable` / `affordable_lots` / `min_weight_pct`），
#:     并把本金口径从「假设 2 万」改成上下文里的实值（5 万）。
#:   `arm-agent-ds-v4` ↔ `trader-v4.txt`（P86）—— **现役（有复盘）**；v4 是 v3 的**超集**：
#:     决策段逐字相同，只多「第一步：复盘」与输出里的 `review` 键（G1 的对照只能有一个自变量）。
#: `--arm` 决定用哪套提示词（P86 / G5）；未登记的臂沿用 `--prompt` 的默认值（v3）。
ARM_PROMPTS: dict[str, str] = {
    "arm-agent-ds-v3": "trader-v3.txt",
    "arm-agent-ds-v4": "trader-v4.txt",
}
#: P85 K1：复盘台账里本期唯一的 `kind`（`(arm, asof, kind)` 是幂等键）。
REVIEW_KIND = "daily"
#: P86 T1.4：回执里新增的三个键（既有键只增不改）。
REVIEW_RECEIPT_KEYS = ("review_written", "review_id", "review_skipped_reason")

MAX_TOKENS = 16000     # 思维链 token 也占预算（任务书 §1a 的坑：给 16 会只出空 content）
TEMPERATURE = 0.2
#: 网关前置 Cloudflare 会按客户端签名挡请求（urllib 默认 UA ⇒ 403 code 1010）。
USER_AGENT = "zhman-stocklab-trader/1.0"
#: 网关偶发 503 / 超时（实测同一个请求重试即 200）；重试次数与退避秒数。
HTTP_RETRIES = 3
HTTP_BACKOFF_S = 8
#: 单次请求的读超时。流式下每个 chunk 都会重置计时，所以给大值没有代价。
HTTP_TIMEOUT_S = 600


# ---------------------------------------------------------------- 基础

def load_env(path: Path) -> dict:
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def run_cli(args: list[str], *, timeout: int = 120,
            db: str | None = None) -> subprocess.CompletedProcess:
    #: `--db` 只在给定时追加（`paper agent sha256` 不接 `--db` ⇒ 那条调用走默认）。
    cmd = [str(PY), "-m", "stocklab.cli.main", *args]
    if db:
        cmd += ["--db", str(db)]
    return subprocess.run(cmd, cwd=str(PROJ), capture_output=True, text=True,
                          timeout=timeout)


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- ③ 模型

def _read_sse(resp) -> dict:
    """把 SSE 流折叠成与**非流式**响应同形的 dict（`choices[0].message.content`）。

    2026-09-24 实测原因：网关前置 Cloudflare 对**上游空闲**的连接有上限（满量非流式请求
    ~126 s 被 `524 / Upstream model provider is temporarily unavailable`，而小请求
    2.2 s 就 200）。思维链模型一次要生成 4000–5000 token（~90 s+）⇒ 非流式**必然**撞墙。
    流式下 chunk 持续流出、连接不空闲，因此不再 524。
    """
    parts: list[str] = []
    usage = None
    model_echo = None
    fingerprint = None
    for raw_line in resp:
        line = raw_line.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        model_echo = chunk.get("model") or model_echo
        fingerprint = chunk.get("system_fingerprint") or fingerprint
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                parts.append(delta["content"])
    return {"model": model_echo,
            "system_fingerprint": fingerprint,
            "usage": usage,
            "choices": [{"message": {"content": "".join(parts)}}]}


def call_model(env: dict, *, model: str, system_prompt: str, user_content: str) -> dict:
    base = env["CMD_API_BASE"].rstrip("/")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    t0 = time.time()
    last_err = None
    for attempt in range(HTTP_RETRIES):
        # 流式（见 `_read_sse` 的理由）；**最后一次**尝试退回非流式兜底
        #（万一网关某天把 `stream` 关掉，至少还有一条路）。
        stream = attempt < HTTP_RETRIES - 1
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": TEMPERATURE,
            "max_tokens": MAX_TOKENS,
            "response_format": {"type": "json_object"},
        }
        if stream:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            # 网关前置 Cloudflare：默认的 `Python-urllib/3.x` UA 会被挡成
            # `403 error code: 1010`（2026-09-23 实测）。带一个正常 UA 即可。
            headers={"Content-Type": "application/json",
                     "Accept": "text/event-stream" if stream else "application/json",
                     "User-Agent": USER_AGENT,
                     "Authorization": f"Bearer {env['CMD_API_KEY']}"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                ctype = resp.headers.get("Content-Type") or ""
                if stream and "text/event-stream" in ctype:
                    raw = _read_sse(resp)
                else:                              # 网关没按流式答，就按整包读
                    raw = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:200].decode("utf-8", "replace")
            last_err = f"HTTP {exc.code} {detail}"
            if exc.code in (400, 401, 403, 422):   # 请求本身的毛病，重试无意义
                raise
            log(f"   网关 {last_err}；{HTTP_BACKOFF_S * (attempt + 1)}s 后重试")
            time.sleep(HTTP_BACKOFF_S * (attempt + 1))
        except Exception as exc:                   # 超时 / 连接重置
            last_err = f"{type(exc).__name__}: {exc}"
            log(f"   网关 {last_err}；{HTTP_BACKOFF_S * (attempt + 1)}s 后重试")
            time.sleep(HTTP_BACKOFF_S * (attempt + 1))
    else:
        raise RuntimeError(f"网关 {HTTP_RETRIES} 次都没成功：{last_err}")
    return {"raw": raw, "elapsed_s": round(time.time() - t0, 2),
            "model_echo": raw.get("model"),
            "system_fingerprint": raw.get("system_fingerprint"),
            "usage": raw.get("usage")}


def extract_json(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        text = text[4:] if text.lower().startswith("json") else text
    return json.loads(text.strip())


# ---------------------------------------------------------------- 结果解析

def parse_run_receipt(stdout: str) -> dict:
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------- ③′ 复盘（P86）

def build_review_payload(review: object, *, asof: str, arm: str) -> dict:
    """把模型给的 `review` 段补成 P85 K2 的载荷（`asof`/`arm`/`kind` 由调用方注入）。

    模型只负责 `items` / `lessons` —— 「这条复盘说的是哪条臂的哪一天」不许由模型自称：
    K2 的白名单要求 `asof`/`arm` 与命令行逐字一致，模型猜错必被写入口拒。
    """
    body = review if isinstance(review, dict) else {}
    return {"asof": asof, "arm": arm, "kind": REVIEW_KIND,
            "items": body.get("items") or [], "lessons": body.get("lessons") or []}


def write_review(review: object, *, asof: str, arm: str, model: str, prompt_sha: str,
                 out_dir: Path, db: str | None = None) -> dict:
    """写一条复盘，返回回执三键（P86 / T1.4）。

    退出码语义（P85 K7）：**0** 已写入；**1** `(arm, asof, kind)` 已有一条（重放/重跑，
    视为「今天已经复盘过」，零写入）；其它 ⇒ 记失败（G3：**不阻断决策**）。
    """
    payload = build_review_payload(review, asof=asof, arm=arm)
    path = out_dir / f"review-{asof}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    r = run_cli(["paper", "agent", "review", "--asof", asof, "--arm", arm,
                 "--file", str(path), "--model-id", model,
                 "--prompt-sha256", prompt_sha], db=db)
    reason = (r.stderr.strip().splitlines() or ["(no stderr)"])[0][:300]
    if r.returncode == 0:
        rec = parse_run_receipt(r.stdout)
        log(f"③′ review 已写入 review_id={rec.get('review_id')} "
            f"items={rec.get('n_items')} lessons={rec.get('n_lessons')}")
        return {"review_written": True, "review_id": rec.get("review_id"),
                "review_skipped_reason": None}
    if r.returncode == 1:
        log(f"③′ review 已存在（exit 1，零写入）: {reason}")
        return {"review_written": False, "review_id": None,
                "review_skipped_reason": f"already:{reason}"}
    log(f"!! ③′ review 失败(exit {r.returncode})，**不阻断决策**: {reason}")
    return {"review_written": False, "review_id": None,
            "review_skipped_reason": f"exit={r.returncode}:{reason}"}


def dump_receipt(out_dir: Path, receipt: dict, review_state: dict) -> dict:
    """写 `ai-trader-receipt.json` ＝ 既有回执键 ＋ 复盘三键（既有键只增不改）。"""
    merged = dict(receipt)
    merged.update({k: review_state[k] for k in REVIEW_RECEIPT_KEYS})
    (out_dir / "ai-trader-receipt.json").write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return merged


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True, help="交易日 YYYY-MM-DD")
    ap.add_argument("--arm", default="arm-agent-ds-v4",
                    help="默认 v4（现役，有复盘）：v4 读 tradability 的结论字段、不再心算一手成本，"
                         "且同一次回答里先交 review 再交 decisions；"
                         "v1/v2 两条臂留档不再日更（换提示词 = 换口径 ⇒ 开新版本账户）")
    ap.add_argument("--model", default=None, help="默认取 .secrets 里的 CMD_MODEL")
    ap.add_argument("--db", default=None,
                    help="数据库路径（默认 data/stocklab.db）；**透传给每条项目 CLI 调用** —— "
                         "演练（G3/G4/端到端）请指到 /tmp 副本，别在真库上写")
    ap.add_argument("--run-arm", action="append", default=None, metavar="ARM",
                    help="⑥ `paper agent run` 认领的臂，可重复；不给 ⇒ 本臂 ＋ arm-agent-random"
                         "（P86/T4：同日两条 AI 臂 ⇒ 必须一次点名到齐，否则没写的臂被记 missing_decision）")
    ap.add_argument("--no-run", action="store_true",
                    help="只跑 ①–⑤（不跑 `paper agent run`）——P62 修好之前用这个")
    ap.add_argument("--prompt", default=None,
                    help="提示词路径；不给 ⇒ 按 `--arm` 查 ARM_PROMPTS 定（P86 / G5），"
                         "未登记的臂回退 v3")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--skip-random", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--repair-rounds", type=int, default=1,
                    help="载荷格式不合法时允许的**格式修复**轮数（默认 1；不是参数搜索）")
    args = ap.parse_args()

    asof = args.asof
    env = load_env(SECRETS)
    model = args.model or env["CMD_MODEL"]
    prompt_path = Path(args.prompt) if args.prompt else (
        PROMPTS_DIR / ARM_PROMPTS.get(args.arm, PROMPT_DEFAULT.name))
    args.prompt = str(prompt_path)
    prompt_text = prompt_path.read_text(encoding="utf-8")

    out_dir = Path(args.out_dir) if args.out_dir else (
        Path.home() / ".nanobot" / "workspace" / "artifacts" / "stock"
        / f"ai-trader-{asof}")
    out_dir.mkdir(parents=True, exist_ok=True)
    def cli(extra: list[str], *, timeout: int = 120):
        """项目 CLI 调用；`--db` 给了就逐条透传（演练只在副本上写）。"""
        return run_cli(extra, timeout=timeout, db=args.db)

    #: P86 T1.4：复盘回执三键的初值（v3 形态 ⇒ `no_review_key`，G5）。
    review_state = {"review_written": False, "review_id": None,
                    "review_skipped_reason": "no_review_key", "attempted": False}
    log(f"asof={asof} arm={args.arm} model={model}")
    log(f"prompt={args.prompt}")
    log(f"out={out_dir}")

    # 提示词指纹（项目的唯一算法）
    sha_proc = run_cli(["paper", "agent", "sha256", "--file", args.prompt])
    if sha_proc.returncode != 0:
        log(f"!! sha256 失败: {sha_proc.stderr.strip()}")
        return 2
    prompt_sha = json.loads(sha_proc.stdout)["sha256"]
    log(f"prompt_sha256={prompt_sha[:16]}…")

    # ① 随机对照臂（项目内；缺它 AI 读数一律"不可归因"）
    if not args.skip_random:
        r = cli(["paper", "agent", "random", "--asof", asof, "--arm", "arm-agent-random"])
        log(f"① random exit={r.returncode} {r.stdout.strip()[:200]}")
        if r.returncode not in (0, 1):
            log(f"!! random 失败: {r.stderr.strip()[:400]}")
            return 2
    else:
        log("① random 跳过（--skip-random）")

    # ② PIT 上下文
    r = cli(["paper", "agent", "context", "--asof", asof, "--arm", args.arm])
    if r.returncode != 0:
        log(f"!! context 失败({r.returncode}): {r.stderr.strip()[:600]}")
        return 2
    ctx_payload = json.loads(r.stdout)
    context_sha = ctx_payload["context_sha256"]
    (out_dir / "context.json").write_text(r.stdout, encoding="utf-8")
    log(f"② context ok  pool_codes={ctx_payload['pool_codes']} sha={context_sha[:16]}…")

    # ③ 模型产出决策
    user_content = (
        f"今日 asof = {asof}。下面是你的 PIT 上下文（JSON，只含 ≤ 当日的行）。"
        f"请给出当日目标组合，只输出一个 JSON 对象。\n\n"
        + json.dumps(ctx_payload["context"], ensure_ascii=False, indent=1)
    )
    (out_dir / "prompt.txt").write_text(prompt_text, encoding="utf-8")
    (out_dir / "user_message.txt").write_text(user_content, encoding="utf-8")

    rounds: list[dict] = []
    payload = None
    repair_note = ""
    #: P86 / G4：当天已经有一行决定（append-only 冲突 ⇒ `decide` exit 1）。
    #  这不是「今天的决定写不进去」—— 它**已经**写进去了，且 append-only 不许改写。
    decision_settled = False
    for attempt in range(args.repair_rounds + 1):
        log(f"③ 模型调用 第 {attempt + 1} 次…")
        call = call_model(env, model=model, system_prompt=prompt_text, user_content=user_content)
        content = (call["raw"].get("choices") or [{}])[0].get("message", {}).get("content", "")
        rec = {"attempt": attempt + 1, "elapsed_s": call["elapsed_s"],
               "model_echo": call["model_echo"],
               "system_fingerprint": call["system_fingerprint"],
               "usage": call["usage"], "content": content}
        try:
            candidate = extract_json(content)
        except json.JSONDecodeError as exc:
            rec["parse_error"] = str(exc)
            rounds.append(rec)
            repair_note = f"JSON 解析失败：{exc}"
            continue

        candidate.pop("asof", None)
        candidate.pop("context_sha256", None)
        # P86：`review` 段不属于 decision 载荷（`decide` 的键集是白名单，多一个就拒）⇒ 先摘出来。
        review = candidate.pop("review", None)
        if review is not None:
            review_state["review_skipped_reason"] = None
        candidate["asof"] = asof
        candidate["context_sha256"] = context_sha
        (out_dir / f"decision-attempt{attempt + 1}.json").write_text(
            json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")

        if args.dry_run:
            rec["dry_run"] = True
            rounds.append(rec)
            payload = candidate
            break

        # ③′ 复盘：**先写复盘、再写决策**（G3）。每天只试一次 —— 格式修复轮不重写。
        #   为什么顺序安全：`own_history` 的复盘窗口是 `asof < 决策日`（P86 修订）
        #   ⇒ 这条复盘**不会**进当天决策的上下文，② 的 `context_sha256` 依然成立。
        if review is not None and not review_state["attempted"]:
            review_state["attempted"] = True
            review_state.update(write_review(review, asof=asof, arm=args.arm,
                                             model=model, prompt_sha=prompt_sha,
                                             out_dir=out_dir, db=args.db))

        # 用项目自己的写入口做**校验**（--now 不影响；dry 前置没有，直接试写）
        decided = cli(["paper", "agent", "decide", "--asof", asof,
                           "--file", str(out_dir / f"decision-attempt{attempt + 1}.json"),
                           "--arm", args.arm, "--model-id", model,
                           "--prompt-sha256", prompt_sha])
        rec["decide_exit"] = decided.returncode
        rec["decide_stderr"] = decided.stderr.strip()[:800]
        rounds.append(rec)
        if decided.returncode == 0:
            payload = candidate
            break
        if decided.returncode == 1 and "已经有一行决定" in decided.stderr:
            # 重放通道（G4）。为什么重放会撞冲突：`own_history` 的**净值/成交窗口含当天**
            # （P84：`date <= asof`，不是复盘那种严格窗口）⇒ 第一次跑在 ⑥ 写下的当日净值行
            # 会改变同一天的上下文 ⇒ 指纹变 ⇒ `decide` 报「已有一行」。把这种重放当失败，
            # 会让日更 cron 的一次无害重跑把整条链染红（并丢掉回执）；而修复轮重试也只会
            # 再撞一次（既有行不会因为再喊一次就消失）。⇒ 记为**该日已 settled**、沿用既有行，
            # 继续走 ⑥：`paper agent run` 同日重放本身是幂等的（status=already）。
            decision_settled = True
            payload = candidate
            log("⑤ 该日已有一行决定（append-only 冲突）—— 沿用既有行，不重写")
            break
        repair_note = decided.stderr.strip()[:800]
        if attempt < args.repair_rounds:
            user_content = (
                user_content
                + "\n\n## 上一版被写入口拒绝，原因如下（请修正后重新输出完整 JSON）\n"
                + repair_note)
    (out_dir / "model-calls.json").write_text(
        json.dumps(rounds, ensure_ascii=False, indent=2), encoding="utf-8")

    if payload is None:
        log(f"!! 未拿到合法载荷；最后原因：{repair_note[:300]}")
        return 3
    (out_dir / "decision.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log("④ 载荷已落盘 decision.json")

    if args.dry_run:
        # G5：dry-run 路径**不新产回执文件**（原来的行为就是零回执）—— 只在日志里报复盘状态。
        log("dry-run：不执行 review/decide/run")
        log(json.dumps({"dry_run": True, "review_skipped_reason": "dry_run"},
                       ensure_ascii=False))
        return 0

    # ⑤ 落库（已在上面的校验轮里做过；若 dry 关且成功即已写入）
    if decision_settled:
        log("⑤ decide：该日已有决定（append-only）—— 沿用既有行，本次零写入")
    else:
        log("⑤ decide 已写入台账")

    # ⑥ 日终
    if args.no_run:
        log("⑥ run 跳过（--no-run）——决策已落台账；复盘状态："
            + json.dumps({k: review_state[k] for k in REVIEW_RECEIPT_KEYS},
                         ensure_ascii=False))
        return 0
    # P69 / T1：`paper agent run` **显式点名**在飞的臂。不点名 = 认领全部
    # `executor=agent_decision` 的账户，其中停飞/占位的（如 arm-agent-ds-v2）会被记成
    # 「缺决策」⇒ 交易日退出码恒 1（假警）。口径见 docs/ops/2026-09-23-AI操盘手-日更循环.md §⑤。
    claim_arms = list(dict.fromkeys(
        args.run_arm or ([args.arm]
                         + ([] if args.skip_random else ["arm-agent-random"]))))
    run_cmd = ["paper", "agent", "run", "--asof", asof]
    for _arm in claim_arms:
        run_cmd += ["--arm", _arm]
    log(f"⑥ run 认领范围={claim_arms}")
    r = cli(run_cmd, timeout=180)
    (out_dir / "run-receipt.json").write_text(r.stdout, encoding="utf-8")
    receipt = parse_run_receipt(r.stdout)
    # P86 T1.4：回执落到 `ai-trader-receipt.json`（`run-receipt.json` 仍是原始 stdout，不动）。
    dump_receipt(out_dir, receipt, review_state)
    log(f"⑥ run exit={r.returncode}")
    if r.stderr.strip():
        log(f"   stderr: {r.stderr.strip()[:400]}")
    log(json.dumps({k: receipt.get(k) for k in
                    ("asof", "traded", "missing_decision", "accounts", "anomaly")},
                   ensure_ascii=False)[:800])
    log(json.dumps({k: review_state[k] for k in REVIEW_RECEIPT_KEYS},
                   ensure_ascii=False)[:400])
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
