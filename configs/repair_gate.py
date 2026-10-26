#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""repair-gate — 调用走样修复单机器闸(Stop hook)。2026-10-26 用户批准实施。

背景:模型在处置类工具调用(CronDelete/CronUpdate/看板写入)上有"想说A发成B"的
生成层走样(2026-09-23/10-01/10-15/10-26 四次实证)。但**事后识别100%可靠**——
每次走样都被模型当场从工具结果认出。本闸把"认出之后的修复"从模型的重试路径
(09-23 八连败实证不可靠)移到固定流程上。

约定(见 ZCodeProject/AGENTS.md 2026-10-26 增补):认出走样后,在回复正文
**行首裸写**一行固定格式修复单;本闸负责 登记→回执确认→派发→核销,全程
退回式驱动,状态由本脚本的台账文件驱动(不要求后续回复重复单行)。V1 **没有
脚本直接执行面**——执行一律派发给两条已实证通道:Cron 类=干净子代理(全新
上下文,无走样先例);看板类=Bash 经 WSL rb.sh(10-15 实证)。脚本直接执行面
=V2 欠账(需先接入 quorum_guard 同款治理检查,否则绕过建帖门),见 docs/DEBT.md #10。

防误触发五层(用户 2026-10-26 点名担忧"讨论中引用格式会被误触发"):
  L1 行首精确前缀 `修复单::` + 白名单工具名 + 合法 JSON 参数;
  L2 代码块/反引号内的内容不扫描(讨论引用按规矩放代码块里);
  L3 只扫当前这条回复(历史消息里的引用一律无效);
  L4 两段式确认:首次见到只登记,必须收到回执(`执行 <编号>` 或 `误报 <编号>`)
     才派发——纯讨论性误触发在此层被拦;两次无回执自动作废;
  L5 核销只认原始会话记录(rollout)里出现该工具名的执行痕迹,不认模型自述;
     全部判定写日志 configs/repair-gate-log.txt。

失败哲学:脚本任何异常一律放行(fail-open,不 brick 会话);但"不确定的修复单"
只往 作废/待确认 方向走,绝不往执行方向走。每张单退回有上限,超限写报警文件
转人工,绝不把回合锁在循环里。
"""
import hashlib
import json
import os
import re
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.environ.get("RB_REPAIR_STATE", os.path.join(BASE, "repair-gate-state.json"))
LOG_PATH = os.environ.get("RB_REPAIR_LOG", os.path.join(BASE, "repair-gate-log.txt"))
ALARM_PATH = os.environ.get("RB_REPAIR_ALARM", os.path.join(BASE, "repair-gate-alarm.txt"))

ORDER_RE = re.compile(r"^修复单::\s*([A-Za-z_][A-Za-z0-9_.]*?)\s+(\{.*\})\s*$")
RECEIPT_RE = re.compile(r"^(执行|误报)\s+([0-9a-f]{6})\s*$")
ALLOW_TOOLS = {
    "CronDelete", "CronUpdate",
    "mcp__review-board__create_thread",
    "mcp__review-board__set_quorum",
    "mcp__review-board__set_verdict",
}
SHORT_TOOLS = {
    "create_thread": "mcp__review-board__create_thread",
    "set_quorum": "mcp__review-board__set_quorum",
    "set_verdict": "mcp__review-board__set_verdict",
}
MAX_BOUNCES = 4      # 每张单的退回上限(登记/确认/派发/催办合计),超限报警转人工
STATE_TTL = 86400    # 台账条目保质期(秒)


def log(event: str, detail: str = "") -> None:
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {event} {detail}\n")
    except OSError:
        pass


def allow() -> None:
    sys.exit(0)


def bounce(reason: str) -> None:
    sys.stdout.write(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    sys.exit(0)


def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        return {}
    if not isinstance(st, dict):
        return {}
    now = time.time()
    return {h: v for h, v in st.items() if isinstance(v, dict) and now - v.get("ts", 0) < STATE_TTL}


def save_state(st: dict) -> None:
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except OSError as e:
        log("state-write-fail", str(e))


def extract_text(payload):
    """取"刚写完的回复",与 plain_speak_gate.py 同款(生产已实证)。"""
    def strings(o):
        if isinstance(o, str):
            yield o
        elif isinstance(o, dict):
            for v in o.values():
                yield from strings(v)
        elif isinstance(o, list):
            for v in o:
                yield from strings(v)
    keyed = []
    if isinstance(payload, dict):
        for k, v in payload.items():
            if re.search(r"response|preview|last|message|assistant|text|output", str(k), re.I):
                keyed += [s for s in strings(v)]
    keyed = [s for s in keyed if len(s) >= 30]
    if keyed:
        return max(keyed, key=len)
    return None


def strip_code(text: str) -> str:
    """L2:围栏代码块与反引号行内代码整体剔除——讨论引用按规矩都在这里面。"""
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"`[^`\n]*`", "", text)
    return text


def rollout_tail(payload) -> str:
    """读原始会话记录尾段(核销证据来源)。读不到返回空串(核销保守处理)。"""
    sid = payload.get("session_id") if isinstance(payload, dict) else None
    sid = sid or os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("ZCODE_SESSION_ID")
    path = os.environ.get("AG_ROLLOUT")
    if not path and sid:
        path = os.path.join(os.path.expanduser("~"), ".zcode", "cli", "rollout",
                            f"model-io-session_{sid}.jsonl")
    if not path:
        return ""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 262144))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def executed_in_tail(tail: str, tool: str) -> bool:
    """L5:原始记录里出现该工具名的调用痕迹(多种记录格式兼容)。"""
    if not tail or not tool:
        return False
    for pat in (f'"name": "{tool}"', f'"name":"{tool}"', f"'name': '{tool}'",
                f"<name>{tool}</name>"):
        if pat in tail:
            return True
    return False


def dispatch_instruction(h: str, tool: str, args: str) -> str:
    if tool.startswith("Cron"):
        return (f"修复单 {h} 已确认。请**立即新开一个干净子代理**执行,任务书只有一句:"
                f"『调用 {tool} 工具,参数 {args},核对返回确认成功后报告结果』。"
                f"禁止在本会话原地重发该调用(走样病灶);完成前本回合不会结束。")
    short = tool.replace("mcp__review-board__", "")
    return (f"修复单 {h} 已确认。请在当前回合用 Bash 经 rb.sh 执行(换通道,10-15 实证):\n"
            f"  wsl -e bash -c \"~/opt/rb/rb.sh {short} '{args}'\"\n"
            f"完成并核对返回成功前本回合不会结束。")


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except ValueError:
        allow()
    if not isinstance(payload, dict):
        allow()

    text = extract_text(payload)
    if text is None:
        allow()

    plain = strip_code(text)
    state = load_state()
    tail = rollout_tail(payload)
    now = time.time()

    # ---- 收集当前回复里的修复单与回执(L1/L2/L3) ----
    orders = {}
    receipts = {}
    for line in plain.splitlines():
        mo = ORDER_RE.match(line)
        if mo:
            tool, args = mo.group(1), mo.group(2)
            tool = SHORT_TOOLS.get(tool, tool)
            if tool not in ALLOW_TOOLS:
                log("order-reject-tool", f"{tool} 不在白名单")
                continue
            try:
                json.loads(args)
            except ValueError:
                log("order-reject-json", args[:80])
                continue
            h = hashlib.sha256(f"{tool}|{args}".encode("utf-8")).hexdigest()[:6]
            orders[h] = (tool, args)
            continue
        mr = RECEIPT_RE.match(line)
        if mr:
            receipts[mr.group(2)] = mr.group(1)

    # ---- 回执落账(L4) ----
    for h, act in receipts.items():
        v = state.get(h)
        if not v:
            log("receipt-unknown", h)
            continue
        if act == "误报" and not v.get("done"):
            v["void"] = True
            v["done"] = True
            log("order-void", f"{h} {v.get('tool')}")
        elif act == "执行" and not v.get("confirmed"):
            v["confirmed"] = True
            log("order-confirmed", h)

    # ---- 新单登记 ----
    for h, (tool, args) in orders.items():
        if h not in state:
            state[h] = {"ts": now, "bounces": 0, "confirmed": False, "tool": tool, "args": args}
            log("order-register", f"{h} {tool}")

    # ---- 台账状态机:驱动所有未结条目 ----
    msgs = []
    need_bounce = False
    for h, v in list(state.items()):
        if v.get("done") or v.get("void") or v.get("alarm"):
            continue
        tool, args = v.get("tool", ""), v.get("args", "")
        if not v.get("confirmed"):
            if v.get("bounces", 0) >= 2:
                # 两次催要回执无果 → 按误触发作废(真单可随时重开,作废有日志)
                v["void"] = True
                v["done"] = True
                log("order-stale-void", h)
                continue
            v["bounces"] = v.get("bounces", 0) + 1
            msgs.append(
                f"修复单 {h} 已登记(工具 {tool},参数 {args})。防误触发确认步——"
                f"真实修复:单独一行回复『执行 {h}』;"
                f"只是讨论中引用了格式:单独一行回复『误报 {h}』作废。"
                f"两次无回执将自动作废。")
            need_bounce = True
        elif not v.get("dispatched"):
            v["dispatched"] = True
            v["bounces"] = v.get("bounces", 0) + 1
            msgs.append(dispatch_instruction(h, tool, args))
            need_bounce = True
        else:
            if executed_in_tail(tail, tool):
                v["done"] = True
                v["finished"] = now
                log("order-done", f"{h} {tool}")
                continue
            if v.get("bounces", 0) >= MAX_BOUNCES:
                v["alarm"] = True
                try:
                    with open(ALARM_PATH, "a", encoding="utf-8") as f:
                        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} 修复单 {h} {tool} {args} "
                                f"多次派发未完成,转人工处理\n")
                except OSError:
                    pass
                log("order-alarm", h)
                continue  # 放行收工,报警文件留痕
            v["bounces"] = v.get("bounces", 0) + 1
            msgs.append(
                f"修复单 {h} 在原始会话记录中未见 {tool} 的执行痕迹。若确已执行完,"
                f"直接结束回合即可(本闸下次会核到);若尚未执行,按派发指令继续。")
            need_bounce = True

    save_state(state)
    if need_bounce:
        bounce("[repair-gate] 修复单事务未结:\n" + "\n".join("- " + m for m in msgs))
    allow()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 — fail-open 兜底,绝不 brick 会话
        sys.stderr.write(f"[repair-gate] 内部错误(放行): {e}\n")
        sys.exit(0)
