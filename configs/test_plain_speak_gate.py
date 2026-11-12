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


def _clean_env(extra=None):
    """测试唯一洗表（V1.1.11，板 #2077 §二-4）：原先 Harness.gate 与
    TestHostSkip._run 各抄一份、g14 只取并集——任一调用点单独漂移仍绿
    的后门；现全部调用点共用本函数。名单 = 闸源码实读 15 名（g14 ast
    派生 + g14b 单一入口计数共同钉死）。"""
    e = dict(os.environ, **{"RB_REPAIR_DRYRUN": "1"})
    for k in ("AG_ROLLOUT", "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID",
              "CURSOR_AGENT", "CURSOR_VERSION", "CURSOR_PROJECT_DIR",
              "CURSOR_TRANSCRIPT_PATH", "CURSOR_CONVERSATION_ID",
              "CURSOR_REQUEST_ID", "CURSOR_PLUGIN_ROOT",
              "GROK_HOOK_EVENT", "GROK_HOOK_NAME", "GROK_SESSION_ID",
              "GROK_WORKSPACE_ROOT", "REVIEWBOARD_PLAIN_SPEAK_FORCE"):
        e.pop(k, None)
    if extra:
        e.update(extra)
    return e

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

    def gate(self, payload, env_extra=None):
        # 环境洗收进 _clean_env 单一来源（板 #2077 §二-4；V1.1.4 缺口①的
        # 洗名单不再在此处逐个抄）
        e = _clean_env(env_extra)
        p = subprocess.run([PY, GATE],
                           input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                           capture_output=True, env=e, timeout=30)
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
        # 缺口④(dsh #2037):原实现 fixture 写的文件不在第三条路读路径上=空过;
        # 现以 AG_ROLLOUT 显式指向 fixture——第三条路真读、真判
        rollout = os.path.join(self.d, "rollout-fixture.jsonl")
        with open(rollout, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "assistant",
                                "message": {"role": "assistant", "content": JARGON}},
                               ensure_ascii=False) + "\n")
            f.write(json.dumps({"type": "assistant",
                                "message": {"role": "assistant", "content": CLEAN}},
                               ensure_ascii=False) + "\n")
        payload = {"hook_event_name": "Stop", "session_id": "z",
                   "transcript_path": os.path.join(self.d, "missing.jsonl")}
        rc, b, _ = self.gate(payload, env_extra={"AG_ROLLOUT": rollout})
        self.assertIsNone(b, "第三条路（rollout）同样取最后一条 assistant：干净 → 放行")
        # 反例:乱序(JARGON 在最后)→ 必须拦(证明 fixture 真被读了)
        with open(rollout, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "assistant",
                                "message": {"role": "assistant", "content": CLEAN}},
                               ensure_ascii=False) + "\n")
            f.write(json.dumps({"type": "assistant",
                                "message": {"role": "assistant", "content": JARGON}},
                               ensure_ascii=False) + "\n")
        rc, b, _ = self.gate(payload, env_extra={"AG_ROLLOUT": rollout})
        self.assertIsNotNone(b, "乱序 fixture 真被读取并拦截(反空过)")


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


    # ── GPT round-20 后续(dsh #2041/pi #2042 五缺口收口验证) ──

    def test_g6_window_grows_for_large_reply(self):
        # 缺口⑤(dsh #2041 §一):≥64KB 单条长回复被窗口整条切掉 → 漏检;
        # 渐进窗口(64KB→×4→8MB 封顶)必须把它找回来。
        # GPT round-21 三层验证:①直接调用断言 (text,reason)=(big,"found")——
        # NameError/fail-open 在这一层就会现形;②端到端 stderr 无 fail-open 痕;
        # ③≥64KB 且末尾含黑话的回复必须真拦截(不是异常放行)。
        big = "这批改动的说明已经用日常语言写清楚，没有需要解释的词。" * 1200  # ≈ 66KB+
        self.write_transcript([
            {"type": "assistant", "message": {"role": "assistant", "content": JARGON}},
            {"type": "assistant", "message": {"role": "assistant", "content": big}},
        ])
        import importlib.util
        s = importlib.util.spec_from_file_location("psg_g6", GATE)
        m = importlib.util.module_from_spec(s)
        s.loader.exec_module(m)
        text, reason = m.last_assistant_text_in_tail(self.transcript)
        self.assertEqual(reason, "found", "渐进窗口必须找到长回复")
        self.assertTrue(text.startswith("这批改动的说明"), "找到的必须是长回复本体")
        rc, b, err = self.gate(self.stop_payload())
        self.assertIsNone(b, "≥64KB 的干净长回复必须被找到并放行")
        self.assertEqual(err, "", "干净放行路径本应零 stderr（GPT round-20 §一.2）")

    def test_g6b_large_reply_ending_with_jargon_is_blocked(self):
        # ≥64KB 且末尾含黑话 → 必须真拦截(证明是检查在拦,不是异常放行)
        # V1.1.6(dsh #2052 §三.1):fixture 抬到 ≥64KB——旧 36.1KB 从未进渐进窗口
        big_jargon = ("这段说明先落盘再走治理链路。" * 2200) + \
            "最终把挂进主干的形态定下来。"   # ≈ 92KB,末尾黑话
        self.write_transcript([
            {"type": "assistant",
             "message": {"role": "assistant", "content": big_jargon}},
        ])
        self.assertGreater(os.path.getsize(self.transcript), 65536,
                           "场景前提:fixture ≥ 64KB(进渐进窗口)")
        rc, b, _ = self.gate(self.stop_payload())
        self.assertIsNotNone(b, "≥64KB 且末尾黑话 → 必须拦截")
        self.assertIn("落盘", b["reason"])

    def test_g7_explicit_reply_keys_beat_longer_output(self):
        # 缺口②(dsh):键优先于最长串——response 是当前回复,output 是旧诊断回文
        self.write_transcript([
            {"type": "assistant", "message": {"role": "assistant", "content": CLEAN}},
        ])
        payload = {**self.stop_payload(),
                   "response": {"text": CLEAN},
                   "output": JARGON}          # 更长的旧文在另一键里
        rc, b, _ = self.gate(payload)
        self.assertIsNone(b, "显式回复键优先:不得被另一键里的更长旧文冒充")

    def test_g8_distinct_reasons(self):
        # 缺口③(dsh):三种病因不再合流同一个 None——原因可区分
        import importlib.util
        s = importlib.util.spec_from_file_location("psg_tail", GATE)
        m = importlib.util.module_from_spec(s)
        s.loader.exec_module(m)
        f1 = os.path.join(self.d, "a.jsonl")   # 只有人话/用户记录
        with open(f1, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user",
                                "message": {"role": "user", "content": JARGON}},
                               ensure_ascii=False) + "\n")
        self.assertEqual(m.last_assistant_text_in_tail(f1)[1], "no-assistant-in-file")
        f2 = os.path.join(self.d, "b.jsonl")   # 尾行坏 JSON
        with open(f2, "w", encoding="utf-8") as f:
            f.write(CLEAN_ASSISTANT + "\n")
            f.write('{"broken')
        self.assertEqual(m.last_assistant_text_in_tail(f2)[1], "tail-unparsed")
        f3 = os.path.join(self.d, "c.jsonl")   # 空文件
        open(f3, "w", encoding="utf-8").close()
        self.assertEqual(m.last_assistant_text_in_tail(f3)[1], "empty")
        f4 = os.path.join(self.d, "d.jsonl")   # 正常:found
        with open(f4, "w", encoding="utf-8") as f:
            f.write(CLEAN_ASSISTANT + "\n")
        self.assertEqual(m.last_assistant_text_in_tail(f4)[1], "found")

    def test_g9_fail_open_diagnostic_carries_reason(self):
        # fail-open 的 stderr 诊断痕带可区分原因(GPT round-19:可见;round-20:可分)
        self.write_transcript([
            {"type": "user", "message": {"role": "user", "content": JARGON}},
        ])
        rc, b, err = self.gate(self.stop_payload())
        self.assertIsNone(b)
        self.assertIn("no-assistant-in-file", err,
                      "诊断痕必须带原因代码(不再合流)")

    # ── GPT round-4 P2：容器值的元数据不得冒充正文（V1.1.8） ──
    def test_h_metadata_container_body_beats_id_field(self):
        # dict 插入序里 id/status 排在 text 前——旧递归会先取到 "abc123"；
        # 容器必须取 text 正文：黑话正文 → 打回（取到元数据就会漏放）
        self.write_transcript([
            {"type": "assistant", "message": {"role": "assistant", "content": CLEAN}},
        ])
        payload = {**self.stop_payload(),
                   "response": {"id": "abc123", "status": "completed",
                                "text": JARGON}}
        rc, b, _ = self.gate(payload)
        self.assertIsNotNone(b, "容器值必须取 text 正文——元数据冒充正文=漏检")
        self.assertIn("落盘", b["reason"])

    def test_h2_metadata_only_container_fails_open_with_diag(self):
        # 未识别形状（纯元数据容器）→ 不扫描元数据、fail-open，但必须留诊断痕
        self.write_transcript([
            {"type": "user", "message": {"role": "user", "content": JARGON}},
        ])
        payload = {**self.stop_payload(),
                   "response": {"id": "abc123", "status": "completed"}}
        rc, b, err = self.gate(payload)
        self.assertIsNone(b, "纯元数据容器 → 不扫描、fail-open")
        self.assertIn("未识别出正文", err, "未识别结构必须留 stderr 诊断（GPT round-4 P2）")

    def test_h3_body_in_nonstandard_key_visible_in_diag(self):
        # 板 ⑤b（dsh #2073 定稿）：正文落在 preview 等非消费面键 → 不得静默
        # 丢弃，stderr 必须可见；且不盲选（消费面不动，防键序定成败）
        self.write_transcript([
            {"type": "user", "message": {"role": "user", "content": JARGON}},
        ])
        payload = {**self.stop_payload(), "preview": JARGON}
        rc, b, err = self.gate(payload)
        self.assertIsNone(b, "非消费面键不盲选 → 走尾读/fail-open")
        self.assertIn("不在消费面", err, "正文在非标准键必须留 stderr 诊断（板 ⑤b）")

    # ── 板 #109 终版形状（V1.1.11：dsh #2073 §二/§四 + #2077 §三 + #2078 定稿） ──
    def test_i_b_key_preview_never_bounced(self):
        # ③a（保护性）：B 档键 conversation_preview 永不打回——防实现者把旧文
        # 键请回消费面（修前修后双绿、理由不同；不许单独当判据，板 #2074 §1）
        self.write_transcript([
            {"type": "user", "message": {"role": "user", "content": JARGON}},
        ])
        payload = {**self.stop_payload(), "conversation_preview": JARGON}
        rc, b, _ = self.gate(payload)
        self.assertIsNone(b, "B 档键永不进消费面（板 #2073 §二）")
        self.assertEqual(rc, 0)

    def test_i2_b_key_failopen_diag_visible(self):
        # ③b（判别器）：同形状 stderr 必含 fail-open（修前红/修后绿；只钉子串
        # ——出口文案多处，禁整句）。V1.1.12 修正（板 #2138 §四 test_i2 注释
        # 修正项）：承重灯改钉「不在消费面」键名 token（b-tier-ignored）——
        # c7 后 b-tier 痕独立成行、伞句只留 ③/⑤，fail-open 词不再是承重灯。
        self.write_transcript([
            {"type": "user", "message": {"role": "user", "content": JARGON}},
        ])
        payload = {**self.stop_payload(), "conversation_preview": JARGON}
        rc, b, err = self.gate(payload)
        self.assertIsNone(b)
        self.assertIn("fail-open", err, "③ 出口伞句在位")
        self.assertIn("b-tier-ignored: conversation_preview", err,
                      "承重灯=b-tier token 键名痕（V1.1.12 i2 修正）")
        self.assertIn("不在消费面", err, "B 档诊断必须点名键与档位")

    def test_i3_structured_label_never_scanned(self):
        # ⑦ 标签案（dsh #2070 定稿）：{"type":"text","text":J} 不得取到标签
        # "text"——内容键 text 被取、元数据键被跳过（修后必拦）
        self.write_transcript([
            {"type": "assistant", "message": {"role": "assistant", "content": CLEAN}},
        ])
        payload = {**self.stop_payload(),
                   "response": {"type": "text", "text": JARGON}}
        rc, b, _ = self.gate(payload)
        self.assertIsNotNone(b, "结构化值取正文不为标签（板 ⑦ 修后必拦）")
        self.assertIn("落盘", b["reason"])

    def test_i4_multiblock_joined_like_transcript_path(self):
        # 多块 join（板 #2077 §三/#2078 §2 定稿）：容器内全部 text 块 join，
        # 与 transcript 路同判——后块黑话必拦（「首个非空」修法下本案仍漏，
        # 是三条路里唯一能区分二者的判别器，pi #2078 §3）
        self.write_transcript([
            {"type": "assistant", "message": {"role": "assistant", "content": CLEAN}},
        ])
        payload = {**self.stop_payload(),
                   "response": {"content": [
                       {"type": "text", "text": CLEAN},
                       {"type": "text", "text": JARGON}]}}
        rc, b, _ = self.gate(payload)
        self.assertIsNotNone(b, "多块 join 后块黑话必拦（两路判决一致）")
        self.assertIn("落盘", b["reason"])

    def test_i5_nonmetadata_string_fallback_scanned(self):
        # ⑦ 其余串兜底（pi #2074 §3 定稿）：内容键缺席时，非元数据串按文档序
        # 首个非空兜底进扫描；元数据键（id 等）依旧跳过
        self.write_transcript([
            {"type": "assistant", "message": {"role": "assistant", "content": CLEAN}},
        ])
        payload = {**self.stop_payload(),
                   "response": {"id": "abc123", "note": "先落盘再说。"}}
        rc, b, _ = self.gate(payload)
        self.assertIsNotNone(b, "非元数据串兜底可被扫描（元数据不冒充）")
        self.assertIn("落盘", b["reason"])


class TestTailReasonsAndWash(unittest.TestCase):
    """GPT round-20/21 §三:七态原因、形状族、洗环境表派生(dsh #2052 §三)。"""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = self._dir.name
        sys.path.insert(0, HERE)
        import importlib.util
        s = importlib.util.spec_from_file_location("psg_rw", GATE)
        self.m = importlib.util.module_from_spec(s)
        s.loader.exec_module(self.m)
        self._old_cap = self.m.WINDOW_CAP

    def tearDown(self):
        self.m.WINDOW_CAP = self._old_cap
        self._dir.cleanup()

    def test_g10_cap_reached_reason(self):
        # WINDOW_CAP 压到 64KB + 92KB 文件 → 扩窗在 cap 处停下 → cap-reached
        self.m.WINDOW_CAP = 65536
        f1 = os.path.join(self.d, "cap.jsonl")
        with open(f1, "w", encoding="utf-8") as f:
            f.write(CLEAN_ASSISTANT + "\n")
            f.write('{"type":"assistant","message":{"content":"' + "y" * 91000 + '"}}\n')
        text, reason = self.m.last_assistant_text_in_tail(f1)
        self.assertEqual(reason, "cap-reached")
        self.assertIsNone(text)

    def test_g11_no_file_reason(self):
        missing = os.path.join(self.d, "missing.jsonl")
        self.assertEqual(self.m.last_assistant_text_in_tail(missing)[1], "no-file")

    def test_g15_window_boundary_on_record_edge_keeps_first_line(self):
        # GPT round-5 P2：start>0 且窗口恰好落在记录边界（start-1 是 \n）时，
        # 首行是完整记录，不得无条件丢弃。构造：rec1(用户记录) + rec2(assistant,
        # 恰 65535B)，并把 WINDOW_CAP 压到 64KB——扩窗在 cap 停住、start 恰压在
        # rec2 起点（窗口再大就会 start=0 全读，丢首行无害，故必须钉 cap）。
        # 旧实现丢首行 → 窗内无 assistant 且到 cap → cap-reached(漏检)；
        # 新实现前置字节判界 → found。
        self.m.WINDOW_CAP = 65536   # tearDown 恢复；扩窗到此即停,start 保持 >0
        rec1 = json.dumps({"type": "user",
                           "message": {"role": "user", "content": JARGON}},
                          ensure_ascii=False)
        base = json.dumps({"type": "assistant",
                           "message": {"role": "assistant", "content": CLEAN}},
                          ensure_ascii=False)
        pad = 65535 - len(base.encode("utf-8"))
        self.assertGreater(pad, 0)
        rec2 = json.dumps({"type": "assistant",
                           "message": {"role": "assistant",
                                       "content": CLEAN + "x" * pad}},
                          ensure_ascii=False)
        self.assertEqual(len(rec2.encode("utf-8")), 65535, "测试前提:rec2 恰 65535B")
        f1 = os.path.join(self.d, "edge.jsonl")
        with open(f1, "w", encoding="utf-8", newline="") as f:
            f.write(rec1 + "\n" + rec2 + "\n")
        size = os.path.getsize(f1)
        self.assertEqual(size - 65536, len(rec1.encode("utf-8")) + 1,
                         "测试前提:窗口起点恰在 rec2 边界")
        text, reason = self.m.last_assistant_text_in_tail(f1)
        self.assertEqual(reason, "found",
                         "边界上的完整首行不得被丢弃(旧实现此处 cap-reached)")
        self.assertTrue(text.startswith(CLEAN), "找回的必须是 rec2 正文")

    def test_g12_unreadable_reason(self):
        dpath = os.path.join(self.d, "adir")   # 传目录:open 必失败
        os.makedirs(dpath, exist_ok=True)
        self.assertEqual(self.m.last_assistant_text_in_tail(dpath)[1], "unreadable")

    def test_g13_shape_family_cursor_native(self):
        # 进程内直调 _skip_for_cursor_host 读的是本进程真实 os.environ——
        # CURSOR_AGENT=1 的机器会沿 env 路径误跳过,"显式 Stop 不跳过"断言必红
        # (PR#35 评审 follow-up 1,pre-existing)。先按 _clean_env 同名单清场,
        # 结束恢复;名单须与 _clean_env 一致(g14 派生钉死 15 名)。
        saved = {}
        for k in ("AG_ROLLOUT", "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID",
                  "CURSOR_AGENT", "CURSOR_VERSION", "CURSOR_PROJECT_DIR",
                  "CURSOR_TRANSCRIPT_PATH", "CURSOR_CONVERSATION_ID",
                  "CURSOR_REQUEST_ID", "CURSOR_PLUGIN_ROOT",
                  "GROK_HOOK_EVENT", "GROK_HOOK_NAME", "GROK_SESSION_ID",
                  "GROK_WORKSPACE_ROOT", "REVIEWBOARD_PLAIN_SPEAK_FORCE"):
            saved[k] = os.environ.pop(k, None)

        def _restore():
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v
        self.addCleanup(_restore)
        # cursor 原生形状(无 hook_event_name + status/loop_count)→ 跳过
        self.assertTrue(self.m._skip_for_cursor_host(
            {"loop_count": 1, "status": "completed"}))
        # 同形状 + 显式 hook_event_name=Stop → 不跳过(闸在岗)
        self.assertFalse(self.m._skip_for_cursor_host(
            {"hook_event_name": "Stop", "loop_count": 1, "status": "completed"}))

    def test_g14_wash_list_derived_from_gate_source(self):
        # GPT round-20 §三.5 / dsh #2065 §2:洗环境表从**闸源码 ast 派生**——
        # 旧正则只捕直读字面量(5 枚),循环键族 os.environ.get(key) 是动态名捕不到。
        # 收三类:①os.environ.get("X")/os.environ["X"] 直读字面量
        # ②For 循环绑定的常量元组(cursor/grok 键族) ③.pop("X") 显式洗名;
        # 全大写形状过滤普通字符串。断言:闸真读集 ⊆ 本测试文件洗表——
        # 闸新读一个名字而洗表没跟 = 测试红;数目漂移同样红(钉 dsh #2052 §一)。
        import ast as _ast
        import re as _re

        def _env_names(src):
            names = set()
            for node in _ast.walk(_ast.parse(src)):
                if isinstance(node, _ast.For) and isinstance(node.iter, _ast.Tuple):
                    names.update(e.value for e in node.iter.elts
                                 if isinstance(e, _ast.Constant)
                                 and isinstance(e.value, str))
                if isinstance(node, _ast.Call) and isinstance(node.func, _ast.Attribute) \
                        and node.func.attr == "pop" and node.args \
                        and isinstance(node.args[0], _ast.Constant) \
                        and isinstance(node.args[0].value, str):
                    names.add(node.args[0].value)
                if isinstance(node, _ast.Call) and isinstance(node.func, _ast.Attribute) \
                        and node.func.attr == "get" \
                        and isinstance(node.func.value, _ast.Attribute) \
                        and node.func.value.attr == "environ" and node.args \
                        and isinstance(node.args[0], _ast.Constant) \
                        and isinstance(node.args[0].value, str):
                    names.add(node.args[0].value)
                if isinstance(node, _ast.Call) and isinstance(node.func, _ast.Name) \
                        and node.func.id == "_gate_env" and node.args \
                        and isinstance(node.args[0], _ast.Constant) \
                        and isinstance(node.args[0].value, str):
                    names.add(node.args[0].value)   # V1.1.11 唯一环境入口
                elif isinstance(node, _ast.Subscript) \
                        and isinstance(node.value, _ast.Attribute) \
                        and node.value.attr == "environ" \
                        and isinstance(node.slice, _ast.Constant) \
                        and isinstance(node.slice.value, str):
                    names.add(node.slice.value)
            return {n for n in names if _re.fullmatch(r"[A-Z][A-Z0-9_]*", n)}

        with open(GATE, encoding="utf-8") as f:
            gate_names = _env_names(f.read())
        with open(os.path.abspath(__file__), encoding="utf-8") as f:
            test_names = _env_names(f.read())
        self.assertTrue(gate_names <= test_names,
                        f"闸新读的环境名未进测试洗表: {sorted(gate_names - test_names)}")
        self.assertEqual(len(gate_names), 15,
                         f"闸实读环境名数漂移(dsh #2052 §一 = 15): {sorted(gate_names)}")

    def test_g14b_environ_confined_to_gate_env(self):
        # 板 #2073 §四-4（#2070 §四 方向定稿）：闸源码 os.environ 只准出现在
        # _gate_env() 单一入口——函数体内与**模块级**（含嵌套语句块）出现
        # os.environ / environ.pop / environ.update 即红；洗表派生从此有
        # 单一锚点（V1.1.11）
        import ast as _ast
        with open(GATE, encoding="utf-8") as f:
            tree = _ast.parse(f.read())
        funcs = [n for n in _ast.walk(tree) if isinstance(n, _ast.FunctionDef)]
        inside_fn = {id(n) for fn in funcs
                     if fn.name == "_gate_env" for n in _ast.walk(fn)}
        bad = []
        for node in _ast.walk(tree):
            if id(node) in inside_fn:
                continue
            if isinstance(node, _ast.Attribute) and node.attr == "environ":
                bad.append("os.environ 出现在 _gate_env 之外（模块级或非入口函数）")
            if isinstance(node, _ast.Call) and isinstance(node.func, _ast.Attribute) \
                    and node.func.attr in ("pop", "update") \
                    and isinstance(node.func.value, _ast.Attribute) \
                    and node.func.value.attr == "environ":
                bad.append(f"environ.{node.func.attr}() 出现在 _gate_env 之外")
        self.assertEqual(bad, [], "环境读取必须收进 _gate_env() 单一入口")

    def test_g16_import_shapes_forbidden(self):
        # 板 #2141 §四 v3「getenv 灯＋import 形状四条」：环境读取的绕行通道
        # 全部封死（作用域 GATE 非 __file__——测试文件 import importlib 5 处
        # 属正常）。①from os import … 禁；②import os as o（alias asname）禁；
        # ③getattr(…, "environ") 常量串即红；④__import__/importlib 名字出现即红。
        import ast as _ast
        tree = _ast.parse(open(GATE, encoding="utf-8").read())
        bad = []
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom) and node.module == "os":
                bad.append("from os import …（绕过 _gate_env 判据面）")
            if isinstance(node, _ast.Import):
                for a in node.names:
                    if a.asname:
                        bad.append(f"import {a.name} as {a.asname}（别名绕灯）")
            if isinstance(node, _ast.Call) and isinstance(node.func, _ast.Name) \
                    and node.func.id == "getattr" and len(node.args) >= 2 \
                    and isinstance(node.args[1], _ast.Constant) \
                    and node.args[1].value == "environ":
                bad.append("getattr(…, \"environ\") 常量串（动态绕灯）")
            for attr in ("__import__", "importlib"):
                if isinstance(node, _ast.Name) and node.id == attr:
                    bad.append(f"{attr} 名字出现（动态导入绕灯）")
                if isinstance(node, _ast.Attribute) and node.attr == attr:
                    bad.append(f".{attr} 属性出现（动态导入绕灯）")
        self.assertEqual(bad, [], "闸源码不得存在环境读取的绕行通道")


class TestV1112Shapes(Harness):
    """V1.1.12（板 #110 终版清单实装）：c8 role 桶/哨兵传播/五出口 emit/
    契约三句/X/Y′/§6 格/e5 格/排序灯。断言一律走 harness 三返回元组分通道
    （block 钉 decision 载荷、痕钉 stderr——pi #2095 §4 用例纪律）。"""

    def _load(self):
        import importlib.util
        s = importlib.util.spec_from_file_location("psg_v1112", GATE)
        m = importlib.util.module_from_spec(s)
        s.loader.exec_module(m)
        return m

    def test_j1_t2_role_user_bounced_with_dedicated_bucket(self):
        # c8 主案（dsh #2090 §二）：容器 role=user → 整键跳过进专用桶，
        # 不得落 meta_only 旧句（「只含元数据」与事实相反）。前提：session_id=""
        # ∧ transcript 仅 user ⇒ 落出口③。
        self.write_transcript([{"type": "user",
                                "message": {"role": "user", "content": JARGON}}])
        payload = {**self.stop_payload(),
                   "last_message": {"role": "user", "content": JARGON}}
        rc, b, err = self.gate(payload)
        self.assertIsNone(b)
        self.assertIn("role_skipped: last_message", err, "c8 专用桶+键名")
        self.assertNotRegex(err, r"未识别出正文[^\n]*\blast_message\b",
                            "按键负向：meta_only 旧句不得吃掉本键（板 #2095 §三）")

    def test_j2_non_str_role_same_bucket(self):
        # c8 一致性锁（pi #2093 §2）：role=5 非 str 同判同桶同痕。
        self.write_transcript([{"type": "user",
                                "message": {"role": "user", "content": JARGON}}])
        payload = {**self.stop_payload(),
                   "last_message": {"role": 5, "content": JARGON}}
        rc, b, err = self.gate(payload)
        self.assertIsNone(b)
        self.assertIn("role_skipped: last_message", err)

    def test_j3_role_assistant_still_blocked(self):
        # c8 不变式（dsh #2090 §二-2）：role=assistant 的容器照常取正文打回。
        self.write_transcript([{"type": "assistant",
                                "message": {"role": "assistant", "content": CLEAN}}])
        payload = {**self.stop_payload(),
                   "last_message": {"role": "assistant", "content": JARGON}}
        rc, b, _ = self.gate(payload)
        self.assertIsNotNone(b, "role=assistant 不得被 role 判据误放")
        self.assertIn("落盘", b["reason"])

    def test_j4_d1_propagation_through_list(self):
        # d1（pi #2093 §3）：哨兵沿 list 传播——嵌套块角色不符 → 整键跳过
        # 进专用桶（负向断言锁的是传播完整性，不是白给）。
        self.write_transcript([{"type": "user",
                                "message": {"role": "user", "content": JARGON}}])
        payload = {**self.stop_payload(),
                   "last_message": {"content": [{"role": "user", "content": JARGON}]}}
        rc, b, err = self.gate(payload)
        self.assertIsNone(b)
        self.assertIn("role_skipped: last_message", err)
        self.assertNotRegex(err, r"未识别出正文[^\n]*\blast_message\b")

    def test_j5_d2_whole_key_dropped_with_clean_part(self):
        # d2（pi #2093 §4 定稿：整键跳过）——text:CLEAN 与 role=user 容器并存：
        # CLEAN 随整键丢（不传播=CLEAN 进扫描而黑话无声消失=扫错对象零痕）。
        self.write_transcript([{"type": "user",
                                "message": {"role": "user", "content": JARGON}}])
        payload = {**self.stop_payload(),
                   "last_message": {"text": CLEAN,
                                    "content": {"role": "user", "content": JARGON}}}
        rc, b, err = self.gate(payload)
        self.assertIsNone(b)
        self.assertIn("role_skipped: last_message", err)

    def test_j6_identity_sentinel_direct_call(self):
        # 身份锁直调（pi #2093 §2/§5.1）：哨兵以 is/is not 判定；extract_text
        # 出口绝不把哨兵漏给调用方。
        m = self._load()
        self.assertIs(m._body_text({"role": "user", "content": JARGON}),
                      m._ROLE_SKIPPED)
        self.assertIsNot(m._body_text({"role": "assistant", "content": CLEAN}),
                         m._ROLE_SKIPPED)
        self.assertIsNot(m.extract_text({"hook_event_name": "Stop"}),
                         m._ROLE_SKIPPED)

    def test_j7_guard_no_role_judgement_in_type_dimension(self):
        # guard 断言（pi #2095 §7）：块只有 type 没有 role → 照常取正文；
        # 锁「别把记录级谓词搬进递归面」。
        self.write_transcript([{"type": "assistant",
                                "message": {"role": "assistant", "content": CLEAN}}])
        payload = {**self.stop_payload(),
                   "last_message": {"content": [{"type": "text", "text": JARGON}]}}
        rc, b, err = self.gate(payload)
        self.assertIsNotNone(b, "type 判据不得进容器递归面（板 #2094 §二）")
        self.assertNotIn("role_skipped", err, "无 role 键 ⇒ role_skipped 桶必须为空")
        self.assertIn("落盘", b["reason"])

    def test_j8_dual_verdict_same_dict_two_paths(self):
        # 双面断言（pi #2093 §7/#2095 §7 反例两翼）：同一 dict，
        # transcript 记录语义取正文、payload 容器语义出哨兵——两期望钉死。
        m = self._load()
        rec = {"type": "assistant", "role": "user",
               "content": [{"type": "text", "text": CLEAN}]}
        self.assertEqual(m._assistant_text(rec), CLEAN)
        self.assertIs(m._body_text(rec), m._ROLE_SKIPPED)

    def test_j9_multi_key_whole_span_sorted(self):
        # 多键格（板 #2112 §2 定稿）：整段钉兼排序唯一直接灯。前提：本格
        # 桶键集恰为 payload 所列（docstring 承诺）。
        self.write_transcript([{"type": "user",
                                "message": {"role": "user", "content": JARGON}}])
        payload = {**self.stop_payload(),
                   "last_message": {"role": "user", "content": JARGON},
                   "assistant_message": {"role": "user", "content": JARGON}}
        rc, b, err = self.gate(payload)
        self.assertIsNone(b)
        self.assertIn("role_skipped: assistant_message, last_message", err)

    def test_j10_sorted_swap_byte_identical(self):
        # sorted 确定性（板 #2104 §2 I3）：键互换 JSON 序 → stderr 逐字节同。
        self.write_transcript([{"type": "user",
                                "message": {"role": "user", "content": JARGON}}])
        inner = {"role": "user", "content": JARGON}
        p1 = {**self.stop_payload(), "message": inner, "preview": JARGON}
        p2 = {**self.stop_payload(), "preview": JARGON, "message": inner}
        _, _, e1 = self.gate(p1)
        _, _, e2 = self.gate(p2)
        self.assertEqual(e1, e2, "桶行序与键序均不得随 payload 插入序漂移")

    def test_j11_answer_never_collected(self):
        # 收集面上界（板 #2104 §3 I4）：不命中收集面正则 ⇒ 不扫、不进桶、零痕。
        self.write_transcript([{"type": "assistant",
                                "message": {"role": "assistant", "content": CLEAN}}])
        payload = {**self.stop_payload(), "answer": JARGON, "response": CLEAN}
        rc, b, err = self.gate(payload)
        self.assertIsNone(b)
        self.assertEqual(err, "", "收集面外键零痕（反向禁令的正面格）")

    def test_j12_I1_lam_exit_outside_contract(self):
        # I1（板 #2104 §1）：出口①（lam 早退）在收集面之前 = 契约外——
        # 桶未建，stderr 全空；docstring 明写，防「无痕可丢」误判成缺陷。
        self.write_transcript([{"type": "user",
                                "message": {"role": "user", "content": JARGON}}])
        payload = {**self.stop_payload(),
                   "last_assistant_message": CLEAN,
                   "last_message": {"role": "user", "content": JARGON}}
        rc, b, err = self.gate(payload)
        self.assertIsNone(b)
        self.assertEqual(err, "", "出口①契约外（本格桶未建）")

    def test_j13_I2_block_still_carries_bucket_line(self):
        # I2（板 #2104 §1）：block 载荷同样带跳过行（「allow∧」前缀去掉成立）。
        self.write_transcript([{"type": "assistant",
                                "message": {"role": "assistant", "content": CLEAN}}])
        payload = {**self.stop_payload(),
                   "last_message": {"role": "user", "content": CLEAN},
                   "response": JARGON}
        rc, b, err = self.gate(payload)
        self.assertIsNotNone(b)
        self.assertIn("落盘", b["reason"])
        self.assertIn("role_skipped: last_message", err)

    def test_j14_c3_message_key_never_consumed(self):
        # c3（dsh #2090 §一 定稿：消费面保持 4 名）：message 键永不进消费面；
        # 前提钉形状（session_id="" ∧ 尾读无 assistant）——有 sid 且 rollout
        # 可读时走 return best，痕不出现。
        self.write_transcript([{"type": "user",
                                "message": {"role": "user", "content": JARGON}}])
        payload = {**self.stop_payload(), "message": JARGON}
        rc, b, err = self.gate(payload)
        self.assertIsNone(b)
        self.assertIn("b-tier-ignored: message", err)

    def test_j15_X_exit4_silent_without_buckets(self):
        # case X（dsh #2114 §三/§2116 §二）：出口④ + 零桶键 ⇒ err==""。
        # **锁现状、非红→绿**，同时是反向禁令的守卫（谁把 ④ 扩成整份 diag
        # 谁红）。反向禁令正反两句成对：允许出口④用例级 assertEqual(err,"")；
        # 禁止推广成 allow ⇒ 无 stderr（c/d/g5/g4/g9 五格假红）。
        missing = os.path.join(self.d, "missing.jsonl")
        rollout = os.path.join(self.d, "rollout-x.jsonl")
        with open(rollout, "w", encoding="utf-8") as f:
            f.write(CLEAN_ASSISTANT + "\n")
        payload = {"hook_event_name": "Stop", "stop_hook_active": False,
                   "session_id": "z", "transcript_path": missing}
        rc, b, err = self.gate(payload, env_extra={"AG_ROLLOUT": rollout})
        self.assertIsNone(b)
        self.assertEqual(err, "", "出口④零桶键 ⇒ 零字节（原因整批不发）")

    def test_j16_Yprime_exit4_bucket_without_reason(self):
        # case Y′（pi #2115 §2 钉 sid="z"；dsh #2116 §三）：出口④ + b-tier 桶
        # ⇒ 桶行必发、原因必不发。assertNotIn("fail-open") 是本格唯一的
        # 出口④判别器——它红了先查 sid/transcript 前提，再谈改断言。
        missing = os.path.join(self.d, "missing.jsonl")
        rollout = os.path.join(self.d, "rollout-y.jsonl")
        with open(rollout, "w", encoding="utf-8") as f:
            f.write(CLEAN_ASSISTANT + "\n")
        payload = {"hook_event_name": "Stop", "stop_hook_active": False,
                   "session_id": "z", "transcript_path": missing,
                   "preview": JARGON}
        rc, b, err = self.gate(payload, env_extra={"AG_ROLLOUT": rollout})
        self.assertIsNone(b)
        self.assertIn("b-tier-ignored: preview", err)
        self.assertNotIn("fail-open", err, "出口④不带伞句")
        self.assertNotIn("尾读未取得回复", err, "出口④原因整批不发")

    def test_j17_sec2_grid_four_lines(self):
        # §6 格（板 #2108 §2 定稿三件断言）：③ 出口、桶+双原因并存——
        # 伞句行/b-tier 行/transcript 原因/rollout 原因各一行；三件 =
        # 桶 token 行恰 1 ∧ 该行不含 fail-open ∧ 桶 token 全文恰 1 次。
        self.write_transcript([{"type": "user",
                                "message": {"role": "user", "content": JARGON}}])
        rollout = os.path.join(self.d, "rollout-user-only.jsonl")
        with open(rollout, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user",
                                "message": {"role": "user", "content": JARGON}},
                               ensure_ascii=False) + "\n")
        payload = {"hook_event_name": "Stop", "stop_hook_active": False,
                   "session_id": "s6", "transcript_path": self.transcript,
                   "preview": JARGON}
        rc, b, err = self.gate(payload, env_extra={"AG_ROLLOUT": rollout})
        self.assertIsNone(b)
        rows = [l for l in err.splitlines() if "b-tier-ignored" in l]
        self.assertEqual(len(rows), 1, "独立行灯：桶 token 行恰一")
        self.assertNotIn("fail-open", rows[0], "该行不含伞句词")
        self.assertEqual(err.count("b-tier-ignored"), 1, "桶 token 全文恰一次")
        self.assertIn("rollout 尾读未取得回复", err)
        self.assertIn("b-tier-ignored: preview", err)

    def test_j18_e5_exit5_umbrella_present(self):
        # ⑤ 格（板 #2108 §1：出口⑤此前零用例；A6 判别对象=变体D）：
        # sid 非空 + 两路尾读皆失败 ⇒ 伞句在位（fail-open 词）＋桶行＋原因行。
        self.write_transcript([{"type": "user",
                                "message": {"role": "user", "content": JARGON}}])
        rollout = os.path.join(self.d, "rollout-user-only-e5.jsonl")
        with open(rollout, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user",
                                "message": {"role": "user", "content": JARGON}},
                               ensure_ascii=False) + "\n")
        payload = {"hook_event_name": "Stop", "stop_hook_active": False,
                   "session_id": "e5", "transcript_path": self.transcript,
                   "preview": JARGON}
        rc, b, err = self.gate(payload, env_extra={"AG_ROLLOUT": rollout})
        self.assertIsNone(b)
        self.assertIn("fail-open", err, "A6：出口⑤伞句定义性在位")
        self.assertIn("b-tier-ignored: preview", err)


class TestHostSkip(unittest.TestCase):
    """GPT round-21 P2:宿主跳过逻辑(Cursor/Grok)独立测试——六用例。
    (此前该类头在提交中丢失,六用例沦为模块级死代码——本轮补回。)"""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.d = self._dir.name
        self.transcript = os.path.join(self.d, "transcript.jsonl")
        fixture = ("这段改动把配置读写收进一个入口，退出时统一落盘。")
        # V1.1.7(dsh #2065 §1 闭合点①):牙钉——fixture 必含词典词「落盘」且
        # 无解释标记,三条 skip 用例的 assertIsNone 才有判别力;fixture 换成
        # 干净正文时本断言立即红,防六用例退化成空过
        self.assertIn("落盘", fixture)
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "assistant",
                                "message": {"role": "assistant",
                                            "content": fixture}},
                               ensure_ascii=False) + "\n")

    def tearDown(self):
        self._dir.cleanup()

    def _run(self, env_extra):
        # V1.1.7(dsh #2065 §1 闭合点②):改回全返回 (rc, b, err)——b=None 有
        # 两种病因(宿主跳过生效 / 取文失败 fail-open),只回 body 分不开;
        # 与 Harness.gate 同一返回形状。洗表并入 _clean_env(板 #2077 §二-4,
        # 第三份洗表消灭——原「随本表并入 gate() 后消灭」注释兑现)
        payload = {"hook_event_name": "Stop", "stop_hook_active": False,
                   "session_id": "hs", "transcript_path": self.transcript}
        e = _clean_env(env_extra)
        r = subprocess.run([PY, GATE],
                           input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                           capture_output=True, env=e, timeout=30)
        out = r.stdout.decode("utf-8", "replace").strip()
        err = r.stderr.decode("utf-8", "replace")
        try:
            return r.returncode, (json.loads(out) if out else None), err
        except ValueError:
            return r.returncode, None, err

    def test_plain_env_gate_runs(self):
        rc, b, err = self._run({})
        self.assertIsNotNone(b, "普通环境(Claude Code)闸必须在岗")
        self.assertEqual(rc, 0)

    def test_cursor_env_skips(self):
        rc, b, err = self._run({"CURSOR_AGENT": "1"})
        self.assertIsNone(b, "Cursor 宿主跳过(用户已要求 cursor 不做该检查)")
        self.assertEqual(err, "", "跳过=原样放过去,零 stderr")

    def test_cursor_hook_env_skips(self):
        rc, b, err = self._run({"CURSOR_TRANSCRIPT_PATH": "x"})
        self.assertIsNone(b, "Cursor hook 环境标记同样跳过")
        self.assertEqual(err, "")

    def test_grok_env_skips(self):
        rc, b, err = self._run({"GROK_SESSION_ID": "x"})
        self.assertIsNone(b, "Grok 宿主跳过")
        self.assertEqual(err, "")

    def test_force_overrides_skip(self):
        rc, b, err = self._run({"CURSOR_AGENT": "1",
                                "REVIEWBOARD_PLAIN_SPEAK_FORCE": "1"})
        self.assertIsNotNone(b, "FORCE=1 强制启用(覆盖宿主跳过)")
        self.assertEqual(rc, 0)

    def test_both_markers_force_wins(self):
        rc, b, err = self._run({"CURSOR_AGENT": "1", "GROK_SESSION_ID": "x",
                                "REVIEWBOARD_PLAIN_SPEAK_FORCE": "1"})
        self.assertIsNotNone(b, "双宿主标记同时存在:FORCE 仍强制启用(行为明确)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
