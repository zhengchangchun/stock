#!/usr/bin/env python3
"""AI 操盘手日更循环的**项目外**驱动（P56 / D-48 / D-49 / D-50）。

分工（任务书 §1）：
  项目内 = 校验 → 落库 → 执行 → 计账（零 API key、零联网、零模型调用）
  本脚本 = 项目外，负责：
     ① paper agent random   （随机对照臂，项目内跑，种子固定 ⇒ 可复现）
     ② paper agent context  （PIT 输入的唯一来源）
     ③ 调网关模型产出决策 JSON（唯一的联网点）
     ④ 注入 asof / context_sha256 落盘
     ⑤ paper agent decide   （预注册闸门 + 上下文指纹闸门）
     ⑥ paper agent run      （成交 + 写净值；P69 起**必须显式点名**在飞的臂，见下）

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
PROMPT_DEFAULT = Path.home() / ".nanobot" / "workspace" / "tools" / "stock" / "prompts" / "trader-v3.txt"
#: 提示词与 `--arm` 必须成对（`paper agent enroll` 把 `prompt_sha256` 预注册进账户，
#: `decide` 时逐字段比对，配错即拒）：
#:   `arm-agent-ds-v1` ↔ `trader-v1.txt`（`231c12ad…`）—— 已退役
#:   `arm-agent-ds-v2` ↔ `trader-v2.txt`（`e3c50bc2…`）—— 已退役（09-23/09-24 两天历史留档）
#:   `arm-agent-ds-v3` ↔ `trader-v3.txt`（`66200c1d…`）—— **现役**；v3 不再让模型心算一手成本，
#:     直接读 P81 的 `tradability`（`tradable` / `affordable_lots` / `min_weight_pct`），
#:     并把本金口径从「假设 2 万」改成上下文里的实值（5 万）。

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


def run_cli(args: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess:
    cmd = [str(PY), "-m", "stocklab.cli.main", *args]
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True, help="交易日 YYYY-MM-DD")
    ap.add_argument("--arm", default="arm-agent-ds-v3",
                    help="默认 v3（现役）：v3 读 tradability 的结论字段、不再心算一手成本；"
                         "v1/v2 两条臂留档不再日更（换提示词 = 换口径 ⇒ 开新版本账户）")
    ap.add_argument("--model", default=None, help="默认取 .secrets 里的 CMD_MODEL")
    ap.add_argument("--no-run", action="store_true",
                    help="只跑 ①–⑤（不跑 `paper agent run`）——P62 修好之前用这个")
    ap.add_argument("--prompt", default=str(PROMPT_DEFAULT))
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--skip-random", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--repair-rounds", type=int, default=1,
                    help="载荷格式不合法时允许的**格式修复**轮数（默认 1；不是参数搜索）")
    args = ap.parse_args()

    asof = args.asof
    env = load_env(SECRETS)
    model = args.model or env["CMD_MODEL"]
    prompt_text = Path(args.prompt).read_text(encoding="utf-8")

    out_dir = Path(args.out_dir) if args.out_dir else (
        Path.home() / ".nanobot" / "workspace" / "artifacts" / "stock"
        / f"ai-trader-{asof}")
    out_dir.mkdir(parents=True, exist_ok=True)
    log(f"asof={asof} arm={args.arm} model={model}")
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
        r = run_cli(["paper", "agent", "random", "--asof", asof, "--arm", "arm-agent-random"])
        log(f"① random exit={r.returncode} {r.stdout.strip()[:200]}")
        if r.returncode not in (0, 1):
            log(f"!! random 失败: {r.stderr.strip()[:400]}")
            return 2
    else:
        log("① random 跳过（--skip-random）")

    # ② PIT 上下文
    r = run_cli(["paper", "agent", "context", "--asof", asof, "--arm", args.arm])
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
        candidate["asof"] = asof
        candidate["context_sha256"] = context_sha
        (out_dir / f"decision-attempt{attempt + 1}.json").write_text(
            json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")

        if args.dry_run:
            rec["dry_run"] = True
            rounds.append(rec)
            payload = candidate
            break

        # 用项目自己的写入口做**校验**（--now 不影响；dry 前置没有，直接试写）
        decided = run_cli(["paper", "agent", "decide", "--asof", asof,
                           "--file", str(out_dir / f"decision-attempt{attempt + 1}.json"),
                           "--arm", args.arm, "--model-id", model,
                           "--prompt-sha256", prompt_sha])
        rec["decide_exit"] = decided.returncode
        rec["decide_stderr"] = decided.stderr.strip()[:800]
        rounds.append(rec)
        if decided.returncode == 0:
            payload = candidate
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
        log("dry-run：不执行 decide/run")
        return 0

    # ⑤ 落库（已在上面的校验轮里做过；若 dry 关且成功即已写入）
    log("⑤ decide 已写入台账")

    # ⑥ 日终
    if args.no_run:
        log("⑥ run 跳过（--no-run）——决策已落台账，等 P62 修好再执行")
        return 0
    # P69 / T1：`paper agent run` **显式点名**在飞的臂。不点名 = 认领全部
    # `executor=agent_decision` 的账户，其中停飞/占位的（如 arm-agent-ds-v2）会被记成
    # 「缺决策」⇒ 交易日退出码恒 1（假警）。口径见 docs/ops/2026-09-23-AI操盘手-日更循环.md §⑤。
    run_cmd = ["paper", "agent", "run", "--asof", asof, "--arm", args.arm]
    if not args.skip_random:
        run_cmd += ["--arm", "arm-agent-random"]
    log(f"⑥ run 认领范围={run_cmd[4:]}")
    r = run_cli(run_cmd, timeout=180)
    (out_dir / "run-receipt.json").write_text(r.stdout, encoding="utf-8")
    receipt = parse_run_receipt(r.stdout)
    log(f"⑥ run exit={r.returncode}")
    if r.stderr.strip():
        log(f"   stderr: {r.stderr.strip()[:400]}")
    log(json.dumps({k: receipt.get(k) for k in
                    ("asof", "traded", "missing_decision", "accounts", "anomaly")},
                   ensure_ascii=False)[:800])
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
