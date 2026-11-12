r"""check-hooks：把 config.json 里每条 hook command 原样执行一次，判断"闸在不在岗"。

设计要点（来自 2026-10-22 事故，每条都对应一次真实错误）：
1) 判据是"原样执行"，不是"分段存在性检查"——否则 `...\python.exe 缺失的写法`（无 .exe）会被误判为死。
2) 读子进程输出必须显式指定编码——否则本检查器会重演被检查对象的 bug（GBK 当 UTF-8）。
3) 三类结果必须可区分：起来了 / 拦下了 / 没起来。fail-open 的闸"没起来"与"通过"必须能分开看。

用法：python check-hooks.py [--config <path>] [--hook <substr>]
退出码：0 = 全部在岗；1 = 有闸不在岗；2 = 配置本身读不出来。

runner 约束（pi #745-2，票8 验收②必须注明）：本脚本"原样执行"的命令含 cmd.exe /c +
Windows 宿主 python.exe ⇒ 只在 Windows 宿主跑得动；WSL 内跑会全部 [BAD] DEAD
（rc=None 命令无法执行）——那是环境差异，不是闸死。配置一律给绝对路径：
默认 ~/.zcode/cli/config.json 是 ZCode cli 形状；--config ~/.claude/settings.json
读 claude-code 形状（4.2 装闸起本机两份并存，形状差异见 load_commands）。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

DEFAULT_CFG = pathlib.Path.home() / ".zcode" / "cli" / "config.json"

# 各闸的最小冒烟载荷：能起不来/能拦下都要看得出来
SMOKE = {
    "quorum_guard_hook.py": {"tool_name": "mcp__review-board__create_thread",
                             "tool_input": {"title": "smoke", "author": "smoke", "quorum": ["a", "b"]}},
    "automation_guard_hook.py": {"tool_name": "CronCreate",
                                 "tool_input": {"recurring": True}},
    "plain_speak_gate.py": {"hook_event_name": "Stop", "text": "正常一句话。"},
}


def load_commands(cfg_path: pathlib.Path):
    """两种配置形状都读（票8 起本机两份并存，判形状的依据是有没有 events 中间层）：
    - ZCode cli：hooks.events.<Event> = [{matcher, hooks:[{command, timeoutMs}]}]
    - claude-code settings.json：hooks.<Event> = [{matcher, hooks:[{command, timeout(秒)}]}]
      ——claude 形状没有 events 中间层，把 hooks 本身当事件表遍历即可。
    V2（GPT round-19 P2-1）：结构不识别/条目形状错 → 抛 ValueError（main 落
    [FATAL] rc=2），不再静默跳过——「没检查到」绝不能解释为「检查通过」。
    """
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        raise ValueError("hooks 段缺失或不是对象")
    events = hooks.get("events")
    if events is None:
        events = hooks              # claude-code 形状：事件名直接是键
    elif not isinstance(events, dict):
        raise ValueError("hooks.events 不是对象")
    out = []
    for event, entries in events.items():
        if not isinstance(entries, list):
            raise ValueError(f"事件 {event} 的条目不是列表（形状不识别）")
        for entry in entries or []:
            if not isinstance(entry, dict):
                raise ValueError(f"事件 {event} 的条目不是对象")
            hooks_list = entry.get("hooks")
            # V1.1.4(GPT round-20 P2):hooks 缺失/null/空列表 = 结构错误,
            # 不再静默当空列表——不完整检查会伪装成「全部在岗」
            if not isinstance(hooks_list, list) or not hooks_list:
                raise ValueError(f"事件 {event} 的 hooks 缺失或为空")
            matcher = entry.get("matcher")
            for hook in hooks_list:
                if not isinstance(hook, dict):
                    raise ValueError(f"事件 {event} 的 hook 条目不是对象")
                out.append({"event": event, "matcher": matcher,
                            "command": hook.get("command") or "",
                            "timeout_ms": hook.get("timeoutMs")})
    return out


# 票8 三道闸（GPT round-19：配置里至少要能看到这三条接线，缺一即 BAD；
# GPT round-4 P1：从「文件名出现」升级为**结构化接线规则**——脚本能运行
# ≠ 宿主会在正确的事件调用它。每条规则 = (闸脚本名, 必须在的事件,
# matcher 必须覆盖的工具名)。覆盖语义与宿主一致：re.search(matcher, 工具名)
# 命中即算覆盖（模板形状 "mcp__review-board__(create_thread|...)"、宽正则、
# catch-all 无 matcher 都按宿主语义判）；matcher 缺失/空 = 全部工具。
EXPECTED_WIRING = (
    ("quorum_guard_hook.py", "PreToolUse",
     ("mcp__review-board__create_thread", "mcp__review-board__set_quorum",
      "mcp__review-board__set_verdict")),
    ("automation_guard_hook.py", "PreToolUse",
     ("CronCreate", "CronUpdate", "OffPeakCreate")),
    ("plain_speak_gate.py", "Stop", ()),
)


def _matcher_covers(matcher, tool: str) -> bool:
    """宿主语义：PreToolUse matcher 是对工具名做正则匹配；空/缺失 = catch-all。
    GPT round-5 P1-1：非法正则 = 不覆盖（BAD）——**禁止**退化成子串包含判断
    （未闭合括号的 matcher 里含全部工具名的子串会假绿，正是本检查器要抓的
    「看起来接线、实际不触发」）；matcher 类型不是字符串 = 不覆盖。"""
    if matcher is None or matcher == "":
        return True
    if not isinstance(matcher, str):
        return False
    try:
        return re.search(matcher, tool) is not None
    except re.error:
        return False   # 非法正则：宿主侧无法匹配 → 判 BAD，不退化


def check_wiring(cmds) -> int:
    """结构化接线验证（GPT round-4 P1）：每道闸必须在正确事件、matcher 覆盖
    其管辖工具。三脚本都在但事件接错 = 全 BAD——负向测试（test_check_hooks
    N6）钉死「脚本都能跑、检查器仍必须报非零」。"""
    bad = 0
    for script, event, tools in EXPECTED_WIRING:
        entries = [c for c in cmds if script in c["command"]]
        if not entries:
            print(f"[BAD] 预期闸未接线   {script} — 配置里看不到这条闸的 command")
            bad += 1
            continue
        ok = any(c["event"] == event
                 and all(_matcher_covers(c["matcher"], t) for t in tools)
                 for c in entries)
        if not ok:
            where = ", ".join(f"{c['event']}(matcher={c['matcher']!r})"
                              for c in entries)
            need = f"，matcher 须覆盖 {'、'.join(tools)}" if tools else ""
            print(f"[BAD] 接线不正确     {script} — 应接在 {event}{need}；实际: {where}")
            bad += 1
    return bad


def smoke_payload(command: str) -> bytes:
    for name, payload in SMOKE.items():
        if name in command:
            return json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return b"{}"


def run_command(command: str, payload: bytes, timeout_s: float = 20.0):
    """原样执行（shell=False + 交给 cmd 解析引号），显式指定编码读输出。"""
    cmdline = f'cmd.exe /c "{command}"'
    try:
        p = subprocess.run(cmdline, input=payload, capture_output=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return None, b"", b"TIMEOUT"
    except OSError as e:
        return None, b"", str(e).encode("utf-8", "replace")
    return p.returncode, p.stdout, p.stderr


def classify(rc, stdout: bytes, stderr: bytes) -> tuple[str, str]:
    """三态：alive-deny（拦截）/ alive-allow（放行）/ dead（起不来）。"""
    if rc is None:
        return "DEAD", "命令无法执行"
    out = stdout.decode("utf-8", errors="replace").strip()
    err = stderr.decode("utf-8", errors="replace").strip()
    if rc == 2:
        return "ALIVE_DENY", (err or out)[:120]
    if rc == 0 and out:
        # fail-open 型钩子用 JSON decision 表达拦截
        if '"block"' in out or '"ask"' in out or "deny" in out.lower():
            return "ALIVE_DENY", out[:120]
        return "ALIVE", out[:120]
    if rc == 0:
        return "ALIVE", "(静默放行)"
    return "ERROR", f"rc={rc} {(err or out)[:120]}"


# --- v2.10 (#77 ④ + #775 改判 F)：watcher 载体静态断言 ------------------------
# 三份载体（pi 模板 / claude 轮询提示词 / dsh 内联任务文本）必须：
#   ① 含 token_invalid 分支（档A 自修四步的文案在，唤醒才有可执行动作）；
#   ② 含 my_comment_due 分支（#79 先评论后投票）；
#   ③ "blocking=true" 必须在位（dsh #820 §3 / pi #823）：漂回「含某帖」不收窄
#      会复活 A5 要消灭的每轮骚扰；
#   ④ claude 载体的优先级链必须与服务端 reason 全序**逐字一致且唯一**
#      （GPT PR#6 review P2：含 verdict_stale 的完整链；旧链/重复链即红）；
#   ⑤ 机制层（curl/POST/JSON-RPC）**不得**出现 claim_token 调用——claim 只能由
#      被唤醒的成员自己发起（明文只显示一次，无人值守领取=把明文写进日志）；
#      文本里对成员的指令（rb.sh claim_token / claim_token(author=…)）不算机制调用。
WATCHERS = ("task-template-pi.txt", "claude-polling-prompt.txt", "dsh-watcher.sh")
_INVOKER = re.compile(r"(curl|wget|Invoke-WebRequest|tools/call|/mcp)[^\n]{0,120}claim_token",
                      re.IGNORECASE)
# 服务端 /attention 的 reason 全序——载体侧镜像用的是轮询**响应字段名**,
# 顺序一致但非逐字同名(awaiting_verdict↔awaiting_my_verdict, mentioned↔mentions_me;
# GPT PR#6 二轮:勿再写"逐字一致")。
_PRIORITY_CHAIN = ("token_invalid > awaiting_my_verdict > verdict_stale"
                   " > awaiting_floor_comment > mentions_me > new_comments")
_PRIORITY_OLD = "优先级 awaiting_my_verdict > mentions_me"  # v2.9 旧链，禁再出现


def check_watcher_texts(texts: dict | None = None) -> int:
    """texts 非空时按给定文本判定（负向测试注入口），否则读载体文件。"""
    here = pathlib.Path(__file__).resolve().parent
    bad = 0
    for name in WATCHERS:
        if texts is not None:
            text = texts.get(name)
            if text is None:
                print(f"[BAD] 载体缺文本   {name}")
                bad += 1
                continue
        else:
            p = here / name
            try:
                text = p.read_text(encoding="utf-8")
            except OSError as e:
                print(f"[BAD] 载体不可读   {name} — {e}")
                bad += 1
                continue
        miss = [k for k in ("token_invalid", "my_comment_due", "blocking=true")
                if k not in text]
        inv = _INVOKER.search(text)
        if name == "claude-polling-prompt.txt":
            # ④ 优先级链：完整且唯一（GPT PR#6 P2）
            n_chain = text.count(_PRIORITY_CHAIN)
            if n_chain != 1 or _PRIORITY_OLD in text:
                miss.append(f"优先级链(期望唯一含 verdict_stale 全序, 实际 {n_chain} 处)"
                            if n_chain != 1 else "旧优先级链残留")
            # GPT PR#32 四轮 P2 + 五轮 P1-2：打卡必须是轮询的真自动心跳，且
            # 路径与 settings 的 RB_POLL_MARKER 同源——提示词必须含 RB_POLL_MARKER
            # （环境变量同源）与打卡文件名；写死具体用户名路径 = 与模板 <你> 占位
            # 脱节（换用户名安装即错位），一律 BAD
            if "rb-claude-poll-heartbeat" not in text:
                miss.append("打卡步骤(rb-claude-poll-heartbeat:每轮成功轮询后必须刷新)")
            if "RB_POLL_MARKER" not in text:
                miss.append("打卡路径同源(须按 RB_POLL_MARKER 取路径,禁写死用户名)")
            if re.search(r">>\s*[\"']?C:/Users/[^/\"'\s]+/", text):
                miss.append("打卡命令写死了用户名路径(改 ${RB_POLL_MARKER:-...})")
        if miss or inv:
            print(f"[BAD] 载体断言     {name} — 缺 {miss or ''} "
                  f"{'机制层出现 claim_token 调用: ' + inv.group(0)[:60] if inv else ''}")
            bad += 1
        else:
            print(f"[OK ] 载体断言     {name} — v2.10 分支在位且收窄到 blocking=true，机制层无 claim 调用")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(DEFAULT_CFG))
    ap.add_argument("--hook", default=None, help="只检查 command 里含该子串的钩子")
    args = ap.parse_args()

    cfg = pathlib.Path(args.config)
    if not cfg.exists():
        print(f"[FATAL] 配置不存在: {cfg}")
        return 2
    try:
        cmds = load_commands(cfg)
    except Exception as e:  # JSON 坏了/结构不识别也算致命（GPT round-19 P2-1）
        print(f"[FATAL] 配置不可用: {e}")
        return 2

    if args.hook:
        cmds = [c for c in cmds if args.hook in c["command"]]
    elif not cmds:
        # GPT round-19 P2-1：空配置/没解析到任何钩子 = 检查器失职的伪装成功，必须失败
        print("[FATAL] 没有解析到任何钩子（配置为空或结构不识别）——"
              "「没检查到」不能解释为「检查通过」")
        return 2

    bad = 0
    if not args.hook:
        # 票8 三道闸 + GPT round-4 P1 结构化接线验证（对两种配置形状同样生效）
        bad += check_wiring(cmds)

    print(f"检查 {len(cmds)} 条钩子（配置: {cfg}）\n")
    for c in cmds:
        rc, out, err = run_command(c["command"], smoke_payload(c["command"]))
        state, detail = classify(rc, out, err)
        flag = "OK " if state.startswith("ALIVE") else "BAD"
        if not state.startswith("ALIVE"):
            bad += 1
        print(f"[{flag}] {state:<11} {c['event']:<11} {c['command'][:70]}")
        print(f"        rc={rc} | {detail}")
    print()
    bad += check_watcher_texts()  # v2.10：watcher 载体静态断言一并计入结论
    print("结论：" + ("全部在岗" if bad == 0 else f"{bad} 条不在岗"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
