# A2A Prompt-Serving — Test Harness Extension

**Status:** Approved design, pending implementation plan.
**Date:** 2026-05-05
**Owner:** shiva@ephapsys.com
**Scope:** `ephapsys-samples/agents/a2a_test_harness.py` only.

---

## 1. Motivation

The existing `a2a_test_harness.py` covers four protocol scenarios (T1.1, T1.2, T2.1, T8.1) that exercise the A2A messaging bus, broadcast, guardrails, and journal completeness. None of them invoke an agent's actual model. The wire protocol for *prompt-serving* — one agent asking another to run inference and return the result — already exists and works interactively in `helloworld-{a,b,c}/a2a_peer.py`, but has no automated test coverage.

This spec adds a second test phase to the harness that exercises real model inference end-to-end: driver agent **D** sends a `tool_call` to server agent **S**, **S** runs `TrustedAgent.run(text, model_kind="language")`, and replies with a `tool_result` carrying the inference output. Three new test cases (T3.1, T3.2, T3.3) cover the happy path, the unknown-tool error reply, and the no-responder timeout.

## 2. Wire protocol (already exists, lifted from `a2a_peer.py:127-162`)

| Direction | `message_type` | `payload` shape |
|-----------|---------------|----------------|
| driver → responder | `"tool_call"` | `{"tool": "<name>", "args": {...}}` |
| responder → driver | `"tool_result"` | `{"ok": True, "tool": "<name>", "result": <any>}` *or* `{"ok": False, "tool": "<name>", "error": "<class>: <msg>"}` *or* `{"ok": False, "error": "unknown tool: '<name>'"}` |

Both messages share the same `correlation_id` (format `"req-<10 hex>"` for the harness; existing T-tests use `"t<NN>-<8 hex>"`). For T3.x we only use `tool="language.respond"`, but the dispatch table is keyed by tool name so adding more later is one line.

## 3. Architecture

Single-process orchestrator with these roles:

| Role | Count | What it owns |
|------|-------|--------------|
| Org client | 1 | `A2AClient.from_env()` — shared by everyone |
| Responder worker | N ≥ 1 (one per `--responder-dir`) | A `TrustedAgent` instance, one daemon poller thread running `c.process_inbox(agent_id=ME, on_verified=dispatch)` in a loop |
| Driver | 1 (= first `--responder-dir`) | Same as a responder, plus responsibility for sending test asks and waking PENDING entries on `tool_result` arrival |
| Idle target | 0 or 1 (`--idle-dir`) | Just an `agent_id` string read from `.ephapsys_state/agent_id`. No `TrustedAgent`, no poller. Used only by T3.3. |

Shared state (one mutex):

```python
PENDING: dict[str, dict] = {}     # cid -> {event: threading.Event, payload: dict|None, sent_at: float}
PENDING_LOCK = threading.Lock()
stop_event = threading.Event()

@dataclass
class Responder:
    ref: str                       # public_id; used for from_/to_agent_id
    state_dir: Path
    agent: TrustedAgent            # post-verify(), post-prepare_runtime()
    is_driver: bool = False
    poller: Optional[threading.Thread] = None
```

### Concurrency invariants

- All `PENDING` mutations go through `PENDING_LOCK`.
- Test bodies run on the main thread *only*; poller threads never run a test, only dispatch.
- Pollers ack every message they handle (matches `a2a_peer.py` and current harness behavior).
- At most one outstanding `PENDING` entry at a time in this iteration (no T3.5 parallel asks).
- `A2AClient` makes fresh `requests.get/post` per call (no shared session), so concurrent pollers don't race on HTTP state.

## 4. CLI (additive, fully backward-compatible)

```
--env PATH                # existing; defaults to ./helloworld-a/.env
--cluster-id ID           # existing
--responder-dir PATH      # NEW, repeatable. First is driver. T3.x require ≥2.
--idle-dir PATH           # NEW, optional. T3.3 target. Omitted → T3.3 SKIP.
```

If neither new flag is given, the harness behaves exactly as today (T1.1, T1.2, T2.1, T8.1 only).

## 5. Lifecycle — two phases

The harness's pollers will conflict with the existing T1/T2/T8 tests if a `--responder-dir` ref is also a cluster member used by those tests (which is the realistic setup — helloworld-a/b/c double as cluster members and responders). The pollers would ack T1.1's payload before `find_by_correlation` sees it.

**Solution: two phases.** Pollers exist only during phase 2.

```
phase 1 (no pollers):  T1.1, T1.2, T2.1, T8.1     ← unchanged from today
[load models, drain inboxes, start pollers]
phase 2 (pollers up):  T3.1, T3.2, T3.3
[stop pollers]
```

Side benefit: cheap network tests fail fast before paying the model-load cost.

### Bootstrap sequence in `main()`

1. Parse args, `load_env(args.env)`, build `c = A2AClient.from_env()`.
2. Existing cluster discovery (unchanged).
3. **Phase 1**: run T1.1, T1.2, T2.1, T8.1 as today.
4. **If `--responder-dir` given** (phase 2 setup):
   1. Build `did_map = build_did_to_ref_map(c)` once.
   2. `responders = [build_responder(d, did_map, i == 0) for i, d in enumerate(args.responder_dir)]`.
   3. **Eager pre-load**: for each responder, print `[loading model for {ref}...]`, call `r.agent.verify()` then `r.agent.prepare_runtime()`. Fail fast on any error (exit 2).
   4. **Drain inboxes** before pollers start: `for r in responders: drain_inbox(c, r.ref)`. If `--idle-dir` is given, drain its inbox too.
   5. Start one daemon poller thread per responder.
5. **If `--idle-dir` given**: `idle_ref = did_map[raw] if raw.startswith("did:") else raw`. No `TrustedAgent`, no poller.
6. Run T3.1, T3.2, T3.3 (with SKIP guards from §8).
7. **Teardown**: `stop_event.set()`; `for r in responders: r.poller.join(timeout=5.0)`; return existing failure-count exit code.

### Operator-visible startup output

```
aoc       = https://api.staging.ephapsys.ai
cluster   = clu_abc123
  sender  (A) = helloworld-a
  primary (B) = helloworld-b
  third   (C) = helloworld-c

[PASS] T1.1 direct P2P — verified, sender_status=ok
[PASS] T1.2 broadcast + self-skip — sent=2, all recipients delivered, sender skipped
[PASS] T2.1 guardrail block — blocked, pattern='ignore previous instructions'
[PASS] T8.1 journal completeness — summary=..., journal lines=2

[loading model for helloworld-a...] (driver)
[loading model for helloworld-b...]
[idle target: helloworld-c]
[pollers up: helloworld-a, helloworld-b]

[PASS] T3.1 prompt-serving happy — result_len=42, elapsed=2.1s
[PASS] T3.2 unknown tool — err='unknown tool: \'frobnicate.gizmo\'', elapsed=0.92s
[PASS] T3.3 timeout no responder — timed out cleanly after 5.02s

---- 7/7 passed ----
```

## 6. Components

### 6.1 Builders (lifted patterns from `a2a_peer.py:49-78`)

```python
def build_did_to_ref_map(c) -> dict[str, str]:
    """GET /agents → {did: public_id} for the org."""
    ...

def read_agent_id(state_root: Path) -> str:
    return (state_root / ".ephapsys_state" / "agent_id").read_text().strip()

def build_responder(state_root: Path, did_map: dict, is_driver: bool) -> Responder:
    raw = read_agent_id(state_root)
    ref = did_map[raw] if raw.startswith("did:") else raw
    agent = TrustedAgent(
        agent_id=ref,
        api_base=os.environ["AOC_BASE_URL"],
        storage_dir=str(state_root / ".ephapsys_state"),
        verify_ssl=os.environ.get("AOC_VERIFY_SSL", "1") != "0",
    )
    return Responder(ref=ref, state_dir=state_root, agent=agent, is_driver=is_driver)
```

### 6.2 Dispatch (per-responder closure)

```python
def make_on_verified(r: Responder):
    def _cb(msg: dict) -> None:
        mtype = (msg.get("message_type") or "").lower()
        if mtype == "tool_call":
            handle_tool_call(r, msg)
        elif mtype == "tool_result" and r.is_driver:
            handle_tool_result(msg)
        # else: ignore (broadcasts/events from earlier tests are dropped silently)
    return _cb

def handle_tool_call(r: Responder, msg: dict) -> None:
    payload = msg.get("payload") or {}
    tool = str(payload.get("tool") or "")
    args = payload.get("args") or {}
    cid = msg.get("correlation_id") or ""
    sender = msg.get("from_agent_id") or ""

    if tool not in TOOLS:
        result = {"ok": False, "error": f"unknown tool: {tool!r}"}
    else:
        try:
            result = {"ok": True, "tool": tool, "result": TOOLS[tool](r, args)}
        except Exception as exc:
            result = {"ok": False, "tool": tool, "error": f"{type(exc).__name__}: {exc}"}

    c.send_message(
        from_agent_id=r.ref, to_agent_id=sender,
        payload=result, message_type="tool_result", correlation_id=cid,
    )

def handle_tool_result(msg: dict) -> None:
    cid = msg.get("correlation_id") or ""
    with PENDING_LOCK:
        entry = PENDING.get(cid)
        if entry is None:
            return                              # stale or unknown reply → drop
        entry["payload"] = msg.get("payload") or {}
    entry["event"].set()

TOOLS = {
    "language.respond": lambda r, args: r.agent.run(str(args.get("text", "")), model_kind="language"),
}
```

### 6.3 Poller (one daemon thread per responder, mirrors `a2a_peer.py:177-196`)

```python
POLL_SECONDS = 1.0   # harness-internal, tighter than a2a_peer.py's 2s

def poller_loop(r: Responder) -> None:
    journal = MessageJournal(path=str(r.state_dir / "harness_journal.jsonl"))
    on_verified = make_on_verified(r)
    while not stop_event.is_set():
        try:
            c.process_inbox(
                agent_id=r.ref, journal=journal,
                ack_rejected=True, on_verified=on_verified,
            )
        except Exception as exc:
            print(f"[poll error {r.ref}] {exc}")
        stop_event.wait(POLL_SECONDS)
```

### 6.4 Test-side primitive

```python
def request_and_wait(
    *, requester: str, responder: str, tool: str, args: dict, timeout: float,
) -> tuple[dict | None, float]:
    """Send a tool_call from `requester` to `responder`, wait up to `timeout` for the
    matching tool_result. Returns (payload, elapsed). payload is None on timeout."""
    cid = f"req-{uuid.uuid4().hex[:10]}"
    event = threading.Event()
    with PENDING_LOCK:
        PENDING[cid] = {"event": event, "payload": None, "sent_at": time.time()}
    c.send_message(
        from_agent_id=requester, to_agent_id=responder,
        payload={"tool": tool, "args": args},
        message_type="tool_call", correlation_id=cid,
    )
    event.wait(timeout)
    with PENDING_LOCK:
        entry = PENDING.pop(cid, None)
    elapsed = time.time() - (entry["sent_at"] if entry else time.time())
    return (entry["payload"] if entry else None), elapsed
```

## 7. T3.x test bodies

### 7.1 T3.1 — happy path

```python
def test_t3_1_happy(D: str, S: str) -> tuple[bool, str]:
    """D asks S to run language.respond; expects ok=True with non-empty string."""
    payload, elapsed = request_and_wait(
        requester=D, responder=S,
        tool="language.respond",
        args={"text": "Reply with one short sentence."},
        timeout=15.0,
    )
    if payload is None:
        return False, "no tool_result within 15s"
    if not payload.get("ok"):
        return False, f"server reported error: {payload.get('error')!r}"
    result = payload.get("result")
    if not isinstance(result, str) or not result.strip():
        return False, f"result not a non-empty string: {result!r}"
    return True, f"result_len={len(result)}, elapsed={elapsed:.1f}s"
```

### 7.2 T3.2 — unknown tool

```python
def test_t3_2_unknown_tool(D: str, S: str) -> tuple[bool, str]:
    """D asks for a tool S doesn't have; expects ok=False with 'unknown tool' error."""
    payload, elapsed = request_and_wait(
        requester=D, responder=S,
        tool="frobnicate.gizmo", args={},
        timeout=15.0,
    )
    if payload is None:
        return False, "no tool_result within 15s"
    if payload.get("ok") is not False:
        return False, f"expected ok=False, got {payload!r}"
    err = (payload.get("error") or "").lower()
    if "unknown tool" not in err:
        return False, f"unexpected error: {payload.get('error')!r}"
    return True, f"err={payload.get('error')!r}, elapsed={elapsed:.2f}s"
```

### 7.3 T3.3 — timeout no responder

```python
def test_t3_3_timeout(D: str, idle_ref: str) -> tuple[bool, str]:
    """D asks an idle agent (no poller); expects no reply within 5s."""
    payload, elapsed = request_and_wait(
        requester=D, responder=idle_ref,
        tool="language.respond", args={"text": "anyone home?"},
        timeout=5.0,
    )
    if payload is not None:
        return False, f"unexpected reply: {payload!r}"
    if elapsed < 4.5 or elapsed > 6.5:
        return False, f"elapsed={elapsed:.2f}s outside [4.5, 6.5]"
    return True, f"timed out cleanly after {elapsed:.2f}s"
```

### Why no journal assertions in T3.x

Existing **T8.1** already proves journal completeness for tool messages on the inbox path. Re-asserting it in every T3.x adds boilerplate without catching new bugs. T3.x focuses on round-trip semantics; T8.1 stays the journal sentinel.

## 8. SKIP guards in `main()`

```python
if len(responders) >= 2:
    D, S = responders[0].ref, responders[1].ref
    tests.append(("T3.1 prompt-serving happy",  lambda: test_t3_1_happy(D, S)))
    tests.append(("T3.2 unknown tool",          lambda: test_t3_2_unknown_tool(D, S)))
    if idle_ref:
        tests.append(("T3.3 timeout no responder", lambda: test_t3_3_timeout(D, idle_ref)))
    else:
        print("[SKIP] T3.3 — needs --idle-dir")
elif args.responder_dir:
    print(f"[SKIP] T3.x — needs ≥2 --responder-dir (got {len(args.responder_dir)})")
```

Skipped tests don't enter the `tests` list — the existing `passed/total` summary tallies only attempted cases. SKIP lines are visible in stdout.

## 9. Error matrix

| Failure | Where | Behavior |
|---------|-------|----------|
| `verify()` / `prepare_runtime()` raises | startup (phase 2 setup) | Print, exit 2 — abort before any poller starts |
| `process_inbox` raises mid-loop | poller thread | Caught in `poller_loop`'s `try/except` → log + continue |
| `agent.run()` raises | inside `handle_tool_call` | Caught → reply `{"ok": False, "error": "<class>: <msg>"}` |
| `send_message` 4xx | test body | Propagates → existing top-level `try/except` in main test loop marks test FAIL |
| `process_inbox` blocks message (guardrail / sender revoked) | runtime | Never reaches `on_verified`; driver times out (this is what T3.3 exploits) |
| Late `tool_result` after timeout | runtime | `handle_tool_result` finds no `PENDING[cid]` → drops silently |
| Ctrl-C | runtime | `try/finally` in `main()` → `stop_event.set()` → pollers exit on next 1s tick |

## 10. File diff scope

Single file: `ephapsys-samples/agents/a2a_test_harness.py`.

| Section | LoC delta |
|---------|-----------|
| Imports (`dataclass`, `threading`, `json`, `TrustedAgent`) | +5 |
| Module-level state (`PENDING`, `PENDING_LOCK`, `stop_event`, `Responder`) | +20 |
| Builders (`build_did_to_ref_map`, `read_agent_id`, `build_responder`) | +30 |
| Dispatch (`make_on_verified`, `handle_tool_call`, `handle_tool_result`, `request_and_wait`) | +60 |
| Poller (`poller_loop`) | +15 |
| New CLI args + bootstrap block in `main()` | +40 |
| Phase-2 startup/teardown in `main()` | +25 |
| T3.1 / T3.2 / T3.3 test bodies | +35 |
| **Total** | **~230 lines added; existing code unchanged** |

Net file size: 285 → ~515 lines. Single file remains the right structure; revisit if it crosses ~700.

## 11. Runbook (replaces current Usage docstring)

```
Phase 1 only (existing behavior):
    python a2a_test_harness.py --env helloworld-a/.env --cluster-id <id>

Phase 1 + 2 (new — adds T3.1/T3.2/T3.3):
    python a2a_test_harness.py \
        --env helloworld-a/.env \
        --responder-dir helloworld-a \
        --responder-dir helloworld-b \
        --idle-dir       helloworld-c

Required: pre-personalized state dirs (run quickstart.sh in each
folder first). Recommended: stop any a2a_peer.py instances — they
share inboxes with the harness's responder pollers.
```

## 12. Risks + mitigations

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Model load fails for one responder (e.g., GCS auth issue) | Med | Eager pre-load → fail fast with clear error before any T3 test runs |
| Poller thread silently dies (uncaught exception type) | Low | Defensive `try/except Exception` already in `poller_loop` |
| `agent.run()` is slow (> 15s on cold inference graph) | Med | If observed, upgrade to single warmup ping post-load. Not implementing now. |
| Late `tool_result` arrives after T3.3's pop | High | `handle_tool_result` checks `PENDING.get(cid) is None` and drops; harmless |
| `requests` thread-safety across pollers | Low | `A2AClient` creates fresh `requests.get/post` calls per invocation (no shared session) → independent connections per thread |
| Operator runs harness while `a2a_peer.py` is also running | Med | Module docstring warns; restate next to `--responder-dir` docs |

## 13. Dependencies

None new. All used modules (`threading`, `dataclasses`, `json`, `TrustedAgent`, `MessageJournal`, `A2AClient`, `requests`) are already imported either by the harness or by `a2a_peer.py`.

## 14. Out of scope (deferred)

- T3.4 guardrail-on-tool-call interaction (has a real spec question: silent-drop vs `ok=False` reply for guardrail-blocked tool_calls).
- T3.5 concurrent fan-out — `request_and_wait` is built to handle multiple outstanding `PENDING` entries, but no concurrent test exercises that yet.
- T3.6 bidirectional (B asks A back) — driver-as-responder design supports it; no test yet.
- Folding `send_message` / `process_message` into `TrustedAgent` itself per `ephapsys-platform/docs/A2A_MCP.md`.
- MCP-side (`list_tools`, `serve_mcp`, `ToolRegistry` mixin).

## 15. Decisions log

| # | Question | Decision |
|---|----------|----------|
| 1 | Where do responder identities come from? | (c) CLI arg `--responder-dir` repeatable, points at pre-personalized state dirs |
| 2 | Who is the requester? | (a) Driver = first `--responder-dir`; symmetric (also serves) |
| 3 | Scope of new tests | Tier 2: T3.1 happy, T3.2 unknown tool, T3.3 timeout |
| 4 | Model load lifecycle | (a) Eager pre-load; timeouts 15s happy / 5s no-reply |
| 5 | T3.3 unreachable target | (a) Explicit `--idle-dir`; SKIP if omitted |
| — | Implementation approach | (A) Thread-per-responder polling `PENDING` dict with `threading.Event` |
| — | Phase ordering | Two-phase: T1/T2/T8 first (no pollers), then start pollers, then T3.x |
