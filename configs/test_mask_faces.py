#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_mask_faces.py — 声明表断言面（v11 ①，载荷 #2451 §2 + v4 #2461 +
评审链 #2540-#2554 的可执行载体。表在 mask_faces.py 尾（#2442 §3 落点）。

断言面（mask_faces.py docstring :22-35 逐条）：
  B  派生集 − 表行集 == ∅（每个派生点一行）
  C  ∀行: (行 ∈ WIRED) ⟺ NEEDS_MASK(行)   NEEDS_MASK = sink≠none ∧ class∉EXEMPT
  D  class ∈ CLASS ∧ (sink ∈ SINK ∨ RESP_RE.fullmatch(sink))
     D1 机械行（override_reason 为空）sink 逐字 == sink_for(...)；前置
        qname ∈ 派生 qname 集（unknown_qname 红，#2461）；裸 "resp" 红（#2461）
     D2 override 行 sink 为 resp:* 形 → fullmatch 先于 split 解包（#2461）
        ∧ 解析式配对 kind == rootkinds[name]（#2504 §1）
  E  {行: sink=="none"} ⟺ SINK_NONE_LANDINGS（八穷尽落点的行粒度展开，15 行）
  F  ∀行 class∈{identity, closed_domain}: fp ∉ FREE_TEXT_FP（家族派生禁手抄，
     #2392；EX1 已掩码下游 / EX2 len 计数 / EX3 SQL 嵌词 三条排除）
  键位① issuance 行 ⊆ ISSUANCE_FACES（#2393 §3①）
  键位② echo 行 ⊆ ECHO_FACES（#2393 §3②）
  audit 补丁 sink=audit 行必须在 AUDIT_ECHO 登记回声 resp 行（#2382 §3）
反例夹具：
  F-H  {title} 标 identity ⇒ F 谓词红（#2392 夹具 H）
  receipt  claim_token 回执行 ∈ WIRED ⇒ 红（#2382 红夹具 F）
  none-wired  sink=none 行 ∈ WIRED ⇒ 红（负例 E）
  living-example  _load_credential_registry 行保持机械行（override_reason 空
      ∧ sink==sink_for）——显式断言，不指望 D1/D2（pi #2542 运行时坐实）
  empty-reason  override 标记（sink≠机械）而 override_reason=="" ⇒ 红
      （『override 行缺理由』单盏灯，pi #2552）
  unknown-qname  表行 qname ∉ 派生集 ⇒ 红（#2461）
  resp-names-24  len(resp_names)==24 反真空锁（#2461，写死区分塌空与改对）
WIRED 接线清单（#2382 伪码）：机械 _mask_credential_text 直包（deriver wired
  字段）∪ occurrence 级补扫（子树含任一掩码器——_search_snippet 搜索面
  #2355 §3；IfExp 后置掩码如 register_meta masked_entry）。派生器本体零改动。

Run: .venv/bin/python configs/test_mask_faces.py
"""
import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mask_faces as mf

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f" — {detail}" if detail and not cond else ""))


SRC = mf.load_server_src()
PTS, ROOTMAP, RESP_NAMES, ROOTKINDS = mf.derive_points(SRC)
DERIVED_QNAMES = {q for (q, _fp) in PTS}
MASKERS = {"_mask_credential_text", "_search_snippet"}


def in_free_family(fp):
    """FREE_TEXT_FP 家族判定（#2392 冻结清单 + 三条排除，随表落库）。"""
    if "_mask_credential_text" in fp:
        return False                    # EX1 已掩码载体的下游
    if fp.startswith("call:len("):
        return False                    # EX2 长度/计数元数据（值是 int）
    if fp.startswith("call:conn.execute("):
        return False                    # EX3 SQL 文本嵌词（值是 id/cursor）
    return bool(re.search(r"(title|body|body_preview|note|context|entry|last_wake_error|snippet)",
                          fp, re.I))


FREE_TEXT_FP = {p.fp for p in PTS.values() if in_free_family(p.fp)}


class _Probe(mf._Deriver):
    """occurrence 级补扫：插值表达式子树（含一级赋值载体）里出现任一掩码器
    ⇒ 该 (qname, fp) 记入 WIRED。"""
    occ_mask = set()

    def _carriers(self, expr):
        yield expr
        if isinstance(expr, ast.Name):
            s, kind = self._scope_of(expr.id)
            if kind == "assign":
                yield s.assigns[expr.id]
            elif kind == "for":
                yield s.forvars[expr.id]

    def _point(self, expr, family):
        qname = self.scopes[-1].qname if self.scopes else "<module>"
        fp = self.core(expr, 0, [])
        for cand in self._carriers(expr):
            for n in ast.walk(cand):
                if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                        and n.func.id in MASKERS):
                    self.occ_mask.add((qname, fp))
                    break
            else:
                continue
            break
        super()._point(expr, family)


_probe = _Probe()
_probe.visit(ast.parse(SRC))
OCC_MASK = _probe.occ_mask

WIRED = {k for k, p in PTS.items() if p.wired} | OCC_MASK


def needs_mask(row):
    return row["sink"] != "none" and row["class"] not in mf.EXEMPT


def d_domain_ok(row):
    """D 主句谓词：class ∈ CLASS ∧ (sink ∈ SINK ∨ RESP_RE.fullmatch(sink))。"""
    return (row["class"] in mf.CLASS
            and (row["sink"] in mf.SINK or mf.RESP_RE.fullmatch(row["sink"])))


def f_violation(k, row):
    """F 谓词：identity/closed_domain 行的指纹 ∩ FREE_TEXT_FP ≠ ∅ ⇒ 红。"""
    return (row["class"] in ("identity", "closed_domain")
            and k[1] in FREE_TEXT_FP)


def main():
    declared = mf.DECLARED

    print("B 表行集 == 派生全集（#2384 §3 / v9）")
    derived_keys = set(PTS.keys())
    declared_keys = set(declared.keys())
    check("B1 派生集 − 表行集 == ∅", derived_keys - declared_keys == set(),
          str(sorted(derived_keys - declared_keys))[:200])
    check("B2 表行集 − 派生集 == ∅（无幽灵行）", declared_keys - derived_keys == set(),
          str(sorted(declared_keys - derived_keys))[:200])

    print("C 等式：(行 ∈ WIRED) ⟺ NEEDS_MASK(行)（#2382 §2）")
    c_bad = []
    for k, row in declared.items():
        if ((k in WIRED) != needs_mask(row)):
            c_bad.append((k[0], k[1][:40], row["class"], row["sink"], k in WIRED))
    check("C1 全表等式成立", not c_bad, str(c_bad[:5]))

    print("D 值域 + D1 逐字 + D2 文法配对")
    d_bad, d1_bad, d2_bad = [], [], []
    for k, row in declared.items():
        q, fp = k
        if not d_domain_ok(row):
            d_bad.append((q, fp[:40], row["class"], row["sink"]))
        if row["sink"] == "resp":
            d1_bad.append((q, "bare resp", row["sink"]))          # #2461 裸 resp 红
        mech = mf.sink_for(q, ROOTMAP, ROOTKINDS) if q in DERIVED_QNAMES else None
        if row["override_reason"] is None:
            if q not in DERIVED_QNAMES:
                d1_bad.append((q, "unknown_qname", fp[:40]))      # #2461 前置
            elif mech is not None and row["sink"] != mech:
                d1_bad.append((q, fp[:40], f"{row['sink']} != mech {mech}"))
        else:
            if mf.RESP_RE.fullmatch(row["sink"]):
                parts = row["sink"].split(":")                    # fullmatch 已先行（#2461）
                kind, name = parts[1], parts[2]
                if kind != ROOTKINDS.get(name):
                    d2_bad.append((q, fp[:40], row["sink"]))
    check("D1a 值域谓词全表成立", not d_bad, str(d_bad[:5]))
    check("D1b 机械行逐字 == sink_for ∧ 无裸 resp ∧ 无 unknown_qname", not d1_bad, str(d1_bad[:5]))
    check("D2 override resp:* 行文法 ∧ 解析式配对", not d2_bad, str(d2_bad[:5]))

    print("E sink=none 行集 ⟺ SINK_NONE_LANDINGS（#2382 §3）")
    none_keys = {k for k, row in declared.items() if row["sink"] == "none"}
    check("E1 行集 == 冻结落点集", none_keys == mf.SINK_NONE_LANDINGS,
          f"extra={sorted(none_keys - mf.SINK_NONE_LANDINGS)[:3]} "
          f"missing={sorted(mf.SINK_NONE_LANDINGS - none_keys)[:3]}")

    print("F identity/closed_domain 行指纹 ∩ FREE_TEXT_FP == ∅（#2392）")
    f_bad = [(k[0], k[1][:40]) for k, row in declared.items() if f_violation(k, row)]
    check("F1 全表成立", not f_bad, str(f_bad[:5]))

    print("键位① issuance 行 ⊆ ISSUANCE_FACES / 键位② echo 行 ⊆ ECHO_FACES")
    iss_rows = {k for k, row in declared.items() if row["class"] == "issuance"}
    echo_rows = {k for k, row in declared.items() if row["class"] == "echo"}
    check("K1 issuance ⊆ ISSUANCE_FACES", iss_rows <= mf.ISSUANCE_FACES,
          str(sorted(iss_rows - mf.ISSUANCE_FACES))[:160])
    check("K2 echo ⊆ ECHO_FACES", echo_rows <= mf.ECHO_FACES,
          str(sorted(echo_rows - mf.ECHO_FACES))[:160])

    print("audit 补丁：sink=audit 行必须在 AUDIT_ECHO 登记（#2382 §3）")
    a_missing = [k for k, row in declared.items()
                 if row["sink"] == "audit" and k not in mf.AUDIT_ECHO]
    a_ghost = [k for k in mf.AUDIT_ECHO if k not in declared]
    check("A1 audit 行均有回声登记 ∧ 无幽灵登记", not a_missing and not a_ghost,
          f"missing={a_missing[:3]} ghost={a_ghost[:3]}")

    print("反例夹具")
    # F-H（#2392）：{title} 标 identity ⇒ F 谓词必须红
    check("FH 合成：title 行标 identity ⇒ F 红",
          f_violation(("synthetic-h", "t['title']"),
                      {"class": "identity", "sink": "resp:tool:get_thread",
                       "override_reason": None}))
    # 红夹具 F（#2382）：claim_token 回执行不得接线
    claim_key = ("claim_token", "call:secrets.token_hex(16)")
    check("FR claim_token 回执行 ∉ WIRED（接线禁入）",
          claim_key in declared and claim_key not in WIRED)
    # 负例 E：sink=none 行不得接线
    check("NE sink=none 行全部 ∉ WIRED",
          all(k not in WIRED for k in none_keys))
    # 活例行保持机械行（pi #2542 运行时坐实：D1/D2 兜不住，须显式断言）
    living_key = ("_load_credential_registry", "r['key']")
    living = declared.get(living_key)
    check("LE 活例行存在", living is not None, str(living_key[:60]))
    if living:
        mech_living = mf.sink_for(living_key[0], ROOTMAP, ROOTKINDS)
        check("LE2 活例行保持机械行（override_reason 空 ∧ sink==sink_for）",
              living["override_reason"] is None and living["sink"] == mech_living,
              f"{living['override_reason']!r} vs {mech_living!r}")
    # empty-reason 灯（pi #2552）：sink≠机械 而 override_reason=="" ⇒ 红
    er_bad = [k[0] for k, row in declared.items()
              if row["override_reason"] == ""
              and k in DERIVED_QNAMES
              and row["sink"] != mf.sink_for(k[0], ROOTMAP, ROOTKINDS)]
    check("ER override 标记行不得缺理由（"" 灯）", not er_bad, str(er_bad[:4]))
    # unknown-qname 灯（#2461）
    uq_bad = [k[0] for k in declared if k[0] not in DERIVED_QNAMES
              and declared[k]["override_reason"] is None]
    check("UQ unknown_qname 红（#2461）", not uq_bad, str(uq_bad[:4]))
    # resp-names 反真空锁（#2461，写死 24）
    check("RN len(resp_names)==24 反真空", len(RESP_NAMES) == 24, str(len(RESP_NAMES)))

    print("SA 模块源静态（#2461 §4 验收 + dsh #2544 §3 三数）")
    mask_src = open(os.path.join(HERE, "mask_faces.py"), encoding="utf-8").read()
    check("SA1 模块源 sank == 0 命中", mask_src.count("sank") == 0, str(mask_src.count("sank")))
    check("SA2 模块源 sink=none == 5 处", mask_src.count("sink=none") == 5,
          str(mask_src.count("sink=none")))
    check("SA3 不可执行并句 SINK ∪ RESP_RE == 0（dsh #2544 §3）",
          SRC.count("SINK ∪ RESP_RE") == 0)

    print(f"\n=== {len(PASS)} passed, {len(FAIL)} failed ===")
    if FAIL:
        for name in FAIL:
            print("  FAIL:", name)
        sys.exit(1)


if __name__ == "__main__":
    main()
