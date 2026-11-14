#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_attention_contract.py — attention_contract 唯一权威模块回归
（board #44 三件定稿，执行计划 4.3③ 落库验收）。

跑法：python configs/test_attention_contract.py（纯标准库）

覆盖：
  A  派生正确性：REASON_CHAIN 6 枚全序（与 dsh #882 一手复核逐字钉死）、
     H2R 三对、REASON_ONLY={verdict_stale}
  B  归一器：字段面混装链归一 == REASON_CHAIN（M1/M3 直通容忍——改对不红）
  C  权威行结构判定：healthy 绿；变异矩阵抽格——M9 等价副本容错绿、
     M10/M19 两侧加料红、M11 换序红、M13/M15 极性红、M6 叙事句要素红
  D  check_watcher_texts 注入口：healthy 绿 / 权威行删除红 / 极性篡改红
  E  旧措辞清零验收：全仓（git 跟踪面、排除本文件自计）恰 1 处（dsh #1048 §二② 的验收判据；唯一预期承载 = docs/DEBT.md 验收行）
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import unittest

HERE = os_dir = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import attention_contract as ac  # noqa: E402

REPO = HERE.parent


class TestDerivation(unittest.TestCase):
    """A：从 server.py AST 派生的正确性（不 import server，#1048 ①）。"""

    def test_reason_chain_full_order(self):
        self.assertEqual(
            ac.REASON_CHAIN,
            ("token_invalid", "awaiting_verdict", "verdict_stale",
             "awaiting_floor_comment", "mentioned", "new_comments"),
            "6 枚全序，与 dsh #882 一手复核逐字相同")

    def test_h2r_three_pairs(self):
        self.assertEqual(len(ac.H2R), 3)
        self.assertEqual(ac.H2R, {"awaiting_my_verdict": "awaiting_verdict",
                                  "mentions_me": "mentioned",
                                  "my_comment_due": "awaiting_floor_comment"})
        self.assertEqual(ac.normalize(["awaiting_my_verdict", "mentions_me",
                                       "my_comment_due"]),
                         ("awaiting_verdict", "mentioned", "awaiting_floor_comment"))

    def test_reason_only(self):
        self.assertEqual(set(ac.REASON_ONLY), {"verdict_stale"})
        self.assertNotIn("verdict_stale", ac.H2R.values())
        self.assertNotIn("verdict_stale", ac.H2R.keys())


class TestNormalize(unittest.TestCase):
    """B：字段面混装链（载体今天的形态）归一后 == REASON_CHAIN——M1/M3
    「把命名改对」不再红（直通容忍，claude #884 §1）。"""

    MIXED = ("token_invalid", "awaiting_my_verdict", "verdict_stale",
             "awaiting_floor_comment", "mentions_me", "new_comments")

    def test_mixed_carrier_normalizes_to_chain(self):
        self.assertEqual(ac.normalize(self.MIXED), ac.REASON_CHAIN)

    def test_pure_reason_chain_passes_through(self):
        self.assertEqual(ac.normalize(ac.REASON_CHAIN), ac.REASON_CHAIN)


class TestAuthorityCheck(unittest.TestCase):
    """C：权威行结构判定的变异矩阵抽格（基线=仓库 claude-polling-prompt.txt）。"""

    @classmethod
    def setUpClass(cls):
        cls.healthy = (HERE / "claude-polling-prompt.txt").read_text(encoding="utf-8")

    def test_healthy_passes(self):
        self.assertEqual(ac.authority_check(self.healthy), [])

    def _variant(self, old, new):
        self.assertIn(old, self.healthy, f"变异锚不在位: {old[:40]}")
        return self.healthy.replace(old, new, 1)

    def test_m10_tail_append_token_red(self):
        v = self._variant("mentions_me > new_comments(v2.10",
                          "mentions_me > new_comments > extra_token(v2.10")
        self.assertTrue(ac.authority_check(v), "M10：链尾追加第 7 枚必须红")

    def test_m19_head_prepend_token_red(self):
        v = self._variant("优先级 token_invalid >",
                          "优先级 prepended_token > token_invalid >")
        self.assertTrue(ac.authority_check(v), "M19：链头前插必须红（等长判据）")

    def test_m11_swap_tail_red(self):
        v = self._variant("mentions_me > new_comments(v2.10",
                          "new_comments > mentions_me(v2.10")
        self.assertTrue(ac.authority_check(v), "M11：末两位交换必须红")

    def test_m9_equivalent_copy_tolerated(self):
        # M9 定稿改判：等长元组相等对空格差异容错（归一后相等即绿）；
        # 「第二权威」由「恰 1 行」挡——同格两行才红。
        v = self._variant("token_invalid > awaiting_my_verdict",
                          "token_invalid>  awaiting_my_verdict")
        self.assertEqual(ac.authority_check(v), [], "M9：空格差异容错（归一化）")

    def test_m13_polarity_flip_red(self):
        v = self._variant("本行是唯一权威定义",
                          "以下顺序禁止(仅举例,非权威):本行定义")
        self.assertTrue(ac.authority_check(v), "M13：权威极性篡改必须红")

    def test_m15_authority_downgraded_red(self):
        v = self._variant("本行是唯一权威定义", "本行仅为举例,可随时删除")
        self.assertTrue(ac.authority_check(v), "M15：权威降级必须红")

    def test_m6_whole_paren_note_removed_narrative_red(self):
        # M6（删整段括注）——V1.1.12 D 终稿括注含内层半角括号
        # 「my_comment_due(本行按 reason 名写)」（board #2244 预警）：
        # find(")") 索引法会切半段（极性句幸存=假绿），改平衡括号删除。
        v = self.healthy
        i = v.find("(v2.10")
        depth, j = 0, i
        while j < len(v):
            if v[j] == "(":
                depth += 1
            elif v[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        v = v[:i] + v[j + 1:]
        # 板 #883 M6 一手：括注里含极性句（唯一权威定义），整段删除后极性判据红
        self.assertTrue(ac.authority_check(v), "M6：删括注（含极性句）必须红")


class TestWatcherIntegration(unittest.TestCase):
    """D：check_watcher_texts 注入口三格（healthy / 删权威行 / 极性篡改）。"""

    HEALTHY = (HERE / "claude-polling-prompt.txt").read_text(encoding="utf-8")

    def _run(self, texts):
        import check_hooks_shim  # noqa: F401  占位防误用（实际走下式）
        raise AssertionError("unreachable")

    def _checker(self, texts):
        import importlib.util
        s = importlib.util.spec_from_file_location(
            "chk", str(HERE / "check-hooks.py"))
        m = importlib.util.module_from_spec(s)
        s.loader.exec_module(m)
        return m.check_watcher_texts(texts=texts)

    def test_healthy_zero_bad(self):
        texts = {n: self.HEALTHY for n in (
            "task-template-pi.txt", "claude-polling-prompt.txt", "dsh-watcher.sh")}
        texts["task-template-pi.txt"] = "token_invalid my_comment_due blocking=true"
        texts["dsh-watcher.sh"] = "token_invalid my_comment_due blocking=true"
        self.assertEqual(self._checker(texts), 0)

    def test_m6_removed_paren_note_red(self):
        # Bugbot 修正：补齐三载体——缺键本身计 bad 会让本格空泛绿
        base = {"task-template-pi.txt": "token_invalid my_comment_due blocking=true",
                "dsh-watcher.sh": "token_invalid my_comment_due blocking=true"}
        v = self.HEALTHY
        i = v.find("(v2.10")
        j = v.find(")", i)
        texts = {**base, "claude-polling-prompt.txt": v[:i] + v[j + 1:]}
        self.assertNotEqual(self._checker(texts), 0, "M6：括注/叙事句删除必须红")
        texts_ok = {**base, "claude-polling-prompt.txt": self.HEALTHY}
        self.assertEqual(self._checker(texts_ok), 0, "对照:同前提 healthy 必须绿")

    def test_m15_polarity_downgrade_red(self):
        base = {"task-template-pi.txt": "token_invalid my_comment_due blocking=true",
                "dsh-watcher.sh": "token_invalid my_comment_due blocking=true"}
        texts = {**base, "claude-polling-prompt.txt": self.HEALTHY.replace(
            "本行是唯一权威定义", "本行仅为举例,可随时删除")}
        self.assertNotEqual(self._checker(texts), 0, "M15：极性降级必须红")
        texts_ok = {**base, "claude-polling-prompt.txt": self.HEALTHY}
        self.assertEqual(self._checker(texts_ok), 0, "对照:同前提 healthy 必须绿")


class TestConfigsFaceZero(unittest.TestCase):
    """E：4.3② 验收终版（板 #2234 §6 / #2235 §3 / #2239 §3 定稿）——
    **configs 面 = 0**：搜索串在测试内字面自拼（"".join 拆字，防止串字面
    进本文件源码使判据首日红在测试自身——dsh #2239 §3 前置条件）；判定走
    git grep -F（dsh #2237 §4 建议），returncode 三分支（0=命中红、1=无命中绿、
    其余=git 不可用时显式 fail 而非静默 0——dsh #2235 §3）；全仓口径退役
    （DEBT 债务描述性引用允许保留，pi #2235 §3）。"""

    def test_configs_face_zero(self):
        needle = "".join(("逐", "字", "一致"))
        r = subprocess.run(
            ["git", "-C", str(REPO), "grep", "-F", "-c", needle,
             "--", "configs/"],
            capture_output=True, text=True, timeout=60)
        if r.returncode not in (0, 1):
            self.fail(f"git grep 不可用（rc={r.returncode}）: {r.stderr[:160]}")
        self.assertEqual(r.returncode, 1,
                         f"configs 面命中旧措辞（应清零）\n{r.stdout[:400]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
