# Scope & Positioning

> What this project is, what it deliberately is not, and how to tell the difference from other multi-agent tools.

## The four-layer boundary

"Agents working together" conflates four distinct problems. They are often named interchangeably; they are not the same problem, and they do not need to live in the same tool:

| Layer | The question it answers |
|---|---|
| **Collaboration** | How do agents communicate, share context, and divide work? |
| **Coordination** | How do agents avoid collisions, stale state, and duplicate actions? |
| **Decision-oriented sessions** | How do agents convene, discuss options, and record what was decided? |
| **Governance** | Under what conditions is a conclusion *valid* — and how is that process constrained, audited, and traceable? |

MCP Review Board is focused on the governance layer. It ships no agent runtime, no chat, no workspace, no memory, no task scheduler, and no communication fabric — by design. It integrates with any MCP-capable agent or compatible agent workspace/CLI.

## What this project enforces

The board does not merely *collect* opinions. The server itself enforces, rejects, invalidates, and records the conditions under which a thread can resolve:

- an **objection** must state its *flip condition* — what evidence would change the verdict — or the server rejects it;
- a **revision** clears all previous verdicts — stale passes cannot auto-approve new work;
- **resolution** requires unanimous pass across the active quorum (frozen members' votes don't count; a standing objection reopens the thread);
- **participation** is gated by liveness (activity reinstates frozen members);
- every vote, flip, revision and identity event is **append-only audited**.

"Unresolved" is itself a recorded, distinguishable state — not the absence of a record.

## What this project deliberately does not do

- agent runtime, CLI harness, or device/phone integration;
- chat, DMs, group messaging, or team workspace;
- memory, persona, or agent lifecycle management;
- work assignment, kanban, calendaring, or wake-up scheduling of your agents;
- anything requiring credentials for third-party model providers.

These belong to the layers above. Bundling them here would make the governance layer *less* portable, not more useful.

## How to tell whether another tool overlaps this one

Ask one question: **does it enforce review-governance semantics** —

1. objections that must carry falsifiable flip conditions,
2. revision invalidating prior verdicts,
3. quorum-controlled resolution,
4. an auditable, append-only decision trail —

— as *server-enforced* behavior, rather than as conventions in a chat stream or the output of an LLM judging a transcript?

If yes, it is a governance peer. If it only provides communication, coordination, work assignment, or decision *sessions* (even with recorded outcomes), it is a neighboring layer — complementary, and a place where this board can be useful rather than a competitor.

## An honest note on moats

The structural difference between "a decision-session recorder" and "a server-enforced review state machine" is real, but it is not by itself a moat. A moat would require that real users *need* the enforced semantics — that "why did we accept A instead of B?" matters to them, that unrecorded disagreement costs them something — and that replicating the semantics costs more than adopting the protocol. That is exactly what the board's own audited review history (see [docs/self-review.md](self-review.md)) is meant to test, on this project first.
