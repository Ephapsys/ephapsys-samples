# A2A Prompt-Serving — Test Harness Extension Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `ephapsys-samples/agents/a2a_test_harness.py` with three new tests (T3.1, T3.2, T3.3) that exercise real prompt-serving — driver agent D sends a `tool_call` to server agent S, S runs `TrustedAgent.run(text, model_kind="language")`, and replies with a `tool_result` carrying inference output.

**Architecture:** Single-process orchestrator. Existing tests (T1.1, T1.2, T2.1, T8.1) run as **Phase 1** with no pollers. New tests run as **Phase 2** with one daemon poller thread per `--responder-dir`, dispatching `tool_call` → real model inference and `tool_result` → wake the matching `PENDING[correlation_id]` entry. First `--responder-dir` is the driver. Optional `--idle-dir` is a real agent_id with no poller, used as the unreachable target for T3.3. See `docs/superpowers/specs/2026-05-05-a2a-prompt-serving-design.md` for full design rationale.

**Tech Stack:** Python 3.10+, `threading`, `dataclasses`, `ephapsys.a2a.A2AClient`, `ephapsys.TrustedAgent`, `ephapsys.journal.MessageJournal`. No new dependencies.

---

## File Structure

**Single file modified:** `/home/shiva/Documents/Ephapsys/product/ephapsys-samples/agents/a2a_test_harness.py` (currently 284 lines, ~515 after).

The file is currently divided into these sections (line numbers from the present file):

| Lines | Section |
|-------|---------|
| 1–22 | Module docstring (Usage block — gets updated in Task 7) |
| 23–34 | Imports |
| 37–50 | `load_env` helper |
| 53–89 | Inbox helpers (`cluster_member_refs`, `drain_inbox`, `find_by_correlation`, `has_correlation`) |
| 92–217 | Existing test bodies (T1.1, T1.2, T2.1, T8.1) |
| 220–284 | `main()` |

**No new files.** All additions go into the same file. Insertion points are called out by line range or "after section X" in each task.

**Why one file:** the harness is a single-purpose script with no unit-test scaffolding in the `ephapsys-samples` repo. Splitting it into modules adds import overhead with no payoff. Spec §10 has the full LoC breakdown.

**Working directory for all commands:** `/home/shiva/Documents/Ephapsys/product/ephapsys-samples/agents/`

---

## Task 1: Add CLI args and module-level state

**Goal:** Add `--responder-dir` (repeatable), `--idle-dir`, plus the shared dispatch state. No behavior wired in yet — purely scaffolding so subsequent tasks have somewhere to plug into.

**Files:**
- Modify: `/home/shiva/Documents/Ephapsys/product/ephapsys-samples/agents/a2a_test_harness.py:23-34` (add imports)
- Modify: same file, after line 34 (add module state + dataclass)
- Modify: same file, `main()` argparse block at lines 222–234 (add flags)

- [ ] **Step 1: Add new imports**

Open the file. Replace lines 23–34 (current import block) with:

```python
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

from ephapsys import TrustedAgent
from ephapsys.a2a import A2AClient
from ephapsys.journal import MessageJournal
```

(`json`, `threading`, `dataclass`, `field`, `TrustedAgent` are new; the rest already exist.)

- [ ] **Step 2: Add module-level state and `Responder` dataclass**

Insert immediately after the imports (before the existing `INJECTION_TEXT = ...` line):

```python
POLL_SECONDS = 1.0      # harness-internal poller interval; tighter than a2a_peer.py's 2s
PENDING: dict = {}      # correlation_id -> {"event": threading.Event, "payload": dict|None, "sent_at": float}
PENDING_LOCK = threading.Lock()
stop_event = threading.Event()


@dataclass
class Responder:
    ref: str                                       # public_id; used for from_/to_agent_id
    state_dir: Path
    agent: TrustedAgent
    is_driver: bool = False
    poller: Optional[threading.Thread] = field(default=None)
```

- [ ] **Step 3: Add the new CLI flags**

In `main()`, locate the argparse block (currently lines 222–234). After the existing `--cluster-id` argument, add:

```python
    parser.add_argument(
        "--responder-dir",
        action="append",
        default=[],
        help="path to a personalized agent dir (repeatable). first dir is the driver. "
             "T3.x tests require at least 2 --responder-dir flags.",
    )
    parser.add_argument(
        "--idle-dir",
        default=None,
        help="path to a personalized agent dir used as T3.3's unreachable target "
             "(read agent_id only, no model load, no poller). T3.3 SKIPs if omitted.",
    )
```

- [ ] **Step 4: Verify the file imports and `--help` shows the new flags**

Run from the `agents/` directory:

```bash
python a2a_test_harness.py --help 2>&1 | head -40
```

Expected output includes lines containing `--responder-dir` and `--idle-dir`. The script should exit 0. If you see `ImportError`, your venv is missing the SDK — install with `pip install -e ../../ephapsys-sdk/sdk/python` first.

- [ ] **Step 5: Verify existing tests still run unchanged (smoke check, no flags passed)**

This step requires a configured `helloworld-a/.env` and a live cluster. If you don't have access right now, **skip the run and just verify the script parses**:

```bash
python -c "import ast; ast.parse(open('a2a_test_harness.py').read()); print('parse OK')"
```

Expected: `parse OK`.

If you do have staging access:

```bash
python a2a_test_harness.py --env helloworld-a/.env
```

Expected: T1.1, T1.2, T2.1, T8.1 each report PASS (or whatever they reported before this change — this task adds no behavior).

- [ ] **Step 6: Commit**

```bash
cd /home/shiva/Documents/Ephapsys/product/ephapsys-samples
git add agents/a2a_test_harness.py
git commit -m "harness: add --responder-dir/--idle-dir flags and dispatch state scaffolding

No behavior change yet; existing tests run as before. Adds Responder
dataclass, PENDING dict, PENDING_LOCK, and stop_event in preparation
for the prompt-serving test phase."
```

---

## Task 2: Add identity helpers and `Responder` builder

**Goal:** Functions that read `agent_id` from a state dir, resolve `did:` to `public_id` via `GET /agents`, and construct a `Responder`. Lifted from `a2a_peer.py:49-78` patterns.

**Files:**
- Modify: same file, insertion point: after the existing inbox helpers section (after line 89, the `has_correlation` function).

- [ ] **Step 1: Add the three identity helpers**

After the closing of `has_correlation` (line 89), insert the following block. The existing `# --- tests ---` comment block at line 92 stays where it is (these new helpers go *above* the tests).

```python
# --- identity helpers (Phase 2 setup) -------------------------------------

def build_did_to_ref_map(client: A2AClient) -> dict[str, str]:
    """GET /agents and build {did: public_id} for the org. Mirrors a2a_peer.py:49-64."""
    import requests
    r = requests.get(
        f"{client.base_url}/agents",
        headers={"Authorization": f"Bearer {client.token}"},
        timeout=client.timeout,
    )
    r.raise_for_status()
    out: dict[str, str] = {}
    for a in r.json() or []:
        did = a.get("did") or (a.get("identity") or {}).get("did") or ""
        ref = a.get("public_id") or a.get("label") or a.get("ID") or a.get("_id") or a.get("id")
        if did and ref:
            out[str(did)] = str(ref)
    return out


def read_agent_id(state_root: Path) -> str:
    """Read .ephapsys_state/agent_id from a personalized helloworld-* dir."""
    path = state_root / ".ephapsys_state" / "agent_id"
    if not path.exists():
        raise SystemExit(f"agent_id file not found: {path} — run quickstart.sh in {state_root}?")
    return path.read_text().strip()


def resolve_ref(raw: str, did_map: dict[str, str]) -> str:
    """did: → public_id; pass-through if already a ref."""
    if raw.startswith("did:"):
        if raw not in did_map:
            raise SystemExit(f"could not resolve {raw} via /agents — is it in this org?")
        return did_map[raw]
    return raw


def build_responder(state_root: Path, did_map: dict[str, str], is_driver: bool) -> Responder:
    """Construct a Responder bound to a pre-personalized state dir.

    Does NOT call verify() or prepare_runtime() — those happen in main()'s Phase 2
    setup so failures are reported with the right context line.
    """
    raw = read_agent_id(state_root)
    ref = resolve_ref(raw, did_map)
    agent = TrustedAgent(
        agent_id=ref,
        api_base=os.environ["AOC_BASE_URL"],
        storage_dir=str(state_root / ".ephapsys_state"),
        verify_ssl=os.environ.get("AOC_VERIFY_SSL", "1") != "0",
    )
    return Responder(ref=ref, state_dir=state_root, agent=agent, is_driver=is_driver)
```

- [ ] **Step 2: Verify the file still imports cleanly**

```bash
python -c "import ast; ast.parse(open('a2a_test_harness.py').read()); print('parse OK')"
```

Expected: `parse OK`.

- [ ] **Step 3: Commit**

```bash
cd /home/shiva/Documents/Ephapsys/product/ephapsys-samples
git add agents/a2a_test_harness.py
git commit -m "harness: add identity helpers (DID map, agent_id reader, Responder builder)

Patterns lifted from helloworld-a/a2a_peer.py:49-78. Builders do not
call verify()/prepare_runtime() — that's deferred to main()'s Phase 2
setup so model-load errors are reported with the right context."
```

---

## Task 3: Add dispatch logic, `request_and_wait`, and `poller_loop`

**Goal:** The runtime core — per-responder `on_verified` closure, `tool_call` handler that runs real inference, `tool_result` handler that wakes `PENDING`, the `request_and_wait` test primitive, the daemon poller loop, and the `TOOLS` registry.

**Files:**
- Modify: same file, insert after the identity helpers added in Task 2 (still above the `# --- tests ---` block).

- [ ] **Step 1: Add the dispatch + handler block**

Insert directly after the closing brace of `build_responder` from Task 2:

```python
# --- dispatch (Phase 2 runtime) -------------------------------------------

def _run_language_tool(r: Responder, args: dict) -> str:
    text = str(args.get("text", ""))
    return r.agent.run(text, model_kind="language")


# Tool registry. Keyed by tool name; each handler takes (Responder, args) -> result.
TOOLS = {
    "language.respond": _run_language_tool,
}


def make_on_verified(r: Responder):
    """Per-responder process_inbox callback. Closes over `r` so each poller
    dispatches into its own TrustedAgent."""
    def _cb(msg: dict) -> None:
        mtype = (msg.get("message_type") or "").lower()
        if mtype == "tool_call":
            handle_tool_call(r, msg)
        elif mtype == "tool_result" and r.is_driver:
            handle_tool_result(msg)
        # else: events/broadcasts from earlier tests are dropped silently
    return _cb


def handle_tool_call(r: Responder, msg: dict) -> None:
    """Server side: look up tool, run model, send tool_result back to sender."""
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

    # Use the module-global A2AClient; main() assigns it before any poller starts.
    c.send_message(
        from_agent_id=r.ref, to_agent_id=sender,
        payload=result, message_type="tool_result", correlation_id=cid,
    )


def handle_tool_result(msg: dict) -> None:
    """Driver side: match correlation_id, stash payload, wake the waiter."""
    cid = msg.get("correlation_id") or ""
    with PENDING_LOCK:
        entry = PENDING.get(cid)
        if entry is None:
            return                              # stale or unknown reply → drop
        entry["payload"] = msg.get("payload") or {}
    entry["event"].set()


def request_and_wait(
    *, requester: str, responder: str, tool: str, args: dict, timeout: float,
) -> Tuple[Optional[dict], float]:
    """Send a tool_call from `requester` to `responder`, wait up to `timeout`
    for the matching tool_result. Returns (payload, elapsed). payload is None
    on timeout; elapsed is wall time from just-before-send to wake-or-timeout."""
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


def poller_loop(r: Responder) -> None:
    """One daemon thread per responder. Polls inbox, dispatches via on_verified.
    Mirrors helloworld-a/a2a_peer.py:177-196."""
    journal = MessageJournal(path=str(r.state_dir / "harness_journal.jsonl"))
    on_verified = make_on_verified(r)
    while not stop_event.is_set():
        try:
            c.process_inbox(
                agent_id=r.ref,
                journal=journal,
                ack_rejected=True,
                on_verified=on_verified,
            )
        except Exception as exc:
            print(f"[poll error {r.ref}] {exc}")
        stop_event.wait(POLL_SECONDS)
```

Note: `c` is a module global in the existing harness — it's currently set inside `main()` at line 242 (`c = A2AClient.from_env()`). Task 4 promotes it to a true module global so handlers can close over it. For now, the file parses fine even though `c` is referenced before assignment; nothing here runs at import time.

- [ ] **Step 2: Verify the file still imports cleanly**

```bash
python -c "import ast; ast.parse(open('a2a_test_harness.py').read()); print('parse OK')"
```

Expected: `parse OK`.

- [ ] **Step 3: Commit**

```bash
cd /home/shiva/Documents/Ephapsys/product/ephapsys-samples
git add agents/a2a_test_harness.py
git commit -m "harness: add dispatch, request_and_wait, and poller_loop

Per-responder on_verified closure dispatches tool_call → real model
inference (TOOLS registry, currently just language.respond) and
tool_result → PENDING wake-up. request_and_wait is the primitive every
T3.x test will use. poller_loop is the daemon-thread body.

Not wired into main() yet — that's Task 4."
```

---

## Task 4: Wire Phase 2 setup into `main()` and add T3.1 (happy path)

**Goal:** First end-to-end test. After existing T1/T2/T8 finish, the harness loads models, drains inboxes, starts pollers, runs T3.1, and tears down cleanly.

**Files:**
- Modify: same file, `main()` function (currently lines 220–284).

- [ ] **Step 1: Promote `c` to a module global**

Currently in `main()` at line 242:

```python
    c = A2AClient.from_env()
```

We need `c` available to `handle_tool_call`, `request_and_wait`, and `poller_loop` — all at module scope. Add a module-level forward declaration near the other module state. At the top of the file, on the line immediately after `stop_event = threading.Event()` (added in Task 1, Step 2), append:

```python
c: A2AClient = None  # type: ignore[assignment]   # set by main() before any poller starts
```

Then in `main()`, change line 242 from:

```python
    c = A2AClient.from_env()
```

to:

```python
    global c
    c = A2AClient.from_env()
```

- [ ] **Step 2: Add the T3.1 test body**

Insert immediately after the existing `test_t8_1_journal` function (currently ends around line 217), still inside the `# --- tests ---` section:

```python
def test_t3_1_happy(D: str, S: str) -> Tuple[bool, str]:
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

- [ ] **Step 3: Add the Phase 2 setup block in `main()`**

After the existing `tests = [...]` list (currently lines 259–264) and the test-loop `for name, fn in tests` block (currently 267–276), but **before** the trailing `print(f"---- ...")` summary line (currently 279), insert the Phase 2 block.

Locate this code in current `main()` (around lines 257–280):

```python
    tests = [
        ("T1.1 direct P2P",            lambda: test_t1_1_direct_p2p(c, A, B, C)),
        ("T1.2 broadcast + self-skip", lambda: test_t1_2_broadcast(c, cluster_id, A, B, C)),
        ("T2.1 guardrail block",       lambda: test_t2_1_guardrail(c, A, B)),
        ("T8.1 journal completeness",  lambda: test_t8_1_journal(c, A, B)),
    ]

    failures = 0
    for name, fn in tests:
        try:
            ok, msg = fn()
        except Exception as exc:
            ok, msg = False, f"unhandled exception: {exc!r}"
        tag = "PASS" if ok else "FAIL"
        suffix = f" — {msg}" if msg else ""
        print(f"[{tag}] {name}{suffix}")
        if not ok:
            failures += 1

    print()
    print(f"---- {len(tests) - failures}/{len(tests)} passed ----")
    return 0 if failures == 0 else 1
```

Replace it with this expanded version (which keeps the Phase 1 logic intact and adds Phase 2 between Phase 1's run loop and the summary):

```python
    # --- Phase 1: existing tests, no pollers --------------------------
    phase1_tests = [
        ("T1.1 direct P2P",            lambda: test_t1_1_direct_p2p(c, A, B, C)),
        ("T1.2 broadcast + self-skip", lambda: test_t1_2_broadcast(c, cluster_id, A, B, C)),
        ("T2.1 guardrail block",       lambda: test_t2_1_guardrail(c, A, B)),
        ("T8.1 journal completeness",  lambda: test_t8_1_journal(c, A, B)),
    ]

    # --- Phase 2: build responders, pre-load models, start pollers ----
    responders: list[Responder] = []
    idle_ref: Optional[str] = None
    phase2_tests: list = []

    if args.responder_dir or args.idle_dir:
        did_map = build_did_to_ref_map(c)
        responders = [
            build_responder(Path(d), did_map, is_driver=(i == 0))
            for i, d in enumerate(args.responder_dir)
        ]
        if args.idle_dir:
            idle_ref = resolve_ref(read_agent_id(Path(args.idle_dir)), did_map)

    if len(responders) >= 2:
        D, S = responders[0].ref, responders[1].ref
        phase2_tests.append(("T3.1 prompt-serving happy", lambda: test_t3_1_happy(D, S)))
    elif args.responder_dir:
        print(f"[SKIP] T3.x — needs ≥2 --responder-dir (got {len(args.responder_dir)})")

    # --- run Phase 1 --------------------------------------------------
    failures = 0
    for name, fn in phase1_tests:
        try:
            ok, msg = fn()
        except Exception as exc:
            ok, msg = False, f"unhandled exception: {exc!r}"
        tag = "PASS" if ok else "FAIL"
        suffix = f" — {msg}" if msg else ""
        print(f"[{tag}] {name}{suffix}")
        if not ok:
            failures += 1

    # --- Phase 2 setup (only if we have any phase2 tests) -------------
    if phase2_tests:
        print()
        for r in responders:
            tag = " (driver)" if r.is_driver else ""
            print(f"[loading model for {r.ref}...]{tag}")
            try:
                r.agent.verify()
                r.agent.prepare_runtime()
            except Exception as exc:
                print(f"  FAILED: {type(exc).__name__}: {exc}")
                return 2
        if idle_ref:
            print(f"[idle target: {idle_ref}]")
            drain_inbox(c, idle_ref)
        for r in responders:
            drain_inbox(c, r.ref)
        for r in responders:
            t = threading.Thread(target=poller_loop, args=(r,), daemon=True)
            t.start()
            r.poller = t
        print(f"[pollers up: {', '.join(r.ref for r in responders)}]")
        print()

        try:
            for name, fn in phase2_tests:
                try:
                    ok, msg = fn()
                except Exception as exc:
                    ok, msg = False, f"unhandled exception: {exc!r}"
                tag = "PASS" if ok else "FAIL"
                suffix = f" — {msg}" if msg else ""
                print(f"[{tag}] {name}{suffix}")
                if not ok:
                    failures += 1
        finally:
            stop_event.set()
            for r in responders:
                if r.poller is not None:
                    r.poller.join(timeout=5.0)

    total = len(phase1_tests) + len(phase2_tests)
    print()
    print(f"---- {total - failures}/{total} passed ----")
    return 0 if failures == 0 else 1
```

- [ ] **Step 4: Verify the file still parses**

```bash
python -c "import ast; ast.parse(open('a2a_test_harness.py').read()); print('parse OK')"
```

Expected: `parse OK`.

- [ ] **Step 5: Smoke test against staging**

This step requires live staging access and pre-personalized `helloworld-a` and `helloworld-b` instances. If you don't have access, **skip the run** and rely on the parse check from Step 4 — Task 8 will run the full smoke test after all T3.x are in.

If you do have access:

```bash
cd /home/shiva/Documents/Ephapsys/product/ephapsys-samples/agents
python a2a_test_harness.py \
    --env helloworld-a/.env \
    --responder-dir helloworld-a \
    --responder-dir helloworld-b
```

Expected output (after the existing T1/T2/T8 lines):

```
[loading model for helloworld-a...] (driver)
[loading model for helloworld-b...]
[pollers up: helloworld-a, helloworld-b]

[PASS] T3.1 prompt-serving happy — result_len=<some>, elapsed=<some>s

---- 5/5 passed ----
```

If T3.1 fails with `no tool_result within 15s`: confirm helloworld-b can run inference standalone (`./run_local.sh` in helloworld-b/ should produce text output). If that works, increase the 15s timeout temporarily and re-run; consistent slowness here is the warmup-ping signal mentioned in spec §12.

- [ ] **Step 6: Commit**

```bash
cd /home/shiva/Documents/Ephapsys/product/ephapsys-samples
git add agents/a2a_test_harness.py
git commit -m "harness: add Phase 2 setup and T3.1 (prompt-serving happy path)

After Phase 1 finishes, harness builds Responders from --responder-dir
flags, eagerly pre-loads each responder's model, drains inboxes, and
starts one daemon poller thread per responder. T3.1 sends a tool_call
from the driver and asserts ok=True with a non-empty string result
within 15s. Existing tests are unchanged.

Promotes c to a module global so dispatch handlers can use it."
```

---

## Task 5: Add T3.2 (unknown tool)

**Goal:** Test that the responder's error-reply path works — `frobnicate.gizmo` is not in `TOOLS`, so `handle_tool_call` returns `{"ok": False, "error": "unknown tool: ..."}`.

**Files:**
- Modify: same file, add `test_t3_2_unknown_tool` after `test_t3_1_happy`; append it to `phase2_tests` in `main()`.

- [ ] **Step 1: Add the T3.2 test body**

Immediately after `test_t3_1_happy` (added in Task 4), add:

```python
def test_t3_2_unknown_tool(D: str, S: str) -> Tuple[bool, str]:
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

- [ ] **Step 2: Append T3.2 to `phase2_tests` in `main()`**

In `main()`, find the block from Task 4 that reads:

```python
    if len(responders) >= 2:
        D, S = responders[0].ref, responders[1].ref
        phase2_tests.append(("T3.1 prompt-serving happy", lambda: test_t3_1_happy(D, S)))
    elif args.responder_dir:
```

Add a single line so it becomes:

```python
    if len(responders) >= 2:
        D, S = responders[0].ref, responders[1].ref
        phase2_tests.append(("T3.1 prompt-serving happy", lambda: test_t3_1_happy(D, S)))
        phase2_tests.append(("T3.2 unknown tool",         lambda: test_t3_2_unknown_tool(D, S)))
    elif args.responder_dir:
```

- [ ] **Step 3: Verify the file parses**

```bash
python -c "import ast; ast.parse(open('a2a_test_harness.py').read()); print('parse OK')"
```

Expected: `parse OK`.

- [ ] **Step 4: Smoke test (optional, requires live staging)**

```bash
python a2a_test_harness.py \
    --env helloworld-a/.env \
    --responder-dir helloworld-a \
    --responder-dir helloworld-b
```

Expected: `[PASS] T3.2 unknown tool — err="unknown tool: 'frobnicate.gizmo'", elapsed=<small>s` and overall `6/6 passed`.

- [ ] **Step 5: Commit**

```bash
cd /home/shiva/Documents/Ephapsys/product/ephapsys-samples
git add agents/a2a_test_harness.py
git commit -m "harness: add T3.2 (unknown tool error reply)

Asserts the server returns ok=False with an 'unknown tool' error when
asked for a tool not in the TOOLS registry. Exercises the error-reply
path in handle_tool_call without involving the model."
```

---

## Task 6: Add T3.3 (timeout) and idle-dir handling

**Goal:** Last new test. Send a `tool_call` to an agent that exists in the org but has no poller running — assert no reply within 5s and clean PENDING cleanup.

**Files:**
- Modify: same file, add `test_t3_3_timeout` after `test_t3_2_unknown_tool`; append to `phase2_tests` with a SKIP guard.

- [ ] **Step 1: Add the T3.3 test body**

After `test_t3_2_unknown_tool`, add:

```python
def test_t3_3_timeout(D: str, idle_ref: str) -> Tuple[bool, str]:
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

- [ ] **Step 2: Append T3.3 to `phase2_tests` with SKIP guard**

In `main()`, the block from Task 5 currently reads:

```python
    if len(responders) >= 2:
        D, S = responders[0].ref, responders[1].ref
        phase2_tests.append(("T3.1 prompt-serving happy", lambda: test_t3_1_happy(D, S)))
        phase2_tests.append(("T3.2 unknown tool",         lambda: test_t3_2_unknown_tool(D, S)))
    elif args.responder_dir:
        print(f"[SKIP] T3.x — needs ≥2 --responder-dir (got {len(args.responder_dir)})")
```

Update it to:

```python
    if len(responders) >= 2:
        D, S = responders[0].ref, responders[1].ref
        phase2_tests.append(("T3.1 prompt-serving happy", lambda: test_t3_1_happy(D, S)))
        phase2_tests.append(("T3.2 unknown tool",         lambda: test_t3_2_unknown_tool(D, S)))
        if idle_ref:
            phase2_tests.append(("T3.3 timeout no responder",
                                  lambda: test_t3_3_timeout(D, idle_ref)))
        else:
            print("[SKIP] T3.3 — needs --idle-dir")
    elif args.responder_dir:
        print(f"[SKIP] T3.x — needs ≥2 --responder-dir (got {len(args.responder_dir)})")
```

- [ ] **Step 3: Verify the file parses**

```bash
python -c "import ast; ast.parse(open('a2a_test_harness.py').read()); print('parse OK')"
```

Expected: `parse OK`.

- [ ] **Step 4: Smoke test (optional, requires live staging)**

```bash
python a2a_test_harness.py \
    --env helloworld-a/.env \
    --responder-dir helloworld-a \
    --responder-dir helloworld-b \
    --idle-dir       helloworld-c
```

Expected:

```
[idle target: helloworld-c]
...
[PASS] T3.3 timeout no responder — timed out cleanly after 5.0Xs

---- 7/7 passed ----
```

If T3.3 fails with `unexpected reply: ...`: helloworld-c probably has an `a2a_peer.py` running. Stop it.

If T3.3 fails with `elapsed=X.XXs outside [4.5, 6.5]`: investigate event-wait latency. The window is generous; values outside it suggest a real bug in `request_and_wait`.

- [ ] **Step 5: Commit**

```bash
cd /home/shiva/Documents/Ephapsys/product/ephapsys-samples
git add agents/a2a_test_harness.py
git commit -m "harness: add T3.3 (timeout no responder) and --idle-dir wiring

T3.3 sends a tool_call to a real agent_id whose poller was never
started (read from --idle-dir). Asserts no reply within the 5s
timeout and that elapsed wall-clock is in [4.5, 6.5]. Skipped with
visible [SKIP] line if --idle-dir is omitted."
```

---

## Task 7: Update module docstring with the new runbook

**Goal:** The module docstring (lines 1–22) still describes only Phase 1 usage. Update it to cover the two-phase runbook so anyone running the script sees the new flags.

**Files:**
- Modify: same file, lines 1–22 (module docstring).

- [ ] **Step 1: Replace the docstring**

Replace the current module docstring (lines 1–22) with:

```python
"""A2A integration test harness.

Drives test scenarios T1.1, T1.2, T2.1, T8.1 (existing — protocol/journal
checks) and optionally T3.1, T3.2, T3.3 (prompt-serving — real model
inference) against a live AOC. No manual input required; prints PASS/FAIL
per test and exits non-zero if any test fails.

Phase 1 only (existing behavior — no model inference):
    python a2a_test_harness.py --env helloworld-a/.env

Phase 1 + Phase 2 (adds T3.1, T3.2, T3.3 — real prompt-serving):
    python a2a_test_harness.py \\
        --env helloworld-a/.env \\
        --responder-dir helloworld-a \\
        --responder-dir helloworld-b \\
        --idle-dir       helloworld-c

Env required (loaded from --env, defaults to ./helloworld-a/.env):
    AOC_BASE_URL, AOC_A2A_TOKEN (or AOC_MODULATION_TOKEN), AOC_ORG_ID,
    A2A_CLUSTER_ID

The cluster must have at least 2 enabled members; T1.2 also exercises
the third member when present.

T3.x require:
    - At least 2 --responder-dir flags pointing at pre-personalized
      agent dirs (run quickstart.sh in each first). The first dir is
      the driver and also serves; subsequent dirs are server-only.
    - --idle-dir for T3.3 (a personalized dir whose agent_id will be
      addressed but never have a poller — used as the unreachable
      target for the timeout test). T3.3 SKIPs if omitted.

Run this with no a2a_peer.py instances active — concurrent pollers can
ack messages out from under the harness and cause flakes.
"""
```

- [ ] **Step 2: Verify the file parses**

```bash
python -c "import ast; ast.parse(open('a2a_test_harness.py').read()); print('parse OK')"
```

Expected: `parse OK`.

- [ ] **Step 3: Verify `--help` reflects the new docstring's first line**

```bash
python a2a_test_harness.py --help 2>&1 | head -5
```

Expected: the first line of help output reads `usage: a2a_test_harness.py [-h] ...`. The argparse description (currently `description=__doc__.splitlines()[0]` at the existing line 223) will be `A2A integration test harness.` — the new docstring's first line.

- [ ] **Step 4: Commit**

```bash
cd /home/shiva/Documents/Ephapsys/product/ephapsys-samples
git add agents/a2a_test_harness.py
git commit -m "harness: update docstring with two-phase runbook for T3.x

Documents the new --responder-dir / --idle-dir flags, the
pre-personalization requirement, and the existing 'no concurrent
a2a_peer.py' caveat."
```

---

## Task 8: Final end-to-end smoke test

**Goal:** Single-shot verification that all changes from Tasks 1–7 work together against live staging. No code changes.

**Files:** None.

- [ ] **Step 1: Confirm prerequisites**

Run from `ephapsys-samples/agents/`:

```bash
ls helloworld-a/.ephapsys_state/agent_id helloworld-b/.ephapsys_state/agent_id helloworld-c/.ephapsys_state/agent_id
```

Expected: all three files exist. If any are missing, run `./quickstart.sh` in the corresponding folder first.

Confirm no `a2a_peer.py` instances are running:

```bash
pgrep -af a2a_peer.py
```

Expected: no output (or `pgrep` exits non-zero with no matches). If output appears, kill those processes before continuing.

- [ ] **Step 2: Run the full harness**

```bash
python a2a_test_harness.py \
    --env helloworld-a/.env \
    --responder-dir helloworld-a \
    --responder-dir helloworld-b \
    --idle-dir       helloworld-c
```

Expected output ends with:

```
[PASS] T1.1 direct P2P — ...
[PASS] T1.2 broadcast + self-skip — ...
[PASS] T2.1 guardrail block — ...
[PASS] T8.1 journal completeness — ...

[loading model for helloworld-a...] (driver)
[loading model for helloworld-b...]
[idle target: helloworld-c]
[pollers up: helloworld-a, helloworld-b]

[PASS] T3.1 prompt-serving happy — result_len=<>, elapsed=<>s
[PASS] T3.2 unknown tool — err='unknown tool: ...', elapsed=<>s
[PASS] T3.3 timeout no responder — timed out cleanly after 5.0Xs

---- 7/7 passed ----
```

Exit code: 0.

- [ ] **Step 3: Verify failure-mode hygiene (negative test)**

Run with only one `--responder-dir` to confirm the SKIP path:

```bash
python a2a_test_harness.py --env helloworld-a/.env --responder-dir helloworld-a
```

Expected: a line `[SKIP] T3.x — needs ≥2 --responder-dir (got 1)` appears, and the harness exits 0 with `4/4 passed` (Phase 1 only).

Run with 2 responders but no `--idle-dir` to confirm T3.3-only SKIP:

```bash
python a2a_test_harness.py \
    --env helloworld-a/.env \
    --responder-dir helloworld-a \
    --responder-dir helloworld-b
```

Expected: `[SKIP] T3.3 — needs --idle-dir` and overall `6/6 passed`.

- [ ] **Step 4: No commit needed** (Task 7 was the last code-change commit). If you want to mark the milestone, optionally tag the head:

```bash
cd /home/shiva/Documents/Ephapsys/product/ephapsys-samples
git tag a2a-prompt-serving-v1
```

(Optional; do not push without permission.)

---

## Spec coverage check

| Spec § | Requirement | Implemented in |
|--------|-------------|----------------|
| §2 | Wire protocol (`tool_call` ↔ `tool_result`) | Task 3 (TOOLS, handle_tool_call, handle_tool_result) |
| §3 | Org client + Responder workers + Driver + Idle target roles | Task 1 (state), Task 2 (builders), Task 4 (wiring) |
| §3 | `PENDING`, `PENDING_LOCK`, `stop_event`, `Responder` dataclass | Task 1 |
| §4 | `--responder-dir` (repeatable), `--idle-dir` CLI flags | Task 1 |
| §5 | Two-phase lifecycle (Phase 1 first, then Phase 2 with pollers) | Task 4 |
| §5 | Eager pre-load with operator-visible `[loading model for X...]` | Task 4 |
| §5 | Drain inboxes before pollers start | Task 4 |
| §5 | Teardown via `stop_event.set()` + `join(timeout=5.0)` | Task 4 |
| §6.1 | `build_did_to_ref_map`, `read_agent_id`, `build_responder` | Task 2 |
| §6.2 | `make_on_verified`, `handle_tool_call`, `handle_tool_result`, `TOOLS` | Task 3 |
| §6.3 | `poller_loop` + `MessageJournal(harness_journal.jsonl)` | Task 3 |
| §6.4 | `request_and_wait` primitive | Task 3 |
| §7.1 | T3.1 happy-path test body | Task 4 |
| §7.2 | T3.2 unknown-tool test body | Task 5 |
| §7.3 | T3.3 timeout test body | Task 6 |
| §8 | SKIP guards (`<2 --responder-dir`, no `--idle-dir`) | Tasks 4 + 6 |
| §9 | Error matrix (verify/prepare exits 2; poller try/except; etc.) | Tasks 3 + 4 |
| §11 | Runbook in module docstring | Task 7 |

All §1–§14 spec sections have a corresponding task. §15 (decisions log) is documentation in the spec, not code.
