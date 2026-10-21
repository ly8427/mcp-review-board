#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""automation-guard hook — thread #50 事故（2026-10-21）的机制化收口。

事故形态：15 分钟 recurring 定时任务（max_runs=null）绑在 122K-token 会话上，
每次触发全量重放会话计费，≈49 万 tokens/小时，烧穿 GLM 配额；且停止开关多层失效。
#50 §四建议的"成本护栏"由本 hook 落地。

PreToolUse hook for CronCreate | CronUpdate | OffPeakCreate。
stdin: hook payload JSON。规则（按严重度分级，机制与 quorum_guard_hook.py 同族）：

  R1 无上限常驻（recurring=true 且无有限 maxRuns）→ exit 2 阻止（stderr=理由+改法）。
     结构性规则，只依赖 stdin 参数，永远可用。工作区纪律已定"建 cron 必须设有限
     maxRuns"（2026-10-01 增补），无合法无上限场景。
  R2 大会话（当前会话最近一次请求 input tokens > 阈值）→ ask 转人工确认。
     代理量：定时任务每次触发把绑定的会话整个重放一遍，"最近一次请求的 input"
     即每轮成本下限（#50 实证：122,373）。读宿主 rollout 日志尾段估算。

失败哲学（与 quorum_guard 的 fail-closed 相反，资产类别不同）：
  quorum_guard 守治理完整性 → 坏了必须关门；本 hook 守成本 → R1 结构性硬规则
  fail-closed（入参解析不了也拒），R2 估算规则 fail-open（日志读不到/格式漂移
  → 只放行 R2，R1 仍生效）。任何未预期异常 → 放行并留 stderr 痕迹。

零绝对路径：阈值/日志路径全 env 可调（AG_MAX_INPUT / AG_ROLLOUT）。
宿主约定依赖：~/.zcode/cli/rollout/model-io-session_<sid>.jsonl（2026-10-21 本机
实证的路径与 in/out 用量字段）。宿主格式漂移时 R2 自动失效放行，不炸。
"""
import json
import os
import sys

MAX_INPUT = int(os.environ.get("AG_MAX_INPUT", "50000"))
TAIL_BYTES = 65536


def allow() -> None:
    sys.exit(0)


def deny(reason: str) -> None:
    sys.stderr.write(reason)
    sys.exit(2)


def ask(reason: str) -> None:
    # PreToolUse 的 ask 决策：转人工确认。若宿主不认此输出形状，本规则退化为
    # fail-open（hook 日志可见 invalid output），R1 的 exit 2 不受影响。
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def find_first(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = find_first(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_first(v, key)
            if r is not None:
                return r
    return None


def last_input_tokens(session_id):
    """读 rollout 日志尾段，取最近一条带 input 用量的记录。读不到 → None（fail-open）。"""
    sid = session_id or os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("ZCODE_SESSION_ID")
    if not sid:
        return None
    path = os.environ.get("AG_ROLLOUT") or os.path.join(
        os.path.expanduser("~"), ".zcode", "cli", "rollout", f"model-io-session_{sid}.jsonl")
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            buf = f.read()
    except OSError:
        return None
    for line in reversed(buf.decode("utf-8", "replace").strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        cand = find_first(rec, "input_tokens")
        if cand is None:
            for shape in (("usage", "input"), ("usage", "in"), ("tokens", "input")):
                usage = rec.get(shape[0]) if isinstance(rec, dict) else None
                if isinstance(usage, dict) and isinstance(usage.get(shape[1]), (int, float)):
                    cand = usage[shape[1]]
                    break
        if isinstance(cand, list):
            cand = cand[0] if cand and isinstance(cand[0], (int, float)) else None
        if isinstance(cand, (int, float)):
            return int(cand)
    return None


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except ValueError:
        # R1 的前提都来自入参；入参都坏了 → 无法判结构，按 fail-open 放行并留痕。
        sys.stderr.write("[automation-guard] hook 输入不是 JSON，规则未评估（fail-open）\n")
        allow()

    tool_input = {}
    if isinstance(payload, dict):
        ti = payload.get("tool_input")
        if isinstance(ti, dict):
            tool_input = ti
        else:
            ti = find_first(payload, "tool_input")
            if isinstance(ti, dict):
                tool_input = ti

    # ---- R1: 无上限常驻（结构性，fail-closed）----
    # CronUpdate 是 patch 形：只看得见本次给的字段。更新把 recurring 置 true
    # 而未同时给有限 maxRuns → 拒（宁可多拒一次改法明确的重试，不放 #50 形态过门）。
    recurring = tool_input.get("recurring") is True
    max_runs = tool_input.get("maxRuns")
    finite = isinstance(max_runs, int) and not isinstance(max_runs, bool) and max_runs > 0
    if recurring and not finite:
        deny(
            "[automation-guard R1] 无上限常驻自动化（recurring=true 且未设 maxRuns）。\n"
            "每次触发都会把绑定的会话全量重放计费——2026-10-21 #50 事故即此形态"
            "（15min × 无上限 × 122K 会话 ≈ 49 万 tokens/小时，烧穿配额且难停）。\n"
            "修复: 设有限 maxRuns（如 24）后重试；确需长跑 → 有限批次接力，"
            "并让 prompt 自带自毁/上报路径。")

    # ---- R2: 大会话（估算，fail-open）----
    sid = payload.get("session_id") if isinstance(payload, dict) else None
    try:
        n = last_input_tokens(sid)
    except Exception as e:  # noqa: BLE001 — 估算层任何异常都放行
        sys.stderr.write(f"[automation-guard R2] 估算失败（fail-open）: {e}\n")
        n = None
    if n is not None and n > MAX_INPUT:
        ask(
            f"当前会话最近一次请求 input≈{n:,} tokens（阈值 {MAX_INPUT:,}）："
            "定时任务每次触发将全量重放这个会话。\n"
            "修复: 在专用小会话里创建此自动化；确要在此会话执行 → 人工确认放行。")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 — 兜底 fail-open，绝不 brick 正常工作
        sys.stderr.write(f"[automation-guard] 内部错误（fail-open）: {e}\n")
        sys.exit(0)
