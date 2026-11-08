#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2.12.0 batch3 C2 写点谓词门 + 真可达格（#1657 §四.3 / #1665 §3；两件并集同批勿漏）。

═══ 契约头（判据出处；改动本门先读这段）══════════════════════════════

- **W1 写点函数白名单**：`participants.meta` 的写点集合 ⊆ 7 写源函数表 —— dsh
  #1664 §三③ 自我更正后的**现行版**（11 调用点 / 7 写源 + 1 重建站，8=7 写源
  +1 重建；#1624 §三 的五名清单已作废，勿引）。表重建站
  （`_participants_nullable_last_seen` 的 `INSERT…SELECT`）是**字节拷贝、不计写源**，
  单独枚举。
- **W2 写点实参形态**：每个写站的 meta 值 = meta 值位上的 `json.dumps(...)` 调用
  **或** 恰落在 meta 值位的 SQL 字面 `'{}'`（P3-1(a)(b) 收紧后，GPT 第四轮/dsh
  #1727 §二 判本批修；**谓词门不是数字门**）。空 TEXT 健康（S1f 旧行为）的已知残差
  即由这道门看住：谁写出 `meta=''`/裸字符串，先红。
- **W3 反向真空**：白名单每函数 ≥1 站命中（**谓词非数字**，#1647 数字不进门
  同纪律）——删站/换函数即红，集合约束只封上界，这道封下界。
- **W4 真可达格（pi #1633 §7 / dsh #1632 §三）**：`last_seen IS NULL ∧ meta
  corrupt` ⇒ `_invited_never_present` except 早退 False ⇒ 渲染 `[⚠️停摆]`
  假话。**今天仍可达，只能由盘上坏字节造出**（API 写点全 json.dumps ⇒ W2
  看住新增；改 `in META_CORRUPT` 也不解决它）。本格**钉现状=残留 pin**，
  不是背书；处置（修渲染或修判据）归后续票面，不进本批。
  本格 = **已知残留、非预期行为**（GPT 第四轮 P3-2 加码 / host #1730 定稿④；
  加一句、零判据变更）：渲染若修，本格必须由「正例」改「负例」（期望不出现
  ⚠️停摆），不得删格；删格 = 残留失去见证（dsh #1727 §三 原句）。

Run: python3 test_v212_meta_writegate.py
     （W1–W3 零执行面：ast.parse 只解析不运行 server；W4 纯函数 + stub 隔离库）
"""
import ast
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "server.py")

# ── 单点配置（dsh #1664 §三③ 7 写源函数表；表重建单独计）────────────────
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


def _insert_meta_idx(sql):
    """`meta` 列在 INSERT 列清单里的位次（P3-1(b)：按列名定位，不假设末位；
    无 meta 列 ⇒ -1，fail-closed）。"""
    head = sql.split("VALUES", 1)[0]
    i, j = head.find("("), head.rfind(")")
    if i < 0 or j <= i:
        return -1
    cols = [c.strip().lower() for c in head[i + 1:j].split(",")]
    return cols.index("meta") if "meta" in cols else -1


def _insert_meta_value(sql, params, k):
    """meta 值位（VALUES 第 k 格）上的值：先看字面 `'{}'`（恰落在 meta 值位才
    解锁，P3-1(a)；_touch 站是唯一正当形态）；否则取对应绑定参数 —— VALUES
    前缀里第 k 格之前的 `?` 计数（字面 NULL 占位不耗参），不再盲取末位
    （P3-1(b)）。越界/非 Tuple ⇒ None（fail-closed）。"""
    tail = sql.split("VALUES", 1)[1] if "VALUES" in sql else ""
    i = tail.find("(")
    j = tail.find(")", i + 1)
    if i < 0 or j <= i:
        return None
    vals = [v.strip() for v in tail[i + 1:j].split(",")]
    if k < len(vals) and vals[k] == "'{}'":
        return "literal-ok"
    if params is None or not isinstance(params, ast.Tuple):
        return None
    pos = sum(1 for v in vals[:k] if v == "?")
    return params.elts[pos] if pos < len(params.elts) else None


def _meta_value_ok(sql, params):
    """W2 判据：meta 值 = meta 值位上的 json.dumps(...) 调用，或 SQL 字面 '{}'
    **恰落在 meta 值位**。位置一律按列清单/占位计数定位（P3-1(a)(b)，GPT 第四轮，
    dsh #1727 §二 判本批修 / #1733 §一 六格修正形状）：旧行为「`'{}'` 在 SQL
    任意位置即 True」与「INSERT 盲取末位参数」已废 —— note<>'{}' 不解锁
    （fail-open 方向，N-a 看住），列序变更不再放行坏值/误拒良值
    （N-b/N-b2 两个方向都看住）。UPDATE 站 meta 在首个绑定参数（SET meta=?）。
    本函数 docstring 与判据同笔改（#1699 ④ 纪律：别留自打的话）。"""
    if "UPDATE participants SET meta" in sql:
        if params is None or not isinstance(params, ast.Tuple) or not params.elts:
            return False
        return _is_json_dumps(params.elts[0])
    if "INSERT INTO participants(" in sql:
        k = _insert_meta_idx(sql)
        if k < 0:
            return False
        node = _insert_meta_value(sql, params, k)
        if node == "literal-ok":
            return True
        return node is not None and _is_json_dumps(node)
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
                          sql, params, _meta_value_ok(sql, params)))
        elif "INSERT INTO participants_c2_rebuild" in sql:
            d = _enclosing_def(node, by_id)
            rebuilds.append((d.name if d else "<module>",))

    names = {s[0] for s in sites}
    check("W1: participants.meta 写点函数集合 ⊆ 7 写源函数表(8=7 写源+1 重建,重建站单独枚举;dsh #1664 §三③ 现行版)",
          names <= WRITE_SOURCE_DEFS,
          f"越界={sorted(names - WRITE_SOURCE_DEFS)} sites={sorted(names)}")
    check("W2: 每个写站 meta 值 = meta 值位上的 json.dumps(...) 或恰落在 meta 值位的字面 '{}'(#1624 §三 谓词;P3-1(a)(b) 收窄取数后)",
          all(s[4] for s in sites),
          f"违例={[(s[0], s[1]) for s in sites if not s[4]]}")
    check("W1: 表重建站单独枚举(字节拷贝、不计写源,enclosing def 相符)",
          len(rebuilds) >= 1 and {r[0] for r in rebuilds} == {REBUILD_DEF},
          f"rebuilds={rebuilds}")
    # W3 反向真空：白名单每函数 ≥1 站(谓词,非数字)。
    missing = [n for n in WRITE_SOURCE_DEFS if n not in names]
    check("W3: 反向真空 —— 7 写源函数每函数 ≥1 站(删站/换函数即红)",
          not missing, f"缺席={missing}")
    # ---------- P3-1 (a)(b) 红绿格（GPT 第四轮；dsh #1727 §二 判本批修 /
    # #1733 §一 六格修正形状：N-a 必须是「坏 meta 值 + '{}' 在同条 SQL 别处」，
    # 喂 json.dumps 的原写法修前修后都 True，无区分力=#853 族死格）----------
    def _mk(sql_txt, params_src):
        return _meta_value_ok(sql_txt, ast.parse(params_src, mode="eval").body)

    check("P-a 对照: _touch 站 —— 字面 '{}' 恰在 meta 值位(5/5 列) ⇒ True(唯一正当字面形态，不许顺手打红)",
          _mk("INSERT INTO participants(author, first_seen, last_seen, last_read, meta)"
              " VALUES(?,?,?,NULL,'{}')", "(author, now, now)"))
    check("P-b 对照: UPDATE SET meta=? 首参 json.dumps ⇒ True(良站不许被顺手打红;params 合成 Tuple 形状=真站形状,非裸 Call)",
          _mk("UPDATE participants SET meta=? WHERE author=?", "(json.dumps('{}'), 'anna')"))
    check("N-a(P3-1a 真红格): 坏 meta 值(首参) ∧ '{}' 在同条 SQL 别处(note<>'{}') ⇒ False —— 旧判据 True(escape 任意位置命中)= fail-open，本格最先红;红来自取值判别非形状早退",
          _mk("UPDATE participants SET meta=? WHERE author=? AND note<>'{}'",
              "('raw-bytes', 'anna')") is False)
    check("N-a2(无区分力对照): 同 SQL 良值 json.dumps ⇒ True(escape 收窄后不误伤良站)",
          _mk("UPDATE participants SET meta=? WHERE author=? AND note<>'{}'",
              "(json.dumps('{}'), 'anna')"))
    check("N-b(P3-1b 真红格): meta 列在中间 ∧ meta 位=坏值 ∧ 末位=json.dumps ⇒ 按列名取 meta 位 ⇒ False(旧判据取末位放行坏值=fail-open)",
          _mk("INSERT INTO participants(author, meta, last_seen) VALUES(?,?,?)",
              "'a', 'raw-bytes', json.dumps('{}')") is False)
    check("N-b2(P3-1b 反向格): meta 位=json.dumps ∧ 末位=他值 ⇒ True(旧判据误拒良值=假红)",
          _mk("INSERT INTO participants(author, meta, last_seen) VALUES(?,?,?)",
              "'a', json.dumps('{}'), 'x'"))
    # 接线证明（变异体自证生效，dsh #1731 §四.2 / #853 恒真假绿族）：同一真站集
    # 用取反判据重评 ⇒ ≥1 站 verdict 翻转（锚点=每站恰评 1 次 ∧ 结果确实变了）。
    # 若 W2 没真正调用判据（恒真假绿），本格必红。
    _ok_mut = [not _meta_value_ok(s[2], s[3]) for s in sites]
    _flip = sum(1 for a, b in zip([s[4] for s in sites], _ok_mut) if a != b)
    check("W2 接线证明: 判据取反变异体下 ≥1 真站 verdict 翻转(判据没接上的恒真假绿在此必红)",
          _flip >= 1, f"flip={_flip}/{len(sites)}")

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
