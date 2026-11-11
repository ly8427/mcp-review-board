#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_check_hooks.py — check-hooks 检查器自身回归（GPT round-19 P2-1 收口）。

跑法：python configs/test_check_hooks.py

覆盖：
  N1 空 hooks 段           → FATAL rc=2（「没检查到」不得解释为「检查通过」）
  N2 缺 hooks 段           → FATAL rc=2
  N3 events 不是对象       → FATAL rc=2（形状不识别必须显式报错）
  N4 事件条目不是列表      → FATAL rc=2
  P1 claude 形状三闸全接线 → rc=0 全 ALIVE
  P2 缺一道闸              → rc=1 且 [BAD] 预期闸未接线
（三闸用例用真实脚本路径——冒烟会真跑三闸，兼作接线活性验证。）
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CHECKER = os.path.join(HERE, "check-hooks.py")
REPO = os.path.dirname(HERE)
PY = sys.executable

GATES = ("quorum_guard_hook.py", "automation_guard_hook.py", "plain_speak_gate.py")


def _hook(script, timeout=10):
    return {"type": "command",
            "command": f'"{PY}" "{os.path.join(HERE, script)}"',
            "timeout": timeout}


def _claude_shape(with_gates=True):
    hooks = {"PreToolUse": [], "Stop": []}
    if with_gates:
        hooks["PreToolUse"].append(
            {"matcher": "mcp__review-board__(create_thread|set_quorum|set_verdict)",
             "hooks": [_hook("quorum_guard_hook.py", 20)]})
        hooks["PreToolUse"].append(
            {"matcher": "CronCreate|CronUpdate|OffPeakCreate",
             "hooks": [_hook("automation_guard_hook.py", 8)]})
    hooks["Stop"].append({"hooks": [_hook("plain_speak_gate.py", 10)]})
    return {"hooks": hooks}


class TestCheckHooks(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.cfg = pathlib.Path(self._dir.name) / "settings.json"

    def tearDown(self):
        self._dir.cleanup()

    def _write(self, obj):
        self.cfg.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")

    def _run(self):
        r = subprocess.run([PY, CHECKER, "--config", str(self.cfg)],
                           capture_output=True, timeout=120)
        return (r.returncode, r.stdout.decode("utf-8", "replace"),
                r.stderr.decode("utf-8", "replace"))

    def test_n1_empty_hooks_is_fatal(self):
        self._write({"hooks": {}})
        rc, out, _ = self._run()
        self.assertEqual(rc, 2)
        self.assertIn("没有解析到任何钩子", out)

    def test_n2_missing_hooks_is_fatal(self):
        self._write({"env": {}})
        rc, out, _ = self._run()
        self.assertEqual(rc, 2)
        self.assertIn("hooks 段缺失", out)

    def test_n3_events_not_dict_is_fatal(self):
        self._write({"hooks": {"events": "broken"}})
        rc, out, _ = self._run()
        self.assertEqual(rc, 2)
        self.assertIn("hooks.events 不是对象", out)

    def test_n4_entry_not_list_is_fatal(self):
        self._write({"hooks": {"Stop": "not-a-list"}})
        rc, out, _ = self._run()
        self.assertEqual(rc, 2)
        self.assertIn("不是列表", out)

    def test_p1_claude_shape_three_gates_alive(self):
        self._write(_claude_shape(with_gates=True))
        rc, out, _ = self._run()
        self.assertEqual(rc, 0, out[-600:])
        self.assertIn("全部在岗", out)
        # 检查器把 command 截到 70 字符显示——脚本名断言改在配置本身上做
        cfg = json.loads(self.cfg.read_text(encoding="utf-8"))
        joined = json.dumps(cfg)
        for gate in GATES:
            self.assertIn(gate, joined)
        self.assertEqual(out.count("[OK ] ALIVE"), 3,
                         "三闸各一行 OK(本配置形状;不含 ZCode 回归行)")

    def test_n5_hooks_field_null_is_fatal(self):
        # GPT round-20 P2：hooks 缺失/null 不再静默当空列表——结构错误必须显式报
        self._write({"hooks": {"Stop": [{"matcher": None, "hooks": None}]}})
        rc, out, _ = self._run()
        self.assertEqual(rc, 2)
        self.assertIn("hooks 缺失或为空", out)

    def test_p2_missing_gate_is_bad(self):
        shape = _claude_shape(with_gates=True)
        # 拆掉 automation_guard 接线
        shape["hooks"]["PreToolUse"] = [
            e for e in shape["hooks"]["PreToolUse"]
            if "automation_guard_hook.py" not in json.dumps(e)]
        self._write(shape)
        rc, out, _ = self._run()
        self.assertEqual(rc, 1)
        self.assertIn("预期闸未接线", out)
        self.assertIn("automation_guard_hook.py", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
