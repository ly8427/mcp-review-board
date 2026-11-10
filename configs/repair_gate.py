#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""repair-gate — 调用走样修复单机器闸(Stop/PostToolUse hook)。2026-10-26 用户批准实施。

V2.1(2026-11-10):按 GPT round-9 评审收口两处 P1——
  P1-1 直执两阶段化:治理检查(纯读)→ executing 状态持久化成功(失败=报警放行
    绝不执行,F6"状态未持久化绝不驱动副作用"在此落地)→ 执行 → done+证据落盘。
    任一环节断链,下轮看到 executing 无 done → 转人工核,绝不自动重执行
    (副作用窗口本地不可判,防重复写入);executing 封顶 MAX_BOUNCES → 报警。
  P1-2 治理拒绝分支补封顶(此前漏封:打卡持续不新鲜等场景会无限重试)。
  另:direct-exec 成功判据弱(无业务字段校验)与 PostToolUse 首扫锚点两处 P2
    维持 DEBT #10 残余留账(等返回形状实证/真实宿主验证)。
V2.0(2026-11-10):按 docs/DEBT.md #10 落地脚本直接执行面与时机(批次4.1)——
  ① 看板类修复单脚本直执:post_comment/reply_comment/create_thread 三类确认后由
    本脚本直接经 WSL rb.sh 执行(不经模型重发,消走样病灶通道),执行前置
    quorum_guard 同款治理检查(import 复用,不复制判据):board_alive 全员必过、
    create_thread 加 F6 打卡新鲜度 + quorum 名单校验——治理拒绝 = 带理由退回
    (模型可修前置,MAX_BOUNCES 封顶),不静默绕门。set_quorum/set_verdict 不进
    直执面(治理语义复杂,维持 V1 派发通道)。
  ② PostToolUse 时机:同一脚本容忍 PostToolUse 形状 payload(payload 本就
    payload-agnostic,bounce 的 {"decision":"block"} 两事件协议同形),回合内
    开出的单不必等收工;宿主接线=config.json hooks.events 加 PostToolUse 条目。
  ③ 残余收口:状态原子写改唯一临时名(并发 Stop/PostToolUse 不再共用 .tmp);
    直执面每次记录返回形状头 200 字节进日志(为"看板族业务成功字段"积累实证,
    V1.5 留账的后续);宿主真实走样端到端首验仍留账(等下一次真实走样,不可预演)。

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
import subprocess
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
    "mcp__review-board__post_comment",
    "mcp__review-board__reply_comment",
    "mcp__review-board__set_quorum",
    "mcp__review-board__set_verdict",
}
SHORT_TOOLS = {
    "create_thread": "mcp__review-board__create_thread",
    "post_comment": "mcp__review-board__post_comment",
    "reply_comment": "mcp__review-board__reply_comment",
    "set_quorum": "mcp__review-board__set_quorum",
    "set_verdict": "mcp__review-board__set_verdict",
}
# V2①:直执面白名单(确认后由脚本直接执行,不经模型重发)。
# set_quorum/set_verdict 治理语义复杂(创建者末票门/名单改写审计),维持 V1 派发通道。
DIRECT_TOOLS = {
    "mcp__review-board__post_comment",
    "mcp__review-board__reply_comment",
    "mcp__review-board__create_thread",
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

    def _keep(v):
        if not isinstance(v, dict):
            return False
        fresh = now - v.get("ts", 0) < STATE_TTL
        # V2.2(GPT round-10 P2):executing 未完成/已报警的条目不随 TTL 静默过期——
        # 副作用结果未定的记录消失 = 下一轮重执行风险;超期走报警留档,不删除
        pinned = (v.get("executing") and not v.get("done")) or v.get("alarm")
        return fresh or pinned

    return {h: v for h, v in st.items() if _keep(v)}


def save_state(st: dict) -> bool:
    """原子写(唯一临时名+替换)。返回是否成功——失败时调用方必须停发派发/催办
    指令并报警转人工(F6:状态没落盘,绝不驱动副作用)。
    V2③:临时名带 pid+时间戳——Stop 与 PostToolUse 钩子并发时不再共用 .tmp
    (评审第四轮非阻塞建议);并发写语义 = 各自原子替换、后写者胜(状态条目
    幂等,回执/登记重复由 L4/alloc_order_id 去重吸收)。"""
    tmp = f"{STATE_PATH}.tmp{os.getpid()}-{time.time_ns()}"
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


# ---- V2.2(GPT round-10 P1):跨进程互斥原语 ----
# V2.3(GPT round-11):双平台统一的有界等待——非阻塞请求 + 轮询,至多等
# LOCK_WAIT_SECS(默认 8s,env RB_REPAIR_LOCK_WAIT 可调),超时抛 OSError →
# 调用方 fail-closed 放行本回合、不做任何副作用。
# V2.4(GPT round-14):拆出 _try_lock 单次非阻塞原语——竞争测试用它做
# "已实际遭遇锁竞争"的确定性断言(先试锁失败、再发 WAITING),生产语义不变。
LOCK_WAIT_SECS = float(os.environ.get("RB_REPAIR_LOCK_WAIT", "8"))


def _try_lock(f):
    """单次非阻塞排他获取;成功=持有,忙/无原语=抛 OSError。"""
    f.seek(0)
    try:
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        f.seek(0, 2)
        return
    except ImportError:
        pass
    try:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_NB | fcntl.LOCK_EX)
        return
    except ImportError:
        raise OSError("no locking primitive available (neither msvcrt nor fcntl)")


def _lock_exclusive(f):
    deadline = time.time() + LOCK_WAIT_SECS
    while True:
        try:
            _try_lock(f)
            return
        except ImportError as e:
            raise OSError(str(e))
        except OSError:
            pass
        if time.time() >= deadline:
            raise OSError("claim lock busy (waited %.1fs)" % LOCK_WAIT_SECS)
        time.sleep(0.25)


def _unlock(f):
    try:
        f.seek(0)
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    except ImportError:
        try:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
    except Exception:
        pass


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

def _rollout_dir():
    """rollout 目录:RB_ROLLOUT_DIR 覆盖(测试/多实例),否则宿主默认。"""
    return os.environ.get("RB_ROLLOUT_DIR") or os.path.join(
        os.path.expanduser("~"), ".zcode", "cli", "rollout")


def _session_key(payload):
    sid = payload.get("session_id") if isinstance(payload, dict) else None
    return sid or os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("ZCODE_SESSION_ID")


def _rollout_file_for(payload, session_key=None):
    """V2.6(GPT round-17 P1):session → rollout 文件路径按 session 分区——
    扫描游标与核验锚点都必须落在**原始 session 自己的文件**上,不再全局单份
    (旧实现:跨会话 hook 互相覆盖游标 → 对方新增的修复单/回执永久漏扫)。
    AG_ROLLOUT 仍强制单文件(向后兼容单文件部署)。"""
    forced = os.environ.get("AG_ROLLOUT")
    if forced:
        return forced
    sid = session_key or _session_key(payload)
    if not sid:
        return None
    return os.path.join(_rollout_dir(), f"model-io-session_{sid}.jsonl")


def _rollout_path(payload):
    return _rollout_file_for(payload)


def rollout_size(payload) -> int:
    """当前 payload 对应 rollout 文件字节数。读不到返回 -1。"""
    path = _rollout_path(payload)
    if not path:
        return -1
    try:
        return os.path.getsize(path)
    except OSError:
        return -1


def rollout_bytes_after(path, offset):
    """V2.6(GPT round-17 P2):锚点后到 EOF 的全部字节,分块读,**无 1 MiB 截断**——
    证据核验(verify_execution)此前沿用截断读取:>1MiB 之后才出现的调用+结果
    会被误判为未核验而转人工。
    V2.7(GPT round-18):负/无效锚点一律拒绝(返回空=保守不核),绝不带病 seek——
    seek(-1) 抛 ValueError 会穿透 OSError 捕获、逃进顶层 fail-open,使核验
    长期失效(GPT round-18 P2 实景)。"""
    if not path or not isinstance(offset, int) or offset < 0:
        return ""
    try:
        if os.path.getsize(path) <= offset:
            return ""
        chunks = []
        with open(path, "rb") as f:
            f.seek(offset)
            while True:
                chunk = f.read(262144)
                if not chunk:
                    break
                chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", "replace")
    except OSError:
        return ""


def rollout_after(payload, offset) -> str:
    """兼容残留(V2.6 起证据核验改走 rollout_bytes_after;本函数已无调用方,
    留作外部脚本兼容面,下版本移除)。"""
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


def _file_id(path):
    """V2.5(P3-1):文件身份(轮转检测)。ino/dev 可用时优先,否则 mtime_ns+size。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    if getattr(st, "st_ino", 0) and getattr(st, "st_dev", 0):
        return "i%d:%d" % (st.st_dev, st.st_ino)
    return "m%d:s%d" % (st.st_mtime_ns, st.st_size)


def rollout_complete_lines_after(path, offset):
    """V2.5(GPT round-16 P2-1)/V2.6 改路径入参:锚点之后的新增字节中,只取
    **完整 JSONL 行**(以 \\n 结尾);返回 (完整行文本, 新偏移=锚点+完整行字节数)。
    分块读到当前文件末尾——无 1 MiB 截断(截断+指针越界=数据永久丢失,旧缺陷);
    未写完的尾行保留在文件里,偏移不越过它,下轮重读。轮转/锚点无效 → ("", -1)。"""
    if not path or not isinstance(offset, int) or offset < 0:
        return "", -1
    try:
        if os.path.getsize(path) <= offset:
            return "", offset
        chunks = []
        with open(path, "rb") as f:
            f.seek(offset)
            while True:
                chunk = f.read(262144)
                if not chunk:
                    break
                chunks.append(chunk)
        raw = b"".join(chunks)
        nl = raw.rfind(b"\n")
        if nl < 0:
            return "", offset            # 只有未完成尾行:一字不消费
        keep = raw[:nl + 1]
        return keep.decode("utf-8", "replace"), offset + len(keep)
    except OSError:
        return "", -1


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


def verify_execution(payload, tool: str, args: str, offset, session_key=None) -> bool:
    """F1+F4(V1.4)/V2.6(GPT round-17 P2):销账证据 = 锚点后 ①存在同一条记录内
    工具名+参数 双匹配的调用(带 id) ②该调用的执行结果(tool_use_id 配对)存在
    且 is_error 不为真。读取目标 = **原始 session 自己的 rollout**(session_key,
    不再跟随当前 payload 的会话),分块读到 EOF(无 1 MiB 截断——截断会使
    >1MiB 之后才出现的调用+结果被误判未核验而转人工)。
    只有调用没结果 / 结果带错 / 调用不带编号(格式不受支持) → 一律不算成功,
    保守待核,超限转人工——"发出过调用"绝不等于"修复成功"。"""
    path = _rollout_file_for(payload, session_key)
    if not path:
        return False
    appended = rollout_bytes_after(path, offset)
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


# ─────────────────── V2①:看板类脚本直接执行面 ───────────────────

DRYRUN = os.environ.get("RB_REPAIR_DRYRUN") == "1"


def _gov_check(tool: str, args: str):
    """直执前置:quorum_guard 同款治理检查(import 复用,判据不复制)。
    返回 (True, "") 或 (False, 拒绝理由)。治理拒绝 ≠ 闸故障——理由退回给
    模型修前置(如开轮询打卡),MAX_BOUNCES 封顶,绝不静默绕门(DEBT #10①)。"""
    try:
        import quorum_guard_hook as qg  # 同目录;die() 走 SystemExit
    except Exception as e:
        return False, f"治理模块不可导入({e})——按 fail-closed 处理,转人工"
    import contextlib
    import io as _io
    buf = _io.StringIO()
    try:
        with contextlib.redirect_stderr(buf):
            if tool == "mcp__review-board__create_thread":
                qg.check_poll_marker()                       # F6:发起者打卡新鲜度
                try:
                    names = json.loads(args).get("quorum") or []
                except ValueError:
                    names = []
                if names:
                    qg.check_quorum_names(names, qg.load_roster())   # 名单/形态/闸门
            qg.board_alive()                                 # 全员:板不可达不执行
        return True, ""
    except SystemExit:
        # V2.5(P2-3):捕获 quorum_guard die() 写到 stderr 的**具体理由**随 bounce
        # 传给模型——"自动修前置"必须让模型知道缺什么(打卡过期/名单不在册/板不可达)
        reason = (buf.getvalue() or "").strip()
        head = reason.splitlines()[0][:200] if reason else "quorum_guard 拒绝(理由未捕获)"
        return False, f"治理前置未过:{head}——请先修复前置再重开修复单"
    except Exception as e:
        return False, f"治理检查异常({e})——按 fail-closed 处理,转人工"


def _direct_exec(tool: str, args: str):
    """看板类直执:与 V1 派发同一条 rb.sh 通道,但由本脚本直接跑(不经模型)。
    成功判据(V2.5,GPT round-16 P2-2 收紧):退出码 0 + 输出可解析为 **JSON 对象**
    + 无 error 键 + 无显式失败标志(ok=false/success=false)。false/null/0/[]
    一律不算成功——无效返回不得销账。业务级成功字段仍是 DEBT #10 残余:
    当前保证=「返回未被既有规则判失败」,不含「业务目标已确认实现」。
    测试接缝:RB_REPAIR_EXEC_CMD 可替换命令模板({short}/{b64} 占位)。"""
    short = tool.replace("mcp__review-board__", "")
    b64 = base64.b64encode(args.encode("utf-8")).decode("ascii")
    tmpl = os.environ.get(
        "RB_REPAIR_EXEC_CMD",
        '~/opt/rb/rb.sh {short} "$(printf %s {b64} | base64 -d)"')
    cmd = tmpl.format(short=short, b64=b64)
    if DRYRUN:
        log("direct-exec-dryrun", f"{short} (dryrun,未触板)")
        return True, "dryrun"
    try:
        if os.name == "nt":
            r = subprocess.run(["wsl", "-e", "bash", "-c", cmd], capture_output=True,
                               timeout=60)
        else:
            r = subprocess.run(["bash", "-c", cmd], capture_output=True, timeout=60)
    except Exception as e:
        return False, f"执行异常({e})"
    out = (r.stdout or b"").decode("utf-8", "replace").strip()
    log("direct-exec-shape", f"{short} rc={r.returncode} head={out[:200]!r}")  # V2③:形状实证
    if r.returncode != 0:
        return False, f"rb.sh 退出码 {r.returncode}: {(r.stderr or b'').decode('utf-8', 'replace')[:200]}"
    if not out:
        return False, "返回为空"
    try:
        parsed = json.loads(out)
    except ValueError:
        return False, f"返回不是 JSON: {out[:200]}"
    if not isinstance(parsed, dict):
        return False, f"返回非 JSON 对象({type(parsed).__name__}): {out[:200]}"
    if parsed.get("error"):
        return False, f"返回带 error: {out[:200]}"
    if parsed.get("ok") is False or parsed.get("success") is False:
        return False, f"返回显式失败标志: {out[:200]}"
    return True, out[:200]


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

    def scan_text(text: str) -> None:
        for line in strip_code(text).splitlines():
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
                orders[(tool, args)] = _session_key(payload)   # V2.6:来源会话
                continue
            mr = RECEIPT_RE.match(line)
            if mr:
                receipts[mr.group(2)] = mr.group(1)

    for s in candidate_strings(payload):
        scan_text(s)

    # ---- V2.2(GPT round-10 P1):跨进程互斥——读→处理→存全程单飞 ----
    # Stop 与 PostToolUse 两入口可能并发:整台状态机(领单检查→executing 持久化→
    # 执行→结果落盘)持同一把排他锁串行;后到者要么排队等锁后重读最新状态
    # (看到 executing/done 即不再执行),要么等锁超时放行本回合(fail-closed)。
    # V2.1 的两阶段纪律(状态落盘先于副作用)在锁内保持不变。
    _lf = None
    try:
        _lf = open(STATE_PATH + ".claim", "a+")
        try:
            _lock_exclusive(_lf)
        except OSError:
            log("claim-lock-busy", "另一钩子实例处理中——本轮回放行,不做任何副作用")
            sys.stderr.write("[repair-gate] 领取锁忙,本轮回放行(fail-closed)\n")
            allow()
        try:
            state = load_state()
            now = time.time()

            # ---- V2.6(GPT round-17 P1):rollout 增量扫描,游标按 session 分区 ----
            # 每个会话有各自的 rollout 文件与各自的扫描游标(_scan.sessions[sid])
            # ——跨会话的 hook 不再互相覆盖游标(旧全局单份:A 锚定→B 重锚→A 的新
            # 单永久漏扫)。原地截断(同身份文件变小)→ 重锚(scan-shrunk);
            # 同身份截断后**又长回超过旧偏移**的窗口仍不可检测,留账 DEBT #10。
            sc = state.get("_scan")
            sessions = (sc.get("sessions") if isinstance(sc, dict) and isinstance(sc.get("sessions"), dict) else {}) or {}
            sid = _session_key(payload)
            path = _rollout_file_for(payload)
            cur = -1
            if path:
                try:
                    cur = os.path.getsize(path)
                except OSError:
                    cur = -1
            if path and sid:
                slot = sessions.get(sid) or {}
                if cur < 0:
                    # V2.6:文件尚未产生 → 锚 0。此后该 session 写出的任何内容
                    # 都会被增量扫描覆盖(旧逻辑整块跳过+不留锚=中间内容永久漏扫)
                    slot = {"off": 0, "fid": None, "size": 0, "ts": now}
                    sessions[sid] = slot
                    state["_scan"] = {"ts": now, "sessions": sessions}
                else:
                    prev_off = slot.get("off")
                    prev_fid = slot.get("fid")
                    prev_size = slot.get("size")
                    fid = _file_id(path)
                    rescan = (isinstance(prev_off, int) and 0 <= prev_off <= cur
                              and (prev_fid is None or prev_fid == fid)
                              and not (isinstance(prev_size, int) and cur < prev_size))
                    if rescan:
                        # V2.5(P2-1):只消费完整 JSONL 行,偏移推进到完整行末——
                        # >1MiB 分块全读(无截断丢数据);未写完的尾行保留下轮重读
                        text, new_off = rollout_complete_lines_after(path, prev_off)
                        for ln in text.splitlines():
                            ln = ln.strip()
                            if not ln:
                                continue
                            try:
                                rec = json.loads(ln)     # rollout 行是 JSON 记录:解析后递归抽串,
                            except ValueError:           # 否则嵌在 text 字段里的回执/单子匹配不到
                                log("scan-nonjson-line", ln[:60])   # V2.5:非 JSON 行可观测
                                scan_text(ln)            # 非 JSON 行按原文本扫(注释即承诺)
                                continue
                            for sx in candidate_strings(rec):
                                scan_text(sx)
                        slot = {"off": new_off, "fid": fid, "size": cur, "ts": now}
                    else:
                        # 首锚/轮转(身份变化)/锚点越界/原地截断 → 重锚当前位,绝不全扫;
                        # 各留一行日志可观测(P3-1)
                        reason = ("scan-anchor" if prev_off is None
                                  else "scan-shrunk" if isinstance(prev_size, int) and cur < prev_size
                                  else "scan-rotated")
                        log(reason, f"off={cur} fid={fid}")
                        slot = {"off": cur, "fid": fid, "size": cur, "ts": now}
                    sessions[sid] = slot
                    state["_scan"] = {"ts": now, "sessions": sessions}

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

            # ---- 新单登记(F3:独立编号;V2.6:登记来源 session——核验/派发锚点
            #      都必须回到**原始 session 自己的 rollout**,不随当前 hook 会话漂移) ----
            for (tool, args), src_sid in orders.items():
                h = alloc_order_id(state, tool, args)
                state[h] = {"ts": now, "bounces": 0, "confirmed": False, "tool": tool,
                            "args": args, "src_session": src_sid}
                log("order-register", f"{h} {tool} src={src_sid}")

            # ---- 台账状态机:驱动所有未结条目(不依赖正文提取,V1.2) ----
            msgs = []
            need_bounce = False
            for h, v in list(state.items()):
                if h.startswith("_"):
                    continue   # V2②:_scan 等内部键不是票据
                if v.get("done") or v.get("void") or v.get("alarm"):
                    continue
                if v.get("executing") and not v.get("done"):
                    # V2.2(GPT round-10 P2):executing 超 TTL(24h)未人工核 → 报警留行,
                    # 但条目保留不删(load_state 已豁免其过期)——静默消失 = 重执行风险
                    if now - v.get("ts", 0) >= STATE_TTL and not v.get("stale_alarmed"):
                        v["stale_alarmed"] = True
                        try:
                            with open(ALARM_PATH, "a", encoding="utf-8") as f:
                                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} 修复单 {h} {v.get('tool')} "
                                        f"executing 超24h未人工核——条目保留不删,请尽快人工核对看板\n")
                        except OSError:
                            pass
                        log("order-executing-stale", h)
                        sys.stderr.write(f"[repair-gate] 修复单 {h} executing 超24h未核,已报警留档\n")
                    # V2.1(P1-1):executing 已持久化 = 副作用窗口曾打开——本地无法区分
                    # 「已写成功但完成态没落盘」与「写了没成功」,绝不自动重执行;
                    # 转人工核,封顶后报警(GPT round-9:交给核验或人工)
                    if v.get("bounces", 0) >= MAX_BOUNCES:
                        v["alarm"] = True
                        try:
                            with open(ALARM_PATH, "a", encoding="utf-8") as f:
                                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} 修复单 {h} {v.get('tool')} "
                                        f"executing 状态多轮未核(执行结果未确认),转人工\n")
                        except OSError:
                            pass
                        log("order-alarm-executing", h)
                        sys.stderr.write(f"[repair-gate] 修复单 {h} 执行结果未确认,转人工\n")
                        continue
                    v["bounces"] = v.get("bounces", 0) + 1
                    msgs.append(f"修复单 {h} 处于 executing(执行已发起、完成态未落盘)。为防重复写入"
                                f"不会自动重试——请人工核对该写入是否已在看板上:在→结束回合;"
                                f"不在→重开新修复单。累计 {v['bounces']}/{MAX_BOUNCES}。")
                    need_bounce = True
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
                    if tool in DIRECT_TOOLS:
                        # V2.1(GPT round-9 P1-1/P1-2):直执两阶段化——
                        #   阶段0 治理检查(纯读,无副作用,失败可重试但受封顶);
                        #   阶段1 治理过 → 先把 executing 状态持久化成功(F6:状态落盘先于副作用),
                        #         落盘失败 → 报警放行,绝不执行;
                        #   阶段2 执行;成功 → done+证据落盘(再落盘失败也不重执——下轮看到
                        #         executing 无 done → 转人工核,绝不重复写);
                        #         未确认成功 → 保持 executing,转人工核(副作用窗口本地不可判,
                        #         绝不自动重试——GPT round-9:交给核验或人工)。
                        v["bounces"] = v.get("bounces", 0) + 1
                        ok_gov, why_gov = _gov_check(tool, args)
                        if not ok_gov:
                            # 治理拒绝:无副作用,可修前置后重试——但封顶(P1-2:此前漏封)
                            if v.get("bounces", 0) >= MAX_BOUNCES:
                                v["alarm"] = True
                                try:
                                    with open(ALARM_PATH, "a", encoding="utf-8") as f:
                                        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} 修复单 {h} {tool} "
                                                f"治理前置多次未过({why_gov[:80]}),转人工\n")
                                except OSError:
                                    pass
                                log("order-alarm-gov", h)
                                sys.stderr.write(f"[repair-gate] 修复单 {h} 治理前置多次未过,转人工\n")
                                continue
                            msgs.append(f"修复单 {h} 治理前置未过:{why_gov}(下轮自动重试治理检查,"
                                        f"累计 {v['bounces']}/{MAX_BOUNCES} 次后转人工)")
                            need_bounce = True
                            continue
                        # 阶段1:副作用前持久化 executing 意图——失败绝不执行(F6)
                        v["executing"] = True
                        v["verify_session"] = v.get("src_session")
                        if not save_state(state):
                            _save_failure_alarm()
                            allow()   # 状态没落盘 → 不驱动副作用,报警放行
                        # 阶段2:执行
                        done, detail = _direct_exec(tool, args)
                        if done:
                            v["done"] = True
                            v["finished"] = now
                            v["direct"] = True
                            v["exec_evidence"] = detail[:200]
                            log("order-direct-done", f"{h} {tool} {detail[:120]}")
                            continue   # done 落盘统一走末尾 save;若这次落盘失败,下轮走 executing 人工核,不重执
                        # 未确认成功:保持 executing——下轮转人工核,绝不自动重试
                        v["exec_unconfirmed"] = detail[:200]
                        log("order-direct-unconfirmed", f"{h} {tool} {detail[:120]}")
                        msgs.append(f"修复单 {h} 直执未确认成功:{detail}。为防重复写入,本单不会自动重试——"
                                    f"请人工核对看板上是否已有该写入:有→直接结束回合即可;没有→重开新修复单。")
                        need_bounce = True
                        continue
                    v["dispatched"] = True
                    # V2.6:核销锚点=**原始 session 自己的 rollout**此刻的字节位——
                    # 证据核验也将读同一文件(不随当前 hook 的会话漂移)
                    src_path = _rollout_file_for(payload, v.get("src_session"))
                    try:
                        v["dispatch_offset"] = os.path.getsize(src_path) if src_path else 0
                    except FileNotFoundError:
                        # V2.7(GPT round-18):文件尚未产生 → 新文件从头即派发后记录,
                        # 锚 0(负锚点会让核验 seek(-1) 崩溃逃进 fail-open,核验长期失效)
                        v["dispatch_offset"] = 0
                    except OSError:
                        v["dispatch_offset"] = -1   # 其他 I/O 故障:无效锚,核验侧拒读
                    v["verify_session"] = v.get("src_session")
                    v["bounces"] = v.get("bounces", 0) + 1
                    msgs.append(dispatch_instruction(h, tool, args))
                    need_bounce = True
                else:
                    if verify_execution(payload, tool, args, v.get("dispatch_offset"),
                                        v.get("verify_session")):
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
        finally:
            _unlock(_lf)
    finally:
        if _lf is not None:
            _lf.close()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 — fail-open 兜底,绝不 brick 会话
        sys.stderr.write(f"[repair-gate] 内部错误(放行): {e}\n")
        sys.exit(0)
