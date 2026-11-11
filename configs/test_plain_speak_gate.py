#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_plain_speak_gate.py — 说人话闸回归（board #109 修法验收 + 主路径覆盖）。

跑法：python configs/test_plain_speak_gate.py   （纯标准库 + 隔离临时文件）

覆盖：
  A  尾读取「最后一条 assistant 回复」，不被更长的旧黑话文冒充（#109 根因）
  B  #109 验收场景全链：黑话长文打回 → 改写干净短回复 → 第二次必须放行
  C  user/tool 记录里的黑话不触碰（只扫 assistant 回复）
  D  尾段无 assistant 记录 → extract None → 放行（fail-open，留 stderr 痕）
  E  payload 回复字段（第一路）仍优先
  F  zcode rollout 第三条路（AG_ROLLOUT）同一语义
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GATE = os.path.join(HERE, "plain_speak_gate.py")
PY = sys.executable

JARGON = ("把这次变更落盘固化，走完治理链路后把挂进主干的形态定下来，"
          "quorum 收敛口径按帖子里的决议对齐。") * 3     # 长 + 含词典词
CLEAN = "这次改动已经用日常语言重新说明：改了什么、为什么改、影响哪些文件。"

JARGON_ASSISTANT = json.dumps(
    {"type": "assistant", "message": {"role": "assistant", "content": JARGON}},
    ensure_ascii=False)
CLEAN_ASSISTANT = json.dumps(
    {"type": "assistant", "message": {"role": "assistant", "content": CLEAN}},
    ensure_ascii=False)
USER_WITH_JARGON = json.dumps(
    {"type": "user", "message": {"role": "user", "content": JARGON}},
    ensure_ascii=False)


class Harness(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = self._dir.name
        self.transcript = os.path.join(self.d, "transcript.jsonl")
        self.state = os.path.join(self.d, "state.json")   # 闸不读；仅占位

    def tearDown(self):
        self._dir.cleanup()

    def write_transcript(self, records):
        with open(self.transcript, "w", encoding="utf-8") as f:
            for r_ in records:
                f.write(json.dumps(r_, ensure_ascii=False) + "\n")

    def append_transcript(self, record):
        with open(self.transcript, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def gate(self, payload):
        p = subprocess.run([PY, GATE],
                           input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                           capture_output=True, env=dict(os.environ, **{
                               "RB_REPAIR_DRYRUN": "1"}), timeout=30)
        out = p.stdout.decode("utf-8", "replace").strip()
        try:
            return p.returncode, (json.loads(out) if out else None), \
                p.stderr.decode("utf-8", "replace")
        except ValueError:
            return p.returncode, None, p.stderr.decode("utf-8", "replace")

    def stop_payload(self):
        return {"hook_event_name": "Stop", "stop_hook_active": False,
                "session_id": "", "transcript_path": self.transcript}


class TestTailExtraction(Harness):
    """extract_text 的尾读语义（不经闸主流程，直接断言取到的文本）。"""

    def _extract(self, records):
        self.write_transcript(records)
        payload = {"hook_event_name": "Stop", "transcript_path": self.transcript}
        spec = {"file": GATE}
        import importlib.util
        s = importlib.util.spec_from_file_location("psg_extract", GATE)
        m = importlib.util.module_from_spec(s)
        s.loader.exec_module(m)
        return m.extract_text({"transcript_path": self.transcript})

    def test_a_last_assistant_wins_over_longer_older_jargon(self):
        # #109 根因复现位：旧黑话长文在前、干净短回复在后 → 必须取后者
        text = self._extract([
            {"type": "assistant", "message": {"role": "assistant", "content": JARGON}},
            {"type": "assistant", "message": {"role": "assistant", "content": CLEAN}},
        ])
        self.assertEqual(text, CLEAN)
        self.assertNotIn("落盘", text)

    def test_a2_tool_and_user_records_skipped(self):
        text = self._extract([
            {"type": "user", "message": {"role": "user", "content": JARGON}},
            {"type": "assistant", "message": {"role": "assistant", "content": CLEAN}},
        ])
        self.assertEqual(text, CLEAN)

    def test_a3_no_assistant_records_returns_none(self):
        self.assertIsNone(self._extract([
            {"type": "user", "message": {"role": "user", "content": JARGON}},
        ]))

    def test_a4_incomplete_tail_line_fails_open(self):
        # V1.1.3（GPT round-20 P1）预期翻转：尾行不完整（可能正被写入）→
        # 返回 None（fail-open），**禁止**拿前面完整的 assistant 回复冒充当前轮
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write(CLEAN_ASSISTANT + "\n")
            f.write('{"type":"assistant","message":{"content":"半截')   # 无换行，坏 JSON
        payload = {"transcript_path": self.transcript}
        import importlib.util
        s = importlib.util.spec_from_file_location("psg_extract2", GATE)
        m = importlib.util.module_from_spec(s)
        s.loader.exec_module(m)
        self.assertIsNone(m.extract_text(payload))


class TestGateEndToEnd(Harness):
    """#109 验收场景全链：黑话长文打回 → 改写干净 → 第二次必须放行。"""

    def test_b_rewrite_then_clean_passes_second_time(self):
        self.write_transcript([{"type": "assistant",
                                "message": {"role": "assistant", "content": JARGON}}])
        rc1, b1, _ = self.gate(self.stop_payload())
        self.assertIsNotNone(b1, "第一轮：黑话长文应被打回")
        self.assertIn("落盘", b1["reason"])

        # 成员改写：transcript 追加干净的短回复（旧黑话文仍在文件里、且更长）
        self.append_transcript({"type": "assistant",
                                "message": {"role": "assistant", "content": CLEAN}})
        rc2, b2, _ = self.gate(self.stop_payload())
        self.assertIsNone(b2, "第二轮：改写后的干净回复必须放行（#109 验收）")
        self.assertEqual(rc2, 0)

    def test_c_user_jargon_never_flags(self):
        self.write_transcript([{"type": "user",
                                "message": {"role": "user", "content": JARGON}}])
        rc, b, _ = self.gate(self.stop_payload())
        self.assertIsNone(b, "user 记录里的词不归闸管（只扫 assistant 回复）")

    def test_d_no_assistant_in_tail_fails_open(self):
        self.write_transcript([{"type": "user",
                                "message": {"role": "user", "content": JARGON}}])
        rc, b, err = self.gate(self.stop_payload())
        self.assertIsNone(b, "尾段无 assistant 记录 → fail-open 放行")

    def test_e_payload_reply_field_still_first_path(self):
        rc, b, _ = self.gate({**self.stop_payload(),
                              "response": {"text": JARGON}})
        self.assertIsNotNone(b, "payload 回复字段（第一路）仍生效")

    def test_f_zcode_rollout_third_path_last_assistant(self):
        rollout = os.path.join(self.d, "model-io-session_z.jsonl")
        with open(rollout, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "assistant",
                                "message": {"role": "assistant", "content": JARGON}},
                               ensure_ascii=False) + "\n")
            f.write(json.dumps({"type": "assistant",
                                "message": {"role": "assistant", "content": CLEAN}},
                               ensure_ascii=False) + "\n")
        payload = {"hook_event_name": "Stop", "session_id": "z",
                   "transcript_path": os.path.join(self.d, "missing.jsonl")}
        rc, b, _ = self.gate(payload)
        self.assertIsNone(b, "第三条路（rollout）同样取最后一条 assistant：干净 → 放行")


    # ── GPT round-19 P1 子项：短回复不得丢弃 / last_assistant_message 权威 ──
    def test_g_short_reply_via_payload_not_dropped(self):
        # 旧实现 len>=30 门槛会把短回复丢进尾读 → 错扫旧轮（GPT round-19 P1）
        self.write_transcript([
            {"type": "assistant",
             "message": {"role": "assistant", "content": JARGON}},   # 旧长文
        ])
        payload = {**self.stop_payload(), "last_message": "好的。"}   # 短回复
        rc, b, _ = self.gate(payload)
        self.assertIsNone(b, "短回复也必须被检查（这次它是干净的→放行）")

    def test_g2_short_jargon_reply_still_blocked(self):
        self.write_transcript([
            {"type": "assistant",
             "message": {"role": "assistant", "content": CLEAN}},
        ])
        payload = {**self.stop_payload(), "last_message": "先落盘再说。"}  # 短黑话
        rc, b, _ = self.gate(payload)
        self.assertIsNotNone(b, "短黑话回复同样要拦（门槛不得放走它）")
        self.assertIn("落盘", b["reason"])

    def test_g3_last_assistant_message_is_authoritative(self):
        # claude Stop 官方字段：任意长度、优先于尾读
        self.write_transcript([
            {"type": "assistant",
             "message": {"role": "assistant", "content": JARGON}},   # 尾读会选中它
        ])
        payload = {**self.stop_payload(),
                   "last_assistant_message": CLEAN}                  # 短而干净
        rc, b, _ = self.gate(payload)
        self.assertIsNone(b, "官方字段优先于尾读，短干净回复放行")

    def test_g4_fail_open_leaves_diagnostic_on_stderr(self):
        self.write_transcript([
            {"type": "user", "message": {"role": "user", "content": JARGON}},
        ])
        rc, b, err = self.gate(self.stop_payload())
        self.assertIsNone(b)
        self.assertIn("fail-open", err, "fail-open 必须留 stderr 诊断痕（GPT 要求可见）")


    # ── GPT round-20 P1 验收：旧黑话 assistant 在前 + 尾行不完整 → 不得拿旧文打回 ──
    def test_g5_incomplete_tail_fails_open_not_blocked_with_old_jargon(self):
        self.write_transcript([
            {"type": "assistant",
             "message": {"role": "assistant", "content": JARGON}},   # 旧黑话长文
        ])
        with open(self.transcript, "a", encoding="utf-8") as f:
            f.write('{"type":"assistant","message":{"content":"这半截')   # 无换行，坏 JSON
        rc, b, _ = self.gate(self.stop_payload())
        self.assertIsNone(b, "尾行不完整 → fail-open 放行，不得拿旧黑话文打回")
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
