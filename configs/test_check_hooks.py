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

    def test_n6_wrong_event_wiring_is_bad(self):
        # GPT round-4 P1 负向测试：三脚本都在、都能独立运行，但事件接错
        # （quorum/automation 接到 Stop、plain_speak 接到 PreToolUse）——
        # 检查器必须报非零；「脚本存在」绝不等于「会在正确时机触发」
        shape = _claude_shape(with_gates=True)
        hooks = {"Stop": [], "PreToolUse": []}
        for entry in shape["hooks"]["Stop"]:
            hooks["PreToolUse"].append(entry)          # plain_speak → 错接 PreToolUse
        for entry in shape["hooks"]["PreToolUse"]:
            hooks["Stop"].append(entry)                # 两道 PreToolUse 闸 → 错接 Stop
        self._write({"hooks": hooks})
        rc, out, _ = self._run()
        self.assertEqual(rc, 1, out[-600:])
        self.assertEqual(out.count("接线不正确"), 3, out[-600:])
        self.assertIn("应接在 PreToolUse", out)
        self.assertIn("应接在 Stop", out)

    def test_n7_matcher_too_narrow_is_bad(self):
        # GPT round-4 P1 另一半：事件对、但 matcher 覆盖不了管辖工具
        # （quorum 的 matcher 只写 Edit|Write，治理工具漏接）——必须 BAD
        shape = _claude_shape(with_gates=True)
        shape["hooks"]["PreToolUse"][0]["matcher"] = "Edit|Write"
        self._write(shape)
        rc, out, _ = self._run()
        self.assertEqual(rc, 1, out[-600:])
        self.assertIn("接线不正确", out)
        self.assertIn("quorum_guard_hook.py", out)
        self.assertIn("mcp__review-board__create_thread", out)

    def test_n8_invalid_regex_with_all_tools_is_bad(self):
        # GPT round-5 P1-1 负向：matcher 是非法正则（未闭合括号）但含全部
        # 工具名子串——禁止退化成子串判断假绿，必须 BAD
        shape = _claude_shape(with_gates=True)
        shape["hooks"]["PreToolUse"][0]["matcher"] = (
            "mcp__review-board__create_thread|"
            "mcp__review-board__set_quorum|"
            "mcp__review-board__set_verdict(")      # 未闭合括号 → 非法正则
        self._write(shape)
        rc, out, _ = self._run()
        self.assertEqual(rc, 1, out[-600:])
        self.assertIn("接线不正确", out)
        self.assertIn("quorum_guard_hook.py", out)

    def test_n9_matcher_wrong_type_is_bad_not_crash(self):
        # GPT round-5 P1-1 负向：matcher 类型错误（对象）→ 明确 BAD，rc=1，
        # 检查器不得异常退出
        shape = _claude_shape(with_gates=True)
        shape["hooks"]["PreToolUse"][0]["matcher"] = {"bad": True}
        self._write(shape)
        rc, out, _ = self._run()
        self.assertEqual(rc, 1, out[-600:])
        self.assertIn("接线不正确", out)

    def test_n10_hook_type_not_command_is_bad(self):
        # GPT round-6 P1 负向：事件/matcher/command 全对，但 hook type 不是
        # "command"（宿主还有 prompt/agent/http 类型）——检查器必须判 BAD；
        # type 缺失同样走 None != "command" 判 BAD（同一条代码路径）
        shape = _claude_shape(with_gates=True)
        shape["hooks"]["PreToolUse"][0]["hooks"][0]["type"] = "prompt"
        self._write(shape)
        rc, out, _ = self._run()
        self.assertEqual(rc, 1, out[-600:])
        self.assertIn("接线不正确", out)
        self.assertIn("type=command", out)
        self.assertIn("quorum_guard_hook.py", out)

    def test_p3_prompt_and_hook_default_marker_paths_match(self):
        # GPT round-6 P2：轮询提示词的打卡缺省路径必须与 quorum_guard 的
        # 缺省一致（env 未生效时两边落同一文件，F6 门不空转）+ 写前建父目录
        prompt = open(os.path.join(HERE, "claude-polling-prompt.txt"),
                      encoding="utf-8").read()
        hook_src = open(os.path.join(HERE, "quorum_guard_hook.py"),
                        encoding="utf-8").read()
        self.assertIn("${RB_POLL_MARKER:-$HOME/.zcode/rb-zcode-poll-heartbeat}",
                      prompt, "提示词缺省路径必须与 hook 默认同文件")
        self.assertIn('os.path.expanduser("~/.zcode/rb-zcode-poll-heartbeat")',
                      hook_src, "测试前提:hook 默认路径在位(漂移时先对齐此断言)")
        self.assertIn("mkdir -p", prompt, "打卡写入前必须先建父目录")


if __name__ == "__main__":
    unittest.main(verbosity=2)
