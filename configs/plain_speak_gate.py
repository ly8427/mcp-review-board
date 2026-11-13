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

# 钩子经管道收 UTF-8 JSON、写出 UTF-8 决策。Windows 默认 stdio 是
# cp1252：bounce() 写出中文决策（或诊断）会 UnicodeEncodeError，被下面
# fail-open 兜底吞成放行——CI 上 15 项「该打回却 unexpectedly None」即此根因。
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    _reconf = getattr(_stream, "reconfigure", None)
    if _reconf is not None:
        try:
            _reconf(encoding="utf-8", errors="replace")
        except (OSError, ValueError, AttributeError, TypeError):
            pass

DICT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plain-speak-dictionary.md")
EXPLAIN_MARKERS = ("（", "(", "，即", "（即", "就是", "指的是", "意思是", "——", "＝", "=")
HASH_CTX = re.compile(r"[\w-]")
MAX_HITS_REPORTED = 6


def _gate_env(name: str, default=None):
    """宿主环境键的唯一读入口（V1.1.11，板 #2073 §四-4/#2070 §四）：
    所有 os.environ 读取收进本函数——测试侧 g14b 用 ast 计数钉死
    「_gate_env 之外 os.environ 出现次数 = 0」，洗表派生从此有单一锚点。"""
    return os.environ.get(name, default)


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
        # 窗口边界切出的首行：仅当它真的是被截断的半条记录时才跳过。
        # V1.1.9（GPT round-5 P2）：旧实现 start>0 一律丢 lines[0]——窗口恰好
        # 落在记录边界（start-1 处是换行）时首行本为完整记录，白白丢掉可能
        # 让最新 assistant 记录在 cap 处不可见（fail-open）。判据：读 start-1
        # 一个字节，是 \n = 边界完整（保留首行）；否则首行确为半条（跳过）。
        first_line_complete = True
        if start > 0:
            try:
                with open(path, "rb") as f:
                    f.seek(start - 1)
                    first_line_complete = f.read(1) == b"\n"
            except OSError:
                first_line_complete = False
        body = lines if first_line_complete else lines[1:]
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


# ⑦ 值侧键名分档（V1.1.11，板 #2070 §二 / #2074 §3 定稿）：
# 元数据键一律跳过（不为标签打回）；内容键优先；其余非元数据串按文档序
# 首个非空兜底（V1.1.6「长文不冒充正文」不受损——它钉的是跨键 max(len) 通道）。
_METADATA_KEYS = ("type", "role", "id", "index", "stop_reason", "name", "status")
_CONTENT_KEYS = ("text", "content", "body")


class _RoleSkipped(str):
    """c8 身份哨兵（板 #2093 §1 处方版定稿）：值 ""、真值为假——漏改点的
    退化停在「可见假痕」(b) 级而非「零痕放行」(c) 级；身份判定一律 is/is not，
    置于真值与 strip 判断之前（pi #2093 §1 三型退化实弹的排序依据）。"""


_ROLE_SKIPPED = _RoleSkipped("")

# 三桶中文短句（板 #2104 §4 格式定稿：行 =「[plain-speak] <中文短句>（token: 键）」）
_BUCKET_PHRASES = {
    "meta_only": "回复键存在但未识别出正文",
    "role_skipped": "回复键整键跳过",
    "b-tier-ignored": "回复键有正文但不在消费面",
}
# 桶行输出序=固定枚举序（非构建序）——板 #2104 §2 I3 要求互换 payload 键序后
# stderr 逐字节相同，构建序做不到（先建桶先输出会随插入序漂移）；本表即钉死序。
_BUCKET_ORDER = ("meta_only", "role_skipped", "b-tier-ignored")


def _emit_diag(buckets, reasons, *, reasons_out=True):
    """诊断装配（V1.1.12，板 #2102/#2104/#2106 定稿）：一行一桶——
    行 =「[plain-speak] <中文短句>（<token>: <sorted键名>）」，**桶行输出序
    =固定枚举序 _BUCKET_ORDER**（对「桶序=构建序」原稿的偏离：I3 互换序
    逐字节断言与构建序不可兼得，承重断言优先；未登记 token 兜底在固定序后
    按插入序）；键一律 sorted(set)；reasons 各独立成行（尾读原因原样、带
    可分原因，g9 灯）。
    reasons_out=False（成功出口，dsh #2114 §4 裁定①：成功出口只发桶行——
    「成功却带着上一路的失败痕」措辞为假，pi #2115 F1 实弹）时原因整批不写。
    全空 → 返回 ""（调用方零字节，g6 灯不假红）。伞句由调用方在 fail-open
    两出口（③/⑤）先行独立成行，本函数不产出伞句。"""
    lines = []
    ordered = [t for t in _BUCKET_ORDER if t in buckets] + \
              [t for t in buckets if t not in _BUCKET_ORDER]
    for token in ordered:
        keys = buckets.get(token) or []
        if keys:
            short = _BUCKET_PHRASES.get(token, token)
            lines.append(f"[plain-speak] {short}（{token}: {', '.join(sorted(set(keys)))}）")
    if reasons_out:
        lines.extend(reasons)
    return "\n".join(lines) + "\n" if lines else ""


def _emit(buckets, reasons, *, reasons_out=True):
    """出口侧包装：装配结果非空才写 stderr（零诊断 = 零字节，g6 灯不假红）。"""
    d = _emit_diag(buckets, reasons, reasons_out=reasons_out)
    if d:
        sys.stderr.write(d)


def _body_text(v):
    """payload 容器的正文提取（V1.1.12 终版，板 #110 c8/#2078/#2074 定稿）：
    裸串 → 原样返回（「首个非空」只留给裸串/跨键）；
    容器(dict) → ①role 判据（内联单点、不抽共用谓词，板 #2095 §7 终稿）：
    role 键存在且值非 "assistant"（含 JSON null / 非 str，与「键缺失」区分，
    dsh #2090 §二）→ 返回 _ROLE_SKIPPED
    身份哨兵；②内容键 text/content/body（含 message 包装递归）全部 join，
    内容链上任一环节为哨兵 → 整键传播哨兵（d2 定稿：不传播=干净部分进扫描
    而黑话无声消失=扫错对象零痕）；③内容键缺席 → 其余**非元数据**串按文档序
    首个非空兜底；type/role/id 等元数据键一律不取。
    容器(list) → 显式 for 逐元素递归（pi #2093 §1 SD 落法），任一元素为哨兵
    → 整体哨兵（传播完整性，d1）。
    与 transcript 路 _assistant_text 的口径差异（两处 docstring 各记一句，
    板 #2095 §7）：那条是记录级（type/role 判据先行），本条是容器级（role
    内联单点）；同一 dict 两处结论可不同（双面断言钉死，j_dual）。
    取不到（纯元数据/未知形状）→ None。"""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        if "role" in v and (
            not isinstance(v["role"], str) or v["role"] != "assistant"
        ):
            return _ROLE_SKIPPED   # c8：role 键在且值非 assistant（含 null）→ 整键跳过
        parts = []
        for bk in _CONTENT_KEYS + ("message",):
            if bk in v:
                t = _body_text(v[bk])
                if t is _ROLE_SKIPPED:
                    return t       # d2：哨兵沿容器链向上传播 = 整键跳过
                if t and t.strip():
                    parts.append(t)
        if parts:
            return "\n".join(parts)
        for sk, sv in v.items():   # 兜底：非元数据串，文档序首个非空
            if sk in _METADATA_KEYS or sk in _CONTENT_KEYS or sk == "message":
                continue
            if isinstance(sv, str) and sv.strip():
                return sv
        return None
    if isinstance(v, list):
        parts = []
        for x in v:                # 显式 for：哨兵逐元素传播（不用推导式）
            t = _body_text(x)
            if t is _ROLE_SKIPPED:
                return t
            if t and t.strip():
                parts.append(t)
        return "\n".join(parts) or None
    return None


def extract_text(payload):
    """取"刚写完的回复"，按权威级四路（V1.1.4，board #109 五缺口收口）：
    ① payload["last_assistant_message"]——claude Stop 官方权威字段，任意长度都算；
    ② payload 里名字像回复的字段——**A/B 档分诊 + 三桶诊断**（V1.1.12，
       板 #110 终稿）：A 档（last_message > response > text > output 精确消费
       名单）取到正文才消费——值是裸串取该串；值是容器按 _body_text 取
       text/content/body 正文字段（多块 join，元数据 id/status/type 不冒充
       正文）；role 键存在且值非 assistant（含 JSON null / 非 str）→
       role_skipped 桶（c8，整键跳过）；
       未识别形状 → meta_only 桶（h2 灯）。B 档（其余命中键名的字段，如
       conversation_preview）**永不打回**，进 b-tier-ignored 桶（板 #2073
       §二；不盲选防键序定成败）。
       **诊断契约三句**（板 #2112 §1 定稿，docstring 载）：
       弱形——进入收集面后零跳过 ⇒ stderr 不出现任何桶 token
       （meta_only:/role_skipped:/b-tier-ignored: 皆无）；
       强形——零跳过 ∧ 两路尾读成功 ⇒ stderr 空（g6 唯一灯）；
       反向禁令——不得把 assertEqual(err,"") 推广成「allow ⇒ 无 stderr」
       （c/d/g5/g4/g9 五格假红）；出口④用例级允许（X 格，正反两句成对）。
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
    buckets = {}   # 桶：token → [键名]（V1.1.12 双列表之一；输出序=_BUCKET_ORDER 固定枚举序，非构建序）
    reasons = []   # 原因：尾读失败可分原因（双列表之二；与桶分族，④ 丢弃不误伤 ③）
    for k, v in payload.items():
        if re.search(r"response|preview|last|message|assistant|text|output", str(k), re.I):
            body = _body_text(v)
            if body is _ROLE_SKIPPED:
                # c8（板 #2090 §二/#2095 §一 定稿）：有正文但角色不符 → 专用桶，
                # 不落 meta_only 旧句（「只含元数据」与事实相反=假痕）
                buckets.setdefault("role_skipped", []).append(k)
            elif k in ("last_message", "response", "text", "output"):
                # A 档（板 #2073 §二定稿）：精确消费名单，取到正文才进 keyed；
                # 容器只含元数据/未识别形状 → meta_only 桶（h2 灯）
                if body and body.strip():
                    keyed[k] = body
                elif isinstance(v, (dict, list)) and v:
                    buckets.setdefault("meta_only", []).append(k)
            elif (body and body.strip()) or (isinstance(v, (dict, list)) and v) \
                    or (isinstance(v, str) and v.strip()):
                # B 档（板 #2073 §二定稿；c7 收集点进桶，#2138 §四）：命中回复
                # 键名但不在消费面——永不打回、不盲选（防键序定成败，dsh #2070
                # 反例）；桶行由五出口统一 emit（「成功路径不写 stderr」旧裁
                # 已被板 #110/V1.1.12 推翻）
                buckets.setdefault("b-tier-ignored", []).append(k)
    # 显式"最近一条"键优先于其余(V1.1.4 ②;flat 兜底已删——同键内长度选择
    # 正是冒充通道,dsh #2052 §二.3)
    for pref in ("last_message", "response", "text", "output"):
        if pref in keyed:
            _emit(buckets, [], reasons_out=False)
            return keyed[pref]          # 出口②'（A 档 pref）：成功路只发桶行
    tp = payload.get("transcript_path")
    if isinstance(tp, str) and tp:
        best, reason = last_assistant_text_in_tail(tp)
        if best:
            _emit(buckets, [], reasons_out=False)
            return best                 # 出口②（transcript）：桶行同④；原因此刻尚未攒下
        reasons.append(f"transcript 尾读未取得回复({reason}): {tp}")
    sid = payload.get("session_id")
    sid = sid or _gate_env("CLAUDE_SESSION_ID") or _gate_env("ZCODE_SESSION_ID")
    if not sid:
        d = _emit_diag(buckets, reasons, reasons_out=True)
        if d:
            sys.stderr.write("[plain-speak] fail-open（无当前轮文本可查）\n" + d)
        return None                     # 出口③：伞句独立行＋桶行＋原因行（g4/g9 灯）
    path = _gate_env("AG_ROLLOUT") or os.path.join(
        os.path.expanduser("~"), ".zcode", "cli", "rollout", f"model-io-session_{sid}.jsonl")
    best, reason = last_assistant_text_in_tail(path)
    if best:
        _emit(buckets, [], reasons_out=False)
        return best                     # 出口④：只发桶行——transcript 原因在此丢弃
    reasons.append(f"rollout 尾读未取得回复({reason}): {path}")
    d = _emit_diag(buckets, reasons, reasons_out=True)
    if d:
        sys.stderr.write("[plain-speak] fail-open（无当前轮文本可查）\n" + d)
    return None                         # 出口⑤（A6 灯：伞句在此出口定义性在位）


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
    if _gate_env("REVIEWBOARD_PLAIN_SPEAK_FORCE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return False
    # Agent worker
    if _gate_env("CURSOR_AGENT"):
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
        if _gate_env(key):
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
    if _gate_env("REVIEWBOARD_PLAIN_SPEAK_FORCE", "").strip().lower() in (
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
        if _gate_env(key):
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
