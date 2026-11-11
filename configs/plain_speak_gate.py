#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""plain-speak gate — "说人话"硬约束的机器闸（Stop hook）。

用户要求（2026-10-21/22）：向用户的汇报禁止黑话裸奔。文本规矩（AGENTS.md）只能降频
不能封死——与 quorum-guard / automation-guard 同一结论：上机器闸。

工作机制：宿主在 agent 结束每次回复时（Stop 事件）调用本脚本。脚本取出刚写完的回复
文本，对照词典（configs/plain-speak-dictionary.md，人可直接编辑）扫描黑话与项目编码，
发现"没带解释的黑话"→ 输出 block 决策把回复打回重写；干净/已解释/读不到 → 放行。

词典与设计借鉴 chenyongquan2/plain-chinese-skills（MIT）：markdown 表格词典 + 扫描
脚本 + "命中不是罪，句中有解释就保留"的豁免原则。

失败哲学：**fail-open**——脚本任何内部错误都放行（不拦正常说话），打回每轮最多一次
（宿主的 stop_hook_active 标记为真时不再拦），宿主自身另有三次上限。
"""
import json
import os
import re
import sys

DICT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plain-speak-dictionary.md")
EXPLAIN_MARKERS = ("（", "(", "，即", "（即", "就是", "指的是", "意思是", "——", "＝", "=")
HASH_CTX = re.compile(r"[\w-]")
MAX_HITS_REPORTED = 6


def allow() -> None:
    sys.exit(0)


def bounce(reason: str) -> None:
    sys.stdout.write(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    sys.exit(0)


def load_dictionary():
    """返回 (words{term: plain}, regexes[(compiled, demand)], allowlist set)。"""
    words, regexes, allowset = {}, [], set()
    section = None
    for line in open(DICT_PATH, encoding="utf-8"):
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")] if line.strip().startswith("|") else []
        if len(cells) < 2 or set(cells[0]) <= {"-", ":"}:
            continue
        term, plain = cells[0], cells[1]
        if section.startswith("一"):
            words[term] = plain
        elif section.startswith("二") and term.startswith("`") and term.endswith("`"):
            pat = term[1:-1]
            flags = re.IGNORECASE if "(?i)" in pat else 0
            pat = pat.replace("(?i)", "")
            try:
                regexes.append((re.compile(pat, flags), plain))
            except re.error:
                continue
        elif section.startswith("四"):
            words.setdefault(term, plain)
    # 白名单（第三节标题下的正文行，词以顿号分隔）
    in_three = False
    for line in open(DICT_PATH, encoding="utf-8"):
        if line.startswith("## "):
            in_three = line.startswith("## 三")
            continue
        if in_three:
            m = re.findall(r"[\u4e00-\u9fffA-Za-z]+", line)
            allowset.update(x for x in m if x not in ("这些是", "日常", "技术", "用语"))
    return words, regexes, allowset


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


# 渐进窗口（V1.1.4）：起始 64KB，找不到 assistant 记录时 ×4 倍增至 CAP——
# ≥64KB 单条长回复不再被切掉（dsh #2041 §一 / pi #2042 执行实证的漏检）。
# GPT round-21 P1：WINDOW_CAP 常量此前漏定义——渐进分支一进扩展即 NameError、
# 被 fail-open 吞成静默放行，长回复漏检路径形同未修；本常量为该路径的生效前提。
WINDOW_START = 65536
WINDOW_CAP = 8 * 1024 * 1024
WINDOW_GROWTH = 4


def _assistant_text(rec):
    """从一条 JSONL 记录提取 assistant 回复文本；非 assistant 记录返回 None。"""
    if not isinstance(rec, dict):
        return None
    msg = rec.get("message") if isinstance(rec.get("message"), dict) else rec
    role = msg.get("role") if isinstance(msg, dict) else None
    if rec.get("type") != "assistant" and role != "assistant":
        return None
    content = msg.get("content") if isinstance(msg, dict) else None
    parts = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" \
                    and isinstance(item.get("text"), str):
                parts.append(item["text"])
    if isinstance(rec.get("text"), str):
        parts.append(rec["text"])          # 简式记录（fixture/部分宿主）
    text = "\n".join(p for p in parts if p)
    return text or None


def last_assistant_text_in_tail(path):
    """读 JSONL 尾窗，返回 (最后一条 assistant 回复文本 | None, 原因)。

    V1.1.4（board #109 五缺口收口，dsh #2041/pi #2042）：
      * **渐进窗口**：从 64KB 起，找不到 assistant 记录且文件仍有更早内容时
        窗口按 WINDOW_GROWTH 倍增、min 夹到 WINDOW_CAP——≥64KB 的单条长回复
        不再被窗口整条切掉（pi #2042 执行实证：70KB 黑话回复旧版会漏检）。
      * **出口可区分（7 态）**：found / no-assistant-in-file / tail-unparsed /
        cap-reached / no-file / unreadable / empty——V1.1.3 曾把三种病因压进
        同一个 None 出口（dsh #2041 §一），诊断从此可分。
      * 尾行（最后一条非空行）解析失败/非 JSON 开头 → **立即** tail-unparsed
        （V1.1.6，dsh #2052 §二.4：扩窗解不开尾行不完整——它在文件尾，
        与窗口大小无关；禁止拿更早回复冒充）。
    zcode rollout 与 claude transcript（#713 §3/#714 收敛的第三条路）同一读法。
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return None, "no-file"
    window = WINDOW_START
    while True:
        start = max(0, size - window)
        try:
            with open(path, "rb") as f:
                f.seek(start)
                tail = f.read().decode("utf-8", "replace")
        except OSError:
            return None, "unreadable"
        lines = [l.strip() for l in tail.splitlines() if l.strip()]
        if not lines:
            return None, "empty"
        last = None
        final_unparsed = False
        # 窗口边界切出的首行（start>0 时可能是不完整记录的前半）：跳过
        body = lines[1:] if start > 0 else lines
        for i, line in enumerate(body):
            last_line_of_file = i == len(body) - 1   # 窗口永远延伸到文件尾
            if not line.startswith("{"):
                if last_line_of_file:
                    final_unparsed = True   # 尾行非 JSON 开头：无法确认
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                if last_line_of_file:
                    final_unparsed = True   # 尾行解析失败：本轮不确认
                continue
            text = _assistant_text(rec)
            if text:
                last = text                 # 记录序在后 = 更新；只记不比长度
        if last is not None and not final_unparsed:
            return last, "found"
        if start == 0:
            # 全文件已消费仍无可用回复
            if final_unparsed:
                return None, "tail-unparsed"
            return None, "no-assistant-in-file"
        if window >= WINDOW_CAP or window >= size:
            return None, "cap-reached"
        window = min(window * WINDOW_GROWTH, WINDOW_CAP)


def extract_text(payload):
    """取"刚写完的回复"，按权威级四路（V1.1.4，board #109 五缺口收口）：
    ① payload["last_assistant_message"]——claude Stop 官方权威字段，任意长度都算；
    ② payload 里名字像回复的字段——**显式"最近一条"键优先于最长串**
       （last_message > response > text > output；dsh 缺口②：最长串会让旧
       诊断回文冒充当前回复），同键内按文档序取**首个非空串**
       （V1.1.6，dsh #2052 §二.2：同键 diagnostics 长文不得冒充 text 正文）；
    ③ payload["transcript_path"]（claude 宿主自带）→ 尾窗读最后一条 assistant 回复；
    ④ 会话 rollout 日志尾窗（zcode）同一语义。
    ③④ 都取不到 → stderr 留**可区分原因**的诊断痕后 fail-open 放行
    （V1.1.4：found/no-assistant-in-file/tail-unparsed/cap-reached 不再合流）。
    尾读两条路都取**最后一条 assistant 回复**（V1.1.1，board #109）——
    禁止用尾段最长串冒充当前回复。"""
    if not isinstance(payload, dict):
        return None
    lam = payload.get("last_assistant_message")
    if isinstance(lam, str) and lam.strip():
        return lam
    keyed = {}
    for k, v in payload.items():
        if re.search(r"response|preview|last|message|assistant|text|output", str(k), re.I):
            # V1.1.6(dsh #2052 §二.2):同键内按文档序取**首个非空 str**——
            # 不再收全部嵌套串按长度选(同键 diagnostics 长文不得冒充 text 正文)
            first = next((x for x in _strings(v) if x.strip()), None)
            if first:
                keyed[k] = first
    # 显式"最近一条"键优先于其余(V1.1.4 ②;flat 兜底已删——同键内长度选择
    # 正是冒充通道,dsh #2052 §二.3)
    for pref in ("last_message", "response", "text", "output"):
        if pref in keyed:
            return keyed[pref]
    diag = []
    tp = payload.get("transcript_path")
    if isinstance(tp, str) and tp:
        best, reason = last_assistant_text_in_tail(tp)
        if best:
            return best
        diag.append(f"transcript 尾读未取得回复({reason}): {tp}")
    sid = payload.get("session_id")
    sid = sid or os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("ZCODE_SESSION_ID")
    if not sid:
        if diag:
            sys.stderr.write("[plain-speak] fail-open（无当前轮文本可查）: "
                             + "; ".join(diag) + "\n")
        return None
    path = os.environ.get("AG_ROLLOUT") or os.path.join(
        os.path.expanduser("~"), ".zcode", "cli", "rollout", f"model-io-session_{sid}.jsonl")
    best, reason = last_assistant_text_in_tail(path)
    if best:
        return best
    diag.append(f"rollout 尾读未取得回复({reason}): {path}")
    sys.stderr.write("[plain-speak] fail-open（无当前轮文本可查）: " + "; ".join(diag) + "\n")
    return None


def strip_code_and_urls(text: str) -> str:
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"`[^`\n]*`", "", text)
    return re.sub(r"https?://\S+", "", text)


def explained(line: str, after: str) -> bool:
    return any(m in after[:40] for m in EXPLAIN_MARKERS)


def scan(text, words, regexes, allowset):
    hits = []
    lines = strip_code_and_urls(text).splitlines()
    for lno, line in enumerate(lines, 1):
        if not line.strip():
            continue
        for term, plain in words.items():
            if term in allowset or len(term) < 2:
                continue
            idx = line.find(term)
            if idx >= 0 and not explained(line, line[idx + len(term):]):
                hits.append((lno, term, plain))
                if len(hits) >= MAX_HITS_REPORTED * 3:
                    return hits
        for rx, demand in regexes:
            for m in rx.finditer(line):
                frag = m.group(0)
                if frag in allowset:
                    continue
                ctx_ok = ("#" in rx.pattern and re.search(r"板|帖|评论|议题|讨论|thread|issue",
                           line[max(0, m.start() - 30):m.end() + 15]))
                if not ctx_ok and not explained(line, line[m.end():]):
                    hits.append((lno, frag, demand))
                    break
    return hits


def _skip_for_cursor_host(payload: dict | None = None) -> bool:
    """Cursor IDE opted out of this gate (2026-11-12 user request).

    Cursor loads Claude Code Stop hooks from ~/.claude/settings.json (third-party
    hooks). The hook child often lacks CURSOR_AGENT but still gets Cursor hook
    env (CURSOR_VERSION / CURSOR_PROJECT_DIR / CURSOR_TRANSCRIPT_PATH, …) and/or
    Cursor-native stop fields (status + loop_count). claude-code CLI and zcode
    do not set those → they keep the gate. Force-on: REVIEWBOARD_PLAIN_SPEAK_FORCE=1.
    """
    if os.environ.get("REVIEWBOARD_PLAIN_SPEAK_FORCE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return False
    # Agent worker
    if os.environ.get("CURSOR_AGENT"):
        return True
    # Cursor hook runner env (documented for command hooks)
    for key in (
        "CURSOR_VERSION",
        "CURSOR_PROJECT_DIR",
        "CURSOR_TRANSCRIPT_PATH",
        "CURSOR_CONVERSATION_ID",
        "CURSOR_REQUEST_ID",
        "CURSOR_PLUGIN_ROOT",
    ):
        if os.environ.get(key):
            return True
    # Cursor-native stop payload shape (Claude Stop uses hook_event_name instead)
    if isinstance(payload, dict):
        if payload.get("hook_event_name") in (None, "") and "loop_count" in payload:
            if payload.get("status") in ("completed", "aborted", "error"):
                return True
        # Some builds expose transcript via env-mapped payload keys
        if payload.get("cursor_version") or payload.get("generation_id"):
            return True
    return False


def _skip_for_grok_host() -> bool:
    """Grok CLI opted out of this gate (2026-11-12 user request).

    Grok loads the same Claude Code Stop hook from ~/.claude/settings.json.
    The hook child gets GROK_HOOK_EVENT / GROK_SESSION_ID / GROK_WORKSPACE_ROOT
    / GROK_HOOK_NAME. claude-code, zcode, and Cursor do not set those, so they
    keep the gate. Force-on: REVIEWBOARD_PLAIN_SPEAK_FORCE=1.
    """
    if os.environ.get("REVIEWBOARD_PLAIN_SPEAK_FORCE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return False
    for key in (
        "GROK_HOOK_EVENT",
        "GROK_HOOK_NAME",
        "GROK_SESSION_ID",
        "GROK_WORKSPACE_ROOT",
    ):
        if os.environ.get(key):
            return True
    return False


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except ValueError:
        allow()
    if not isinstance(payload, dict) or payload.get("stop_hook_active"):
        allow()  # 本轮已打回过一次：不再拦（配合宿主三次上限）
    if _skip_for_cursor_host(payload) or _skip_for_grok_host():
        allow()
    text = extract_text(payload)
    if not text:
        allow()
    words, regexes, allowset = load_dictionary()
    hits = scan(text, words, regexes, allowset)
    if not hits:
        allow()
    shown, seen = [], set()
    for lno, term, plain in hits:
        if term in seen:
            continue
        seen.add(term)
        shown.append(f"第{lno}行「{term}」→ {plain}")
        if len(shown) >= MAX_HITS_REPORTED:
            break
    bounce(
        "[plain-speak 发话前检查] 这条回复里有没解释的黑话，用户看不懂——重写后再发：\n"
        + "\n".join("- " + s for s in shown)
        + "\n改法二选一：换成右边的日常说法；或保留原词、但首次出现处紧跟一句括号解释。"
        "代码块和链接里的词不算数；完整词典见 configs/plain-speak-dictionary.md（可直接增删词条）。")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 — fail-open 兜底
        sys.stderr.write(f"[plain-speak gate] 内部错误（放行）: {e}\n")
        sys.exit(0)
