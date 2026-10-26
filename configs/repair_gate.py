#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""repair-gate — 调用走样修复单机器闸(Stop hook)。2026-10-26 用户批准实施。

V1.1(2026-10-27):按外部评审(GPT,PR #8 Request Changes)修复三项——
  F1 核销证据改为三重关联:①派发时刻的字节锚点(只认锚点之后新增的记录,
     派发前的旧调用天然排除)②工具名命中③参数序列化命中(原样/紧凑/默认间距
     等变体)。任一不满足→保守待核,超限转人工,绝不误核销;
  F2 看板派发指令的参数改经 base64 传递,模型提供的 JSON 不再拼进 shell 命令,
     杜绝引号/命令替换被重解释;
  F3 修复单编号 = 工具+参数哈希前 6 位 + 序号后缀(-2/-3/…),同工具同参数的
     后续单子各自独立登记、独立生命周期,不再被旧记录吞掉。
V1.2(2026-10-27):短回复修复(用户追问证伪"真实回复不可能少于 30 字"的判断)——
  ①待办单子的催办/派发/核对不再依赖正文提取,状态机始终运行(否则单子静默停滞);
  ②正文提取兜底:无 30 字以上候选时,接受含修复单/回执行的短串
  (协议回执行如"执行 ab12cd"仅 9 字,轮询短汇报同样可能不足 30 字)。

背景:模型在处置类工具调用(CronDelete/CronUpdate/看板写入)上有"想说A发成B"的
生成层走样(2026-09-23/10-01/10-15/10-26 四次实证)。但**事后识别100%可靠**——
每次走样都被模型当场从工具结果认出。本闸把"认出之后的修复"从模型的重试路径
(09-23 八连败实证不可靠)移到固定流程上。

约定(见 ZCodeProject/AGENTS.md 2026-10-26 增补):认出走样后,在回复正文
**行首裸写**一行固定格式修复单;本闸负责 登记→回执确认→派发→核销,全程
退回式驱动,状态由本脚本的台账文件驱动。V1 **没有脚本直接执行面**——执行
一律派发给两条已实证通道:Cron 类=干净子代理(全新上下文,无走样先例);
看板类=Bash 经 WSL rb.sh(10-15 实证)。脚本直接执行面=V2 欠账,见 docs/DEBT.md #10。

防误触发五层:
  L1 行首精确前缀 `修复单::` + 白名单工具名 + 合法 JSON 参数;
  L2 代码块/反引号内的内容不扫描;
  L3 只扫当前这条回复(历史消息里的引用一律无效);
  L4 两段式确认:首次见到只登记,必须收到回执(`执行 <编号>` 或 `误报 <编号>`)
     才派发;两次无回执自动作废;
  L5 核销只认原始会话记录(rollout)里 派发之后 出现的 工具名+参数 双命中,
     不认模型自述;全部判定写日志 configs/repair-gate-log.txt。

失败哲学:脚本任何异常一律放行(fail-open,不 brick 会话)——因此本闸是
"漏了有人工兜底"的护栏,不是强制执行的安全闸;但"不确定的修复单"只往
作废/待确认 方向走,绝不往执行方向走。每张单退回有上限,超限写报警文件
转人工,绝不把回合锁在循环里。
"""
import base64
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
RECEIPT_RE = re.compile(r"^(执行|误报)\s+([0-9a-f]{6}(?:-[0-9]+)?)\s*$")
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
    longs = [s for s in keyed if len(s) >= 30]
    if longs:
        return max(longs, key=len)
    # V1.2 兜底:短回复也必须能处理——协议回执行(如"执行 ab12cd")不到 30 字,
    # 轮询短汇报也可能不到;任何含修复单/回执行(行首裸写)的候选串都接受。
    hits = [s for s in keyed if _has_action_line(s)]
    if hits:
        return max(hits, key=len)
    return None


def _has_action_line(s) -> bool:
    if not isinstance(s, str):
        return False
    for line in strip_code(s).splitlines():
        if ORDER_RE.match(line) or RECEIPT_RE.match(line):
            return True
    return False


def strip_code(text: str) -> str:
    """L2:围栏代码块与反引号行内代码整体剔除——讨论引用按规矩都在这里面。"""
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"`[^`\n]*`", "", text)
    return text


# ---------- rollout 读取与核销证据(F1 三重关联) ----------

def _rollout_path(payload):
    sid = payload.get("session_id") if isinstance(payload, dict) else None
    sid = sid or os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("ZCODE_SESSION_ID")
    path = os.environ.get("AG_ROLLOUT")
    if not path and sid:
        path = os.path.join(os.path.expanduser("~"), ".zcode", "cli", "rollout",
                            f"model-io-session_{sid}.jsonl")
    return path


def rollout_size(payload) -> int:
    """当前 rollout 文件字节数(派发时刻的锚点)。读不到返回 -1。"""
    path = _rollout_path(payload)
    if not path:
        return -1
    try:
        return os.path.getsize(path)
    except OSError:
        return -1


def rollout_after(payload, offset) -> str:
    """锚点之后新增的字节(上限 1 MiB)。文件不存在/比锚点还小(轮转清空)/锚点
    无效 → 空串 = 保守待核,绝不把锚点前的旧记录当证据。"""
    path = _rollout_path(payload)
    if not path or not isinstance(offset, int) or offset < 0:
        return ""
    try:
        if os.path.getsize(path) <= offset:
            return ""
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read(1048576).decode("utf-8", "replace")
    except OSError:
        return ""


def _name_pats(tool: str):
    return (f'"name": "{tool}"', f'"name":"{tool}"', f"'name': '{tool}'",
            f"<name>{tool}</name>")


def _arg_variants(args: str):
    """参数在记录里可能以多种序列化形态出现,全部尝试(漏配新形态=待核,不误核)。"""
    out = [args]
    try:
        parsed = json.loads(args)
        for kw in ({"ensure_ascii": False, "separators": (",", ":")},
                   {"ensure_ascii": False},
                   {"separators": (",", ":")},
                   {}):
            try:
                out.append(json.dumps(parsed, **kw))
            except (TypeError, ValueError):
                pass
    except ValueError:
        pass
    return [v for v in out if v]


def verify_execution(payload, tool: str, args: str, offset) -> bool:
    """F1:成功证据必须同时满足 ①在派发锚点之后 ②工具名命中 ③参数序列化命中。
    任一不满足 → False(保守待核,超限转人工)。"""
    appended = rollout_after(payload, offset)
    if not appended:
        return False
    if not any(p in appended for p in _name_pats(tool)):
        return False
    return any(v in appended for v in _arg_variants(args))


# ---------- 编号与派发指令 ----------

def alloc_order_id(state: dict, tool: str, args: str) -> str:
    """F3:编号 = 哈希前 6 位,被占用时追加序号后缀(-2/-3/…),保证每张新单
    独立登记、独立生命周期。"""
    base = hashlib.sha256(f"{tool}|{args}".encode("utf-8")).hexdigest()[:6]
    if base not in state:
        return base
    n = 2
    while f"{base}-{n}" in state:
        n += 1
    return f"{base}-{n}"


def dispatch_instruction(h: str, tool: str, args: str) -> str:
    if tool.startswith("Cron"):
        return (f"修复单 {h} 已确认。请**立即新开一个干净子代理**执行,任务书只有一句:"
                f"『调用 {tool} 工具,参数 {args},核对返回确认成功后报告结果』。"
                f"禁止在本会话原地重发该调用(走样病灶);完成前本回合不会结束。")
    short = tool.replace("mcp__review-board__", "")
    b64 = base64.b64encode(args.encode("utf-8")).decode("ascii")
    return (f"修复单 {h} 已确认。请在当前回合用 Bash 经 rb.sh 执行——参数经 base64 传递,"
            f"避免 shell 重解释引号(10-15 实证通道):\n"
            f"  wsl -e bash -c \"~/opt/rb/rb.sh {short} \\\"$(printf %s {b64} | base64 -d)\\\"\"\n"
            f"完成并核对返回成功前本回合不会结束。")


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except ValueError:
        allow()
    if not isinstance(payload, dict):
        allow()

    # V1.2:正文提取失败(超短回复)不再整体退出——新单/回执解析自然为空,
    # 但待办单子的催办/派发/核对不依赖正文,必须照常运行(否则单子静默停滞)。
    text = extract_text(payload)
    plain = strip_code(text) if isinstance(text, str) else ""
    state = load_state()
    now = time.time()

    # ---- 收集当前回复里的修复单与回执(L1/L2/L3) ----
    orders = []
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
            orders.append((tool, args))
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

    # ---- 新单登记(F3:独立编号) ----
    for tool, args in orders:
        h = alloc_order_id(state, tool, args)
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
            v["dispatch_offset"] = rollout_size(payload)  # F1:核销锚点=此刻文件字节位
            v["bounces"] = v.get("bounces", 0) + 1
            msgs.append(dispatch_instruction(h, tool, args))
            need_bounce = True
        else:
            if verify_execution(payload, tool, args, v.get("dispatch_offset")):
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
                # 报警文件的独立备份信号:标准错误流(宿主钩子日志可见),文件写失败也有痕
                sys.stderr.write(f"[repair-gate] 修复单 {h} {tool} 多次派发未完成,转人工"
                                 f"(报警文件 {ALARM_PATH})\n")
                continue  # 放行收工,报警文件留痕
            v["bounces"] = v.get("bounces", 0) + 1
            msgs.append(
                f"修复单 {h} 在派发之后的原始会话记录中未找到 {tool} 与参数的执行痕迹。"
                f"若确已执行完,直接结束回合即可(本闸下次会核到);若尚未执行,按派发指令继续。")
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
