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


def extract_text(payload):
    """取"刚写完的回复"：优先 payload 里名字像回复的字段，退到 rollout 日志尾段。"""
    def strings(obj):
        if isinstance(obj, str):
            yield obj
        elif isinstance(obj, dict):
            for v in obj.values():
                yield from strings(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from strings(v)
    keyed = []
    for k, v in (payload.items() if isinstance(payload, dict) else []):
        if re.search(r"response|preview|last|message|assistant|text|output", str(k), re.I):
            keyed += [s for s in strings(v)]
    keyed = [s for s in keyed if len(s) >= 30]
    if keyed:
        return max(keyed, key=len)
    # rollout 退路：本会话日志尾段里最长的一段文本
    sid = payload.get("session_id") if isinstance(payload, dict) else None
    sid = sid or os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("ZCODE_SESSION_ID")
    if not sid:
        return None
    path = os.environ.get("AG_ROLLOUT") or os.path.join(
        os.path.expanduser("~"), ".zcode", "cli", "rollout", f"model-io-session_{sid}.jsonl")
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    best, best_len = None, 30
    for line in tail.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        for s in strings(rec):
            if len(s) > best_len and "```" not in s[:0]:
                best, best_len = s, len(s)
    return best


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


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except ValueError:
        allow()
    if not isinstance(payload, dict) or payload.get("stop_hook_active"):
        allow()  # 本轮已打回过一次：不再拦（配合宿主三次上限）
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
