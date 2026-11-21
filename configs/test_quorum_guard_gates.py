#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_quorum_guard_gates.py — quorum_guard_hook 闸门表解析单测（DEBT #22 修法①）。

覆盖 resolve_watcher_gates 的三通道解析链（内建默认 < 磁盘配置文件 < env JSON）
与全部降级面：文件缺失 / 损坏 JSON / 非 JSON 对象 / env JSON 损坏——一律退内建
默认，绝不因配置面问题阻断板操作。

测试经 importlib 直载钩子模块（有 __main__ 守卫，import 无副作用）；
resolve_watcher_gates 支持显式传参，测试不依赖真实 env 与真实 home。
"""
import importlib.util
import json
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "quorum_guard_hook.py")

_spec = importlib.util.spec_from_file_location("quorum_guard_hook", HOOK)
qg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qg)

DEFAULT_MEMBERS = ("claude", "dsh", "pi", "zcode")


class ResolveWatcherGatesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qg-gates-")

    def _write(self, name, content):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    # ---------- 内建默认表 ----------
    def test_defaults_cover_four_members_under_gate_dir(self):
        gates = qg.resolve_watcher_gates(gate_dir=self.tmp, gates_file="", gates_json="{}")
        for n in DEFAULT_MEMBERS:
            self.assertEqual(gates[n], os.path.join(self.tmp, f"rb-{n}-watcher.fails"))
        self.assertEqual(len(gates), 4)

    # ---------- 磁盘配置文件 ----------
    def test_disk_file_overrides_single_member(self):
        f = self._write("gates.json", json.dumps({"pi": "/custom/pi-kit.fails"}))
        gates = qg.resolve_watcher_gates(gate_dir=self.tmp, gates_file=f, gates_json="{}")
        self.assertEqual(gates["pi"], "/custom/pi-kit.fails")  # 覆盖生效
        self.assertEqual(gates["dsh"], os.path.join(self.tmp, "rb-dsh-watcher.fails"))  # 其余保持默认
        self.assertEqual(gates["zcode"], os.path.join(self.tmp, "rb-zcode-watcher.fails"))

    def test_disk_file_can_add_member(self):
        f = self._write("gates.json", json.dumps({"grok": "/w/grok.fails"}))
        gates = qg.resolve_watcher_gates(gate_dir=self.tmp, gates_file=f, gates_json="{}")
        self.assertEqual(gates["grok"], "/w/grok.fails")
        self.assertEqual(len(gates), 5)

    def test_missing_file_falls_back_to_defaults(self):
        gates = qg.resolve_watcher_gates(gate_dir=self.tmp,
                                         gates_file=os.path.join(self.tmp, "nope.json"), gates_json="{}")
        self.assertEqual(gates["pi"], os.path.join(self.tmp, "rb-pi-watcher.fails"))

    def test_corrupt_file_falls_back_to_defaults_no_throw(self):
        f = self._write("bad.json", "{not valid json")
        gates = qg.resolve_watcher_gates(gate_dir=self.tmp, gates_file=f, gates_json="{}")
        self.assertEqual(gates["pi"], os.path.join(self.tmp, "rb-pi-watcher.fails"))

    def test_broken_file_warns_on_stderr(self):
        # GPT 外审(PR#59 P2): 显式配置损坏 ≠ 未配置——退默认必须 stderr 可观测。
        import contextlib, io as _io
        f = self._write("bad.json", "{not valid json")
        buf = _io.StringIO()
        with contextlib.redirect_stderr(buf):
            qg.resolve_watcher_gates(gate_dir=self.tmp, gates_file=f, gates_json="{}")
        self.assertIn("WARNING: watcher gates file unreadable/broken", buf.getvalue())
        self.assertIn("explicit=True", buf.getvalue())

    def test_explicit_missing_file_warns(self):
        # GPT 外审(PR#59 P2): 显式指定的文件不存在=配置面问题 ⇒ 报警(非静默)。
        import contextlib, io as _io
        buf = _io.StringIO()
        with contextlib.redirect_stderr(buf):
            qg.resolve_watcher_gates(gate_dir=self.tmp,
                                     gates_file=os.path.join(self.tmp, "nope.json"),
                                     gates_json="{}")
        self.assertIn("explicitly configured watcher gates file missing", buf.getvalue())

    def test_unconfigured_default_stays_silent(self):
        # 仅"从未有过任何文件"(无显式、无 env、默认路径不存在)保持静默=真未配置。
        import contextlib, io as _io
        from unittest import mock
        buf = _io.StringIO()
        with mock.patch("os.path.expanduser", return_value=self.tmp):
            with contextlib.redirect_stderr(buf):
                saved = os.environ.pop("RB_WATCHER_GATES_FILE", None)
                try:
                    qg.resolve_watcher_gates(gate_dir=self.tmp, gates_json="{}")
                finally:
                    if saved is not None:
                        os.environ["RB_WATCHER_GATES_FILE"] = saved
        self.assertEqual(buf.getvalue(), "", f"stderr={buf.getvalue()!r}")

    def test_non_object_json_ignored(self):
        f = self._write("list.json", '["pi", "/x/pi.fails"]')
        gates = qg.resolve_watcher_gates(gate_dir=self.tmp, gates_file=f, gates_json="{}")
        self.assertEqual(gates["pi"], os.path.join(self.tmp, "rb-pi-watcher.fails"))

    def test_env_rb_watcher_gates_file_respected_when_param_none(self):
        f = self._write("env.json", json.dumps({"pi": "/via-env-file/pi.fails"}))
        saved = os.environ.get("RB_WATCHER_GATES_FILE")
        try:
            os.environ["RB_WATCHER_GATES_FILE"] = f
            gates = qg.resolve_watcher_gates(gate_dir=self.tmp, gates_json="{}")
            self.assertEqual(gates["pi"], "/via-env-file/pi.fails")
        finally:
            if saved is None:
                os.environ.pop("RB_WATCHER_GATES_FILE", None)
            else:
                os.environ["RB_WATCHER_GATES_FILE"] = saved

    # ---------- env JSON（最高优先） ----------
    def test_env_json_wins_over_disk_file(self):
        f = self._write("gates.json", json.dumps({"pi": "/disk/pi.fails"}))
        gates = qg.resolve_watcher_gates(gate_dir=self.tmp, gates_file=f,
                                         gates_json=json.dumps({"pi": "/env/pi.fails"}))
        self.assertEqual(gates["pi"], "/env/pi.fails")

    def test_corrupt_env_json_ignored_no_throw(self):
        gates = qg.resolve_watcher_gates(gate_dir=self.tmp, gates_file="",
                                         gates_json="{broken")
        self.assertEqual(gates["pi"], os.path.join(self.tmp, "rb-pi-watcher.fails"))

    def test_env_json_valid_but_non_object_ignored_no_throw(self):
        # Bugbot(PR#59 Medium): null/数组/标量是合法 JSON,过 ValueError 关后
        # .items() 会 AttributeError 炸 import——必须对象判型后退默认。
        for bad in ("null", "[1,2]", "5", '"str"', "true"):
            gates = qg.resolve_watcher_gates(gate_dir=self.tmp, gates_file="",
                                             gates_json=bad)
            self.assertEqual(gates["pi"], os.path.join(self.tmp, "rb-pi-watcher.fails"),
                             f"env_json={bad}")

    # ---------- 模块级装配冒烟 ----------
    def test_module_level_watcher_gates_is_dict_with_defaults(self):
        for n in DEFAULT_MEMBERS:
            self.assertIn(n, qg.WATCHER_GATES)


if __name__ == "__main__":
    unittest.main()
