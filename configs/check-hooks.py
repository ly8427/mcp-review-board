"""check-hooks：把 config.json 里每条 hook command 原样执行一次，判断"闸在不在岗"。

设计要点（来自 2026-10-22 事故，每条都对应一次真实错误）：
1) 判据是"原样执行"，不是"分段存在性检查"——否则 `...\python.exe 缺失的写法`（无 .exe）会被误判为死。
2) 读子进程输出必须显式指定编码——否则本检查器会重演被检查对象的 bug（GBK 当 UTF-8）。
3) 三类结果必须可区分：起来了 / 拦下了 / 没起来。fail-open 的闸"没起来"与"通过"必须能分开看。

用法：python check-hooks.py [--config <path>] [--hook <substr>]
退出码：0 = 全部在岗；1 = 有闸不在岗；2 = 配置本身读不出来。
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
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    out = []
    for event, entries in (data.get("hooks") or {}).get("events", {}).items():
        for entry in entries or []:
            matcher = entry.get("matcher")
            for hook in entry.get("hooks") or []:
                out.append({"event": event, "matcher": matcher, "command": hook.get("command") or "",
                            "timeout_ms": hook.get("timeoutMs")})
    return out


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
#   ③ 机制层（curl/POST/JSON-RPC）**不得**出现 claim_token 调用——claim 只能由
#      被唤醒的成员自己发起（明文只显示一次，无人值守领取=把明文写进日志）；
#      文本里对成员的指令（rb.sh claim_token / claim_token(author=…)）不算机制调用。
WATCHERS = ("task-template-pi.txt", "claude-polling-prompt.txt", "dsh-watcher.sh")
_INVOKER = re.compile(r"(curl|wget|Invoke-WebRequest|tools/call|/mcp)[^\n]{0,120}claim_token",
                      re.IGNORECASE)


def check_watcher_texts() -> int:
    here = pathlib.Path(__file__).resolve().parent
    bad = 0
    for name in WATCHERS:
        p = here / name
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as e:
            print(f"[BAD] 载体不可读   {name} — {e}")
            bad += 1
            continue
        miss = [k for k in ("token_invalid", "my_comment_due") if k not in text]
        inv = _INVOKER.search(text)
        if miss or inv:
            print(f"[BAD] 载体断言     {name} — 缺分支 {miss or ''} "
                  f"{'机制层出现 claim_token 调用: ' + inv.group(0)[:60] if inv else ''}")
            bad += 1
        else:
            print(f"[OK ] 载体断言     {name} — 两个 v2.10 分支在位，机制层无 claim 调用")
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
    except Exception as e:  # JSON 坏了也算致命
        print(f"[FATAL] 配置不是合法 JSON: {e}")
        return 2

    if args.hook:
        cmds = [c for c in cmds if args.hook in c["command"]]

    print(f"检查 {len(cmds)} 条钩子（配置: {cfg}）\n")
    bad = 0
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

