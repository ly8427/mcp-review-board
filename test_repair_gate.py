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
import shutil
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


def _readline_bounded(stream, secs=30):
    """有界读行(GPT round-14/16:子进程启动失败时测试不得挂死)。"""
    import queue
    import threading
    q = queue.Queue()
    threading.Thread(target=lambda: q.put(stream.readline()), daemon=True).start()
    try:
        return q.get(timeout=secs)
    except queue.Empty:
        raise AssertionError("子进程输出行超时(%ss)——启动失败或未打印" % secs)


class _StubBoard:
    """内嵌 stub 看板(GPT round-16 P2-4:并发/治理测试不得依赖本机 8765)。"""

    def __init__(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.url = None
        self._srv = None

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"{}"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self._H = Handler
        self._srv_cls = ThreadingHTTPServer

    def start(self):
        self._srv = self._srv_cls(("127.0.0.1", 0), self._H)
        port = self._srv.server_address[1]
        self.url = "http://127.0.0.1:%d" % port
        import threading
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        return self.url

    def stop(self):
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()
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
        self.assertEqual([k for k in self.st() if not k.startswith("_")], [])   # V2.6:忽略 _scan 内部键

    def test_bare_order_registers_unconfirmed(self):
        rc, b, _ = self.gate("开单说明文字超过三十个字符长度要求补足内容。\n"
                             + ORDER_LINE + "\n继续说明。")
        self.assertEqual(rc, 0)
        self.assertIsNotNone(b)
        v = [x for k, x in self.st().items() if not k.startswith("_")][0]   # V2.6:忽略 _scan
        self.assertFalse(v["confirmed"])

    def test_non_whitelist_tool_rejected(self):
        rc, b, _ = self.gate("开单说明文字超过三十个字符长度要求补足内容。\n"
                             '修复单:: Bash {"command":"ls"}\n继续说明。')
        self.assertIsNone(b)
        self.assertEqual([k for k in self.st() if not k.startswith("_")], [])   # V2.6:忽略 _scan 内部键

    def test_duplicate_order_lines_register_once(self):
        text = ("开单说明文字超过三十个字符长度要求补足内容。\n"
                + ORDER_LINE + "\n" + ORDER_LINE + "\n继续说明。")
        self.gate(text)
        self.assertEqual(len([k for k in self.st() if not k.startswith("_")]), 1)   # V2.6:忽略 _scan

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
        v = [x for k, x in self.st().items() if not k.startswith("_")][0]   # V2.6:忽略 _scan
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
    """独立于 Harness(避免继承把 V1 测试重跑一遍)。
    治理检查的板依赖走内嵌 stub(P2-4)——不依赖本机 8765。"""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.state = os.path.join(self._dir.name, "state.json")
        self.logp = os.path.join(self._dir.name, "log.txt")
        self.alarm = os.path.join(self._dir.name, "alarm.txt")
        self.rollout = os.path.join(self._dir.name, "rollout.jsonl")
        self.board = _StubBoard()
        self.board_url = self.board.start()

    def tearDown(self):
        self.board.stop()
        self._dir.cleanup()

    def _env(self):
        return dict(os.environ,
                    RB_REPAIR_STATE=self.state, RB_REPAIR_LOG=self.logp,
                    RB_REPAIR_ALARM=self.alarm, AG_ROLLOUT=self.rollout,
                    RB_BOARD=self.board_url)

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
        p.stdin = None  # Py3.13 communicate() 会对已关闭的 stdin flush → ValueError
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
        barrier_ok = False

        try:
            rg._lock_exclusive(lf)             # 父进程持锁 = 屏障
            for _ in range(2):
                e = self._child_env()
                cp = subprocess.Popen([PY, "-c", wrapper], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=e)
                children.append(cp)   # P3-2:先入清理列表,再写 stdin(防写入期异常漏清理)
                cp.stdin.write(json.dumps({"hook_event_name": "PostToolUse",
                                           "session_id": "v22", "tool_name": "Read",
                                           "tool_input": {}}).encode("utf-8"))
                cp.stdin.close()
                cp.stdin = None  # Py3.13 communicate() 会对已关闭的 stdin flush → ValueError
            for cp in children:                # 确定性握手:收齐两个 WAITING 才放行
                self.assertEqual(_readline_bounded(cp.stdout).strip(), b"WAITING",
                                 "子进程未实际遭遇锁竞争(非阻塞尝试未失败)")
            rg._unlock(lf)                     # 双 WAITING 到齐 → 真竞争开始
            barrier_ok = True
        finally:
            # 失败路径统一清理(GPT round-15 P2):屏障未走通时,先释放父锁,
            # 再回收全部已启动子进程——防"失败测试留下仍在跑的子进程访问
            # 临时目录/污染后续测试"。正常路径(barrier_ok)跳过 kill:放行后
            # 子进程在合法运行主流程,由下方 communicate 有界等待自然回收。
            try:
                rg._unlock(lf)
            except Exception:
                pass  # 正常路径已释放;幂等兜底
            if not barrier_ok:
                for cp in children:
                    if cp.poll() is None:
                        cp.kill()
                    try:                       # 失败路径回收资源(含已退出者)
                        cp.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        cp.kill()
                        cp.communicate()
        for cp in children:                    # 全路径回收资源;有界等待防泄漏
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
            # 行为正确(零副作用)且只留着当记录,不是异常。
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
            self.assertEqual(_readline_bounded(holder.stdout).strip(), b"LOCKED")
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


class V25ScanAndExec(unittest.TestCase):
    """GPT round-16 P2-5:PostToolUse 增量扫描数据链 + 直执判据矩阵回归。
    全部走内嵌 stub 看板(治理)与 RB_REPAIR_EXEC_CMD 桩(执行)——
    不依赖本机 8765;POSIX 上连 wsl 都不需要(执行桩走 bash 直连)。"""

    ORDER = '修复单:: post_comment {"thread_id": 31, "body": "hi"}'
    ARGS = '{"thread_id": 31, "body": "hi"}'

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        d = self._dir.name
        self.state = os.path.join(d, "state.json")
        self.logp = os.path.join(d, "log.txt")
        self.alarm = os.path.join(d, "alarm.txt")
        self.rollout = os.path.join(d, "rollout.jsonl")
        open(self.rollout, "w", encoding="utf-8").close()
        self.board = _StubBoard()
        self.board_url = self.board.start()

    def tearDown(self):
        self.board.stop()
        self._dir.cleanup()

    def _wsl_path(self, win_path):
        """Windows 路径 → WSL 形态(/mnt/c/...)。不是 Git-Bash 的 /c/...——
        直执子进程经 wsl -e bash 执行,必须用 WSL 路径(round-16 调试实证)。"""
        p = win_path.replace("\\", "/")
        if len(p) > 1 and p[1] == ":":
            p = "/mnt/" + p[0].lower() + p[2:]
        return p

    def _env(self, exec_stub=False):
        e = dict(os.environ,
                 RB_REPAIR_STATE=self.state, RB_REPAIR_LOG=self.logp,
                 RB_REPAIR_ALARM=self.alarm, AG_ROLLOUT=self.rollout,
                 RB_BOARD=self.board_url, RB_REPAIR_DRYRUN="0")
        if exec_stub:
            stub = os.path.join(self._dir.name, "stub.sh")
            with open(stub, "w", encoding="utf-8", newline="\n") as f:
                f.write('#!/bin/sh\ncat "$(dirname "$0")/stub.sh.out"\n')
            cmd = "bash %s/stub.sh {short} {b64}" % self._wsl_path(
                os.path.join(self._dir.name))
            e["RB_REPAIR_EXEC_CMD"] = cmd
        return e

    def _stub_out(self, text):
        """写执行桩的返回体(stub.sh 会 cat 它)。"""
        stub = os.path.join(self._dir.name, "stub.sh")
        if not os.path.exists(stub):
            with open(stub, "w", encoding="utf-8", newline="\n") as f:
                f.write('#!/bin/sh\ncat "$(dirname "$0")/stub.sh.out"\n')
        with open(os.path.join(self._dir.name, "stub.sh.out"), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write(text)

    def _run(self, env, text=None, post=True):
        payload = {"hook_event_name": "PostToolUse" if post else "Stop",
                   "session_id": "v25", "tool_name": "Read", "tool_input": {}}
        if text is not None:
            payload["last_message"] = {"role": "assistant", "content": text}
        r = subprocess.run([PY, GATE], input=json.dumps(payload, ensure_ascii=False)
                           .encode("utf-8"), capture_output=True, env=env, timeout=60)
        out = r.stdout.decode("utf-8", "replace").strip()
        try:
            return json.loads(out) if out else None
        except ValueError:
            return None

    def _orders(self):
        st = self.st()
        return {k: v for k, v in st.items() if not k.startswith("_")}

    def st(self):
        with open(self.state, encoding="utf-8") as f:
            return json.load(f)

    def _append(self, text, newline=True):
        with open(self.rollout, "a", encoding="utf-8", newline="") as f:
            f.write(text + ("\n" if newline else ""))

    # ── 1:PostToolUse 从 rollout 增量发现新修复单 ──
    def test_a_order_discovered_from_delta(self):
        e = self._env()
        self._run(e)                       # 首轮:锚定当前位(空 rollout)
        self.assertEqual(len(self._orders()), 0, "首轮只锚定不登记")
        self._append(self.ORDER)
        self._run(e)                       # PostToolUse:增量发现新单
        st = self.st()
        self.assertEqual(len(self._orders()), 1, "增量发现新修复单")
        self.assertIn("_scan", st)

    # ── 2:回执经增量读取 → 直执销账(经执行桩,全链) ──
    def test_b_receipt_via_delta_completes_direct_exec(self):
        e = self._env(exec_stub=True)
        self._stub_out('{"thread_id": 31, "body": "hi", "ok": true}')
        self._run(e)
        self._append(self.ORDER)
        self._run(e)
        hid = list(self._orders())[0]
        self._append("执行 " + hid)
        out = self._run(e, post=False)     # Stop 事件也可(此处验证增量路径)
        st = self.st()
        self.assertTrue(st[hid].get("done"), "直执销账")
        self.assertTrue(st[hid].get("exec_evidence"))
        self.assertIsNone(out, "完成态静默放行")

    # ── 3:未写完的尾行保留下轮,不被跳过 ──
    def test_c_incomplete_tail_line_deferred(self):
        e = self._env()
        self._run(e)
        self._append(self.ORDER)
        self._run(e)
        self.assertEqual(len(self._orders()), 1)
        size_after_first = os.path.getsize(self.rollout)
        # 追加完整第二单 + 半条第三单(无换行)
        self._append(self.ORDER.replace('"thread_id": 31', '"thread_id": 32'))
        partial = '修复单:: post_comment {"thread_id": 33, "bo'
        self._append(partial, newline=False)
        self._run(e)
        st = self.st()
        self.assertEqual(len(self._orders()), 2, "完整行被消费")
        scan = st["_scan"]["sessions"]["v25"]
        self.assertLess(scan["off"], os.path.getsize(self.rollout),
                        "偏移不越过未完成尾行")
        self.assertGreater(scan["off"], size_after_first, "偏移推进到完整行末")
        self._append('dy": "z"}\n')       # 补完半条
        self._run(e)
        self.assertEqual(len(self._orders()), 3, "补完后下轮读出第三单")

    # ── 4:增量超过 1 MiB 不丢数据 ──
    def test_d_large_delta_over_1mib(self):
        e = self._env()
        self._run(e)
        pad = '{"pad": 1}'                 # ~10B 良性行
        blob = "\n".join([pad] * 100000) + "\n" + self.ORDER + "\n"   # ≈1.2MiB
        self._append(blob)
        self.assertGreater(os.path.getsize(self.rollout) - 1, 1024 * 1024)
        self._run(e)
        self.assertEqual(len(self._orders()), 1, ">1MiB 增量末尾的修复单仍被发现")

    # ── 5:文件轮转(身份变化)→ 重锚不误读 ──
    def test_e_rotation_reanchors(self):
        e = self._env()
        self._run(e)                       # 锚定
        st = self.st()
        st["_scan"] = {"ts": time.time(),
                       "sessions": {"v25": {"off": 10 ** 9, "fid": "bogus",
                                            "size": 10 ** 9, "ts": time.time()}}}
        json.dump(st, open(self.state, "w", encoding="utf-8"))
        self._append(self.ORDER)           # 若被误读会登记新单
        self._run(e)
        st = self.st()
        self.assertEqual(len(self._orders()), 0, "轮转后不按旧偏移误读")
        self.assertLess(st["_scan"]["sessions"]["v25"]["off"], 10 ** 9,
                        "重锚到当前文件末尾")

    # ── 6:重复扫描同段不重复登记 ──
    def test_f_no_double_register(self):
        e = self._env()
        self._run(e)
        self._append(self.ORDER)
        self._run(e)
        self._run(e)                       # 无新增数据
        self.assertEqual(len(self._orders()), 1, "同段不重复登记")

    # ── 7:直执判据矩阵(P2-2)——无效返回不得销账 ──
    def test_g_exec_rejects_invalid_returns(self):
        for out_json, expect_done in (("false", False), ("null", False),
                                      ("[]", False), ("0", False),
                                      ('{"ok": false}', False),
                                      ('{"success": false}', False),
                                      ('{"thread_id": 31, "ok": true}', True)):
            with self.subTest(out_json=out_json):
                d = tempfile.mkdtemp(prefix="rg25g-")
                try:                      # 每例独立目录,互不污染(桩仍指向 self._dir)
                    env = dict(os.environ,
                               RB_REPAIR_STATE=os.path.join(d, "state.json"),
                               RB_REPAIR_LOG=os.path.join(d, "log.txt"),
                               RB_REPAIR_ALARM=os.path.join(d, "alarm.txt"),
                               RB_BOARD=self.board_url, RB_REPAIR_DRYRUN="0",
                               RB_REPAIR_EXEC_CMD="bash %s/stub.sh {short} {b64}"
                                                  % self._wsl_path(self._dir.name))
                    self._stub_out(out_json)
                    env["RB_ROLLOUT_DIR"] = d   # 会话分区模式(P2-4/round-17 接缝)
                    rollout = os.path.join(d, "model-io-session_g.jsonl")

                    def run(text=None):
                        payload = {"hook_event_name": "Stop", "session_id": "g",
                                   "tool_name": "Read", "tool_input": {}}
                        if text is not None:
                            payload["last_message"] = {"role": "assistant",
                                                       "content": text}
                        r = subprocess.run([PY, GATE],
                                           input=json.dumps(payload, ensure_ascii=False)
                                           .encode("utf-8"), capture_output=True,
                                           env=env, timeout=60)
                        o = r.stdout.decode("utf-8", "replace").strip()
                        try:
                            return json.loads(o) if o else None
                        except ValueError:
                            return None

                    run(self.ORDER)
                    st = json.load(open(env["RB_REPAIR_STATE"], encoding="utf-8"))
                    hid = [k for k in st if not k.startswith("_")][0]
                    with open(rollout, "a", encoding="utf-8") as f:
                        f.write(json.dumps({"type": "assistant", "text": "执行 " + hid},
                                           ensure_ascii=False) + "\n")
                    b2 = run()
                    st = json.load(open(env["RB_REPAIR_STATE"], encoding="utf-8"))
                    diag = "bounce=%r scan=%r entry=%r" % (
                        (b2 or {}).get("reason", "")[:220],
                        st.get("_scan"), st[hid])
                    self.assertEqual(bool(st[hid].get("done")), expect_done,
                                     "out=%r done 期望 %s; %s" % (out_json, expect_done, diag))
                    if not expect_done:
                        self.assertTrue(st[hid].get("executing"),
                                        "未确认→executing 人工核; %s" % diag)
                finally:
                    shutil.rmtree(d, ignore_errors=True)

    # ── 8:治理拒绝理由传播(P2-3)——死端口板 → bounce 带具体理由 ──
    def test_h_gov_reason_propagated(self):
        e = self._env(exec_stub=True)
        e["RB_BOARD"] = "http://127.0.0.1:9"
        out = self._run(e, text=self.ORDER, post=False)     # 登记+确认步
        self.assertIsNotNone(out)
        hid = list(self._orders())[0]
        self._append("执行 " + hid)
        out = self._run(e, post=False)
        self.assertIsNotNone(out, "治理拒绝退回")
        self.assertIn("治理前置未过", out["reason"])
        self.assertIn("看板不可达", out["reason"], "具体拒绝理由随 bounce 传给模型")


class V26Sessions(unittest.TestCase):
    """GPT round-17:多 session 游标分区(P1)/ 原始会话 >1MiB 证据核验(P2)/
    真实 JSONL 嵌套形态扫描(P2)/ 同 inode 截断重锚(P3)。"""

    ORDER_CRON = '修复单:: CronDelete {"id":"42"}'

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.state = os.path.join(self._dir.name, "state.json")
        self.logp = os.path.join(self._dir.name, "log.txt")
        self.alarm = os.path.join(self._dir.name, "alarm.txt")
        self.rdir = tempfile.mkdtemp(prefix="rg26roll-")
        self.board = _StubBoard()
        self.board_url = self.board.start()

    def tearDown(self):
        self.board.stop()
        self._dir.cleanup()
        shutil.rmtree(self.rdir, ignore_errors=True)

    def _env(self):
        return dict(os.environ,
                    RB_REPAIR_STATE=self.state, RB_REPAIR_LOG=self.logp,
                    RB_REPAIR_ALARM=self.alarm, RB_ROLLOUT_DIR=self.rdir,
                    RB_BOARD=self.board_url, RB_REPAIR_DRYRUN="0")

    def _sess_file(self, sid):
        return os.path.join(self.rdir, "model-io-session_%s.jsonl" % sid)

    def _run(self, sid, text=None):
        payload = {"hook_event_name": "Stop", "session_id": sid,
                   "tool_name": "Read", "tool_input": {}}
        if text is not None:
            payload["last_message"] = {"role": "assistant", "content": text}
        r = subprocess.run([PY, GATE], input=json.dumps(payload, ensure_ascii=False)
                           .encode("utf-8"), capture_output=True, env=self._env(),
                           timeout=60)
        out = r.stdout.decode("utf-8", "replace").strip()
        try:
            return json.loads(out) if out else None
        except ValueError:
            return None

    def _st(self):
        with open(self.state, encoding="utf-8") as f:
            return json.load(f)

    def _orders(self):
        return {k: v for k, v in self._st().items() if not k.startswith("_")}

    def _jsonl(self, sid, obj):
        with open(self._sess_file(sid), "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def _slot(self, sid):
        return self._st().get("_scan", {}).get("sessions", {}).get(sid)

    # ── S1(P1):A/B 交替触发,A 的新单与回执均不丢失 ──
    def test_s1_alternating_sessions_no_loss(self):
        self._run("sessA")                 # A 首锚(文件未产生 → 锚 0)
        self._run("sessB")                 # B 首锚
        # A 会话里出现新修复单(真实 JSONL 形态,S3 同款嵌套)
        self._jsonl("sessA", {"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "text",
                         "text": "开单说明文字超过三十个字符长度要求补足内容。\n"
                                 + self.ORDER_CRON + "\n继续说明。"}]}})
        self._run("sessA")                 # A 的钩子扫描 → 登记
        st = self._st()
        hid = [k for k in st if not k.startswith("_")][0]
        self.assertEqual(st[hid].get("src_session"), "sessA", "来源会话随单登记")
        slot_a_before = self._slot("sessA")
        # B 的钩子插入处理(旧全局游标在这里会覆盖 A 的游标)
        self._run("sessB")
        # A 的回执也进 A 文件
        self._jsonl("sessA", {"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "text", "text": "执行 " + hid}]}})
        self._run("sessA")                 # A 的钩子:确认 + Cron 派发
        st = self._st()
        self.assertTrue(st[hid].get("confirmed"), "回执确认不丢")
        self.assertTrue(st[hid].get("dispatched"), "派发锚点落原始会话文件")
        self.assertEqual(st[hid].get("verify_session"), "sessA")
        slot_a_after = self._slot("sessA")
        self.assertGreater(slot_a_after["off"], slot_a_before["off"], "A 游标前进")
        # B 的钩子再插入一轮,A 的已 done 条目不被重算,游标互不干扰
        self._run("sessB")
        self.assertEqual(self._slot("sessA")["off"], slot_a_after["off"], "B 轮不越权动 A 游标")

    # ── S2(P2):核验证据 >1MiB(原始会话文件),跨会话钩子核验仍成立 ──
    def test_s2_verify_evidence_beyond_1mib(self):
        self._run("sessA", text=self.ORDER_CRON)      # 登记(payload 文本面)
        st = self._st()
        hid = [k for k in st if not k.startswith("_")][0]
        self._jsonl("sessA", {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "执行 " + hid}]}})
        self._run("sessA")                            # 确认 + Cron 派发(锚点=此时 A 文件大小)
        st = self._st()
        off = st[hid]["dispatch_offset"]
        # A 文件追加 >1MiB 填充行,证据(调用+成功结果)在填充之后——旧 1MiB 截断核验会漏
        pad = "\n".join(json.dumps({"pad": i}) for i in range(100000))
        with open(self._sess_file("sessA"), "a", encoding="utf-8") as f:
            f.write(pad + "\n")
            f.write(_call("cron-42", name="CronDelete", input={"id": "42"}) + "\n")
            f.write(_res("cron-42", '{"deleted": true}') + "\n")
        # 跨会话:B 的钩子跑状态机 → 核验必须读 **sessA** 的文件(verify_session)
        self._run("sessB")
        st = self._st()
        self.assertTrue(st[hid].get("done"), ">1MiB 后的证据仍被核验销账")

    # ── S3(P2):真实 JSONL 嵌套形态——单与回执主路径 ──
    def test_s3_nested_jsonl_main_path(self):
        self._run("sessN")                            # 首锚
        self._jsonl("sessN", {"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "text",
                         "text": "开单说明文字超过三十个字符长度要求补足内容。\n"
                                 + '修复单:: post_comment {"thread_id": 9, "body": "hi"}'
                                 + "\n继续说明。"}]}})
        self._run("sessN")
        st = self._st()
        hid = [k for k in st if not k.startswith("_")][0]
        self.assertEqual(st[hid]["tool"], "mcp__review-board__post_comment",
                         "嵌套 assistant 正文中的修复单被登记")
        self._jsonl("sessN", {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "执行 " + hid}]}})
        self._run("sessN")
        st = self._st()
        self.assertTrue(st[hid].get("confirmed"), "嵌套回执被确认")

    # ── S4(P3):同 inode 原地截断(变小)→ 重锚,不按旧偏移误读 ──
    # ── S4a(P3,可检测截断):终长 < 旧 size → scan-shrunk 重锚 + 被截区不消费 ──
    def test_s4a_detectable_shrink_reanchors(self):
        self._run("sessS")                            # 首锚(文件未产生 → 0)
        with open(self._sess_file("sessS"), "a", encoding="utf-8") as f:
            f.write("x" * 500 + "\n")                 # 内容推进
        self._run("sessS")
        off_before = self._slot("sessS")["off"]
        prev_size = self._slot("sessS")["size"]
        self.assertGreater(off_before, 0)
        # 可检测截断:截断后**终长仍小于旧 size** → cur < prev_size → scan-shrunk
        with open(self._sess_file("sessS"), "r+b") as f:
            f.truncate(off_before - 300)
        with open(self._sess_file("sessS"), "a", encoding="utf-8") as f:
            f.write('修复单:: post_comment {"thread_id": 51, "body": "z"}\n')  # 落在被截区域
        final = os.path.getsize(self._sess_file("sessS"))
        self.assertLess(final, prev_size, "场景前提:终长 < 旧 size(可检测)")
        self._run("sessS")
        slot = self._slot("sessS")
        with open(self.logp, encoding="utf-8") as f:
            self.assertIn("scan-shrunk", f.read(), "触发 shrink 重锚并留痕")
        self.assertEqual(slot["off"], final, "重锚=当前文件末尾")
        self.assertEqual(len([k for k in self._st() if not k.startswith("_")]), 0,
                         "被截区域内容不消费(保守语义,DEBT #10 同族)")

    # ── S4b(P3,不可检测截断):终长 > 旧偏移 → 按旧偏移正常推进(如实断言
    #    已接受的 DEBT 边界,不冒充"重锚成功") ──
    def test_s4b_undetectable_truncate_regrow(self):
        self._run("sessS")                            # 首锚
        with open(self._sess_file("sessS"), "a", encoding="utf-8") as f:
            f.write("x" * 500 + "\n")
        self._run("sessS")
        off_before = self._slot("sessS")["off"]
        # 截断到旧偏移之前,再写入**跨过旧偏移**的内容 → 终长 > 旧偏移,不可检测
        with open(self._sess_file("sessS"), "r+b") as f:
            f.truncate(off_before - 200)
        with open(self._sess_file("sessS"), "a", encoding="utf-8") as f:
            f.write("y" * 500 + "\n")
        final = os.path.getsize(self._sess_file("sessS"))
        self.assertGreater(final, off_before, "场景前提:终长 > 旧偏移(不可检测)")
        self._run("sessS")
        slot = self._slot("sessS")
        # 如实断言已接受的限制(DEBT #10):从旧偏移正常推进,
        # (旧偏移-截断点)之间的新内容被静默跳过——不是"重锚成功"
        self.assertEqual(slot["off"], final, "按旧偏移正常推进到文件末尾")
        log_txt = ""
        with open(self.logp, encoding="utf-8") as f:
            log_txt = f.read()
        self.assertNotIn("scan-shrunk", log_txt,
                         "此窗口不触发 shrink(不可检测,已留账)")

    # ── S5(GPT round-18 P2 回归):rollout 文件未产生时派发 → 锚 0 → 文件延迟
    #    出现后,证据核验正常销账(旧缺陷:锚 -1 → seek(-1) 异常逃进 fail-open,
    #    核验长期失效) ──
    def test_s5_dispatch_before_rollout_exists(self):
        # run1:登记(payload 文本面);此时 sessX 的 rollout 文件不存在
        self._run("sessX", text=self.ORDER_CRON)
        st = self._st()
        self.assertFalse(os.path.exists(self._sess_file("sessX")),
                         "场景前提:派发前文件尚未产生")
        hid = [k for k in st if not k.startswith("_")][0]
        # run2:回执走 payload 文本面(文件仍不产生)→ 确认 + 派发 → 锚必须是 0
        self._run("sessX", text="执行 " + hid)
        st = self._st()
        self.assertTrue(st[hid].get("dispatched"))
        self.assertEqual(st[hid].get("dispatch_offset"), 0,
                         "文件缺失 → 锚 0(新文件从头即派发后记录)")
        # 模型执行派发指令 → rollout 文件此刻才被创建,证据在文件头部
        with open(self._sess_file("sessX"), "a", encoding="utf-8") as f:
            f.write(_call("cron-s5", name="CronDelete", input={"id": "42"}) + "\n")
            f.write(_res("cron-s5", '{"deleted": true}') + "\n")
        # run3:跨会话钩子核验 → 读 sessX 文件(verify_session) → 正常销账
        self._run("sessY")
        st = self._st()
        self.assertTrue(st[hid].get("done"), "延迟出现的文件正常核验销账")


if __name__ == "__main__":
    unittest.main(verbosity=2)
