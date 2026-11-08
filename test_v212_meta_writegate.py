#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2.12.0 batch3 C2 写点谓词门 + 真可达格（#1657 §四.3 / #1665 §3；两件并集同批勿漏）。

═══ 契约头（判据出处；改动本门先读这段）══════════════════════════════

- **W1 写点函数白名单**：`participants.meta` 的写点集合 ⊆ 8 函数表 —— dsh
  #1664 §三③ 自我更正后的**现行版**（11 调用点 / 8 函数；#1624 §三 的五名
  清单已作废，勿引）。表重建站（`_participants_nullable_last_seen` 的
  `INSERT…SELECT`）是**字节拷贝、不计写源**，单独枚举。
- **W2 写点实参形态**：每个写站的 meta 值 = `json.dumps(...)` 调用 **或** SQL
  内字面 `'{}'`（dsh #1624 §三 原判据；**谓词门不是数字门**）。空 TEXT 健康
  （S1f 旧行为）的已知残差即由这道门看住：谁写出 `meta=''`/裸字符串，先红。
- **W3 反向真空**：白名单每函数 ≥1 站命中（**谓词非数字**，#1647 数字不进门
  同纪律）——删站/换函数即红，集合约束只封上界，这道封下界。
- **W4 真可达格（pi #1633 §7 / dsh #1632 §三）**：`last_seen IS NULL ∧ meta
  corrupt` ⇒ `_invited_never_present` except 早退 False ⇒ 渲染 `[⚠️停摆]`
  假话。**今天仍可达，只能由盘上坏字节造出**（API 写点全 json.dumps ⇒ W2
  看住新增；改 `in META_CORRUPT` 也不解决它）。本格**钉现状=残留 pin**，
  不是背书；处置（修渲染或修判据）归后续票面，不进本批。

Run: python3 test_v212_meta_writegate.py
     （W1–W3 零执行面：ast.parse 只解析不运行 server；W4 纯函数 + stub 隔离库）
"""
import ast
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "server.py")

# ── 单点配置（dsh #1664 §三③ 8 函数表；表重建单独计）────────────────────
WRITE_SOURCE_DEFS = {"_touch", "_invite", "_ring_append", "_beat",
                     "_v21_token_backfill", "_set_meta", "register_meta"}
REBUILD_DEF = "_participants_nullable_last_seen"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f" — {detail}" if detail and not cond else ""))


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


def _sql_text(arg):
    """Concatenated constant string parts of an execute() first argument
    (Constant / JoinedStr literal parts / + concatenation). Matching is
    table/column-name based, so f-string holes are harmless here."""
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, ast.JoinedStr):
        return "".join(v.value for v in arg.values
                       if isinstance(v, ast.Constant) and isinstance(v.value, str))
    if isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Add):
        return _sql_text(arg.left) + _sql_text(arg.right)
    return ""


def _is_json_dumps(node):
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "json" and node.func.attr == "dumps")


def _meta_value_ok(sql, params):
    """W2 判据：meta 值 = json.dumps(...) 调用 或 SQL 内字面 '{}'。UPDATE 站
    meta 在首个绑定参数；INSERT 站在末位绑定参数（_touch 的 '{}' 在 SQL 内）。"""
    if "'{}'" in sql:
        return True
    if params is None or not isinstance(params, ast.Tuple) or not params.elts:
        return False
    if "UPDATE participants SET meta" in sql:
        return _is_json_dumps(params.elts[0])
    if "INSERT INTO participants(" in sql:
        return _is_json_dumps(params.elts[-1])
    return False


def main():
    src = open(SERVER, "rb").read().decode("utf-8")
    tree = ast.parse(src)
    by_id = _parents(tree)

    sites = []       # (def_name, kind, ok)
    rebuilds = []    # (def_name,)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute"):
            continue
        if not node.args:
            continue
        sql = _sql_text(node.args[0])
        params = node.args[1] if len(node.args) > 1 else None
        if "UPDATE participants SET meta" in sql or "INSERT INTO participants(" in sql:
            d = _enclosing_def(node, by_id)
            sites.append((d.name if d else "<module>", sql.split("(")[0][:60],
                          _meta_value_ok(sql, params)))
        elif "INSERT INTO participants_c2_rebuild" in sql:
            d = _enclosing_def(node, by_id)
            rebuilds.append((d.name if d else "<module>",))

    names = {s[0] for s in sites}
    check("W1: participants.meta 写点函数集合 ⊆ 8 函数表(dsh #1664 §三③ 现行版)",
          names <= WRITE_SOURCE_DEFS,
          f"越界={sorted(names - WRITE_SOURCE_DEFS)} sites={sorted(names)}")
    check("W2: 每个写站 meta 值 = json.dumps(...) 或 SQL 字面 '{}'(#1624 §三 谓词)",
          all(s[2] for s in sites),
          f"违例={[(s[0], s[1]) for s in sites if not s[2]]}")
    check("W1: 表重建站单独枚举(字节拷贝、不计写源,enclosing def 相符)",
          len(rebuilds) >= 1 and {r[0] for r in rebuilds} == {REBUILD_DEF},
          f"rebuilds={rebuilds}")
    # W3 反向真空：白名单每函数 ≥1 站(谓词,非数字)。
    missing = [n for n in WRITE_SOURCE_DEFS if n not in names]
    check("W3: 反向真空 —— 7 写源函数每函数 ≥1 站(删站/换函数即红)",
          not missing, f"缺席={missing}")
    print(f"  （写站枚举 {len(sites)} 处 / {len(names)} 函数 + 重建 {len(rebuilds)} 处"
          f" —— 数字只进打印行,不进 assert）")

    # ---------- W4 真可达格（pi #1633 §7 / dsh #1632 §三）----------
    sys.path.insert(0, HERE)
    import test_fixture as fx
    tmp = tempfile.mkdtemp(prefix="rb-writegate-")
    srv = fx.stub_env(os.path.join(tmp, "w.db"))
    srv.init_db()

    f = srv._invited_never_present
    check("W4a 真可达格: last_seen=NULL ∧ meta 坏字节 ⇒ except 早退 False(渲染 ⚠️停摆=假话,残留 pin)",
          f(None, b"corrupt bytes #1633s7") is False)
    check("W4b: last_seen=NULL ∧ meta 非对象('5') ⇒ isinstance 门 False(同渲染)",
          f(None, "5") is False)
    check("W4c 对照: 健康受邀者(invited_at 在场) ⇒ True(受邀未到场,非停摆)",
          f(None, '{"invited_at": "2026-11-08 00:00:00"}') is True)
    check("W4d 对照: 无 invited_at ⇒ False(1.1b 单源判据)",
          f(None, "{}") is False)
    check("W4e 对照: last_seen 非 NULL 直接早退(活跃路径不受 meta 影响)",
          f("2026-11-08 00:00:01", b"corrupt bytes #1633s7") is False)

    # 端到端格: 盘上坏字节行(唯一可达通道)经 list_participants 渲染 ⚠️停摆。
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with srv.db() as conn:
        srv._begin_write(conn)
        conn.execute(
            "INSERT INTO participants(author, first_seen, last_seen, last_read, meta)"
            " VALUES(?,?,NULL,NULL,?)",
            ("w4-corrupt", now, b"corrupt bytes #1633s7"))
    out = srv.list_participants()
    line = [l for l in out.split("\n") if "w4-corrupt" in l]
    check("W4f 端到端: 盘上坏字节行经 list_participants 渲染 ⚠️停摆(假话可见,残留 pin;括注含受邀提示与 meta_unparsable 诊断)",
          bool(line) and "⚠️停摆" in line[0] and "meta_unparsable" in line[0]
          and "受邀未到场" in line[0],
          f"line={line}")

    print(f"\n{'='*60}\nv2.12 meta writegate: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", *FAIL, sep="\n  - ")
        sys.exit(1)


if __name__ == "__main__":
    main()
