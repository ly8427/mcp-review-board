#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""repair-gate 回归测试套件（可重复运行）。

跑法：python test_repair_gate.py   （纯标准库，无需 WSL/看板/真实钩子环境）

覆盖 PR #8 两轮外部评审的翻转条件与本闸全部关键行为：
  F1  核销证据=派发锚点后**同一条结构化记录**内 工具名+参数 双匹配
      （锚点前旧调用 / 同名异参 / 名与参分行拼凑 均不得核销）
  F2  看板派发指令：参数仅以 base64 形态出现在命令行，恶意引号载荷不出现在原文
  F3  同请求重复登记：编号带序号后缀，生命周期互相独立
  V1.2/V1.3  短回执可处理；长正文与独立短回执并存时回执不丢失；
      两次无回执自动作废；超限报警（文件+标准错误）后放行

断言原则：看状态文件与副作用，不耦合提示文案（避免文案改版导致假失败）。
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GATE = os.path.join(HERE, "configs", "repair_gate.py")
PY = sys.executable


def _call(cid, name="CronDelete", input=None, arguments=None):
    """构造一条带编号的工具调用记录(真实 rollout 格式:toolCalls[].{id,name,input})。"""
    c = {"id": cid, "name": name}
    if arguments is not None:
        c["arguments"] = arguments
    else:
        c["input"] = input if input is not None else {"id": "42"}
    return json.dumps({"toolCalls": [c]}, ensure_ascii=False)


def _res(rid, content, is_error=None):
    """构造一条执行结果记录(真实 rollout 格式:{type:tool_result,tool_use_id,content})。"""
    d = {"type": "tool_result", "tool_use_id": rid, "content": content}
    if is_error is not None:
        d["is_error"] = is_error
    return json.dumps(d, ensure_ascii=False)

ORDER_LINE = '修复单:: CronDelete {"id":"42"}'
BOARD_ORDER = ('修复单:: create_thread {"title":"a\'; rm -rf /tmp/nope; `echo x` '
               '$(touch /tmp/rb-pwned)","context":"中文\\"引号\\"","n":3}')


class Harness(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.state = os.path.join(self._dir.name, "state.json")
        self.logp = os.path.join(self._dir.name, "log.txt")
        self.alarm = os.path.join(self._dir.name, "alarm.txt")
        self.rollout = os.path.join(self._dir.name, "rollout.jsonl")

    def tearDown(self):
        self._dir.cleanup()

    def _env(self):
        return dict(os.environ,
                    RB_REPAIR_STATE=self.state, RB_REPAIR_LOG=self.logp,
                    RB_REPAIR_ALARM=self.alarm, AG_ROLLOUT=self.rollout)

    def gate(self, text, extra=None, rollout=None, append=False):
        """跑一次钩子。rollout=[行,...] 写入/追加测试记录;返回 (rc, bounce|None, stderr)。"""
        if rollout is not None:
            with open(self.rollout, "a" if append else "w", encoding="utf-8") as f:
                for ln in rollout:
                    f.write(ln + "\n")
        payload = {"session_id": "unittest", "response": {"text": text}}
        if extra:
            payload.update(extra)
        p = subprocess.run([PY, GATE], input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                           capture_output=True, env=self._env(), timeout=60)
        out = p.stdout.decode("utf-8", "replace").strip()
        try:
            bounce = json.loads(out) if out else None
        except ValueError:
            bounce = None
        return p.returncode, bounce, p.stderr.decode("utf-8", "replace")

    def gate_raw(self, raw: bytes):
        p = subprocess.run([PY, GATE], input=raw, capture_output=True, env=self._env(), timeout=60)
        return p.returncode, p.stdout.decode("utf-8", "replace").strip()

    def st(self):
        with open(self.state, encoding="utf-8") as f:
            return json.load(f)

    def first_id(self):
        return [k for k in self.st() if not k.startswith("_")][0]   # V2.2:忽略 _scan 等内部键

    # ---------- 登记与防误触发 ----------

    def test_codeblock_quote_not_order(self):
        rc, b, _ = self.gate("讨论格式说明文字超过三十个字符长度要求补足内容。\n"
                             "```\n" + ORDER_LINE + "\n```\n仅引用示例。")
        self.assertEqual(rc, 0)
        self.assertIsNone(b)
        self.assertEqual(self.st(), {})

    def test_bare_order_registers_unconfirmed(self):
        rc, b, _ = self.gate("开单说明文字超过三十个字符长度要求补足内容。\n"
                             + ORDER_LINE + "\n继续说明。")
        self.assertEqual(rc, 0)
        self.assertIsNotNone(b)
        v = list(self.st().values())[0]
        self.assertFalse(v["confirmed"])

    def test_non_whitelist_tool_rejected(self):
        rc, b, _ = self.gate("开单说明文字超过三十个字符长度要求补足内容。\n"
                             '修复单:: Bash {"command":"ls"}\n继续说明。')
        self.assertIsNone(b)
        self.assertEqual(self.st(), {})

    def test_duplicate_order_lines_register_once(self):
        text = ("开单说明文字超过三十个字符长度要求补足内容。\n"
                + ORDER_LINE + "\n" + ORDER_LINE + "\n继续说明。")
        self.gate(text)
        self.assertEqual(len(self.st()), 1)

    # ---------- 回执（含短回执与并存边界） ----------

    def _register(self, line=ORDER_LINE):
        rc, b, _ = self.gate("开单说明文字超过三十个字符长度要求补足内容。\n"
                             + line + "\n继续说明补足长度。")
        return self.first_id()

    def test_short_receipt_confirms_and_dispatches(self):
        h = self._register()
        rc, b, _ = self.gate(f"执行 {h}")
        v = self.st()[h]
        self.assertTrue(v["confirmed"])
        self.assertTrue(v["dispatched"])

    def test_long_text_plus_separate_short_receipt(self):
        """V1.2/V1.3 边界：长正文与独立短回执并存，回执不得丢失。"""
        h = self._register()
        long_text = ("这轮汇报的正文很长,包含大量与修复单无关的常规内容,"
                     "长度远超三十个字符,用于验证提取逻辑不会因为选中长文"
                     "而丢掉另一个字段里的短回执。")
        rc, b, _ = self.gate(long_text, extra={"last_message": {"text": f"执行 {h}"}})
        v = self.st()[h]
        self.assertTrue(v["confirmed"])
        self.assertTrue(v["dispatched"])

    def test_short_false_report_voids(self):
        h = self._register()
        rc, b, _ = self.gate(f"误报 {h}")
        v = self.st()[h]
        self.assertTrue(v["void"])
        self.assertTrue(v["done"])

    def test_stale_two_nags_auto_void(self):
        self._register()
        rc, b, _ = self.gate("好。")   # 第 1 次催办(计数 1→2)
        self.assertIsNotNone(b)
        rc, b, _ = self.gate("好。")   # 两次无回执 → 自动作废
        self.assertIsNone(b)
        v = list(self.st().values())[0]
        self.assertTrue(v["void"])

    # ---------- F1+F4+F5+F7 核销：同一条调用记录 + 配对的成功结果 ----------

    OK_CONTENT = json.dumps({"deleted": True})
    CALL_OK = [_call("call_ok1"), _res("call_ok1", OK_CONTENT)]
    CALL_STR_ARGS = [_call("call_s1", arguments=json.dumps({"id": "42"})),
                     _res("call_s1", OK_CONTENT)]
    CALL_ERR = [_call("call_e1"), _res("call_e1", "permission denied", is_error=True)]
    CALL_NO_RESULT = [_call("call_n1")]
    # F7 翻转条件夹具:真实字段为否/字段缺失/内容不是 JSON/嵌套文本里出现成功字样
    CALL_FALSE = [_call("call_b1"), _res("call_b1", json.dumps({"deleted": False}))]
    CALL_NESTED_TRAP = [_call("call_t1"),
                        _res("call_t1", json.dumps({"deleted": False,
                                                     "debug": 'previous result: "deleted": true'}))]
    CALL_MISSING_FIELD = [_call("call_m1"), _res("call_m1", json.dumps({"ok": 1}))]
    CALL_INVALID_JSON = [_call("call_i1"), _res("call_i1", "not json at all")]

    def _dispatched(self, initial_rollout):
        h = self._register()
        self.gate("开单说明文字超过三十个字符长度要求补足内容,先写入初始记录。",
                  rollout=initial_rollout)
        rc, b, _ = self.gate(f"执行 {h}")
        v = self.st()[h]
        self.assertTrue(v["dispatched"], "应已派发并记录锚点")
        return h

    def _check(self, h, rollout):
        rc, b, _ = self.gate("汇报内容超过三十个字符,说明本轮情况,补足长度要求。",
                             rollout=rollout, append=True)
        return self.st()[h]

    def test_verify_call_with_success_result_done(self):
        """调用(带编号)+配对的非错误结果 → 销账。"""
        h = self._dispatched(['{"start":true}'])
        self.assertTrue(self._check(h, self.CALL_OK).get("done"))

    def test_verify_string_form_args_done(self):
        h = self._dispatched(['{"start":true}'])
        self.assertTrue(self._check(h, self.CALL_STR_ARGS).get("done"))

    def test_verify_error_result_not_done(self):
        """F4 翻转条件:调用存在但结果带错 → 不得销账。"""
        h = self._dispatched(['{"start":true}'])
        v = self._check(h, self.CALL_ERR)
        self.assertFalse(v.get("done"))
        self.assertFalse(v.get("alarm"))

    def test_verify_call_without_result_not_done(self):
        """F4 翻转条件:只有调用没有结果 → 不得销账。"""
        h = self._dispatched(['{"start":true}'])
        v = self._check(h, self.CALL_NO_RESULT)
        self.assertFalse(v.get("done"))
        self.assertFalse(v.get("alarm"))

    def test_verify_business_false_not_done(self):
        """F5 翻转条件:结果未报错但业务字段为否({"deleted": false}) → 不得销账。"""
        h = self._dispatched(['{"start":true}'])
        v = self._check(h, self.CALL_FALSE)
        self.assertFalse(v.get("done"))
        self.assertFalse(v.get("alarm"))

    def test_verify_nested_marker_trap_not_done(self):
        """F7 翻转条件:真实字段为否、嵌套文本里恰好出现成功字样 → 不得销账。"""
        h = self._dispatched(['{"start":true}'])
        v = self._check(h, self.CALL_NESTED_TRAP)
        self.assertFalse(v.get("done"))
        self.assertFalse(v.get("alarm"))

    def test_verify_missing_success_field_not_done(self):
        """F7 翻转条件:结果 JSON 里没有约定字段 → 不得销账。"""
        h = self._dispatched(['{"start":true}'])
        v = self._check(h, self.CALL_MISSING_FIELD)
        self.assertFalse(v.get("done"))
        self.assertFalse(v.get("alarm"))

    def test_verify_invalid_json_content_not_done(self):
        """F7 翻转条件:结果内容不是 JSON → 不得销账。"""
        h = self._dispatched(['{"start":true}'])
        v = self._check(h, self.CALL_INVALID_JSON)
        self.assertFalse(v.get("done"))
        self.assertFalse(v.get("alarm"))

    def test_state_save_failure_stops_dispatch(self):
        """F6:状态写不进 → 不发任何派发/催办指令,报警转人工,放行。"""
        os.makedirs(self.state, exist_ok=True)  # 状态路径是目录 → 原子替换必败
        rc, b, err = self.gate("开单说明文字超过三十个字符长度要求补足内容。\n"
                               + ORDER_LINE + "\n继续说明。")
        self.assertEqual(rc, 0)
        self.assertIsNone(b, "状态未落盘不得派发/催办")
        self.assertIn("写入失败", err)
        self.assertTrue(os.path.exists(self.alarm))

    def test_verify_split_records_not_done(self):
        """F1 翻转条件:工具名与参数分属不同记录时不得核销。"""
        h = self._dispatched(['{"start":true}'])
        v = self._check(h, ['{"toolCalls":[{"id":"call_x","name":"CronDelete","input":{"id":"99"}}]}',
                            '{"type":"tool_result","tool_use_id":"call_x","content":"ok"}',
                            '{"note":"别处出现的目标参数字符串 {\\"id\\":\\"42\\"}"}'])
        self.assertFalse(v.get("done"))
        self.assertFalse(v.get("alarm"))

    def test_verify_pre_offset_not_done(self):
        """锚点之前就存在的成功调用+结果不得核销。"""
        h = self._dispatched(self.CALL_OK)
        rc, b, _ = self.gate("汇报内容超过三十个字符,说明本轮情况,补足长度要求。")
        self.assertFalse(self.st()[h].get("done"))

    def test_verify_other_args_not_done(self):
        h = self._dispatched(['{"start":true}'])
        v = self._check(h, ['{"toolCalls":[{"id":"call_y","name":"CronDelete","input":{"id":"99"}}]}',
                            '{"type":"tool_result","tool_use_id":"call_y","content":"ok"}'])
        self.assertFalse(v.get("done"))

    # ---------- F2 派发指令的参数安全 ----------

    def test_board_dispatch_base64_no_raw_payload(self):
        # V2.1 起 create_thread 走直执面(gov 检查+base64 经 rb.sh);派发指令面
        # 的防注入回归载体改为仍在派发通道的 set_verdict
        order = ("修复单:: set_verdict {\"thread_id\":\"a'; rm -rf /tmp/nope; `echo x` "
                 "$(touch /tmp/rb-pwned)\",\"verdict\":\"pass\"}")
        h = self._register(order)
        rc, b, _ = self.gate(f"执行 {h}")
        self.assertIsNotNone(b, "应退回派发指令")
        reason = b["reason"]
        self.assertIn("base64", reason)
        self.assertNotIn("rm -rf", reason)
        self.assertNotIn("rb-pwned", reason)

    # ---------- F3 重复登记独立生命周期 ----------

    def test_re_register_same_order_gets_new_id(self):
        h1 = self._dispatched(['{"start":true}'])
        self.gate("子代理已执行完毕并报告成功,本回合汇报结束,内容超过三十个字符。",
                  rollout=self.CALL_OK, append=True)
        self.assertTrue(self.st()[h1].get("done"))
        rc, b, _ = self.gate("同一单再次出现,说明文字超过三十个字符长度要求补足内容。\n"
                             + ORDER_LINE + "\n补长度。")
        self.assertIsNotNone(b)
        self.assertEqual(sorted(k for k in self.st() if not k.startswith("_")),
                         sorted([h1, h1 + "-2"]))   # V2.2:忽略 _scan 等内部键

    # ---------- 报警与放行兜底 ----------

    def test_alarm_after_bounce_cap(self):
        h = self._register()
        self.gate(f"执行 {h}")                     # 派发（无 rollout → 锚点 -1,计数 2）
        errs = []
        for _ in range(3):                          # 催办×2(计数 3、4)→ 第 3 轮超限报警
            rc, b, err = self.gate("继续等待执行结果,说明文字超过三十个字符长度补足。")
            errs.append(err)
        self.assertTrue(any("转人工" in e for e in errs), "报警应有标准错误输出")
        rc, b, err = self.gate("再等一轮,说明文字超过三十个字符长度补足内容。")
        self.assertEqual(rc, 0)
        self.assertIsNone(b, "报警后放行不再退回")
        self.assertTrue(self.st()[h].get("alarm"))
        self.assertTrue(os.path.exists(self.alarm))

    def test_invalid_json_allow(self):
        rc, out = self.gate_raw(b"not json")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")


# ═══════════════ V2.2/V2.3（GPT round-10/11）：跨进程互斥回归 ═══════════════
# GPT round-11 点名五用例（要求提交进仓库、可反复运行，双进程用真 OS 进程 +
# 同步屏障，而非单线程 mock）：
#   1 双进程同单竞争   → _direct_exec 恰一次（WAITING 握手:已实际撞锁才算就位）
#   2 后到进程重读状态 → 看到 executing/done 不执行副作用
#   3 锁持有者异常退出 → OS 回收锁,另一进程可立即取得
#   4 TTL 超期         → executing 条目保留、报警、不重执行
#   5 锁获取失败       → 无任何副作用、正常放行
sys.path.insert(0, os.path.join(HERE, "configs"))


class V22Concurrency(unittest.TestCase):
    """独立于 Harness(避免继承把 V1 测试重跑一遍)。"""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.state = os.path.join(self._dir.name, "state.json")
        self.logp = os.path.join(self._dir.name, "log.txt")
        self.alarm = os.path.join(self._dir.name, "alarm.txt")
        self.rollout = os.path.join(self._dir.name, "rollout.jsonl")

    def tearDown(self):
        self._dir.cleanup()

    def _env(self):
        return dict(os.environ,
                    RB_REPAIR_STATE=self.state, RB_REPAIR_LOG=self.logp,
                    RB_REPAIR_ALARM=self.alarm, AG_ROLLOUT=self.rollout)

    def st(self):
        with open(self.state, encoding="utf-8") as f:
            return json.load(f)
    """直执行（看板类）+ 跨进程锁的竞争回归。全部走 RB_REPAIR_DRYRUN=1——
    验证的是锁与状态机时序，不触真实看板。"""

    ORDER = '修复单:: post_comment {"thread_id": 7, "body": "hi"}'
    ARGS = '{"thread_id": 7, "body": "hi"}'
    HID = "v22a01"

    def _seed(self, **extra):
        st = {"ts": time.time(), "bounces": 0, "confirmed": True,
              "tool": "mcp__review-board__post_comment", "args": self.ARGS}
        st.update(extra)
        json.dump({self.HID: st}, open(self.state, "w", encoding="utf-8"))

    def _receipt(self):
        with open(self.rollout, "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "assistant", "text": f"执行 {self.HID}"},
                               ensure_ascii=False) + "\n")

    def _child_env(self, **over):
        e = self._env()
        e["RB_REPAIR_DRYRUN"] = "1"
        e.update(over)
        return e

    def _spawn(self, e):
        p = subprocess.Popen([PY, GATE], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=e)
        p.stdin.write(json.dumps({"hook_event_name": "PostToolUse", "session_id": "v22",
                                  "tool_name": "Read", "tool_input": {}}).encode("utf-8"))
        p.stdin.close()
        return p

    def _log(self):
        with open(self.logp, encoding="utf-8") as f:
            return f.read()

    def _gate(self):
        """跑一次钩子(独立版,等价 Harness.gate 的最小面)。"""
        p = subprocess.run([PY, GATE],
                           input=json.dumps({"hook_event_name": "PostToolUse",
                                             "session_id": "v22",
                                             "tool_name": "Read",
                                             "tool_input": {}}).encode("utf-8"),
                           capture_output=True, env=self._env(), timeout=60)
        out = p.stdout.decode("utf-8", "replace").strip()
        try:
            return json.loads(out) if out else None
        except ValueError:
            return None

    # ── 用例 1+2（GPT 表格第 1、2 行）：同步屏障下双进程同单 ──
    def test_t3_two_real_processes_claim_exactly_once(self):
        self._seed()
        self._receipt()
        # 确定性同步屏障 V2（GPT round-14 P2:READY 只证明"到达打印行",
        # 不证明"已阻塞在锁上"）——子进程先实际尝试一次非阻塞取锁
        # (父进程持锁,必失败),失败后才发 WAITING;WAITING = 已真实遭遇竞争。
        # 父进程收齐两个 WAITING 才放行 → 放行时两者都已撞过这把锁。
        sys.path.insert(0, os.path.join(HERE, "configs"))
        import repair_gate as rg
        import queue as _q
        import threading as _th
        cfg_dir = os.path.join(HERE, "configs")
        wrapper = (
            "import sys\n"
            "sys.path.insert(0, r'" + cfg_dir.replace("\\", "/") + "')\n"
            "import repair_gate as rg\n"
            "f = open(rg.STATE_PATH + '.claim', 'a+')\n"
            "try:\n"
            "    rg._try_lock(f)\n"              # 非阻塞尝试:父持锁,必失败
            "except OSError:\n"
            "    pass\n"
            "else:\n"
            "    raise AssertionError('child acquired lock before barrier')\n"
            "print('WAITING', flush=True)\n"     # 信号 = 已实际遭遇锁竞争
            "rg.main()\n"                        # main 内有界等待→父放行后真正竞争
        )
        lf = open(self.state + ".claim", "a+")
        children = []

        def _readline_bounded(stream, secs=30):
            q = _q.Queue()
            _th.Thread(target=lambda: q.put(stream.readline()), daemon=True).start()
            try:
                return q.get(timeout=secs)
            except _q.Empty:
                raise AssertionError("子进程 WAITING/READY 行超时(启动失败?)")

        try:
            rg._lock_exclusive(lf)             # 父进程持锁 = 屏障
            for _ in range(2):
                e = self._child_env()
                cp = subprocess.Popen([PY, "-c", wrapper], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=e)
                cp.stdin.write(json.dumps({"hook_event_name": "PostToolUse",
                                           "session_id": "v22", "tool_name": "Read",
                                           "tool_input": {}}).encode("utf-8"))
                cp.stdin.close()
                children.append(cp)
            for cp in children:                # 确定性握手:收齐两个 WAITING 才放行
                self.assertEqual(_readline_bounded(cp.stdout).strip(), b"WAITING",
                                 "子进程未实际遭遇锁竞争(非阻塞尝试未失败)")
            rg._unlock(lf)                     # 双 WAITING 到齐 → 真竞争开始
        finally:
            lf.close()
        for cp in children:                    # 有界等待;失败(超时)才清理,防泄漏
            try:
                out, err = cp.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                cp.kill()
                cp.communicate()
                raise AssertionError("子进程 60s 未退出,已清理(泄漏防护)")
            self.assertEqual(cp.returncode, 0,
                             "子进程须正常退出(排除 fail-open 崩溃) rc=%s err=%r out=%r"
                             % (cp.returncode, err.decode("utf-8", "replace")[:400], out[:200]))
            err_d = err.decode("utf-8", "replace")
            # stderr 断言语义(GPT round-14 的意图=排除 fail-open 吞异常,非字面真空):
            # 禁止「内部错误(放行)」/ Traceback(fail-open 吞异常的两种痕迹);
            # 容忍「领取锁忙」一行——那是输家 8s 等锁超时后合法的 fail-closed 降级,
            # 行为正确(零副作用)且有留痕,不是异常。
            self.assertNotIn("内部错误", err_d, "fail-open 吞异常 err=%r" % err_d[:400])
            self.assertNotIn("Traceback", err_d, "裸 traceback err=%r" % err_d[:400])
            if err_d.strip():
                self.assertIn("领取锁忙", err_d,
                              "stderr 非空且非已知合法降级行 err=%r" % err_d[:400])
        log = self._log()
        self.assertEqual(log.count("order-direct-done"), 1, "恰一次执行")
        self.assertEqual(log.count("direct-exec-dryrun"), 1, "恰一次 _direct_exec")
        st = self.st()
        self.assertTrue(st[self.HID].get("done"))
        self.assertIsNotNone(st[self.HID].get("exec_evidence"))
        self.assertIsNone(st[self.HID].get("alarm"))
        # 用例 2 显式格：第三个钩子（后到者）重读状态,看到 done → 不再执行
        before = self._log()
        self._gate()
        self.assertEqual(self._log(), before, "后到者零副作用")

    # ── 用例 3（GPT 表格第 3 行）：锁持有者异常退出,OS 回收锁 ──
    def test_t4_lock_reclaimed_after_holder_killed(self):
        sys.path.insert(0, os.path.join(HERE, "configs"))
        import repair_gate as rg
        holder = subprocess.Popen(
            [PY, "-c",
             "import sys, time, os\n"
             "sys.path.insert(0, r'" + os.path.join(HERE, "configs") + "')\n"
             "import repair_gate as rg\n"
             "f = open(sys.argv[1], 'a+')\n"
             "rg._lock_exclusive(f)\n"
             "print('LOCKED', flush=True)\n"
             "time.sleep(30)\n",
             sys.argv[0], self.state + ".claim"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertEqual(holder.stdout.readline().strip(), b"LOCKED")
            holder.stdout.close(); holder.stderr.close()
            t0 = time.time()
            holder.kill()                      # 异常退出（非正常释放）
            holder.wait(timeout=10)
            lf = open(self.state + ".claim", "a+")
            try:
                rg._lock_exclusive(lf)         # 必须能立即取得(OS 回收)
                self.assertLess(time.time() - t0, 5.0, "异常退出后锁应立即可得")
            finally:
                rg._unlock(lf)
                lf.close()
        finally:
            if holder.poll() is None:
                holder.kill()

    # ── 用例 4（GPT 表格第 4 行）：超 TTL executing 保留、报警、不重执行 ──
    def test_t5_stale_executing_kept_not_reexecuted(self):
        self._seed(ts=time.time() - 90000, executing=True)   # 超 24h
        self._receipt()
        bounce = self._gate()
        self.assertIsNotNone(bounce, "executing 人工核 bounce")
        st = self.st()
        self.assertIn(self.HID, st, "条目保留不删（TTL 豁免）")
        self.assertTrue(st[self.HID].get("stale_alarmed"))
        self.assertNotIn("direct-exec-dryrun", self._log(), "不重执行")
        with open(self.alarm, encoding="utf-8") as f:
            self.assertIn("executing 超24h", f.read())

    # ── 用例 5（GPT 表格第 5 行）：锁获取失败 → 零副作用、正常放行 ──
    def test_t6_lock_busy_fail_closed(self):
        self._seed()
        self._receipt()
        sys.path.insert(0, os.path.join(HERE, "configs"))
        import repair_gate as rg
        lf = open(self.state + ".claim", "a+")
        try:
            rg._lock_exclusive(lf)                       # 父进程持锁不放
            child = self._spawn(self._child_env(RB_REPAIR_LOCK_WAIT="1.2"))
            out, err = child.communicate(timeout=60)
            self.assertEqual(child.returncode, 0)
            self.assertEqual(out.strip(), b"", "锁忙=静默放行,无派发指令")
            self.assertNotIn("direct-exec-dryrun", self._log(), "零副作用")
            st = self.st()
            self.assertNotIn("executing", st[self.HID], "未领取(未写执行中)")
            self.assertNotIn("done", st[self.HID], "未销账")
            self.assertIn("claim-lock-busy", self._log(), "锁忙留痕")
        finally:
            rg._unlock(lf)
            lf.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
