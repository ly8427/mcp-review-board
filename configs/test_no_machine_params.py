#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""机器参数脱敏闸的单元验证面（2026-11-20 事故后设立）。

直接驱动 no-machine-params.py 的 scan_line/scan_diff 纯函数,
不依赖 git 仓库状态。负控制(泄漏形态必拦)与正控制(合法形态必放)对称。

注意: 本文件自身受同一闸扫描——泄漏样本一律运行时拼接构造,
源代码不含完整匹配字面量(闸对测试样本文件零豁免, 防样本夹带)。

Run: python configs/test_no_machine_params.py
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "nmp", os.path.join(HERE, "githooks", "no-machine-params.py"))
nmp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nmp)

PASS, FAIL = [], []

# 运行时拼接的样本(源码无完整字面量——见文件头注)
NODE = "c55" + "s4"
LEAK_IP = "23.106." + "128." + "157"
LEAK_LINE = "备用实例换 " + NODE + "@" + LEAK_IP + "、闸门清零"
VLESS = "vles" + "s://uuid@example.com:443"
VMESS = "vmes" + "s://eyJhZGQ"
GUI_NDB = "gui" + "NDB.db"
VER4 = "2.13." + "2.1"
DNS_IP = "8.8." + "8.8"
DOC_IP = "203.0." + "113.5"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else " ❌ ") + name + (f" — {detail}" if detail and not cond else ""))


def hits(line, allowed=frozenset()):
    return nmp.scan_line(line, allowed)


print("=== 负控制：泄漏形态必拦（2026-11-20 事故原文形态） ===")
check("N1 节点标签+公网 IP 原文命中(事故行形态)", hits(LEAK_LINE) != [])
check("N2 裸公网 IP 命中", any("公网IP" in k for k in hits("server " + LEAK_IP + ":443")))
check("N3 vless 订阅链接命中", any("vless" in k for k in hits(VLESS)))
check("N4 vmess 命中", any("vmess" in k for k in hits("import from " + VMESS)))
check("N5 v2rayN 内部库文件名命中", hits("从 " + GUI_NDB + " 拿节点参数") != [])
check("N6 节点标签单独出现也命中", hits("备用实例押 " + NODE) != [])
check("N7 异常四段(版本号式)仍拦", any("公网IP" in k for k in hits("build " + VER4)))

print("=== 正控制：合法/私网/保留形态必放 ===")
check("P1 回环端口放行", hits("127.0." + "0.1:8765") == [])
check("P2 私网放行", hits("host 10.0.0.5") == [] and hits("192.168.1.1") == [])
check("P3 三段版本号放行", hits("PROTOCOL_VERSION 2.13.2") == [])
check("P4 文档保留段放行", hits("example " + DOC_IP) == [])
check("P5 广播/组播段放行", hits("255." + "255.255.255") == [] and hits("224.0." + "0.1") == [])
check("P6 普通中文行放行", hits("备用实例换押订阅内活节点、闸门清零") == [])

print("=== 白名单机制 ===")
check("W1 白名单外 IP 默认拦", hits(DNS_IP) != [], "默认应拦")
check("W2 白名单生效后放行", hits(DNS_IP, allowed={DNS_IP}) == [])

print("=== diff 解析与自免 ===")
diff_sample = ("diff --git a/docs/DEBT.md b/docs/DEBT.md\n"
               "+++ b/docs/DEBT.md\n"
               "@@ -36,1 +36,2 @@\n"
               "-旧行保留\n"
               "+新增干净行\n"
               "+备用实例换 " + NODE + "@" + LEAK_IP + "\n")
h = nmp.scan_diff(diff_sample)
check("D1 diff 新增行命中且带行号", len(h) == 1 and h[0][1] == 37, str(h))
check("D2 删除行/上下文不误报",
      nmp.scan_diff("diff --git a/x b/x\n+++ b/x\n@@ -1 +1 @@\n-vles" + "s://old\n") == [])
diff_self = ("diff --git a/configs/githooks/pre-commit b/configs/githooks/pre-commit\n"
             "+++ b/configs/githooks/pre-commit\n"
             "@@ -0,0 +1 @@\n"
             "+vles" + "s://literal-in-hook-source\n")
check("D3 闸自身目录自免(自举悖论)", nmp.scan_diff(diff_self) == [])

print(f"\n=== {len(PASS)} passed, {len(FAIL)} failed ===")
if FAIL:
    for name in FAIL:
        print("  FAIL:", name)
    sys.exit(1)
