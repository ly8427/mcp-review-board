#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2.12.0 修复批 β 验收门（机械门）——单一分区 + 标签制（#1581 定稿，三方签认）。

═══ 契约头（语义不许两读；改动本门先读这段）══════════════════════════════

- 判据锚 = **def 符号成员制**：一个 `json.loads` 调用的标签由它所在**最近 def
  的名字**决定（AST enclosing-def 归属，pi #1576 §二-1）。「无 12/2 盲区」这
  一性质属于 def 符号成员制，不属于任何文本形态匹配（pi #1579 §二）——绑定名
  站点（形参/解包）天然被归属，无需参数文本含 "meta"。
- 三类排除**不同构**（dsh #1572 §一）：① `quorum` / ② `value` 钉的是 **DB 列
  名**（schema 锚，改名即迁移 ⇒ 精确匹配列名常量）；③ marker 钉的是**局部标
  识符 `marker_row`**（纯重构即可改 ⇒ **③ 改名【会】红，属设计如此**，处理法
  = 同步改本文件 EXCLUSION 注记并评审，dsh #1572 方案 (1) 配套 a；「改名不改
  计数」自测（T1）的适用域 = 标签组成员，不覆盖三类排除，pi #1573 §一）。
- 漏斗语义 =「全树恰一处 participants.meta 解析」：漏斗集合内 `json.loads`
  **合计恒 1**（dsh #1572 §二 / pi #1576 §二）；集合成员只许随件① 结构规则
  **明示**增加（diff 可见），不做长度/成员数断言（dsh #1575 §二：位置锚搬到
  集合大小锚是同一个坑；枚举进本配置即单点入口，claude #1574）。
- **默认计入（default-in，dsh #1569 §二）**：`json.loads` 一律先计入，只按三
  类来源形态排除；新非 meta loads 先红一次 = fail-closed，可接受。
- 断言全是**计数与谓词**（dsh #1581 §二）；一切数字（总数 23 / 1 / 2 / 20 /
  17 / 2 / 1）只出现在**汇总打印行**，不进任何 assert（**数字不进门**，
  dsh #1575 §二）。
- **断言 6（来源谓词，dsh #1581 §三 T3）**：每个 excluded 成员必须能在**同一
  def 体内**找到来源形态——total 锚对「换标签」盲，只能由来源谓词补。
- **断言 ③b（反向真空，dsh #1595 ②）**：GUARD_DEFS 每成员 ≥1 站 ∧ excluded
  三类各至少一站——删掉整条守卫站后，③/⑥ 对空集 vacuous-true、④ 守恒仍
  成立 ⇒ 门全绿而守卫消失；只有「枚举成员的命中存在」这一谓词（非数字）能
  抓住「删除」。T4 = 删守卫站的 load ⇒ 门必须红。（dsh #1624 §二后守卫桶缩为
  `_invited_never_present` 单成员：`_invite` 已单源并入漏斗 `_meta_obj`，
  本站零 `json.loads`——缩员由 ③b 谓词自证，非手改数字。）
- **T4 针锚 = 形参标识符 `meta_json`**（dsh #1632 §四）：针已从 DB 列名锚换成
  守卫站唯一 load 的形参名。形参一改名，T4 的 `replace` 变 no-op ⇒ T4b/T4c
  红、门拒——方向 fail-closed 没错，但**诊断会误指「守卫站被删」，实为改名**
  （与 marker_row ③ 同类：改名会红、属设计如此，处理法=同步改本注记并评审；
  T1 的「改名不改计数」自测适用域=标签组，对 T4 针【不】适用）。
- 文件范围：只扫 `server.py`；tests 显式豁免（stage1/stage6 种坏值是夹具职责，
  pi #1564 §三(2)）。
- import 面（pi #1573 §三）：`from json import` ≡ 0 ∧ `import json as` ≡ 0，
  **不锚行首**（函数局部别名语法合法，行首锚会漏）——AST 全文扫描。

Run: python3 test_v212_beta_gate.py   （零执行面：ast.parse 只解析不运行 server）
"""
import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "server.py")

# ── 单点配置（唯一入口；枚举由读 def 手写，数字只作汇总行）────────────────
FUNNEL_DEFS = {"_meta_obj", "_meta_state", "_get_meta"}   # 语义：恰一处 meta 解析
GUARD_DEFS = {"_invited_never_present"}   # 守卫白名单：try+isinstance
                                          # （_invite 单源入漏斗 dsh #1624 §二：本站已无 load 站）
META_DEFS: set[str] = set()   # 其余 participants.meta 站（修复批后为空集：全并入漏斗）
# 三类排除（不同构，见契约头）：①② 精确 DB 列名；③ 局部标识符
QUORUM_COLUMN = "quorum"
VALUE_COLUMN = "value"
MARKER_IDENT = "marker_row"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f" — {detail}" if detail and not cond else ""))


def _loads_calls(tree):
    """All `json.loads(<...>)` Attribute calls."""
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "json" and node.func.attr == "loads"):
            out.append(node)
    return out


def _parents(tree):
    by_id = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            by_id[id(child)] = node
    return by_id


def _enclosing_def(node, by_id):
    cur = node
    while id(cur) in by_id:
        cur = by_id[id(cur)]
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur
    return None


def _subtree_forms(node):
    """(has_quorum, has_value, has_marker_ident) source forms in the arg subtree."""
    has_q = has_v = has_m = False
    for n in ast.walk(node):
        if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant):
            if n.slice.value == QUORUM_COLUMN:
                has_q = True
            elif n.slice.value == VALUE_COLUMN:
                has_v = True
        if isinstance(n, ast.Name) and n.id == MARKER_IDENT:
            has_m = True
    return has_q, has_v, has_m


def _def_body_forms(fdef):
    """Source forms findable anywhere in the def body (assertion 6, independent
    re-verification of the exclusion tags — same def body only)."""
    has_q = has_v = has_m = False
    has_isinstance_dict = False
    for n in ast.walk(fdef):
        if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant):
            if n.slice.value == QUORUM_COLUMN:
                has_q = True
            elif n.slice.value == VALUE_COLUMN:
                has_v = True
        if isinstance(n, ast.Name) and n.id == MARKER_IDENT:
            has_m = True
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "isinstance" and len(n.args) == 2
                and isinstance(n.args[1], ast.Name) and n.args[1].id == "dict"):
            has_isinstance_dict = True
    return has_q, has_v, has_m, has_isinstance_dict


def _reverse_vacuum_ok(sites):
    """断言 ③b（反向真空，dsh #1595 ②）：每个枚举成员至少命中一站 —— 删除型
    漂移（guard/excluded 整站消失）不走 ①/③/④/⑥ 任何一条红灯：③/⑥ 对空集
    vacuous-true、④ 守恒随 total 同步缩。谓词非数字（不破「数字不进门」）。"""
    guard_names = {name for name, tag in sites if tag == "guard"}
    excl_tags = {tag for _, tag in sites if tag.startswith("excluded:")}
    return (GUARD_DEFS <= guard_names
            and excl_tags == {"excluded:quorum", "excluded:value", "excluded:marker"})


def _in_try(node, by_id, fdef):
    cur = node
    while id(cur) in by_id and cur is not fdef:
        cur = by_id[id(cur)]
        if isinstance(cur, ast.Try):
            return True
    return False


def classify(src):
    """→ (sites, unclassified, double_tagged, counts, defs_of, guard_flags)"""
    tree = ast.parse(src)
    by_id = _parents(tree)
    sites, unclassified, double_tagged = [], [], []
    counts = {"funnel": 0, "guard": 0, "meta": 0, "excluded": 0}
    defs_of, guard_flags, excl_forms = {}, {}, {}
    for call in _loads_calls(tree):
        fdef = _enclosing_def(call, by_id)
        name = fdef.name if fdef else "<module>"
        defs_of[id(call)] = name
        tags = []
        if name in FUNNEL_DEFS:
            tags.append("funnel")
        if name in GUARD_DEFS:
            tags.append("guard")
        if name in META_DEFS:
            tags.append("meta")
        has_q, has_v, has_m = _subtree_forms(_call_args(call))
        if has_q:
            tags.append("excluded:quorum")
        if has_v:
            tags.append("excluded:value")
        if has_m:
            tags.append("excluded:marker")
        if len(tags) == 0:
            unclassified.append((name, "no tag"))
            continue
        if len(tags) > 1:
            double_tagged.append((name, tags))
            continue
        tag = tags[0]
        counts[tag.split(":")[0]] += 1
        sites.append((name, tag))
        if tag == "guard":
            guard_flags[name] = (_in_try(call, by_id, fdef), _def_body_forms(fdef)[3])
        if tag.startswith("excluded:"):
            excl_forms[id(call)] = (name, tag, _def_body_forms(fdef))
    total = len(_loads_calls(tree))
    return sites, unclassified, double_tagged, counts, defs_of, guard_flags, excl_forms, total


def _call_args(call):
    """Fake a walkable container for all argument subtrees."""
    return ast.Module(body=list(call.args) + [k.value for k in call.keywords],
                      type_ignores=[])


def _pattern_diagnostic(sites, defs_of, tree):
    """汇总行（不进断言）：pattern 锚（实参文本含 "meta" 下标）能看见几个 meta
    侧站点 —— 打印「pattern=N (= meta 侧 M − K 非下标形态站点: 名单)」（#1581 §二-5）。"""
    by_id = _parents(tree)
    meta_side = [(n, t) for n, t in sites if t in ("funnel", "guard", "meta")]
    seen = []
    for call in _loads_calls(tree):
        name = defs_of.get(id(call))
        if not any(name == n for n, _ in meta_side):
            continue
        has_meta_sub = any(isinstance(x, ast.Subscript) and isinstance(x.slice, ast.Constant)
                           and isinstance(x.slice.value, str) and "meta" in x.slice.value
                           for arg in list(call.args) + [k.value for k in call.keywords]
                           for x in ast.walk(arg))
        if not has_meta_sub:
            seen.append(name)
    print(f"  [汇总] pattern 锚可见 {len(meta_side) - len(seen)} (= meta 侧 {len(meta_side)} − "
          f"{len(seen)} 非下标形态站点: {sorted(set(seen))}) —— pattern 只作诊断打印，不进断言")
    return len(meta_side) - len(seen), len(seen)


def main():
    with open(SERVER, "rb") as f:
        src = f.read().decode("utf-8")
    print("β 机械门：单一分区 + 标签制（#1581 定稿）—— 只扫 server.py，零执行面\n")

    sites, unclassified, double_tagged, counts, defs_of, guard_flags, excl_forms, total = classify(src)
    tree = ast.parse(src)

    print("断言（计数与谓词；数字不进门）")
    check("① unclassified ≡ 0（default-in：表外新站先红）", len(unclassified) == 0, unclassified)
    check("① double_tag ≡ 0（每站恰好一个标签）", len(double_tagged) == 0, double_tagged)
    check("② count(funnel) ≡ 1（全树恰一处 participants.meta 解析）",
          counts["funnel"] == 1, counts)
    check("④ 守恒校验：total ≡ funnel+guard+meta+excluded",
          total == counts["funnel"] + counts["guard"] + counts["meta"] + counts["excluded"],
          (total, counts))
    check("③ 守卫谓词：枚举的每站在 try 内 ∧ 体内有 isinstance(…, dict)",
          all(in_try and isdict for in_try, isdict in guard_flags.values()), guard_flags)
    check("③b 反向真空：GUARD_DEFS 每成员 ≥1 站 ∧ excluded 三类各至少一站（删除必红；谓词非数字，dsh #1595 ②）",
          _reverse_vacuum_ok(sites), {n: t for n, t in sites
                                      if t == "guard" or t.startswith("excluded:")})
    check("⑥ 来源谓词：每个 excluded 成员的来源形态在同一 def 体内可查",
          all((has_q and tag.endswith("quorum")) or (has_v and tag.endswith("value"))
              or (has_m and tag.endswith("marker"))
              for name, tag, (has_q, has_v, has_m, _d) in excl_forms.values()),
          {k: v for k, v in excl_forms.items()})
    check("import 面：`from json import` ≡ 0 ∧ `import json as` ≡ 0（不锚行首）",
          src.count("from json import") == 0 and src.count("import json as") == 0,
          (src.count("from json import"), src.count("import json as")))

    print("\n汇总行（数字不进门，只打印）")
    per_tag: dict[str, int] = {}
    for _, tag in sites:
        per_tag[tag] = per_tag.get(tag, 0) + 1
    print(f"  [汇总] total json.loads 调用 = {total}；标签分布 = {per_tag}")
    print(f"  [汇总] 漏斗集合 {sorted(FUNNEL_DEFS)}（合计 load ≡ 1）；守卫白名单 "
          f"{sorted(GUARD_DEFS)}（枚举，size 不进断言）；meta 桶 = {len(META_DEFS)} 站（修复批后全并入漏斗）")
    _pattern_diagnostic(sites, defs_of, tree)

    print("\nT1 改名不改计数（适用域 = 标签组成员；③ 排除改名【会】红属设计，#1572 (1)）")
    src_t1 = src.replace("meta_json", "mj").replace("meta_raw", "blob")
    s1, u1, d1, c1, *_r1 = classify(src_t1)
    check("T1a 绑定名/局部名改名 → 分区不变、仍绿",
          (len(u1), len(d1)) == (0, 0) and c1 == counts, (c1, counts))

    print("\nT2 新掺一个绑定名形态 meta 站（pattern 失明、def 成员制红，#1579 §三）")
    src_t2 = src + ("\n\ndef _t2_injected_bound_name_site(meta_blob):\n"
                    "    return json.loads(meta_blob or \"{}\")\n")
    s2, u2, d2, c2, df2, *_r2 = classify(src_t2)
    check("T2a 注入站 unclassified ≡ 1（门会红）", len(u2) == 1, u2)
    pat_before, miss_before = _pattern_diagnostic(sites, defs_of, tree)
    pat_after, _miss2 = _pattern_diagnostic(s2, df2, ast.parse(src_t2))
    check("T2b pattern 锚对注入站失明（可见数不变 = 失明实证）",
          pat_after == pat_before, (pat_before, pat_after))

    print("\nT3 来源形态漂移（json.loads 实参内 t[\"quorum\"] → t[\"quorum2\"]：排除钉精确列名）")
    src_t3 = src.replace('json.loads(t["quorum"])', 'json.loads(t["quorum2"])', 1)
    s3, u3, d3, c3, df3, _gf3, ef3, _t3 = classify(src_t3)
    check("T3a 漂移站脱离排除形态 → unclassified ≡ 1（门会红，断言 6 同源）",
          len(u3) == 1, u3)

    print("\nT4 删一条守卫站（删 `_invited_never_present` 的 load ⇒ 门必须红：③/⑥ 对空集 vacuous-true、"
          "④ 守恒仍成立，唯 ③b 抓得住，dsh #1595 ②；_invite 已单源入漏斗 #1624，本站是守卫桶末成员）")
    src_t4 = src.replace('json.loads(meta_json or "{}")', '"{}"', 1)
    s4, u4, d4, c4, df4, gf4, ef4, t4 = classify(src_t4)
    check("T4a 盲区面实证：删站后 ①/④ 仍绿（unclassified ≡ 0 ∧ 守恒成立）",
          len(u4) == 0 and t4 == c4["funnel"] + c4["guard"] + c4["meta"] + c4["excluded"],
          (u4, t4, c4))
    check("T4b 断言③ vacuous-true（guard_flags 变空集 ⇒ all() 为真，盲区坐实）",
          all(in_try and isdict for in_try, isdict in gf4.values()), gf4)
    check("T4c ③b 反向真空变红（门必拒：枚举成员零命中）",
          not _reverse_vacuum_ok(s4), {n: t for n, t in s4
                                       if t == "guard" or t.startswith("excluded:")})

    print(f"\n{'=' * 60}\nβ gate: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", ", ".join(FAIL))
        return 1
    print("✅ β GATE PASSED: 单一分区+标签制 / funnel≡1 / 守卫谓词+反向真空 / 守恒 / 来源谓词 / import 面 / T1·T2·T3·T4")
    return 0


if __name__ == "__main__":
    sys.exit(main())
