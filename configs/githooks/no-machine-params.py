#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""机器参数脱敏闸 —— pre-commit/pre-push 共用扫描器（2026-11-20 事故后设立）。

事故: docs/DEBT.md 剩余清单把 VPN 节点名+IP 从本地记忆原文抄进公开仓库
(PR #53), Cursor Bugbot 抓到后以历史重写+覆盖推送补救。本闸把检查从
"人的记性"改到 "git 动作本身"——提交与推送两道口都机器扫描:
  --staged    pre-commit: 扫暂存区新增行 (git diff --cached -U0)
  --pre-push  pre-push:   扫待推送提交范围的新增行 (stdin: git 推送行)

命中即拒的三类:
  1. 公网 IP 形态字符串——私网/回环/链路本地/文档保留段内置放行,
     其余 IP 须精确列入 ip-allowlist.txt (显式决定, 进 diff 可审计)
  2. 已知 VPN 节点标签族 c55s<digit>
  3. 代理/订阅配置特征: vless:// vmess:// trojan:// ssr:// ss:// guiNDB guiConfigs

边界(如实载明): 只拦"长得像"的形态; 其他形态的机器信息(主机名等)不在此网内,
由 PR 评审 + Bugbot 独立审查兜底。自免: configs/githooks/ 自身文件不扫
(扫描器源码含模式字面量——自举悖论), 该目录下的敏感内容不受此闸保护。
"""
import re
import subprocess
import sys

IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
NODE_RE = re.compile(r"\bc55s\d\b", re.IGNORECASE)
MARKERS = ("vless://", "vmess://", "trojan://", "ssr://", "ss://",
           "guiNDB", "guiConfigs")
SELF_DIR_PREFIX = "configs/githooks/"


def allowlist():
    """ip-allowlist.txt 的精确 IP 集合(文件缺失/无条目 ⇒ 空集)。"""
    import os
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "ip-allowlist.txt")
    out = set()
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.split("#", 1)[0].strip()
                if ln:
                    out.add(ln)
    except OSError:
        pass
    return out


def ip_reserved(ip):
    """内置放行段: 回环/私网/链路本地/组播/保留/文档段。"""
    try:
        a, b, c, d = (int(x) for x in ip.split("."))
    except ValueError:
        return True  # 四段都非数字 ⇒ 不是真 IP, 交给字面匹配(仍会命中 IP_RE)
    if any(v > 255 for v in (a, b, c, d)):
        return False  # 形态异常(版本号式四段) ⇒ 仍拦, 白名单逃生
    if a in (0, 10, 127) or 224 <= a <= 239 or a >= 240:
        return True
    if a == 172 and 16 <= b <= 31:
        return True
    if a == 192 and b == 168:
        return True
    if a == 169 and b == 254:
        return True
    if (a, b, c) in ((192, 0, 2), (198, 51, 100), (203, 0, 113)):
        return True  # RFC 文档保留段
    return False


def scan_line(text, allowed=frozenset()):
    """单行扫描 ⇒ 命中类别列表(空列表=干净)。"""
    hits = []
    for m in IP_RE.finditer(text):
        ip = m.group(0)
        if ip in allowed or ip_reserved(ip):
            continue
        hits.append(f"公网IP {ip}")
    if NODE_RE.search(text):
        hits.append("VPN节点标签 c55s<digit>")
    low = text.lower()
    for mk in MARKERS:
        if mk.lower() in low:
            hits.append(f"订阅/代理配置特征 {mk}")
    return hits


def _added_lines(diff_text):
    """从 git diff -U0 输出解析 (path, lineno, line) 三元组(仅新增行)。"""
    out, path, ln = [], None, 0
    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            path = None
        elif raw.startswith("+++ b/"):
            path = raw[6:]
        elif raw.startswith("@@"):
            # @@ -a,b +c,d @@ ⇒ 新文件起始行 c
            try:
                ln = int(raw.split("+")[1].split(",")[0].split("@")[0].strip())
            except (IndexError, ValueError):
                ln = 0
        elif raw.startswith("+") and not raw.startswith("+++"):
            out.append((path, ln, raw[1:]))
            ln += 1
    return out


def scan_diff(diff_text, allowed=frozenset()):
    """扫一段 diff 的新增行 ⇒ [ (path, lineno, kinds, line) ]。"""
    hits = []
    for path, lineno, line in _added_lines(diff_text):
        if path is None or path.startswith(SELF_DIR_PREFIX):
            continue  # 自免: 闸自身文件(见 docstring 自举悖论)
        kinds = scan_line(line, allowed)
        if kinds:
            hits.append((path, lineno, kinds, line))
    return hits


def die(hits):
    print("[机器参数闸] 命中 %d 处, 提交/推送被拒绝:" % len(hits),
          file=sys.stderr)
    for path, lineno, kinds, line in hits:
        print(f"  {path}:{lineno}  [{'; '.join(kinds)}]", file=sys.stderr)
        print(f"    | {line.strip()[:120]}", file=sys.stderr)
    print("  处理: ①脱敏后再提交——写\"订阅内活节点(参数见本地记忆)\"这个级别;"
          " ②确属合法内容(如测试夹具需要公网 IP) → 把该 IP 精确加入"
          " configs/githooks/ip-allowlist.txt(这个动作进 diff, 可审计)。",
          file=sys.stderr)
    sys.exit(1)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--staged"
    allowed = allowlist()
    if mode == "--staged":
        diff = subprocess.run(["git", "diff", "--cached", "-U0", "--no-color"],
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace").stdout
        die(scan_diff(diff, allowed)) if scan_diff(diff, allowed) else None
    elif mode == "--pre-push":
        # stdin 每行: <local_ref> <local_sha> <remote_ref> <remote_sha>
        for line in sys.stdin:
            parts = line.split()
            if len(parts) != 4:
                continue
            local_sha, remote_sha = parts[1], parts[3]
            if set(local_sha) == {"0"}:
                continue  # 分支删除, 无内容
            if set(remote_sha) == {"0"}:
                # 新分支: 扫 local_sha 相对 origin/master 的新增; 无参照则扫全树差异
                base = subprocess.run(
                    ["git", "merge-base", "origin/master", local_sha],
                    capture_output=True, text=True).stdout.strip()
                rng = [base or "", local_sha]
                diff = subprocess.run(
                    ["git", "diff", "-U0", "--no-color"] + rng,
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace").stdout
            else:
                diff = subprocess.run(
                    ["git", "diff", remote_sha, local_sha, "-U0", "--no-color"],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace").stdout
            hits = scan_diff(diff, allowed)
            if hits:
                print(f"[机器参数闸] 推送范围 {parts[2]} 含命中:", file=sys.stderr)
                die(hits)
    else:
        print(f"unknown mode {mode}", file=sys.stderr)
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
