# -*- coding: utf-8 -*-
"""attention_contract — attention 优先级链的唯一权威契约（board #44 三件定稿，
执行计划 4.3③）。

背景：check-hooks 的 _PRIORITY_CHAIN 曾是手抄字面（2026-10-24 dsh #882 变异
矩阵实证：错句全绿 M7/M8、改对一改就红 M1-M3、等价副本/两侧加料/极性篡改
全绿 M9/M10/M19/M13/M15），「守字面、不守对」。本模块把链的唯一权威改成
**从 server.py 运行时 AST 派生**（claude #884 §2 方案 (a)：不 import、
无副作用、server 改序即传染），并给消费侧提供归一器与结构判定。

定稿锚：#1048 §二③（三件）＋ #882 §2 ＋ #883 §3/§4 ＋ #884 §1/§2。
口径（#884 §1）：字段面 canonical＋reason 名直通容忍——今天载体文本零改动。
"""
from __future__ import annotations

import ast
import pathlib
import re

# header（轮询响应字段名）→ server reason 名 的改名映射（3 对，对称收录；
# 第 3 对今天在链中无处触发，收录以免特判——claude #884 §1）。
H2R = {
    "awaiting_my_verdict": "awaiting_verdict",
    "mentions_me": "mentioned",
    "my_comment_due": "awaiting_floor_comment",
}

# 无对应响应字段的 reason（叙事句断言须标注它——#1048 ②）
REASON_ONLY = ("verdict_stale",)

_SERVER = pathlib.Path(__file__).resolve().parent.parent / "server.py"


def derive_reason_chain(server_path=pathlib.Path(__file__).resolve().parent.parent / "server.py"):
    """从 server.py 的 `if token_fire:` / elif 链 AST 派生 reason 全序。

    不 import server（sys.modules 零污染，#1048 ①）；只认 `token_fire` 名
    测试的 If 节点，沿 orelse 嵌套收集各分支首个 reason 赋值字符串；idle
    在 else 分支、天然不在链内（#884 §2 (a)）。"""
    tree = ast.parse(pathlib.Path(server_path).read_text(encoding="utf-8"))
    chain: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.If) and isinstance(node.test, ast.Name)
                and node.test.id == "token_fire"):
            continue
        cur: ast.stmt | None = node
        while cur is not None:
            reason = None
            for st in ast.walk(cur if cur is node else cur):
                if not isinstance(st, ast.Assign):
                    continue
                for ti, tg in enumerate(st.targets):
                    got = None
                    if isinstance(tg, ast.Name) and tg.id == "reason":
                        got = st.value
                    elif isinstance(tg, ast.Tuple) and any(
                            isinstance(e, ast.Name) and e.id == "reason"
                            for e in tg.elts):
                        # reason, threads = "x", y 形态：取元组对应位
                        if isinstance(st.value, ast.Tuple) \
                                and len(st.value.elts) == len(tg.elts):
                            for ei, e in enumerate(tg.elts):
                                if isinstance(e, ast.Name) and e.id == "reason":
                                    got = st.value.elts[ei]
                    if isinstance(got, ast.Constant) and isinstance(got.value, str):
                        reason = got.value
                    if reason:
                        break
                if reason:
                    break
            if reason:
                chain.append(reason)
            orelse = cur.orelse
            cur = orelse[0] if len(orelse) == 1 and isinstance(orelse[0], ast.If) else None
        break
    if len(chain) != 6:
        # 板 #2236 §2 定稿①：arity fail-loud，诊断指向契约（import 面由
        # check-hooks 的 try/except 接成 fail-closed，总检查器不被带走）
        raise ValueError(
            f"attention 契约派生异常：链枚举数={len(chain)}（期望 6）——"
            "server.py 的 token_fire elif 链形状可能已变；本模块是优先级链唯一权威，"
            "先核对 server.py 的链再改本模块")
    return tuple(chain)


REASON_CHAIN = derive_reason_chain()


def normalize(tokens):
    """字段面 token 序 → reason 面（H2R 归一；reason 名直通容忍——M1/M3 自绿
    的定稿目标，claude #884 §1「今天文本零改动」）。"""
    return tuple(H2R.get(t, t) for t in tokens)


_CHAIN_SPLIT = re.compile(r"[（(;；。，]")


def parse_authority_line(text: str):
    """从载体文本抽「唯一权威」优先级行的 token 元组。

    判据（板 #883 §3 假红修正 + #1048 ③）：含「优先级 」的行恰 1 行（不按
    行首——真实行有列表序号"3. "）；链取「优先级 」之后、遇括号/分号/行尾
    截断，`>` 切分逐段 strip。返回 (行数, token 元组|None, 权威行本身|None)。"""
    rows = [ln for ln in text.splitlines() if "优先级 " in ln]
    tokens = None
    row = rows[0] if rows else None
    if row:
        seg = _CHAIN_SPLIT.split(row.split("优先级 ", 1)[1], 1)[0]
        toks = [x.strip() for x in seg.split(">")]
        if toks and all(toks):
            tokens = tuple(toks)
    return len(rows), tokens, row


def _chain_pattern():
    """归一链全文正则：REASON_CHAIN 每位的「reason 名 | 字段名」交替、`>` 连接
    （board #2235/#2236 定稿的牙——无标签副本 S1/括注后副本 S2 的猎物）。"""
    alts = []
    for tok in REASON_CHAIN:
        alts.append("|".join([tok] + [h for h, r in H2R.items() if r == tok]))
    return re.compile(r"\s*>\s*".join(f"(?:{a})" for a in alts))


def authority_check(text: str) -> list[str]:
    """「唯一权威」结构判定（#1048 ③ + 板 #2236 腿①）。返回违例清单（空=通过）：
    ①含「优先级 」的行恰 1 行；②该行含「唯一权威」极性标注（挡 M13/M15/M16
    ——权威声明被改成"仅举例/旧版/禁止"即红）；③行内链归一后与
    REASON_CHAIN 等长元组相等（挡 M9 等价副本/M11 换序，不再依赖逐字）；
    ④归一链全文出现次数恰 1（腿①：无标签副本 S1/括注后副本 S2 旧版逃逸，
    pi #2236 §1 真跑坐实后由本格接管）。"""
    bad = []
    n, tokens, row = parse_authority_line(text)
    if n != 1:
        bad.append(f"唯一权威行(含'优先级 '的行)恰 1 行, 实际 {n}")
        return bad
    assert row is not None
    if "唯一权威" not in row:
        bad.append("权威行缺「唯一权威」极性标注（不得改为仅举例/旧版/禁止）")
    if tokens is None:
        bad.append("权威行链不可解析")
    elif normalize(tokens) != REASON_CHAIN:
        bad.append(f"链归一后与 REASON_CHAIN 不等: {normalize(tokens)} != {REASON_CHAIN}")
    n_hits = len(_chain_pattern().findall(text))
    if n_hits != 1:
        bad.append(f"归一链全文出现次数恰 1, 实际 {n_hits}（无标签/括注后副本同属违规）")
    return bad


if __name__ == "__main__":
    print("REASON_CHAIN =", REASON_CHAIN)
    print("REASON_ONLY  =", REASON_ONLY)
    print("H2R          =", H2R)
