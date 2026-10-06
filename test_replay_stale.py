#!/usr/bin/env python3
"""Replay the exported board history against the verdict-staleness rule (v2.5).

Fixture: tests/fixtures/history-2026-10-02.json (tools/export_history.py,
structure-only + pseudonymized — no titles/bodies/notes; comment ids kept).

PINNED RULE (thread #33 acceptance; pi #362 demanded a written-down timing
definition or the replay is not reproducible):
  A quorum member is STALE iff their CURRENT standing verdict is 'object' AND
  ≥ STALE_MIN_FOLLOWUP comments BY THAT MEMBER (their own continued posting)
  landed strictly after the object's landing time (verdicts.updated_at of the
  standing row). A re-verdict resets the window by moving updated_at.
  Why OWN posts: others' posts already wake the holder via new_comments; the
  #30 blind spot was precisely the holder talking (text-pass) while the vote
  stayed object. On thread #30 this pins the first trigger EXACTLY at #324
  (m6's object 13:15:14 -> own followups #319, #324; the other two objects
  never reach 2 own followups before flipping).

Acceptance:
  1. Thread #30 replay: first trigger fires at comment id 324 (no earlier).
  2. Noise budget (claude #350's falsifiable line): among TRANSIENT object
     episodes OUTSIDE the documented incident (thread #30 m6 — the expected
     true positive), at most 1/3 may fire. Episode = object landing -> flip
     (transient) or thread end (standing).

Phase-0 mode: until server._stale_holders_pure exists, this suite runs the
fixture sanity checks + prints analysis from a reference copy of the rule and
SKIPS the acceptance assertions (fixtures-first discipline).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
FIXTURE = REPO / "tests" / "fixtures" / "history-2026-10-02.json"

# Reference copy of the pinned rule (Phase 0); the acceptance run MUST use
# server._stale_holders_pure so test and production cannot drift.
STALE_MIN_FOLLOWUP = 2


def ref_stale_holders(quorum, verdicts, verdict_updated, own_comments):
    holders = []
    for m in quorum:
        if verdicts.get(m) == "object":
            landed = verdict_updated.get(m, "9999")
            n = sum(1 for ts in own_comments.get(m, []) if ts > landed)
            if n >= STALE_MIN_FOLLOWUP:
                holders.append(m)
    return holders


def replay_thread(t, rule):
    """Walk the merged (comments + verdict_events) timeline in ts order
    (verdict events before comments on ties). Returns per-episode stats:
    fires[{(member): first_fire_comment_id}], transient_episodes,
    transient_fires, standing_fires. bump_revision clears all verdicts
    (server G2: DELETE FROM verdicts)."""
    events = [(c["ts"], 1, c["id"], c["author"], None) for c in t["comments"]]
    for e in t["verdict_events"]:
        if e["author"] == "system":
            continue  # auto_resolve/auto_reopen audit rows carry no member verdict
        if e["action"] == "bump_revision":
            events.append((e["ts"], 0, 0, e["author"], "bump"))
        elif e["to_v"] in ("pass", "object"):
            events.append((e["ts"], 0, 0, e["author"], e["to_v"]))
    events.sort()

    verdicts: dict[str, str] = {}
    landed: dict[str, str] = {}
    own_ts: dict[str, list[str]] = {}
    fired: dict[str, int] = {}
    first_fire = None
    transient_episodes = transient_fires = standing_fires = 0

    def episode_end(m):
        nonlocal transient_episodes, transient_fires
        transient_episodes += 1
        if m in fired:
            transient_fires += 1

    for ts, is_comment, cid, who, v in events:
        if not is_comment:
            if v == "bump":
                for m in [m for m, v2 in verdicts.items() if v2 == "object"]:
                    episode_end(m)
                verdicts, landed, own_ts = {}, {}, {}
                continue
            m, to = who, v
            if to == "object":
                if verdicts.get(m) == "object":
                    episode_end(m)  # re-object closes and reopens the episode
                verdicts[m] = "object"
                landed[m] = ts
                own_ts.setdefault(m, [])
                fired.pop(m, None)
            else:
                if verdicts.get(m) == "object":
                    episode_end(m)
                verdicts[m] = to
                landed.pop(m, None)
                fired.pop(m, None)
        else:
            m = who
            own_ts.setdefault(m, []).append(ts)
            # evaluate state AFTER this comment, as of this moment
            holders = rule(t["quorum"], verdicts, landed, own_ts)
            for h in holders:
                if h not in fired:
                    fired[h] = cid
                    if first_fire is None:
                        first_fire = cid
    for m, v2 in verdicts.items():
        if v2 == "object":
            if m in fired:
                standing_fires += 1
    return {
        "first_fire": first_fire,
        "fires": dict(fired),
        "transient_episodes": transient_episodes,
        "transient_fires": transient_fires,
        "standing_fires": standing_fires,
    }


def main() -> int:
    fails = 0

    def check(label, ok):
        nonlocal fails
        print(("PASS" if ok else "FAIL"), label)
        if not ok:
            fails += 1

    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    threads = {t["id"]: t for t in doc["threads"]}

    # --- fixture sanity (always asserted) ---
    check("fixture: >=30 threads", len(doc["threads"]) >= 30)
    t30 = threads.get(30)
    check("fixture: thread 30 present", t30 is not None)
    ids30 = {c["id"] for c in t30["comments"]}
    check("fixture: thread 30 holds #319/#324/#327",
          all(i in ids30 for i in (319, 324, 327)))
    cjk = any("\u4e00" <= ch <= "\u9fff" for ch in FIXTURE.read_text(encoding="utf-8"))
    check("fixture: no CJK leakage (pseudonymized/structure-only)", not cjk)

    # --- pick the rule implementation ---
    sys.path.insert(0, str(REPO))
    try:
        import server  # noqa: E402
        rule = server._stale_holders_pure
        src = "server._stale_holders_pure"
    except (ImportError, AttributeError):
        rule = ref_stale_holders
        src = "reference copy (Phase 0)"

    # --- analysis over all threads ---
    stats = {tid: replay_thread(t, rule) for tid, t in threads.items() if t["quorum"]}
    for tid, s in sorted(stats.items()):
        if s["fires"] or s["transient_episodes"]:
            print(f"  thread {tid}: first_fire={s['first_fire']} fires={s['fires']} "
                  f"transient_eps={s['transient_episodes']} "
                  f"transient_fires={s['transient_fires']} standing_fires={s['standing_fires']}")

    if src.startswith("reference"):
        print("SKIP acceptance (Phase 0: server._stale_holders_pure not implemented yet "
              f"— rule used: {src}; analysis above is informational)")
        return 0

    # --- acceptance 1: thread #30 fires exactly at #324, nothing earlier ---
    s30 = stats[30]
    check("acceptance: thread #30 first trigger == comment 324", s30["first_fire"] == 324)

    # --- acceptance 2: noise budget outside the documented incident (#30 m6) ---
    # m6 is the pseudonym of the incident holder; identify it as the member the
    # #30 fire is attributed to, then exclude that one episode from noise.
    incident_member = next(iter(s30["fires"]))
    noise_fires = sum(s["transient_fires"] for tid, s in stats.items() if tid != 30)
    noise_fires += s30["transient_fires"] - 1  # minus the incident episode itself
    transient_total = sum(s["transient_episodes"] for s in stats.values())
    ratio = (noise_fires / transient_total) if transient_total else 0.0
    print(f"  noise: {noise_fires}/{transient_total} transient episodes fired (ratio {ratio:.2f})")
    check("acceptance: noise budget <= 1/3 outside the incident", ratio <= 1 / 3)

    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
