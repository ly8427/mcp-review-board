#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""repair-gate — 调用走样修复单机器闸(Stop hook)。2026-10-26 用户批准实施。

V1.3(2026-10-27):按评审第二轮收口 F1——核销证据从"锚点后区域内 工具名与参数
  两个字符串分别命中"升级为"锚点后**同一条结构化调用记录**内 工具名+参数 同时
  匹配"(逐行解析 JSON,记录内递归查找;参数比对支持结构相等与字符串变体,键序
  无关);名称与参数分属不同记录/不同行 = 不算证据(保守待核转人工)。另修 V1.2
  边界:不再"单选一条最长正文",而是扫描全部回复候选串——长正文与独立短回执
  并存时两者都能被读到。
V1.4(2026-10-27):按评审第三轮收口 F4——销账语义从"观察到目标调用"升级为
  "观察到目标调用**且其执行结果(tool_use_id 配对)成功**":调用带唯一 id,结果
  记录 {type:tool_result, tool_use_id, is_error} 按编号回指(格式为 2026-10-27
  本机真实 rollout 采样实证);只有调用没结果 / 结果带错 / 调用无编号 → 不销账,
  保守待核转人工。
V1.5(2026-10-27):按评审第四轮收口 F5/F6——
  F5:CronDelete 增加业务成功字段校验(结果须含 deleted:true,防"正常返回但
  业务未达成");无业务字段工具(看板族)的 V1 保证范围 = "调用结果未报错",
  不含"业务目标已确认实现"(留档,待实证返回形状后再加字段);
  F6:状态文件改原子写(临时文件+替换);写失败 → 停发一切派发/催办指令,
  报警(文件+标准错误)转人工,放行收工——状态未持久化绝不驱动副作用。
V1.6(2026-10-27):按评审第五轮收口 F7——业务成功判定从子串搜索改为
  **解析结果 JSON 后校验指定字段为 True**(CronDelete: result["deleted"]
  is True);content 非字符串/解析失败/非对象/字段非 True → 一律待核,
  不猜格式,绝不因"文本里出现过成功字样"而销账。
V1.2(2026-10-27):短回复修复(用户追问证伪"真实回复不可能少于 30 字"的判断)——
  待办单子的催办/派发/核对不再依赖正文提取,状态机始终运行;正文提取兜底接受
  含修复单/回执行的短串(V1.3 起由"扫描全部候选串"取代)。
V1.1(2026-10-27):按外部评审(GPT,PR #8 Request Changes 第一轮)修复——
  F1 核销证据加派发时刻字节锚点(只认锚点后新增记录)+工具名+参数;
  F2 看板派发指令参数改经 base64 传递,模型提供的 JSON 不拼进 shell 命令;
  F3 修复单编号 = 哈希前 6 位 + 序号后缀(-2/-3/…),同请求可重复独立登记。

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
  L4 两步式确认:首次见到只登记,必须收到回执(`执行 <编号>` 或 `误报 <编号>`)
     才派发;两次无回执自动作废;
  L5 核销只认原始会话记录(rollout)中 派发之后 同一条结构化调用记录内的
     工具名+参数双匹配,不认模型自述;全部判定写日志 configs/repair-gate-log.txt。

失败哲学:脚本任何异常一律放行(fail-open,不 brick 会话)——因此本闸是
"漏了有人工兜底"的护栏,不是强制执行的安全闸;但"不确定的修复单"只往
作废/待确认 方向走,绝不往执行方向走。每张单退回有上限,超限写报警文件
(并同步标准错误流一行,宿主钩子日志可见)转人工,绝不把回合锁在循环里。
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

_NAME_KEYS = ("name", "tool_name", "toolName")
_ARGS_KEYS = ("arguments", "input", "parameters", "params", "args", "tool_input", "toolInput")


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


def save_state(st: dict) -> bool:
    """原子写(临时文件+替换)。返回是否成功——失败时调用方必须停发派发/催办
    指令并报警转人工(F6:状态没落盘,绝不驱动副作用)。"""
    tmp = STATE_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_PATH)
        return True
    except OSError as e:
        log("state-write-fail", str(e))
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def _save_failure_alarm() -> None:
    msg = (f"修复单状态文件写入失败: {STATE_PATH} —— 修复单流程已停发一切指令,"
           f"本回合放行;请人工检查磁盘/权限后重试(未落盘的单子会重新登记)。")
    try:
        with open(ALARM_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass
    sys.stderr.write(f"[repair-gate] {msg}\n")


def candidate_strings(payload):
    """所有"像回复"的字段里的字符串(V1.3:不限长度、不选唯一一条——长正文与
    独立短回执并存时都要能被扫到;短杂串没有行动行,扫了也无害)。"""
    def strings(o):
        if isinstance(o, str):
            yield o
        elif isinstance(o, dict):
            for v in o.values():
                yield from strings(v)
        elif isinstance(o, list):
            for v in o:
                yield from strings(v)
    out = []
    if isinstance(payload, dict):
        for k, v in payload.items():
            if re.search(r"response|preview|last|message|assistant|text|output", str(k), re.I):
                out += [s for s in strings(v) if isinstance(s, str)]
    return out


def strip_code(text: str) -> str:
    """L2:围栏代码块与反引号行内代码整体剔除——讨论引用按规矩都在这里面。"""
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"`[^`\n]*`", "", text)
    return text


# ---------- rollout 读取与核销证据(F1:同记录结构化匹配) ----------

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


def _arg_variants(args: str):
    """参数以字符串形式出现时的已知序列化变体(漏配新形态=待核,不误核)。"""
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


def _value_matches_args(v, target, variants) -> bool:
    """单条记录里的参数字段与目标参数比对:字符串形式对照变体表(或再解析后
    结构比较);对象形式直接结构相等(键序无关)。对不上=不算。"""
    if isinstance(v, str):
        if v in variants:
            return True
        if target is not None:
            try:
                return json.loads(v) == target
            except ValueError:
                return False
        return False
    if target is not None:
        return v == target
    return False


def _collect_matching_calls(obj, tool: str, target, variants, out: list) -> None:
    """在单条结构化记录(含嵌套)内找:名称字段 == tool 且参数字段与目标参数匹配
    的调用;收集其 id(真实记录格式:toolCalls[].{id,name,input},2026-10-27 本机
    实测采样)。名称与参数分属不同记录/不同行 → 永不拼凑。"""
    if isinstance(obj, dict):
        name = next((obj[k] for k in _NAME_KEYS if isinstance(obj.get(k), str)), None)
        if name == tool:
            for k in _ARGS_KEYS:
                if k in obj and _value_matches_args(obj[k], target, variants):
                    cid = obj.get("id")
                    out.append(cid if isinstance(cid, str) else None)
                    break
        for v in obj.values():
            _collect_matching_calls(v, tool, target, variants, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_matching_calls(v, tool, target, variants, out)


def _collect_results(obj, out: list) -> None:
    """收集工具执行结果(真实记录格式:{type:"tool_result",tool_use_id,is_error,
    content},按 tool_use_id 与调用 id 配对);content 一并留作业务成功校验。"""
    if isinstance(obj, dict):
        if obj.get("type") == "tool_result" and isinstance(obj.get("tool_use_id"), str):
            out.append((obj["tool_use_id"], bool(obj.get("is_error")),
                        obj.get("content")))
        for v in obj.values():
            _collect_results(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_results(v, out)


# F5/F7(评审第四、五轮):业务成功 = **解析结果 JSON 后指定字段为 True**。
# 不做子串搜索——"结果文本里恰好出现这几个字符"不算成功(评审反例:
# {"deleted": false, "debug": "previous: \"deleted\": true"})。
# content 非字符串/解析失败/非对象/字段非 True → 一律待核,不猜格式。
_SUCCESS_FIELDS = {"CronDelete": ("deleted",)}


def _result_success(tool: str, err: bool, content) -> bool:
    if err:
        return False
    fields = _SUCCESS_FIELDS.get(tool)
    if not fields:
        return True  # 无业务字段工具:V1 保证="结果未报错"(文件头 F5 边界声明)
    if not isinstance(content, str):
        return False
    try:
        result = json.loads(content)
    except ValueError:
        return False
    if not isinstance(result, dict):
        return False
    return all(result.get(f) is True for f in fields)


def verify_execution(payload, tool: str, args: str, offset) -> bool:
    """F1+F4(V1.4):销账证据 = 锚点后 ①存在同一条记录内 工具名+参数 双匹配的
    调用(带 id) ②该调用的执行结果(tool_use_id 配对)存在且 is_error 不为真。
    只有调用没结果 / 结果带错 / 调用不带编号(格式不受支持) → 一律不算成功,
    保守待核,超限转人工——"发出过调用"绝不等于"修复成功"。"""
    appended = rollout_after(payload, offset)
    if not appended:
        return False
    try:
        target = json.loads(args)
    except ValueError:
        target = None
    variants = _arg_variants(args)
    call_ids = []
    results = []
    for line in appended.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        _collect_matching_calls(rec, tool, target, variants, call_ids)
        _collect_results(rec, results)
    ids = {cid for cid in call_ids if cid}
    if not ids:
        return False
    ok = {rid for rid, err, content in results if _result_success(tool, err, content)}
    return any(rid in ok for rid in ids)


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

    # ---- 收集全部候选串里的修复单与回执(L1/L2/L3;V1.3 扫描全部候选串) ----
    orders = {}
    receipts = {}
    for s in candidate_strings(payload):
        for line in strip_code(s).splitlines():
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
                orders[(tool, args)] = True
                continue
            mr = RECEIPT_RE.match(line)
            if mr:
                receipts[mr.group(2)] = mr.group(1)

    state = load_state()
    now = time.time()

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
    for (tool, args) in orders:
        h = alloc_order_id(state, tool, args)
        state[h] = {"ts": now, "bounces": 0, "confirmed": False, "tool": tool, "args": args}
        log("order-register", f"{h} {tool}")

    # ---- 台账状态机:驱动所有未结条目(不依赖正文提取,V1.2) ----
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
                # 报警文件的独立备份信号:标准错误流(宿主钩子日志可见)
                sys.stderr.write(f"[repair-gate] 修复单 {h} {tool} 多次派发未完成,转人工"
                                 f"(报警文件 {ALARM_PATH})\n")
                continue  # 放行收工,报警文件留痕
            v["bounces"] = v.get("bounces", 0) + 1
            msgs.append(
                f"修复单 {h} 在派发之后的原始会话记录中未找到『{tool} 以指定参数执行"
                f"且结果成功』的证据。若确已执行成功,直接结束回合即可(本闸下次会核到);"
                f"若尚未执行或执行失败,按派发指令继续或重开修复单。")
            need_bounce = True

    if not save_state(state):
        # F6:状态没落盘 → 停发一切派发/催办指令,报警转人工,放行收工
        _save_failure_alarm()
        allow()
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
