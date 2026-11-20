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
  4. 敏感指针——指向已删除敏感内容的旧提交编号(sensitive-pointers.txt,
     2026-11-20 二次事故后增设: 历史重写后旧 SHA=钥匙, 7 位短号也够打开;
     历史重写的收尾步骤必须把被覆盖的旧编号登记进该文件)

边界(如实载明): 只拦"长得像"的形态; 其他形态的机器信息(主机名等)不在此网内,
由 PR 评审 + Bugbot 独立审查兜底。自免: configs/githooks/ 自身文件不扫
(扫描器源码含模式字面量——自举悖论), 该目录下的敏感内容不受此闸保护。
本闸只覆盖 git 路径; 经 API 发的评论/正文走 --scan-file 模式先扫再发。
"""
import os
import re
import subprocess
import sys

IP_RE = re.compile(r"(?<![0-9.])\d{1,3}(?:\.\d{1,3}){3}(?![0-9.])")
NODE_RE = re.compile(r"(?<![0-9A-Za-z])c55s\d(?![0-9A-Za-z])", re.IGNORECASE)
MARKERS = ("vless://", "vmess://", "trojan://", "ssr://", "ss://",
           "guiNDB", "guiConfigs")
SELF_DIR_PREFIX = "configs/githooks/"


def _data_lines(name):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.split("#", 1)[0].strip()
                if ln:
                    out.append(ln)
    except OSError:
        pass
    return out


def allowlist():
    """ip-allowlist.txt 的精确 IP 集合(文件缺失/无条目 ⇒ 空集)。"""
    return set(_data_lines("ip-allowlist.txt"))


def pointers():
    """sensitive-pointers.txt 的敏感提交编号(小写化; 文件缺失 ⇒ 空表)。"""
    return [x.lower() for x in _data_lines("sensitive-pointers.txt")]


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


def scan_line(text, allowed=frozenset(), pointers=()):
    """单行扫描 ⇒ 命中类别列表(空列表=干净)。pointers=敏感提交编号表。"""
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
    for p in pointers:
        # 编号的 7 位前缀就够 GitHub 解析 ⇒ 文字含名单编号的任一 ≥7 位前缀即命中
        if p and any(p[:n] in low for n in range(7, len(p) + 1)):
            hits.append(f"敏感指针(指向已删除敏感内容的旧提交编号) {p[:12]}")
    return hits


def _unquote_git_path(p):
    """git 对含空格/非 ASCII 的路径加引号+八进制转义("b/\\346\\226\\207")——还原。"""
    if p.startswith('"') and p.endswith('"') and len(p) >= 2:
        body = p[1:-1]
        out = bytearray()
        i = 0
        while i < len(body):
            ch = body[i]
            if ch == "\\" and i + 3 < len(body) and body[i + 1] in "0123":
                try:
                    out.append(int(body[i + 1:i + 4], 8))
                    i += 4
                    continue
                except ValueError:
                    pass
            out.extend(ch.encode("utf-8"))
            i += 1
        s = out.decode("utf-8", errors="replace")
        if s.startswith("b/"):
            s = s[2:]
        return s
    return p


def _added_lines(diff_text):
    """从 git diff -U0 输出解析 (path, lineno, line) 三元组(仅新增行)。"""
    out, path, ln = [], None, 0
    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            path = None
        elif raw.startswith("+++ "):
            # Bugbot 二轮 High: 不认识的形式置 None 会让后续新增行被自免逻辑
            # 吞掉(=整文件不扫)——noprefix 等形式直接当路径; /dev/null 是删除
            # 侧(无新增行), 保留原值
            p = raw[4:]
            if p.startswith("b/"):
                path = p[2:]
            elif p.startswith('"'):
                path = _unquote_git_path(p)
            elif p == "/dev/null":
                pass
            else:
                path = p
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


def scan_diff(diff_text, allowed=frozenset(), pointers=()):
    """扫一段 diff 的新增行 ⇒ [ (path, lineno, kinds, line) ]。"""
    hits = []
    for path, lineno, line in _added_lines(diff_text):
        if path is None or path.startswith(SELF_DIR_PREFIX):
            continue  # 自免: 闸自身文件(见 docstring 自举悖论)
        kinds = scan_line(line, allowed, pointers)
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
    pts = pointers()
    if mode == "--staged":
        diff = subprocess.run(
            ["git", "-c", "core.quotepath=false", "-c", "diff.noprefix=false",
             "-c", "diff.mnemonicPrefix=false", "diff", "--cached",
             "-U0", "--no-color"],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace").stdout
        hits = scan_diff(diff, allowed, pts)
        if hits:
            die(hits)
    elif mode == "--scan-file":
        # API 发布前检查: 扫一个文本文件的全部行(先扫再发), 不依赖 git
        if len(sys.argv) < 3:
            print("usage: --scan-file <path>", file=sys.stderr)
            sys.exit(2)
        with open(sys.argv[2], encoding="utf-8", errors="replace") as f:
            content = f.read()
        hits = []
        for i, ln in enumerate(content.splitlines(), 1):
            kinds = scan_line(ln, allowed, pts)
            if kinds:
                hits.append(("<file>", i, kinds, ln))
        if hits:
            die(hits)
    elif mode == "--pre-push":
        # stdin 每行: <local_ref> <local_sha> <remote_ref> <remote_sha>
        # Bugbot #2(2026-11-20): 端点 diff 会漏"中间提交加入、后续提交删除"的
        # 历史内泄漏(本仓库事故正是此形态——泄漏行进了历史, SHA 直连可达)
        # ⇒ 改为逐提交扫各自 diff; Bugbot #1: 新分支基准拿不到 ⇒ fail-closed。
        for line in sys.stdin:
            parts = line.split()
            if len(parts) != 4:
                continue
            local_sha, remote_sha = parts[1], parts[3]
            if set(local_sha) == {"0"}:
                continue  # 分支删除, 无内容
            if set(remote_sha) == {"0"}:
                mb = subprocess.run(
                    ["git", "merge-base", "origin/master", local_sha],
                    capture_output=True, text=True)
                if mb.returncode != 0 or not mb.stdout.strip():
                    print("[机器参数闸] 无法确定新分支比对基准(merge-base 失败),"
                          " 拒绝推送(fail-closed)。", file=sys.stderr)
                    sys.exit(1)
                rng = mb.stdout.strip() + ".." + local_sha
            else:
                rng = remote_sha + ".." + local_sha
            revs_run = subprocess.run(
                ["git", "rev-list", rng], capture_output=True, text=True)
            # Bugbot 二轮 Medium: 子命令失败 ⇒ 输出空 ⇒ 静默放行——fail-closed
            if revs_run.returncode != 0:
                print("[机器参数闸] rev-list 失败, 拒绝推送(fail-closed)。",
                      file=sys.stderr)
                sys.exit(1)
            for sha in revs_run.stdout.split():
                dt = subprocess.run(
                    ["git", "-c", "core.quotepath=false", "diff-tree", "-p",
                     "--root", "--no-commit-id", "-U0", "--no-color", sha],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace")
                if dt.returncode != 0:
                    print(f"[机器参数闸] diff-tree {sha[:12]} 失败,"
                          " 拒绝推送(fail-closed)。", file=sys.stderr)
                    sys.exit(1)
                diff = dt.stdout
                hits = scan_diff(diff, allowed, pts)
                if hits:
                    print(f"[机器参数闸] 推送范围 {parts[2]} 的提交 "
                          f"{sha[:12]} 含命中:", file=sys.stderr)
                    die(hits)
    else:
        print(f"unknown mode {mode}", file=sys.stderr)
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
