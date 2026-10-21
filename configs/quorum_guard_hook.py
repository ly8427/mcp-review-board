#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""quorum-guard L2 hook（portable rev2）— thread #45 rev2 定稿实施件。

合并来源：zcode 初版（#474）+ dsh 可移植版/R5/R6（#475）+ pi 实测条件（#476）。
PreToolUse hook for mcp__review-board__create_thread | set_quorum | set_verdict。
stdin: hook payload JSON。exit 0 放行 / exit 2 阻止（stderr=拒绝理由+行动指引）/
其他非零 = fail-closed 视同阻止（必须吐可读理由，R6：不许裸 traceback）。

零绝对路径（跨宿主可移植）：全部配置走环境变量，默认值相对本文件位置推导。
宿主只需：RB_BOARD（默认 http://localhost:8765）、可选 RB_GATE_DIR / RB_WATCHER_GATES_JSON。
"""
import json
import os
import re
import sqlite3
import sys
import tempfile
import urllib.request

# ---- 宿主配置段（全 env 驱动，R2/dsh-portable）----
BOARD_URL = os.environ.get("RB_BOARD", "http://localhost:8765")
REPO = os.environ.get("RB_REPO", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ONBOARDING_MD = os.environ.get("RB_ONBOARDING", os.path.join(REPO, "configs", "onboarding.md"))
DB_PATH = os.environ.get("RB_DB", os.path.join(REPO, "data", "reviewboard.db"))
GATE_DIR = os.environ.get("RB_GATE_DIR", tempfile.gettempdir() or "/tmp")
# 闸门命名约定 rb-<member>-watcher.fails（kit watcher 家族统一约定）
WATCHER_GATES = {n: os.path.join(GATE_DIR, f"rb-{n}-watcher.fails") for n in ("claude", "dsh", "pi", "zcode")}
WATCHER_GATES.update({k: str(v) for k, v in json.loads(os.environ.get("RB_WATCHER_GATES_JSON", "{}")).items()})
SUSPEND_HOURS = float(os.environ.get("RB_SUSPEND_HOURS", "24"))
# F6 修复（2026-10-21 用户点名"发起者有没有硬约束强迫他开定时任务"）：
# 本宿主发起者必须维持活跃的轮询打卡——由定时任务每轮 touch 的心跳文件证明。
# 文件年龄超过 POLL_STALE_MIN 分钟 = 无人在环 = 拒绝建帖（拒绝理由教怎么开）。
POLL_MARKER = os.environ.get("RB_POLL_MARKER", os.path.expanduser("~/.zcode/rb-zcode-poll-heartbeat"))
POLL_STALE_MIN = int(os.environ.get("RB_POLL_STALE_MIN", "45"))
# 测试钩子（dsh #475 T5 用法：RB_FAKE_NOW=+27h 平移当前时间判暂缓）
_FAKE_NOW = os.environ.get("RB_FAKE_NOW", "")


def die(reason: str) -> None:
    sys.stderr.write(reason)
    sys.exit(2)


def _now():
    if _FAKE_NOW:
        from datetime import datetime, timedelta
        base = datetime.now()
        m = re.match(r"([+-])(\d+)h", _FAKE_NOW)
        if m:
            return base + timedelta(hours=int(m.group(2)) * (1 if m.group(1) == "+" else -1))
    return None  # None = 调用方用库端时间


def find_keys(obj, key):
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                out.append(v)
            out.extend(find_keys(v, key))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(find_keys(v, key))
    return out


def load_roster() -> dict:
    try:
        text = open(ONBOARDING_MD, encoding="utf-8").read()
    except OSError as e:
        die(f"[quorum-guard fail-closed] 成员档案表不可读: {ONBOARDING_MD} ({e})\n"
            f"修复: 设 RB_ONBOARDING 指向本宿主 onboarding.md，或先建档案表。")
    roster = {}
    for line in text.splitlines():
        m = re.match(r"^\|\s*([a-z][a-z0-9_]*)\s*\(?[^|]*\|", line)
        if not m:
            continue
        fm = re.search(r"\|\s*\((a|b)\)[^|]*\|", line)
        if fm:
            roster[m.group(1)] = f"({fm.group(1)})"
    return roster


def board_alive() -> None:
    try:
        with urllib.request.urlopen(f"{BOARD_URL}/ping", timeout=3):
            return
    except Exception as e:
        die(f"[quorum-guard fail-closed] 看板不可达: {BOARD_URL}/ping ({e})\n"
            f"修复: 确认 server 运行，或设 RB_BOARD。（R2: 看不到板就不同意建帖）")


def check_quorum_names(names, roster) -> None:
    board_alive()
    for name in names:
        if not isinstance(name, str):
            continue
        form = roster.get(name)
        if form is None:
            table = "\n".join(f"  {k}: {v}" for k, v in sorted(roster.items()))
            die(f"[quorum-guard] 名单成员 '{name}' 不在成员档案表（configs/onboarding.md）。\n"
                f"档案表现有:\n{table}\n"
                f"修复: 用表内名字，或先在 onboarding.md 落档（形态/入口/要点）。")
        if form == "(b)":
            die(f"[quorum-guard] '{name}' 是 (b) 形态（on-demand 用户唤起，无 watcher）。\n"
                f"R4（#45 rev2 定稿，两段式）: 用户侧 nonce 落地前，(b) 成员一律不入 quorum；\n"
                f"入场后凭据 = (b) 本人在帖内的评论（#477 两段式）。\n"
                f"修复: 换 (a) 形态成员。")
        gate_path = WATCHER_GATES.get(name)
        if gate_path:
            try:
                n = int(open(gate_path).read().strip() or 0)
            except OSError:
                # v2.9.2 (#493 §2/(a)+pi #476 条件2 收口): 缺文件 = 配错（watcher
                # 启动即写 0，存在性即有意义）——fail-closed，不再当"0 次失败"放行。
                die(f"[quorum-guard fail-closed] '{name}' 的 watcher 闸门文件不存在: {gate_path}\n"
                    f"两种可能: watcher 从未启动(配错) 或 RB_GATE_DIR 指错目录——门分不开这两者,一律拒绝。\n"
                    f"修复: 启动 {REPO}/configs/{name}-watcher.sh(它会在启动时写入 0),或设 RB_WATCHER_GATES_JSON。")
            if n >= 3:
                die(f"[quorum-guard] '{name}' 的 watcher 闸门已锁死（连续 {n} 次唤醒失败, C1.2）。\n"
                    f"修复（把恢复做成本——先恢复再建帖）:\n"
                    f"  rm -f {gate_path} && bash {REPO}/configs/{name}-watcher.sh（常驻）")
        else:
            die(f"[quorum-guard fail-closed] '{name}' 是 (a) 但本宿主无闸门配置（pi #476 条件2："
                f"\"无核验手段\"必须拒绝，不得 warn 放行）。\n"
                f"修复: 在 RB_WATCHER_GATES_JSON 里为 '{name}' 配置闸门文件路径，或从名单移除。")


def _connect():
    """R6: ro 失败（WAL 无 -shm 写权限）→ immutable=1 回退 → 可读 die()。"""
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
        conn.execute("PRAGMA query_only=1")
        conn.execute("SELECT 1 FROM threads LIMIT 1")
        return conn
    except sqlite3.Error:
        pass
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?immutable=1", uri=True, timeout=5)
        conn.execute("PRAGMA query_only=1")
        conn.execute("SELECT 1 FROM threads LIMIT 1")
        return conn
    except sqlite3.Error as e:
        die(f"[quorum-guard fail-closed] 板库不可读: {DB_PATH} ({e})\n"
            f"修复: 设 RB_DB 指向本宿主 reviewboard.db；正解见 v2.9 L1.3 只读 HTTP 端点（hook 免碰 DB）。")


def _suspended(conn, names):
    """R5: 按 participants.last_seen 判 C1 暂缓（≥SUSPEND_HOURS 无心跳）。"""
    sus = set()
    import datetime as _dt
    now = _now() or _dt.datetime.now(_dt.timezone.utc)
    for n in set(names):
        row = conn.execute("SELECT last_seen FROM participants WHERE author=?", (n,)).fetchone()
        if not row or not row[0]:
            sus.add(n)  # 未注册 = 无心跳 = 按暂缓处理（等待集排除，留痕）
            continue
        try:
            then = _dt.datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_dt.timezone.utc)
        except ValueError:
            sus.add(n)
            continue
        if (now - then).total_seconds() >= SUSPEND_HOURS * 3600:
            sus.add(n)
    return sus


def check_poll_marker() -> None:
    """F6 硬约束：发起者建帖前，本宿主的轮询打卡必须新鲜。

    打卡文件由定时任务每轮刷新（touch）。应用/定时任务停转 → 文件变陈旧 →
    建帖被拒。一次性伪造挡不住新鲜度要求（必须被反复刷新才能保持新鲜）。"""
    import datetime as _dt
    import time as _time
    try:
        age = _time.time() - os.path.getmtime(POLL_MARKER)
    except OSError:
        age = None
    if age is None or age > POLL_STALE_MIN * 60:
        have = "打卡文件不存在" if age is None else f"打卡已 {int(age // 60)} 分钟未刷新（上限 {POLL_STALE_MIN}）"
        die(f"[quorum-guard F6] 发起者轮询打卡过期：{have}。\n"
            f"建帖的前提是你将持续在场收票——先开定时轮询并让它打卡:\n"
            f"  1. ZCode 本工作区建 cron 自动化（每 15 分钟），prompt 含:\n"
            f"     '轮询 review board(list_comments_since author=自己)并刷新打卡文件 {POLL_MARKER}'\n"
            f"  2. 等第一轮打卡落地（≤15 分钟）后重试建帖。\n"
            f"打卡由重复刷新维持新鲜——单次 touch 骗不过时间。")


def _guard_data(thread_id: int):
    """v2.9.2 (#47 ③): endpoint-first — GET /guard (read-only, no DB file
    dependency, no WAL ambiguity). Returns parsed dict or None (caller falls
    back to the DB path)."""
    try:
        with urllib.request.urlopen(f"{BOARD_URL}/guard?thread={thread_id}", timeout=3) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def guard_set_verdict(payload) -> None:
    """L1.5(b) 创建者最后投票门（server 端另有同款，本层为宿主侧冗余）+ R5 暂缓出口。
    v2.9.2: /guard 端点优先，DB 直读退化（dsh R6 正解——跨宿主无文件访问）。"""
    authors = [a for a in find_keys(payload, "author") if isinstance(a, str)]
    tids = [t for t in find_keys(payload, "thread_id") if isinstance(t, (int, str))]
    if not authors or not tids:
        return
    author, tid = authors[0], int(tids[0])
    g = _guard_data(tid)
    if g and g.get("thread"):
        th = g["thread"]
        quorum = th.get("quorum") or []
        if not quorum or th.get("creator") != author or author not in quorum or len(quorum) < 2:
            return
        voted = {a for a, v in (g.get("verdicts") or {}).items() if v.get("verdict")}
        now = _now()
        import datetime as _dt
        sus = set()
        for n, p in (g.get("participants") or {}).items():
            ls = p.get("last_seen")
            try:
                then = _dt.datetime.strptime(ls, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_dt.timezone.utc)
                if (now - then).total_seconds() >= SUSPEND_HOURS * 3600:
                    sus.add(n)
            except (TypeError, ValueError):
                sus.add(n)
        for n in quorum:
            if n not in (g.get("participants") or {}):
                sus.add(n)
        waiting = [m for m in quorum if m != author and m not in voted and m not in sus]
        if waiting:
            die(f"[quorum-guard L1.5] 创建者最后投票：thread#{tid} 还有活跃 quorum 成员未投票: {', '.join(waiting)}。\n"
                f"修复: 先触发他们的 watcher / 等其投票。（暂缓成员已按 C1 排除并留痕;数据源=/guard 端点）")
        return
    # --- DB fallback (R6: ro → immutable → readable die) ---
    conn = _connect()
    try:
        row = conn.execute("SELECT author, quorum FROM threads WHERE id=?", (tid,)).fetchone()
        if not row or not row[1]:
            return
        creator, quorum = row[0], json.loads(row[1])
        if author != creator or author not in quorum or len(quorum) < 2:
            return
        voted = {r[0] for r in conn.execute("SELECT author FROM verdicts WHERE thread_id=?", (tid,))}
        sus = _suspended(conn, quorum)
    finally:
        conn.close()
    waiting = [m for m in quorum if m != author and m not in voted and m not in sus]
    if waiting:
        die(f"[quorum-guard L1.5] 创建者最后投票：thread#{tid} 还有活跃 quorum 成员未投票: {', '.join(waiting)}。\n"
            f"修复: 先触发他们的 watcher / 等其投票。（暂缓成员已按 C1 排除并留痕）")
    skipped = [m for m in quorum if m != author and m not in voted and m in sus]
    if skipped:
        sys.stderr.write(f"[quorum-guard L1.5] 暂缓成员不计等待（R5，留痕）: {', '.join(skipped)}\n")


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except ValueError:
        die(f"[quorum-guard fail-closed] hook 输入不是 JSON: {raw[:200]!r}")
    tool = ""
    for cand in find_keys(payload, "tool_name") + find_keys(payload, "name"):
        if isinstance(cand, str) and "review-board" in cand:
            tool = cand
            break
    if not tool:
        return
    roster = load_roster()
    if tool.endswith("create_thread") or tool.endswith("set_quorum"):
        if tool.endswith("create_thread"):
            check_poll_marker()  # F6: 建帖 = 承诺持续在场，先证明轮询在环
        names = [n for v in find_keys(payload, "quorum") + find_keys(payload, "add") if isinstance(v, list) for n in v]
        names += [v for v in find_keys(payload, "quorum") + find_keys(payload, "add") if isinstance(v, str)]
        if names:
            check_quorum_names(names, roster)
    elif tool.endswith("set_verdict"):
        guard_set_verdict(payload)


if __name__ == "__main__":
    main()
