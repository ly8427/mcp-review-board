# -*- coding: utf-8 -*-
"""mask_faces — 批次2 2.1 掩码声明表 + 派生棘轮（#113 v9 终版清单的落库面）。

规格锚（#113 逐条钉定，v6→v9 演化）：
  v6 (#2383)  断言C 等式 / 夹具F / sink=none 闭集 / audit 回声补丁
  v7 (#2385)  断言C 量化域=表格行集(=派生集)；class=mask 点必须入表；WIRED⊆派生集
  v8 (#2386/#2389/#2391)  两维独立：CLASS 五值闭集、SINK 独立闭集、sink=none 只作
              sink 维取值；断言D(值域 fail-fast)、断言E(键位钉 sink 维)、夹具G
  v9 (#2392/#2393/#2394)  断言F(维内错误分类)、夹具H、resp 文法、issuance/echo
              两处键位钉定（交付面全集 / 校验拒绝回显 resp 面）

插值点判据句（#2372 §2 钉定原文）：
  「每个把库存字符串或客户端字符串插值进响应的插值点」都过 _mask_credential_text。

派生域（(b) 方案，#2376 §7 定稿）：server.py 全模块 ast.walk（嵌套 def 天然下潜），
  插值面三族 = JoinedStr ∪ dict 字面量键值 ∪ 下标赋值值（#2375 §2.2/§2.3 加宽）。
粒度 = (限定名链, 表达式指纹)（#2377 §1：render×2 同名 ⇒ 必须带父链；零行号）。
计数只打印、不进断言（#2375 §3.1）；「点数」是链式人账口径（#2387 §4 对账：
  独立按列穷举 23 点，链式台账含 get_thread context 双计 24——同指纹同函数的
  多落点在本粒度下合并为一行，行集是权威、数字只打印）。

断言面（configs/test_mask_faces.py）：
  B: 派生集 − 表格行集 == ∅（每个派生点一行，#2384 §3）
  C: ∀p ∈ 表格行集: (p ∈ WIRED) ⟺ NEEDS_MASK(p)（#2382 §2 等式）
  D: p.class ∈ CLASS ∧ (p.sink ∈ SINK ∨ RESP_RE.fullmatch(p.sink))
     （v8 值域 fail-fast；resp:* 形 sink 按 RESP_RE 值域计——#2458 口径，
     dsh #2528 §4 (b)、#2533 §2 (ii)）——拆两句（#2451 §2）：
     D1 机械行（override_reason 为空的行——None；与 override 行互斥且穷尽，
        由字段判定非自述——dsh #2541 §2①，pi #2542 收进本批；载荷 v4 #2461
        既有 override_reason: str|None）sink 逐字 ==
        sink_for(qname, rootmap, rootkinds)；override 行（override_reason 非 None，
        有标记）不参与逐字比较；理由串内容不进判据（#2465，pi #120 收窄：
        内容不进、在场进）；有标记⇒理由非空（#2461）——"" 即缺理由，红点=
        『override 行缺理由』显式断言，不指望 D1/D2。多根时取根名字典序首（sink_for 的
        for r in sorted(roots)），其余根静默丢弃；活例
        _load_credential_registry：根 10（9 tool + 1 route），机械 sink =
        resp:tool:create_thread（#2458/#2459 双源，zcode #2538 实测同值；
        dsh #2537 §1）。
     D2 override 行（override_reason 非 None）的 sink 为 resp:* 形时另跑：
        RESP_RE 文法 ∧ 解析式配对
        kind == rootkinds[name]（sink_for 的 override 分支原样返回、不过文法，
        配对必须单独跑——#2504 §1；「名字∈派生集」是配对的推论——rootkinds
        即派生装饰器表，不两写第二份名单——#2457 §2）。
  E: {p : p.sink == none} ⟺ SINK_NONE_LANDINGS（八穷尽落点，键位钉 sink 维，
     集合等价非计数——#2382 §3）
  F: ∀p class∈{identity,closed_domain}: fingerprints(p) ∩ FREE_TEXT_FP == ∅
     （FREE_TEXT_FP = mask 行指纹集，从派生集取、禁手抄——#2392 §1）
  键位①: issuance 行 ⊆ ISSUANCE_FACES（交付面全集：resp:claim_token 三回执 ∪
          form_b nonce 一点双落点 log∪file——#2393 §3① / #2394 §1）
  键位②: echo 行 ⊆ ECHO_FACES（校验拒绝回显 resp 面名单——#2393 §3② / #2394 §2）
  audit 回声补丁: sink=audit 的行须给出回声 resp 行（AUDIT_ECHO；同键自回声在
     reason 记两源即可——#2382 §3 / #2374 §1「声明表记两源」）

本模块自身不 import server（同 attention_contract 先例，#1048 ①）；纯静态、
零运行期依赖（标准库 only）。

  sink=none 与 form_b 双落点的记账口径（实现裁定，随回执披露）：
  request_form_b 的 {nonce} 行 sink=none（不进任何工具/路由返回体——
  request_form_b 内「绝不进返回体」注释），class=issuance（交付面全集成员、
  v9 键位①管它）；「八穷尽
  落点」按钉原文含 request_form_b（#2382 §3：7 直调 + 1 间接），故 E 的
  sink=none 声明集含它——class 维（issuance，豁免）与 sink 维（none，不回显）
  两维独立，正是 v8 的本意。
"""
from __future__ import annotations

import ast
import pathlib
import re
from dataclasses import dataclass, field

SERVER = pathlib.Path(__file__).resolve().parent.parent / "server.py"

# --- v8 两维闭集（#2391 §2 逐字） -------------------------------------------
CLASS = ("identity", "issuance", "echo", "closed_domain", "mask")
SINK = ("resp", "log", "file", "hash", "audit", "none")
EXEMPT = ("identity", "issuance", "echo", "closed_domain")  # 四元，#2382 §2 伪码逐字

# resp:* 文法（#2392 §2）：参数化写法不是字面量；名字必须 ∈ 派生集出现过的
# @mcp.tool 名 / HTTP 路由名集合（禁手抄——名字集合运行时从派生树收集）。
RESP_RE = re.compile(r"^resp:(tool|route):[a-z][a-z0-9_]{2,39}$")


def needs_mask(sink: str, cls: str) -> bool:
    """NEEDS_MASK(p) := p.sink != none ∧ p.class ∉ EXEMPT（#2382 §2 逐字）。"""
    return sink != "none" and cls not in EXEMPT


# 掩码包装器与其余解包装置：核心表达式解到最里层自由文本载体（库存下标 /
# 客户端形参），WIRED 的机械面（是否被 _mask_credential_text 包住）随之可判。
# 非包装调用不下钻（len()/sorted()/token_hex() 等 → call:<unparse> 独立点，
# 防 {token} 那类点被 args 下钻错并成 lit:16）。
WRAPPERS = {"_mask_credential_text", "escape", "str", "_oneline", "_search_snippet",
            "json.dumps"}

# 自由文本形参名单（判据句的操作化，小名单、随回执披露；指纹集本身仍从派生集
# 取、禁手抄——断言F 用的是派生指纹，不是这份名单）。
FREE_TEXT_PARAMS = ("title", "body", "context", "entry", "file", "note")


@dataclass
class Point:
    qname: str                 # 限定名链（get_thread.render / search_comments.payload / <module>）
    fp: str                    # 表达式指纹（核心载体 unparse；param:x / const:X / lit:… / name:x）
    wired: bool = False        # 原始插值表达式里出现 _mask_credential_text（机械面，仅报告用）
    families: set = field(default_factory=set)   # fstr / dictkey / dictval / subassign
    lines: set = field(default_factory=set)
    raw: str = ""

    @property
    def key(self) -> tuple:
        return (self.qname, self.fp)


class _Scope:
    def __init__(self, qname, params):
        self.qname = qname
        self.params = set(params)
        self.assigns = {}    # Name -> value node（同函数作用域，首见为准）
        self.forvars = {}    # Name -> iter node


class _Deriver(ast.NodeVisitor):
    """全模块 walk；三族插面；限定名链；核心解包。"""

    def __init__(self):
        self.points: dict[tuple, Point] = {}
        self.scopes: list[_Scope] = []
        self.decorators: dict[str, str] = {}   # 根函数名 -> tool|route
        self.calls: dict[str, set] = {}        # 函数名 -> 直接调用的函数名集合
        self.globals: dict[str, ast.AST] = {}  # 模块级 Assign Name -> value

    # --- 作用域 ---
    def _push(self, node, qname):
        params = []
        a = node.args
        for lst in (a.posonlyargs, a.args, a.kwonlyargs):
            params += [x.arg for x in lst]
        if a.vararg:
            params.append(a.vararg.arg)
        if a.kwarg:
            params.append(a.kwarg.arg)
        self.scopes.append(_Scope(qname, params))

    def _pop(self):
        self.scopes.pop()

    def visit_FunctionDef(self, node):
        qname = (self.scopes[-1].qname + "." + node.name) if self.scopes else node.name
        for dec in node.decorator_list:
            dn = ast.unparse(dec)
            if dn == "mcp.tool":
                self.decorators[node.name] = "tool"
            elif dn.startswith("mcp.custom_route"):
                self.decorators[node.name] = "route"
        self._push(node, qname)
        self.generic_visit(node)
        self._pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    # --- 模块级常量（const:X 判定用）---
    def visit_Module(self, node):
        for st in node.body:
            if isinstance(st, ast.Assign):
                for tg in st.targets:
                    if isinstance(tg, ast.Name):
                        self.globals[tg.id] = st.value
        self.generic_visit(node)

    # --- 记录调用图（sink root 传播用）---
    def visit_Call(self, node):
        f = node.func
        nm = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)
        if nm and self.scopes:
            cur = self.scopes[-1].qname.split(".")[-1]
            self.calls.setdefault(cur, set()).add(nm)
        self.generic_visit(node)

    # --- 三族插值面 ---
    def visit_JoinedStr(self, node):
        for v in node.values:
            if isinstance(v, ast.FormattedValue):
                self._point(v.value, "fstr")
        self.generic_visit(node)

    def visit_Dict(self, node):
        for k, v in zip(node.keys, node.values):
            if k is not None and not isinstance(k, ast.Constant):
                self._point(k, "dictkey")
            if not isinstance(v, ast.Constant):
                self._point(v, "dictval")
        self.generic_visit(node)

    def visit_Assign(self, node):
        for tg in node.targets:
            # 同作用域 Name 赋值登记（Name→assign 解包用）
            if isinstance(tg, ast.Name) and self.scopes and tg.id not in self.scopes[-1].assigns:
                self.scopes[-1].assigns[tg.id] = node.value
            if isinstance(tg, ast.Subscript):
                self._point(node.value, "subassign")
        self.generic_visit(node)

    def visit_For(self, node):
        if isinstance(node.target, ast.Name) and self.scopes:
            self.scopes[-1].forvars.setdefault(node.target.id, node.iter)
        self.generic_visit(node)

    # --- 核心 ---
    def _scope_of(self, name):
        for s in reversed(self.scopes):
            if name in s.params:
                return s, "param"
            if name in s.assigns:
                return s, "assign"
            if name in s.forvars:
                return s, "for"
        return None, None

    def core(self, node, depth=0, wired=None):
        """解包装置到最里层载体。返回指纹串；wired 列表收集 _mask 包装事实。"""
        if depth > 8 or node is None:
            return "amb:deep"
        if isinstance(node, ast.Call):
            f = node.func
            nm = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)
            if nm in WRAPPERS and node.args:
                if nm == "_mask_credential_text" and wired is not None:
                    wired.append(True)
                return self.core(node.args[0], depth + 1, wired)
            return "call:" + ast.unparse(node)[:64]
        if isinstance(node, ast.Attribute):
            return self.core(node.value, depth + 1, wired)
        if isinstance(node, ast.Name):
            return self._name_fp(node.id, depth, wired)
        if isinstance(node, ast.Constant):
            return f"lit:{node.value!r}"[:60]
        if isinstance(node, (ast.BinOp, ast.BoolOp, ast.IfExp, ast.JoinedStr, ast.FormattedValue,
                             ast.Starred, ast.Tuple, ast.List, ast.Set)):
            # 取首个「载体」子节点（下标/名字优先），否则首子节点
            for ch in ast.walk(node):
                if isinstance(ch, ast.Subscript) or isinstance(ch, ast.Name):
                    return self.core(ch, depth + 1, wired)
            return "amb:op"
        if isinstance(node, ast.Subscript):
            # 字符串字面量键的下标本身就是载体：指纹 = 整式 unparse（板钉口径，
            # comment['body'] / t['title'] / meta['last_wake_error'] / 
            # SEARCH_STATE['reason'] 同形）；基仅作 WIRED 传播。
            sl = node.slice
            sval = sl.value if isinstance(sl, getattr(ast, "Index", ())) else sl
            if isinstance(sval, ast.Constant) and isinstance(sval.value, str):
                self.core(node.value, depth + 1, wired)
                return ast.unparse(node)[:80]
            return self.core(node.value, depth + 1, wired)
        if isinstance(node, (ast.GeneratorExp, ast.ListComp, ast.SetComp, ast.DictComp)):
            return "comp:" + ast.unparse(node)[:56]
        # 其余（比较/运算等）：不可解 → 未定
        return "amb:" + type(node).__name__

    def _name_fp(self, name, depth, wired):
        s, kind = self._scope_of(name)
        if kind == "param":
            return f"param:{name}"
        if kind == "assign":
            return self.core(s.assigns[name], depth + 1, wired)
        if kind == "for":
            return self.core(s.forvars[name], depth + 1, wired)
        if name in self.globals:
            return f"const:{name}"
        return f"name:{name}"

    def _point(self, expr, family):
        wired: list = []
        fp = self.core(expr, 0, wired)
        qname = self.scopes[-1].qname if self.scopes else "<module>"
        key = (qname, fp)
        pt = self.points.get(key)
        if pt is None:
            pt = Point(qname=qname, fp=fp, raw=ast.unparse(expr)[:120])
            self.points[key] = pt
        pt.families.add(family)
        pt.lines.add(getattr(expr, "lineno", 0))
        if wired:
            pt.wired = True


def _roots(calls: dict, decorators: dict) -> dict:
    """helper -> set(根工具/路由名)：反向调用图闭包（守卫环）。"""
    rev: dict[str, set] = {}
    for caller, callees in calls.items():
        for c in callees:
            rev.setdefault(c, set()).add(caller)
    out: dict[str, set] = {}

    def roots_of(fn, seen=None):
        seen = seen or set()
        if fn in seen:
            return set()
        seen = seen | {fn}
        if fn in decorators:
            return {fn}
        acc = set()
        for up in rev.get(fn, ()):
            acc |= roots_of(up, seen)
        return acc

    for fn in set(list(calls.keys()) + list(decorators.keys())):
        out[fn] = roots_of(fn)
    return out


def derive_points(src: str) -> tuple[dict, dict, set, dict]:
    """返回 (points: {(qname,fp): Point}, rootmap, resp_names, rootkinds)。

    resp_names = 派生集里出现过的 @mcp.tool 名 ∪ HTTP 路由名（#2392 §2 禁手抄）。
    rootkinds = 根名 -> tool|route（resp:tool:X / resp:route:X 文法用）。
    """
    d = _Deriver()
    d.visit(ast.parse(src))
    rootmap = _roots(d.calls, d.decorators)
    resp_names = set(d.decorators.keys())
    return d.points, rootmap, resp_names, dict(d.decorators)


def sink_for(qname: str, rootmap: dict, rootkinds: dict,
             override: str | None = None) -> str:
    """机械 sink：根=tool → resp:tool:<名>；根=route → resp:route:<名>；
    无根（stdout/模块路径）→ log（stdout 事实落点）。override 供语义行钉值。
    机械分支（无 override）的返回值过 RESP_RE 文法或属闭集；override 分支
    原样返回、不保证文法——语义权重在人工行与断言 D2。"""
    if override:
        return override
    parts = qname.split(".")
    roots: set = set()
    for i in range(len(parts), 0, -1):
        r = rootmap.get(".".join(parts[:i]), set())
        if r:
            roots = r
            break
    for r in sorted(roots):
        kind = rootkinds.get(r, "tool")
        return f"resp:{kind}:{r}"
    return "log"


def load_server_src(path=SERVER) -> str:
    return pathlib.Path(path).read_text(encoding="utf-8")


# --- batch2 v11 ②: declaration table (frozen payload #2451 §2; classification
#     chain #2540-#2554; rows = full derived set, assertion B set-equality) ---
#
# SINK_NONE_LANDINGS: row-granularity expansion of the 8 frozen call-sites
# (#2382 §3 verbatim list; #2380 §2③ exhaustive scan — 15 rows on the current
# tree). A new none row beyond this set ⇒ E red.
# ISSUANCE_FACES: claim_token three receipts merge into ONE row (same fp,
# #2387 §4 行集权威) + form_b nonce (sink 为 none, #2379 §2.3).
# ECHO_FACES: six frozen sites (#2393 §3②/#2395 §2) merge to 5 rows (set_quorum
# {q!r} ×2 same key).
# AUDIT_ECHO: sink=audit row → its echo resp row (#2382 §3 patch). No audit
# rows exist on the current tree — structure in place, vacuously held.

SINK_NONE_LANDINGS = frozenset({
    ('_floor_wm_epoch', "call:conn.execute('SELECT COALESCE(MAX(id),0) FROM verdict_events').f"),
    ('_floor_wm_epoch', "call:int(row['value'])"),
    ('_floor_wm_epoch', 'call:max(FLOOR_WM_EPOCH_CODE, local)'),
    ('_floor_wm_epoch', 'const:FLOOR_WM_EPOCH_CODE'),
    ('_read_epoch', 'call:int(row[0])'),
    ('_read_epoch', 'call:max(F6_READ_EPOCH_CODE, now_s)'),
    ('_read_epoch', 'const:F6_READ_EPOCH_CODE'),
    ('_search_fingerprint', 'name:body'),
    ('_search_fingerprint', 'name:cid'),
    ('main', "SEARCH_STATE['reason']"),
    ('main', "SEARCH_STATE['sqlite']"),
    ('main', 'const:DB_PATH'),
    ('main', 'const:HOST'),
    ('main', 'const:PORT'),
    ('request_form_b', 'call:hashlib.sha256(nonce.encode()).hexdigest()'),
})

ISSUANCE_FACES = frozenset({
    ('claim_token', 'call:secrets.token_hex(16)'),
    ('request_form_b', 'call:hashlib.sha256(nonce.encode()).hexdigest()'),
})

ECHO_FACES = frozenset({
    ('list_threads', 'param:status'),
    ('post_comment', 'param:severity'),
    ('request_form_b', 'comp:[n.strip() for n in names]'),
    ('set_quorum', 'param:add'),
    ('set_status', 'param:status'),
})

AUDIT_ECHO = {}  # 当前树无 sink=audit 行；新增 audit 行必须同时登记回声 resp 行

DECLARED = {
    ('<module>', 'const:PROTOCOL_VERSION'): {
        "sink": 'log', "class": 'closed_domain', "override_reason": None},
    ('<module>', 'const:THREAD_CAP'): {
        "sink": 'log', "class": 'closed_domain', "override_reason": None},
    ('<module>', 'name:_v6_id_cursor'): {
        "sink": 'log', "class": 'closed_domain', "override_reason": None},
    ('_audit_blocked_once', 'param:reason'): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('_audit_floor_via_note', "call:'; '.join(parts)"): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('_audit_floor_via_note', 'call:_author_usage(conn, thread_id, n)'): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('_audit_floor_via_note', 'call:conn.execute("SELECT MAX(LENGTH(TRIM(note))) FROM verdict_events'): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('_audit_floor_via_note', 'param:budget'): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('_audit_floor_via_note', 'param:members'): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('_audit_meta_unparsable', 'name:fp'): {
        "sink": 'resp:tool:ack_token', "class": 'closed_domain', "override_reason": None},
    ('_audit_meta_unparsable', 'name:length'): {
        "sink": 'resp:tool:ack_token', "class": 'closed_domain', "override_reason": None},
    ('_audit_meta_unparsable', 'param:key'): {
        "sink": 'resp:tool:ack_token', "class": 'closed_domain', "override_reason": None},
    ('_audit_meta_unparsable', 'param:state'): {
        "sink": 'resp:tool:ack_token', "class": 'closed_domain', "override_reason": None},
    ('_audit_meta_unparsable', 'param:where'): {
        "sink": 'resp:tool:ack_token', "class": 'closed_domain', "override_reason": None},
    ('_awaiting_split', "call:_l15_waiting(conn, r['id'], r['author'], quorum, author)"): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('_awaiting_split', "r['id']"): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('_beat', 'call:_read_epoch(conn)'): {
        "sink": 'resp:tool:list_comments_since', "class": 'closed_domain', "override_reason": None},
    ('_beat', 'call:len(meta[BEAT_READ_KEY])'): {
        "sink": 'resp:tool:list_comments_since', "class": 'closed_domain', "override_reason": None},
    ('_beat', 'comp:[x for x in legacy if _ts_epoch(x) is not None and _ts_e'): {
        "sink": 'resp:tool:list_comments_since', "class": 'closed_domain', "override_reason": None},
    ('_beat', 'lit:True'): {
        "sink": 'resp:tool:list_comments_since', "class": 'closed_domain', "override_reason": None},
    ('_beat', 'name:len'): {
        "sink": 'resp:tool:list_comments_since', "class": 'closed_domain', "override_reason": None},
    ('_beat_ok', 'call:int(stale // 60)'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_beat_ok', 'call:len(ts)'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_beat_ok', 'call:sum((1 for g in gaps if g >= F6_GAP_S))'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_beat_ok', 'const:F6_BEATS_24H'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_beat_ok', 'const:F6_FRESH_S'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_beat_ok', 'const:F6_GAPS_MIN'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_beat_ok', 'const:F6_GAP_S'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_board_html', 'amb:Compare'): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_board_html', 'amb:op'): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_board_html', "c['author']"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_board_html', "c['id']"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_board_html', "c['t_title']"): {
        "sink": 'resp:route:homepage', "class": 'mask', "override_reason": None},
    ('_board_html', "c['thread_id']"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_board_html', "call:' '.join(sorted({c['author'] for c in by_thread.get(t['id'], [])"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_board_html', "call:', '.join((escape(a) for a in aw))"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_board_html', 'call:_epoch(ts)'): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_board_html', "call:_profile_brief(_profile_numbers(roc, pp['author']))"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_board_html', "call:_relative(c['created_at'])"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_board_html', 'call:_relative(last)'): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_board_html', 'call:_relative(ts)'): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_board_html', "call:briefs.get(p['author'], '')"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_board_html', "call:len(t['context'])"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_board_html', 'call:len(threads)'): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_board_html', 'comp:[n for n in names if n not in have and n in active]'): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_board_html', "lc['created_at']"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_board_html', "lit:''"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_board_html', "lit:'📨'"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_board_html', 'name:escape'): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_board_html', 'name:spk'): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_board_html', "p['author']"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_board_html', "t['context']"): {
        "sink": 'resp:route:homepage', "class": 'mask', "override_reason": None},
    ('_board_html', "t['created_at']"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_board_html', "t['id']"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_board_html', "t['n']"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_board_html', "t['status']"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_board_html', "t['title']"): {
        "sink": 'resp:route:homepage', "class": 'mask', "override_reason": None},
    ('_board_html.render', "c['author']"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_board_html.render', "c['body']"): {
        "sink": 'resp:route:homepage', "class": 'mask', "override_reason": None},
    ('_board_html.render', "c['created_at']"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_board_html.render', "c['file']"): {
        "sink": 'resp:route:homepage', "class": 'mask', "override_reason": None},
    ('_board_html.render', "c['id']"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_board_html.render', "c['line']"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_board_html.render', "c['severity']"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_board_html.render', "c['status']"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_board_html.render', "call:len(c['body'] or '')"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_board_html.render', "lit:''"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_board_html.render', 'param:depth'): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_check_wake_infra', 'const:WAKE_INFRA_GUIDE'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_check_wake_infra', 'name:form_b'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_check_wake_infra', 'name:undeclared'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_check_wake_infra', 'name:why'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_check_wake_infra', 'param:thread_id'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_comment_due_face', "call:_floor_blocking_others(conn, r['id'], active, author)"): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('_comment_due_face', "r['id']"): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('_count_today', 'param:table'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_cred_history_record', 'const:CRED_HASH_KEY_PREFIX'): {
        "sink": 'resp:tool:claim_token', "class": 'closed_domain', "override_reason": None},
    ('_cred_history_record', 'param:cred_sha256'): {
        "sink": 'resp:tool:claim_token', "class": 'closed_domain', "override_reason": None},
    ('_f6_creator_gate', 'name:nprobe'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_f6_gap_text', "call:eta.strftime('%Y-%m-%d %H:%M')"): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_f6_gap_text', 'call:int((now - presence[-1]).total_seconds() // 60)'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_f6_gap_text', 'call:len(ts)'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_f6_gap_text', 'call:max(0, F6_BEATS_24H - len(ts))'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_f6_gap_text', 'call:max(0, F6_BEATS_24H - nprobe)'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_f6_gap_text', 'call:max(0, F6_GAPS_MIN - big)'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_f6_gap_text', 'const:F6_BEATS_24H'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_f6_gap_text', 'const:F6_FRESH_S'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_f6_gap_text', 'const:F6_GAPS_MIN'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_f6_gap_text', 'const:F6_GAP_S'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_f6_gap_text', 'lit:0'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_f6_gap_text', 'name:backlog'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_f6_record_exemption', 'amb:deep'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_f6_record_exemption', 'lit:True'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_f6_record_exemption', 'name:int'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_floor_wm_epoch', "call:conn.execute('SELECT COALESCE(MAX(id),0) FROM verdict_events').f"): {
        "sink": 'none', "class": 'closed_domain', "override_reason": 'E 穷尽落点(#2382 §3/#2380 §2③)'},
    ('_floor_wm_epoch', "call:int(row['value'])"): {
        "sink": 'none', "class": 'closed_domain', "override_reason": 'E 穷尽落点(#2382 §3/#2380 §2③)'},
    ('_floor_wm_epoch', 'call:max(FLOOR_WM_EPOCH_CODE, local)'): {
        "sink": 'none', "class": 'closed_domain', "override_reason": 'E 穷尽落点(#2382 §3/#2380 §2③)'},
    ('_floor_wm_epoch', 'const:FLOOR_WM_EPOCH_CODE'): {
        "sink": 'none', "class": 'closed_domain', "override_reason": 'E 穷尽落点(#2382 §3/#2380 §2③)'},
    ('_form_b_confirmed', "call:p.get('names')"): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_form_b_confirmed', 'param:names'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_form_b_confirmed', 'param:thread_id'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_invite', 'call:_now_iso()'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_invite', 'param:thread_id'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_legacy_shape_fixups', 'name:col'): {
        "sink": 'log', "class": 'closed_domain', "override_reason": None},
    ('_legacy_shape_fixups', 'name:ddl'): {
        "sink": 'log', "class": 'closed_domain', "override_reason": None},
    ('_load_credential_registry', 'name:e'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_load_credential_registry', 'name:state'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_load_credential_registry', "r['author']"): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_load_credential_registry', "r['key']"): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_mentioned', 'call:_MENTION_RE.get(author)'): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('_mentioned', 'param:author'): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('_meta_unparsable_error', 'param:state'): {
        "sink": 'log', "class": 'closed_domain', "override_reason": None},
    ('_meta_unparsable_error', 'param:where'): {
        "sink": 'log', "class": 'closed_domain', "override_reason": None},
    ('_participants_nullable_last_seen', "call:','.join(keep)"): {
        "sink": 'log', "class": 'closed_domain', "override_reason": None},
    ('_probe_channel', "call:conn.execute('SELECT COALESCE(MAX(id),0) FROM comments').fetchon"): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_probe_channel', "call:conn.execute('SELECT last_delivered_id FROM participants WHERE a"): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_probe_channel', 'call:int((now - probes[-1]).total_seconds() // 60)'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_probe_channel', 'call:len(probes)'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_probe_channel', 'call:max(0, int(max_id) - int(delivered))'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_probe_channel', 'const:F6_BEATS_24H'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_profile_brief', "call:j.get('first_verdicts', 0)"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_profile_brief', "call:o.get('active_overridden', 0)"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_profile_brief', "call:o.get('frozen_unadjudicated', 0)"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_profile_brief', "call:o.get('revision_absorbed', 0)"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_profile_brief', "call:prof.get('metric_version')"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_profile_numbers', 'amb:Compare'): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_profile_numbers', 'amb:Dict'): {
        "sink": 'resp:route:homepage', "class": 'mask', "override_reason": None},
    ('_profile_numbers', "call:_invited_never_present(p['last_seen'], p['meta'])"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_profile_numbers', "call:_relative(p['first_seen'])"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_profile_numbers', "call:_relative(p['last_seen'])"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_profile_numbers', 'call:bool(wake_err)'): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_profile_numbers', 'call:len(first_by_tr)'): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_profile_numbers', "call:sum((1 for e in first_by_tr.values() if e['to_v'] == 'object'))"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_profile_numbers', 'const:METRIC_VERSION'): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_profile_numbers', "e['to_v']"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_profile_numbers', "lit:'revision 开始→首判,带宽化展示(无中位数,防速判激励);契约 (a)/(b) 分层读 meta.co"): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_profile_numbers', 'lit:0'): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_profile_numbers', 'lit:None'): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_profile_numbers', 'name:awaiting_threads'): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_profile_numbers', 'param:author'): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_read_epoch', 'call:int(row[0])'): {
        "sink": 'none', "class": 'closed_domain', "override_reason": 'E 穷尽落点(#2382 §3/#2380 §2③)'},
    ('_read_epoch', 'call:max(F6_READ_EPOCH_CODE, now_s)'): {
        "sink": 'none', "class": 'closed_domain', "override_reason": 'E 穷尽落点(#2382 §3/#2380 §2③)'},
    ('_read_epoch', 'const:F6_READ_EPOCH_CODE'): {
        "sink": 'none', "class": 'closed_domain', "override_reason": 'E 穷尽落点(#2382 §3/#2380 §2③)'},
    ('_reissue_window_end', "call:end_dt.strftime('%Y-%m-%d %H:%M:%S')"): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('_reissue_window_end', 'const:AUTO_ACK_REISSUE_HOURS'): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('_reissue_window_end', "last['created_at']"): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('_relative', 'call:int(secs // 3600)'): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_relative', 'call:int(secs // 60)'): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_relative', 'call:int(secs // 86400)'): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_relative', 'call:int(secs)'): {
        "sink": 'resp:route:homepage', "class": 'closed_domain', "override_reason": None},
    ('_revision_floor', "call:conn.execute('SELECT COALESCE(MAX(id),0) FROM comments WHERE thr"): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('_revision_floor', "ev['id']"): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('_ring_append', 'comp:[x for x in meta.get(key) or [] if x]'): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('_search_apply.work', 'call:_now_iso()'): {
        "sink": 'log', "class": 'closed_domain', "override_reason": None},
    ('_search_apply.work', 'call:_search_fingerprint(c)'): {
        "sink": 'log', "class": 'closed_domain', "override_reason": None},
    ('_search_apply.work', "call:c.execute('SELECT COUNT(*) FROM comments').fetchone()"): {
        "sink": 'log', "class": 'closed_domain', "override_reason": None},
    ('_search_apply.work', 'name:sqlite3'): {
        "sink": 'log', "class": 'closed_domain', "override_reason": None},
    ('_search_bootstrap', 'name:e'): {
        "sink": 'log', "class": 'closed_domain', "override_reason": None},
    ('_search_bootstrap', 'name:reason'): {
        "sink": 'log', "class": 'closed_domain', "override_reason": None},
    ('_search_bootstrap', 'name:sqlite3'): {
        "sink": 'log', "class": 'closed_domain', "override_reason": None},
    ('_search_capability', 'name:sqlite3'): {
        "sink": 'log', "class": 'closed_domain', "override_reason": None},
    ('_search_fingerprint', 'name:body'): {
        "sink": 'none', "class": 'mask', "override_reason": 'E 穷尽落点(#2382 §3/#2380 §2③)'},  # 指纹存储面不外显；掩码禁入（改指纹语义）
    ('_search_fingerprint', 'name:cid'): {
        "sink": 'none', "class": 'closed_domain', "override_reason": 'E 穷尽落点(#2382 §3/#2380 §2③)'},
    ('_search_repair.work', 'call:_now_iso()'): {
        "sink": 'log', "class": 'closed_domain', "override_reason": None},
    ('_search_repair.work', 'call:_search_fingerprint(c)'): {
        "sink": 'log', "class": 'closed_domain', "override_reason": None},
    ('_search_repair.work', "call:c.execute('SELECT COUNT(*) FROM comments').fetchone()"): {
        "sink": 'log', "class": 'closed_domain', "override_reason": None},
    ('_search_repair.work', 'name:sqlite3'): {
        "sink": 'log', "class": 'closed_domain', "override_reason": None},
    ('_stale_object_holders', "r['updated_at']"): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('_stale_object_holders', "r['verdict']"): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('_stale_receipt_note', 'call:_stale_object_holders(conn, thread_id)'): {
        "sink": 'resp:tool:post_comment', "class": 'closed_domain', "override_reason": None},
    ('_token_blocked_face', 'amb:Dict'): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('_token_blocked_face', 'call:_tier_out(e)'): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('_token_blocked_face', "call:e.get('since')"): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('_token_blocked_face', "lit:''"): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('_token_blocked_face', "win['reason']"): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('_token_blocked_face', "win['window_end']"): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('_token_gate', 'amb:Dict'): {
        "sink": 'resp:tool:bump_revision', "class": 'closed_domain', "override_reason": None},
    ('_token_gate', 'call:_now_iso()'): {
        "sink": 'resp:tool:bump_revision', "class": 'closed_domain', "override_reason": None},
    ('_token_gate', "call:conn.execute('SELECT revision FROM threads WHERE id=?', (thread_"): {
        "sink": 'resp:tool:bump_revision', "class": 'closed_domain', "override_reason": None},
    ('_token_gate', "call:dict(meta.get('token_blocked') or {})"): {
        "sink": 'resp:tool:bump_revision', "class": 'closed_domain', "override_reason": None},
    ('_token_ok', 'call:_now_iso()'): {
        "sink": 'resp:tool:bump_revision', "class": 'closed_domain', "override_reason": None},
    ('_token_ok', "lit:'auto'"): {
        "sink": 'resp:tool:bump_revision', "class": 'closed_domain', "override_reason": None},
    ('_token_ok', 'lit:True'): {
        "sink": 'resp:tool:bump_revision', "class": 'closed_domain', "override_reason": None},
    ('_v21_token_backfill', 'call:_now_iso()'): {
        "sink": 'log', "class": 'closed_domain', "override_reason": None},
    ('_v21_token_backfill', 'lit:True'): {
        "sink": 'log', "class": 'closed_domain', "override_reason": None},
    ('_wake_infra_gap', "call:m.group('form').lower()"): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('_wake_ledger_ok', 'call:entries.get(n.lower())'): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('_wake_ledger_ok', "call:m.group('result').lower()"): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('_wake_ledger_ok', 'name:why'): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('_wake_ledger_ok', 'param:suspended'): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('ack_token', 'call:_now_iso()'): {
        "sink": 'resp:tool:ack_token', "class": 'closed_domain', "override_reason": None},
    ('ack_token', "call:meta.get('token_acked_at')"): {
        "sink": 'resp:tool:ack_token', "class": 'closed_domain', "override_reason": None},
    ('ack_token', 'const:REISSUE_AFTER_HOURS'): {
        "sink": 'resp:tool:ack_token', "class": 'closed_domain', "override_reason": None},
    ('ack_token', "lit:'explicit'"): {
        "sink": 'resp:tool:ack_token', "class": 'closed_domain', "override_reason": None},
    ('ack_token', 'lit:True'): {
        "sink": 'resp:tool:ack_token', "class": 'closed_domain', "override_reason": None},
    ('ack_token', 'param:author'): {
        "sink": 'resp:tool:ack_token', "class": 'closed_domain', "override_reason": None},
    ('attention_probe', 'amb:Compare'): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('attention_probe', 'amb:Dict'): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('attention_probe', 'call:_author_usage(conn, tid, author)'): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('attention_probe', 'call:_comment_due_face(conn, author)'): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('attention_probe', 'call:_token_blocked_face(conn, author)'): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('attention_probe', 'call:_token_invalid_tids(conn, author)'): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('attention_probe', "call:conn.execute('SELECT last_read, last_delivered_id FROM participa"): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('attention_probe', "call:conn.execute('SELECT verdict FROM verdicts WHERE thread_id=? AND"): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('attention_probe', 'call:int(time.time() - _STARTED_AT)'): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('attention_probe', "call:request.query_params.get('author')"): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('attention_probe', 'comp:[r[\'id\'] for r in conn.execute("SELECT id, quorum FROM t'): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('attention_probe', 'const:_PROBE_ERRORS'): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('attention_probe', 'name:BOARD_INSTANCE_ID'): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('attention_probe', 'name:_PROBE_ERRORS_BY_AUTHOR'): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('attention_probe', 'name:blocked_verdict'): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('attention_probe', 'name:mrow_state'): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('attention_probe', 'name:reason'): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('attention_probe', 'name:threads'): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('attention_probe', "row['last_read']"): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('attention_probe', "trow['per_author_budget']"): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('attention_probe', "trow['revision']"): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('bump_revision', "call:conn.execute('SELECT COALESCE(MAX(id), 0) FROM comments WHERE th"): {
        "sink": 'resp:tool:bump_revision', "class": 'closed_domain', "override_reason": None},
    ('bump_revision', 'param:thread_id'): {
        "sink": 'resp:tool:bump_revision', "class": 'closed_domain', "override_reason": None},
    ('bump_revision', "t['author']"): {
        "sink": 'resp:tool:bump_revision', "class": 'closed_domain', "override_reason": None},
    ('bump_revision', "t['revision']"): {
        "sink": 'resp:tool:bump_revision', "class": 'closed_domain', "override_reason": None},
    ('claim_token', "call:meta.get('token_acked_via', 'explicit')"): {
        "sink": 'resp:tool:claim_token', "class": 'closed_domain', "override_reason": None},
    ('claim_token', 'call:secrets.token_hex(16)'): {
        "sink": 'resp:tool:claim_token', "class": 'issuance', "override_reason": None},  # ISSUANCE_FACES：三回执同指纹合并一行(#2387 §4)；接线禁入(#2382 §1)
    ('claim_token', 'const:UNACKED_REISSUE_MIN'): {
        "sink": 'resp:tool:claim_token', "class": 'closed_domain', "override_reason": None},
    ('claim_token', "lit:'persist it NOW (write file + read back), then ack_token"): {
        "sink": 'resp:tool:claim_token', "class": 'closed_domain', "override_reason": None},
    ('claim_token', 'param:author'): {
        "sink": 'resp:tool:claim_token', "class": 'closed_domain', "override_reason": None},
    ('claim_token', "win['hours']"): {
        "sink": 'resp:tool:claim_token', "class": 'closed_domain', "override_reason": None},
    ('claim_token', "win['last']"): {
        "sink": 'resp:tool:claim_token', "class": 'closed_domain', "override_reason": None},
    ('confirm_form_b', 'call:_now_iso()'): {
        "sink": 'resp:tool:confirm_form_b', "class": 'closed_domain', "override_reason": None},
    ('confirm_form_b', "call:p.get('creator')"): {
        "sink": 'resp:tool:confirm_form_b', "class": 'closed_domain', "override_reason": None},
    ('confirm_form_b', "call:p.get('names')"): {
        "sink": 'resp:tool:confirm_form_b', "class": 'closed_domain', "override_reason": None},
    ('confirm_form_b', "call:p.get('nonce_sha256')"): {
        "sink": 'resp:tool:confirm_form_b', "class": 'closed_domain', "override_reason": None},
    ('confirm_form_b', 'lit:True'): {
        "sink": 'resp:tool:confirm_form_b', "class": 'closed_domain', "override_reason": None},
    ('confirm_form_b', 'param:thread_id'): {
        "sink": 'resp:tool:confirm_form_b', "class": 'closed_domain', "override_reason": None},
    ('create_thread', "call:conn.execute('INSERT INTO threads(title, context, author, quorum"): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('create_thread', 'call:list(dict.fromkeys([author] + names))'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('create_thread', 'call:m.group(1)'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('create_thread', "call:obj_n.get('contract')"): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('create_thread', 'const:THREADS_PER_DAY'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('create_thread', 'name:state_n'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('create_thread', 'param:author'): {
        "sink": 'resp:tool:create_thread', "class": 'closed_domain', "override_reason": None},
    ('create_thread', 'param:title'): {
        "sink": 'resp:tool:create_thread', "class": 'mask', "override_reason": None},
    ('get_thread', "call:' '.join(trails[n])"): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread', "call:' · '.join((f'{a} {_author_usage(conn, thread_id, a)}/{t['per_au"): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread', 'call:_author_usage(conn, thread_id, a)'): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread', "call:json.loads(t['quorum'])"): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread', "call:len(' '.join(str(v['note'] or '').split()))"): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread', 'call:len(all_comments)'): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread', 'call:masked_ctx.splitlines()'): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread', 'const:THREAD_CAP'): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread', "ev['action']"): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread', "ev['author']"): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread', "ev['created_at']"): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread', "ev['from_v']"): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread', "ev['note']"): {
        "sink": 'resp:tool:get_thread', "class": 'mask', "override_reason": None},
    ('get_thread', "ev['revision']"): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread', "ev['to_v']"): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread', "lit:'active'"): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread', "lit:'invited(未到场)'"): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread', "lit:'suspended'"): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread', "lit:'unregistered'"): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread', 'name:a'): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread', 'name:last_voted_rev'): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread', 'name:liveness'): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread', 'param:thread_id'): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread', "t['created_at']"): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread', "t['id']"): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread', "t['per_author_budget']"): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread', "t['quorum']"): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread', "t['revision']"): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread', "t['status']"): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread', "t['title']"): {
        "sink": 'resp:tool:get_thread', "class": 'mask', "override_reason": None},
    ('get_thread', "v['note']"): {
        "sink": 'resp:tool:get_thread', "class": 'mask', "override_reason": None},
    ('get_thread', "v['updated_at']"): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread', "v['verdict']"): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread.render', "call:_mask_credential_text(str(comment['body']), registry).splitlines"): {
        "sink": 'resp:tool:get_thread', "class": 'mask', "override_reason": None},
    ('get_thread.render', "comment['author']"): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread.render', "comment['file']"): {
        "sink": 'resp:tool:get_thread', "class": 'mask', "override_reason": None},
    ('get_thread.render', "comment['id']"): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread.render', "comment['line']"): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread.render', "comment['severity']"): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread.render', "comment['status']"): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread.render', "lit:''"): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('get_thread.render', 'param:depth'): {
        "sink": 'resp:tool:get_thread', "class": 'closed_domain', "override_reason": None},
    ('guard_endpoint', 'amb:Dict'): {
        "sink": 'resp:route:guard_endpoint', "class": 'closed_domain', "override_reason": None},
    ('guard_endpoint', "call:conn.execute('SELECT last_seen FROM participants WHERE author=?'"): {
        "sink": 'resp:route:guard_endpoint', "class": 'closed_domain', "override_reason": None},
    ('guard_endpoint', "call:int(tid_raw or '')"): {
        "sink": 'resp:route:guard_endpoint', "class": 'closed_domain', "override_reason": None},
    ('guard_endpoint', "comp:{r['author']: {'verdict': r['verdict'], 'updated_at': r["): {
        "sink": 'resp:route:guard_endpoint', "class": 'closed_domain', "override_reason": None},
    ('guard_endpoint', "r['updated_at']"): {
        "sink": 'resp:route:guard_endpoint', "class": 'closed_domain', "override_reason": None},
    ('guard_endpoint', "r['verdict']"): {
        "sink": 'resp:route:guard_endpoint', "class": 'closed_domain', "override_reason": None},
    ('guard_endpoint', "t['author']"): {
        "sink": 'resp:route:guard_endpoint', "class": 'closed_domain', "override_reason": None},
    ('guard_endpoint', "t['id']"): {
        "sink": 'resp:route:guard_endpoint', "class": 'closed_domain', "override_reason": None},
    ('guard_endpoint', "t['quorum']"): {
        "sink": 'resp:route:guard_endpoint', "class": 'closed_domain', "override_reason": None},
    ('guard_endpoint', "t['revision']"): {
        "sink": 'resp:route:guard_endpoint', "class": 'closed_domain', "override_reason": None},
    ('guard_endpoint', "t['status']"): {
        "sink": 'resp:route:guard_endpoint', "class": 'closed_domain', "override_reason": None},
    ('init_db', "call:';'.join(_boot_diag)"): {
        "sink": 'log', "class": 'closed_domain', "override_reason": None},
    ('init_db', 'call:int(row[0])'): {
        "sink": 'log', "class": 'closed_domain', "override_reason": None},
    ('init_db', 'call:len(_boot_diag)'): {
        "sink": 'log', "class": 'closed_domain', "override_reason": None},
    ('init_db', 'call:range(v, CORE_SCHEMA_VERSION)'): {
        "sink": 'log', "class": 'closed_domain', "override_reason": None},
    ('init_db', 'const:PROTOCOL_VERSION'): {
        "sink": 'log', "class": 'closed_domain', "override_reason": None},
    ('init_db', 'const:SCHEMA_VERSION'): {
        "sink": 'log', "class": 'closed_domain', "override_reason": None},
    ('list_comments_since', 'amb:Dict'): {
        "sink": 'resp:tool:list_comments_since', "class": 'closed_domain', "override_reason": None},
    ('list_comments_since', 'amb:op'): {
        "sink": 'resp:tool:list_comments_since', "class": 'closed_domain', "override_reason": None},
    ('list_comments_since', 'call:_comment_due_face(conn, author)'): {
        "sink": 'resp:tool:list_comments_since', "class": 'closed_domain', "override_reason": None},
    ('list_comments_since', "call:_mask_credential_text(str(r['body']), registry).replace('\\n', ' "): {
        "sink": 'resp:tool:list_comments_since', "class": 'mask', "override_reason": None},
    ('list_comments_since', 'call:_token_blocked_face(conn, author)'): {
        "sink": 'resp:tool:list_comments_since', "class": 'closed_domain', "override_reason": None},
    ('list_comments_since', 'call:_token_invalid_tids(conn, author)'): {
        "sink": 'resp:tool:list_comments_since', "class": 'closed_domain', "override_reason": None},
    ('list_comments_since', 'call:conn.execute("SELECT COUNT(*) FROM threads WHERE status=\'open\'")'): {
        "sink": 'resp:tool:list_comments_since', "class": 'closed_domain', "override_reason": None},
    ('list_comments_since', 'call:len(rows)'): {
        "sink": 'resp:tool:list_comments_since', "class": 'closed_domain', "override_reason": None},
    ('list_comments_since', "lit:''"): {
        "sink": 'resp:tool:list_comments_since', "class": 'closed_domain', "override_reason": None},
    ('list_comments_since', "lit:'not_implemented'"): {
        "sink": 'resp:tool:list_comments_since', "class": 'closed_domain', "override_reason": None},
    ('list_comments_since', 'lit:None'): {
        "sink": 'resp:tool:list_comments_since', "class": 'closed_domain', "override_reason": None},
    ('list_comments_since', "r['author']"): {
        "sink": 'resp:tool:list_comments_since', "class": 'closed_domain', "override_reason": None},
    ('list_comments_since', "r['created_at']"): {
        "sink": 'resp:tool:list_comments_since', "class": 'closed_domain', "override_reason": None},
    ('list_comments_since', "r['file']"): {
        "sink": 'resp:tool:list_comments_since', "class": 'mask', "override_reason": None},
    ('list_comments_since', "r['id']"): {
        "sink": 'resp:tool:list_comments_since', "class": 'closed_domain', "override_reason": None},
    ('list_comments_since', "r['line']"): {
        "sink": 'resp:tool:list_comments_since', "class": 'closed_domain', "override_reason": None},
    ('list_comments_since', "r['parent_id']"): {
        "sink": 'resp:tool:list_comments_since', "class": 'closed_domain', "override_reason": None},
    ('list_comments_since', "r['severity']"): {
        "sink": 'resp:tool:list_comments_since', "class": 'closed_domain', "override_reason": None},
    ('list_comments_since', "r['thread_id']"): {
        "sink": 'resp:tool:list_comments_since', "class": 'closed_domain', "override_reason": None},
    ('list_participants', 'amb:Compare'): {
        "sink": 'resp:tool:list_participants', "class": 'closed_domain', "override_reason": None},
    ('list_participants', 'call:_relative(last)'): {
        "sink": 'resp:tool:list_participants', "class": 'closed_domain', "override_reason": None},
    ('list_participants', "call:_relative(r['first_seen'])"): {
        "sink": 'resp:tool:list_participants', "class": 'closed_domain', "override_reason": None},
    ('list_participants', "meta['last_wake_error']"): {
        "sink": 'resp:tool:list_participants', "class": 'mask', "override_reason": None},
    ('list_participants', 'name:_invited_never_present'): {
        "sink": 'resp:tool:list_participants', "class": 'closed_domain', "override_reason": None},
    ('list_participants', 'name:state'): {
        "sink": 'resp:tool:list_participants', "class": 'closed_domain', "override_reason": None},
    ('list_participants', "r['author']"): {
        "sink": 'resp:tool:list_participants', "class": 'closed_domain', "override_reason": None},
    ('list_threads', "call:_stale_object_holders(conn, r['id'])"): {
        "sink": 'resp:tool:list_threads', "class": 'closed_domain', "override_reason": None},
    ('list_threads', 'const:VALID_STATUSES'): {
        "sink": 'resp:tool:list_threads', "class": 'closed_domain', "override_reason": None},
    ('list_threads', 'name:stale_by_id'): {
        "sink": 'resp:tool:list_threads', "class": 'closed_domain', "override_reason": None},
    ('list_threads', 'param:status'): {
        "sink": 'resp:tool:list_threads', "class": 'echo', "override_reason": None},  # ECHO_FACES 校验拒绝回显(#2393 §3②)
    ('list_threads', "r['created_at']"): {
        "sink": 'resp:tool:list_threads', "class": 'closed_domain', "override_reason": None},
    ('list_threads', "r['id']"): {
        "sink": 'resp:tool:list_threads', "class": 'closed_domain', "override_reason": None},
    ('list_threads', "r['n']"): {
        "sink": 'resp:tool:list_threads', "class": 'closed_domain', "override_reason": None},
    ('list_threads', "r['status']"): {
        "sink": 'resp:tool:list_threads', "class": 'closed_domain', "override_reason": None},
    ('list_threads', "r['title']"): {
        "sink": 'resp:tool:list_threads', "class": 'mask', "override_reason": None},
    ('main', "SEARCH_STATE['reason']"): {
        "sink": 'none', "class": 'closed_domain', "override_reason": 'E 穷尽落点(#2382 §3/#2380 §2③)'},
    ('main', "SEARCH_STATE['sqlite']"): {
        "sink": 'none', "class": 'closed_domain', "override_reason": 'E 穷尽落点(#2382 §3/#2380 §2③)'},
    ('main', 'const:DB_PATH'): {
        "sink": 'none', "class": 'closed_domain', "override_reason": 'E 穷尽落点(#2382 §3/#2380 §2③)'},
    ('main', 'const:HOST'): {
        "sink": 'none', "class": 'closed_domain', "override_reason": 'E 穷尽落点(#2382 §3/#2380 §2③)'},
    ('main', 'const:PORT'): {
        "sink": 'none', "class": 'closed_domain', "override_reason": 'E 穷尽落点(#2382 §3/#2380 §2③)'},
    ('ping', "SEARCH_STATE['enabled']"): {
        "sink": 'resp:route:ping', "class": 'closed_domain', "override_reason": None},
    ('ping', "SEARCH_STATE['reason']"): {
        "sink": 'resp:route:ping', "class": 'closed_domain', "override_reason": None},
    ('ping', "SEARCH_STATE['sqlite']"): {
        "sink": 'resp:route:ping', "class": 'closed_domain', "override_reason": None},
    ('ping', 'amb:Dict'): {
        "sink": 'resp:route:ping', "class": 'closed_domain', "override_reason": None},
    ('ping', 'const:PROTOCOL_VERSION'): {
        "sink": 'resp:route:ping', "class": 'closed_domain', "override_reason": None},
    ('post_comment', 'call:_author_usage(conn, thread_id, author)'): {
        "sink": 'resp:tool:post_comment', "class": 'closed_domain', "override_reason": None},
    ('post_comment', 'call:_stale_receipt_note(conn, thread_id)'): {
        "sink": 'resp:tool:post_comment', "class": 'mask', "override_reason": None},
    ('post_comment', "call:conn.execute('INSERT INTO comments(thread_id, parent_id, author,"): {
        "sink": 'resp:tool:post_comment', "class": 'closed_domain', "override_reason": None},
    ('post_comment', 'const:POSTS_PER_DAY'): {
        "sink": 'resp:tool:post_comment', "class": 'closed_domain', "override_reason": None},
    ('post_comment', 'const:THREAD_CAP'): {
        "sink": 'resp:tool:post_comment', "class": 'closed_domain', "override_reason": None},
    ('post_comment', 'const:VALID_SEVERITIES'): {
        "sink": 'resp:tool:post_comment', "class": 'closed_domain', "override_reason": None},
    ('post_comment', "lit:''"): {
        "sink": 'resp:tool:post_comment', "class": 'closed_domain', "override_reason": None},
    ('post_comment', "lit:'Your discussion-floor duty is still open (no comment an"): {
        "sink": 'resp:tool:post_comment', "class": 'closed_domain', "override_reason": None},
    ('post_comment', 'lit:False'): {
        "sink": 'resp:tool:post_comment', "class": 'closed_domain', "override_reason": None},
    ('post_comment', 'name:budget'): {
        "sink": 'resp:tool:post_comment', "class": 'closed_domain', "override_reason": None},
    ('post_comment', 'param:author'): {
        "sink": 'resp:tool:post_comment', "class": 'closed_domain', "override_reason": None},
    ('post_comment', 'param:file'): {
        "sink": 'resp:tool:post_comment', "class": 'mask', "override_reason": None},
    ('post_comment', 'param:line'): {
        "sink": 'resp:tool:post_comment', "class": 'closed_domain', "override_reason": None},
    ('post_comment', 'param:severity'): {
        "sink": 'resp:tool:post_comment', "class": 'echo', "override_reason": None},  # ECHO_FACES 校验拒绝回显(#2393 §3②)
    ('post_comment', 'param:thread_id'): {
        "sink": 'resp:tool:post_comment', "class": 'closed_domain', "override_reason": None},
    ('recompute_thread', "call:' · '.join((f'{n} pass' for n in active))"): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('recompute_thread', "call:'; '.join(missing)"): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('recompute_thread', 'call:_ledger_block_key(missing)'): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('recompute_thread', "call:conn.execute('SELECT COALESCE(MAX(id),0) FROM comments WHERE thr"): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('recompute_thread', "comp:[n for n in active if verdicts.get(n) == 'object']"): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('recompute_thread', 'const:_SKIP_REASON'): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('recompute_thread', "crow['last_delivered_id']"): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('recompute_thread', 'name:n'): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('recompute_thread', 'name:silent'): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('recompute_thread', 'name:wm_why'): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('recompute_thread', "t['author']"): {
        "sink": 'resp:route:attention_probe', "class": 'closed_domain', "override_reason": None},
    ('register_meta', 'param:author'): {
        "sink": 'resp:tool:register_meta', "class": 'closed_domain', "override_reason": None},
    ('register_meta', 'param:contract'): {
        "sink": 'resp:tool:register_meta', "class": 'closed_domain', "override_reason": None},
    ('register_meta', 'param:entry'): {
        "sink": 'resp:tool:register_meta', "class": 'mask', "override_reason": None},
    ('reply_comment', "call:_author_usage(conn, row['thread_id'], author)"): {
        "sink": 'resp:tool:reply_comment', "class": 'closed_domain', "override_reason": None},
    ('reply_comment', "call:_stale_receipt_note(conn, row['thread_id'])"): {
        "sink": 'resp:tool:reply_comment', "class": 'mask', "override_reason": None},
    ('reply_comment', "call:conn.execute('INSERT INTO comments(thread_id, parent_id, author,"): {
        "sink": 'resp:tool:reply_comment', "class": 'closed_domain', "override_reason": None},
    ('reply_comment', 'const:POSTS_PER_DAY'): {
        "sink": 'resp:tool:reply_comment', "class": 'closed_domain', "override_reason": None},
    ('reply_comment', 'const:THREAD_CAP'): {
        "sink": 'resp:tool:reply_comment', "class": 'closed_domain', "override_reason": None},
    ('reply_comment', "lit:'Your discussion-floor duty is still open (no comment an"): {
        "sink": 'resp:tool:reply_comment', "class": 'closed_domain', "override_reason": None},
    ('reply_comment', 'lit:False'): {
        "sink": 'resp:tool:reply_comment', "class": 'closed_domain', "override_reason": None},
    ('reply_comment', 'name:budget'): {
        "sink": 'resp:tool:reply_comment', "class": 'closed_domain', "override_reason": None},
    ('reply_comment', 'param:author'): {
        "sink": 'resp:tool:reply_comment', "class": 'closed_domain', "override_reason": None},
    ('reply_comment', 'param:comment_id'): {
        "sink": 'resp:tool:reply_comment', "class": 'closed_domain', "override_reason": None},
    ('reply_comment', "row['thread_id']"): {
        "sink": 'resp:tool:reply_comment', "class": 'closed_domain', "override_reason": None},
    ('request_form_b', 'call:_now_iso()'): {
        "sink": 'resp:tool:request_form_b', "class": 'closed_domain', "override_reason": None},
    ('request_form_b', 'call:_secrets.token_hex(16)'): {
        "sink": 'resp:tool:request_form_b', "class": 'closed_domain', "override_reason": None},
    ('request_form_b', 'call:hashlib.sha256(nonce.encode()).hexdigest()'): {
        "sink": 'none', "class": 'issuance', "override_reason": 'E 穷尽落点(#2382 §3/#2380 §2③)'},  # form_b 唯一交付面 log∪file(#2379 §2.3)；绝不进返回体
    ('request_form_b', 'comp:[n.strip() for n in names]'): {
        "sink": 'resp:tool:request_form_b', "class": 'echo', "override_reason": None},  # ECHO_FACES 校验拒绝回显(#2393 §3②)
    ('request_form_b', 'const:FORM_B_TTL_S'): {
        "sink": 'resp:tool:request_form_b', "class": 'closed_domain', "override_reason": None},
    ('request_form_b', 'param:author'): {
        "sink": 'resp:tool:request_form_b', "class": 'closed_domain', "override_reason": None},
    ('request_form_b', 'param:thread_id'): {
        "sink": 'resp:tool:request_form_b', "class": 'closed_domain', "override_reason": None},
    ('request_form_b', "t['author']"): {
        "sink": 'resp:tool:request_form_b', "class": 'closed_domain', "override_reason": None},
    ('request_form_b', "t['status']"): {
        "sink": 'resp:tool:request_form_b', "class": 'closed_domain', "override_reason": None},
    ('reset_token', "call:','.join(dropped)"): {
        "sink": 'resp:tool:reset_token', "class": 'closed_domain', "override_reason": None},
    ('reset_token', "call:','.join(sorted(preserved))"): {
        "sink": 'resp:tool:reset_token', "class": 'closed_domain', "override_reason": None},
    ('reset_token', "call:bool(meta.get('token_hash'))"): {
        "sink": 'resp:tool:reset_token', "class": 'closed_domain', "override_reason": None},
    ('reset_token', 'name:meta_state'): {
        "sink": 'resp:tool:reset_token', "class": 'closed_domain', "override_reason": None},
    ('reset_token', 'param:author'): {
        "sink": 'resp:tool:reset_token', "class": 'closed_domain', "override_reason": None},
    ('search_comments', "SEARCH_STATE['reason']"): {
        "sink": 'resp:tool:search_comments', "class": 'closed_domain', "override_reason": None},
    ('search_comments', 'amb:op'): {
        "sink": 'resp:tool:search_comments', "class": 'closed_domain', "override_reason": None},
    ('search_comments', 'call:_like_literal(t)'): {
        "sink": 'resp:tool:search_comments', "class": 'closed_domain', "override_reason": None},
    ('search_comments', "call:bool(author and r['author'] == author)"): {
        "sink": 'resp:tool:search_comments', "class": 'closed_domain', "override_reason": None},
    ('search_comments', "comp:[{'comment_id': n['id'], 'author': n['author'], 'snippet"): {
        "sink": 'resp:tool:search_comments', "class": 'mask', "override_reason": None},
    ('search_comments', 'const:SEARCH_MAX_LIMIT'): {
        "sink": 'resp:tool:search_comments', "class": 'closed_domain', "override_reason": None},
    ('search_comments', 'const:SEARCH_MAX_TERMS'): {
        "sink": 'resp:tool:search_comments', "class": 'closed_domain', "override_reason": None},
    ('search_comments', 'const:SEARCH_ROUTE_FTS'): {
        "sink": 'resp:tool:search_comments', "class": 'closed_domain', "override_reason": None},
    ('search_comments', "n['author']"): {
        "sink": 'resp:tool:search_comments', "class": 'closed_domain', "override_reason": None},
    ('search_comments', "n['body']"): {
        "sink": 'resp:tool:search_comments', "class": 'mask', "override_reason": None},
    ('search_comments', "n['id']"): {
        "sink": 'resp:tool:search_comments', "class": 'closed_domain', "override_reason": None},
    ('search_comments', 'name:e'): {
        "sink": 'resp:tool:search_comments', "class": 'closed_domain', "override_reason": None},
    ('search_comments', 'param:status'): {
        "sink": 'resp:tool:search_comments', "class": 'closed_domain', "override_reason": None},
    ('search_comments', "r['author']"): {
        "sink": 'resp:tool:search_comments', "class": 'closed_domain', "override_reason": None},
    ('search_comments', "r['body']"): {
        "sink": 'resp:tool:search_comments', "class": 'mask', "override_reason": None},
    ('search_comments', "r['created_at']"): {
        "sink": 'resp:tool:search_comments', "class": 'closed_domain', "override_reason": None},
    ('search_comments', "r['id']"): {
        "sink": 'resp:tool:search_comments', "class": 'closed_domain', "override_reason": None},
    ('search_comments', "r['rank']"): {
        "sink": 'resp:tool:search_comments', "class": 'closed_domain', "override_reason": None},
    ('search_comments', "r['th_status']"): {
        "sink": 'resp:tool:search_comments', "class": 'closed_domain', "override_reason": None},
    ('search_comments', "r['th_title']"): {
        "sink": 'resp:tool:search_comments', "class": 'mask', "override_reason": None},
    ('search_comments', "r['thread_id']"): {
        "sink": 'resp:tool:search_comments', "class": 'closed_domain', "override_reason": None},
    ('search_comments.payload', 'amb:Dict'): {
        "sink": 'resp:tool:search_comments', "class": 'closed_domain', "override_reason": None},
    ('search_comments.payload', 'param:complete'): {
        "sink": 'resp:tool:search_comments', "class": 'closed_domain', "override_reason": None},
    ('search_comments.payload', 'param:mode_route'): {
        "sink": 'resp:tool:search_comments', "class": 'closed_domain', "override_reason": None},
    ('search_comments.payload', 'param:results'): {
        "sink": 'resp:tool:search_comments', "class": 'closed_domain', "override_reason": None},
    ('search_comments.payload', 'param:status'): {
        "sink": 'resp:tool:search_comments', "class": 'closed_domain', "override_reason": None},
    ('search_comments.payload', 'param:syntax'): {
        "sink": 'resp:tool:search_comments', "class": 'closed_domain', "override_reason": None},
    ('search_comments.payload', 'param:total'): {
        "sink": 'resp:tool:search_comments', "class": 'closed_domain', "override_reason": None},
    ('search_comments.payload', 'param:truncated'): {
        "sink": 'resp:tool:search_comments', "class": 'closed_domain', "override_reason": None},
    ('set_quorum', 'name:n'): {
        "sink": 'resp:tool:set_quorum', "class": 'closed_domain', "override_reason": None},
    ('set_quorum', 'param:add'): {
        "sink": 'resp:tool:set_quorum', "class": 'echo', "override_reason": None},  # ECHO_FACES 校验拒绝回显(#2393 §3②)
    ('set_quorum', 'param:thread_id'): {
        "sink": 'resp:tool:set_quorum', "class": 'closed_domain', "override_reason": None},
    ('set_quorum', 'param:wake_infra'): {
        "sink": 'resp:tool:set_quorum', "class": 'closed_domain', "override_reason": None},
    ('set_quorum', "t['author']"): {
        "sink": 'resp:tool:set_quorum', "class": 'closed_domain', "override_reason": None},
    ('set_quorum', "t['quorum']"): {
        "sink": 'resp:tool:set_quorum', "class": 'closed_domain', "override_reason": None},
    ('set_quorum', "t['status']"): {
        "sink": 'resp:tool:set_quorum', "class": 'closed_domain', "override_reason": None},
    ('set_status', 'call:bool(human_override)'): {
        "sink": 'resp:tool:set_status', "class": 'closed_domain', "override_reason": None},
    ('set_status', "comp:[n for n in names if have.get(n) != 'pass']"): {
        "sink": 'resp:tool:set_status', "class": 'closed_domain', "override_reason": None},
    ('set_status', 'const:VALID_STATUSES'): {
        "sink": 'resp:tool:set_status', "class": 'closed_domain', "override_reason": None},
    ('set_status', 'name:_silent'): {
        "sink": 'resp:tool:set_status', "class": 'closed_domain', "override_reason": None},
    ('set_status', 'name:silent'): {
        "sink": 'resp:tool:set_status', "class": 'closed_domain', "override_reason": None},
    ('set_status', 'param:author'): {
        "sink": 'resp:tool:set_status', "class": 'closed_domain', "override_reason": None},
    ('set_status', 'param:comment_id'): {
        "sink": 'resp:tool:set_status', "class": 'closed_domain', "override_reason": None},
    ('set_status', 'param:status'): {
        "sink": 'resp:tool:set_status', "class": 'echo', "override_reason": None},  # ECHO_FACES 校验拒绝回显(#2393 §3②)
    ('set_status', 'param:thread_id'): {
        "sink": 'resp:tool:set_status', "class": 'closed_domain', "override_reason": None},
    ('set_status', "t['revision']"): {
        "sink": 'resp:tool:set_status', "class": 'closed_domain', "override_reason": None},
    ('set_verdict', 'amb:Compare'): {
        "sink": 'resp:tool:set_verdict', "class": 'closed_domain', "override_reason": None},
    ('set_verdict', "call:_l15_waiting(conn, thread_id, t['author'], quorum, author)"): {
        "sink": 'resp:tool:set_verdict', "class": 'closed_domain', "override_reason": None},
    ('set_verdict', "call:json.loads(t['quorum'])"): {
        "sink": 'resp:tool:set_verdict', "class": 'closed_domain', "override_reason": None},
    ('set_verdict', 'param:author'): {
        "sink": 'resp:tool:set_verdict', "class": 'closed_domain', "override_reason": None},
    ('set_verdict', 'param:thread_id'): {
        "sink": 'resp:tool:set_verdict', "class": 'closed_domain', "override_reason": None},
    ('set_verdict', 'param:verdict'): {
        "sink": 'resp:tool:set_verdict', "class": 'closed_domain', "override_reason": None},
    ('set_verdict', "t['per_author_budget']"): {
        "sink": 'resp:tool:set_verdict', "class": 'closed_domain', "override_reason": None},
    ('set_verdict', "t['revision']"): {
        "sink": 'resp:tool:set_verdict', "class": 'closed_domain', "override_reason": None},
}
