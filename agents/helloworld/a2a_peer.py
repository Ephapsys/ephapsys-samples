"""Bidirectional A2A peer for the helloworld sample.

Reads ./.env, picks up the agent's own DID from ./.ephapsys_state/agent_id,
sends typed lines from stdin to PEER_AGENT_ID, and polls its inbox in the
background — verifying senders + scanning for prompt-injection guardrails
via A2AClient.process_inbox.

Run two terminals (one per folder) for a live conversation.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import uuid
from pathlib import Path

import requests
from ephapsys.a2a import A2AClient
from ephapsys.journal import MessageJournal


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


HERE = Path(__file__).parent
load_env(HERE / ".env")

LOCAL_DID = (HERE / ".ephapsys_state" / "agent_id").read_text().strip()
RAW_PEERS = [p.strip() for p in os.environ.get("A2A_PEER_AGENT_ID", "").split(",") if p.strip()]
CLUSTER_ID = os.environ.get("A2A_CLUSTER_ID", "").strip()
POLL_SECONDS = float(os.environ.get("A2A_POLL_SECONDS", "2"))

if not RAW_PEERS and not CLUSTER_ID:
    raise SystemExit("Set A2A_PEER_AGENT_ID (single or comma-separated) or A2A_CLUSTER_ID in .env")

c = A2AClient.from_env()


def build_did_to_ref_map() -> dict:
    """Fetch /agents and build {did: public_id|label|_id} for the org."""
    r = requests.get(
        f"{c.base_url}/agents",
        headers={"Authorization": f"Bearer {c.token}"},
        timeout=c.timeout,
    )
    r.raise_for_status()
    agents = r.json() or []
    mapping = {}
    for a in agents:
        did = a.get("did") or (a.get("identity") or {}).get("did") or ""
        ref = a.get("public_id") or a.get("label") or a.get("ID") or a.get("_id") or a.get("id")
        if did and ref:
            mapping[str(did)] = str(ref)
    return mapping


def resolve(identifier: str, did_map: dict) -> str:
    """DID → public_id; pass-through if already a ref."""
    if identifier.startswith("did:"):
        ref = did_map.get(identifier)
        if not ref:
            raise SystemExit(f"could not resolve {identifier} via /agents — is it in this org?")
        return ref
    return identifier


DID_MAP = build_did_to_ref_map()
ME = resolve(LOCAL_DID, DID_MAP)
PEERS = [resolve(p, DID_MAP) for p in RAW_PEERS]
journal = MessageJournal(path=str(HERE / "a2a_journal.jsonl"))
stop = threading.Event()

# Outstanding /ask requests awaiting tool_result, keyed by correlation_id.
PENDING_LOCK = threading.Lock()
PENDING: dict = {}

USE_TRUSTED_AGENT = os.environ.get("A2A_USE_TRUSTED_AGENT", "0") == "1"
# Eager-load the model into GPU at startup instead of lazily on first
# tool_call. Default on when USE_TRUSTED_AGENT is set — eliminates the
# 5-15s first-/ask latency cliff and lets the demo driver wait for a
# deterministic "[ready: model in GPU]" marker before starting scenes.
# Set A2A_EAGER_LOAD=0 to restore lazy behavior (e.g., the test harness
# that drives model load through its own phase-2 verify()).
EAGER_LOAD = os.environ.get("A2A_EAGER_LOAD", "1") == "1"
_AGENT = None
_AGENT_LOCK = threading.Lock()


def get_local_agent():
    """Load TrustedAgent against the already-personalized instance.

    The .ephapsys_state agent_id is an Instance DID (already personalized
    by run_local.sh / quickstart.sh), so we skip personalize() — which
    only applies to Templates and would 409 here. We just verify the
    cert/digest chain and prepare the runtime so .run() can be called.
    """
    global _AGENT
    if _AGENT is not None:
        return _AGENT
    with _AGENT_LOCK:
        if _AGENT is not None:
            return _AGENT
        from ephapsys import TrustedAgent
        print(f"\n[loading TrustedAgent locally for inference...]\n> ", end="", flush=True)
        a = TrustedAgent.from_env()
        a.verify()
        a.prepare_runtime()
        _AGENT = a
        return _AGENT


def tool_language_respond(args: dict) -> str:
    text = str(args.get("text", ""))
    if not USE_TRUSTED_AGENT:
        return f"[stub from {ME}] echo: {text!r}"
    return get_local_agent().run(text, model_kind="language")


TOOLS = {
    "language.respond": tool_language_respond,
}


def handle_tool_call(msg: dict) -> None:
    payload = msg.get("payload") or {}
    tool = str(payload.get("tool") or "")
    args = payload.get("args") or {}
    cid = msg.get("correlation_id") or ""
    sender = msg.get("from_agent_id") or ""

    if tool not in TOOLS:
        result = {"ok": False, "error": f"unknown tool: {tool!r}"}
    else:
        try:
            result = {"ok": True, "tool": tool, "result": TOOLS[tool](args)}
        except Exception as exc:
            result = {"ok": False, "tool": tool, "error": f"{type(exc).__name__}: {exc}"}

    c.send_message(
        from_agent_id=ME, to_agent_id=sender,
        payload=result, message_type="tool_result", correlation_id=cid,
    )
    status = "ok" if result.get("ok") else "err"
    print(f"\n[handled tool_call from {sender}: {tool} → {status}]\n> ", end="", flush=True)


def handle_tool_result(msg: dict) -> None:
    cid = msg.get("correlation_id") or ""
    sender = msg.get("from_agent_id") or ""
    payload = msg.get("payload") or {}
    with PENDING_LOCK:
        req = PENDING.pop(cid, None)
    elapsed = f" ({time.time() - req['sent_at']:.1f}s)" if req else ""
    if payload.get("ok"):
        print(f"\n[<- {sender}] tool_result{elapsed}: {payload.get('result')!r}\n> ",
              end="", flush=True)
    else:
        print(f"\n[<- {sender}] tool_result{elapsed} ERROR: {payload.get('error')}\n> ",
              end="", flush=True)


def on_verified_message(msg: dict) -> None:
    mtype = (msg.get("message_type") or "").lower()
    if mtype == "tool_call":
        handle_tool_call(msg)
        return
    if mtype == "tool_result":
        handle_tool_result(msg)
        return
    print(f"\n[<- {msg.get('from_agent_id')}] {msg.get('payload')}\n> ",
          end="", flush=True)


def receive_loop() -> None:
    while not stop.is_set():
        try:
            summary = c.process_inbox(
                agent_id=ME,
                journal=journal,
                ack_rejected=True,
                on_verified=on_verified_message,
                on_rejected=lambda v: print(
                    f"\n[REJECTED] reason={v.reason} hits={v.guardrail_hits}\n> ",
                    end="", flush=True,
                ),
                on_quarantine_alert=lambda m: print(f"\n[QUARANTINE] {m}\n> ", end="", flush=True),
                on_status_change=lambda m: print(f"\n[STATUS] {m}\n> ", end="", flush=True),
            )
            if summary["processed"]:
                print(f"\n[poll] {summary}\n> ", end="", flush=True)
        except Exception as exc:
            print(f"\n[poll error] {exc}\n> ", end="", flush=True)
        stop.wait(POLL_SECONDS)


def cmd_list() -> None:
    if not CLUSTER_ID:
        print("  /list requires A2A_CLUSTER_ID")
        return
    info = c.cluster_info(cluster_id=CLUSTER_ID)
    print(f"  cluster {CLUSTER_ID}  health={info.get('health')}")
    for m in info.get("members", []):
        mark = "  <-- me" if m["agent_id"] == ME else ""
        print(f"  - {m['agent_id']:40s} status={m['status']}{mark}")


def send_direct(target: str, body: str) -> None:
    ref = resolve(target, DID_MAP) if target.startswith("did:") else target
    resp = c.send_message(
        from_agent_id=ME,
        to_agent_id=ref,
        payload={"text": body},
        message_type="event",
    )
    msg_id = (resp.get("message") or {}).get("id")
    print(f"[-> {ref}] sent id={msg_id}")


def cmd_ask(rest: str) -> None:
    if " " not in rest:
        print("  usage: /ask <peer-ref-or-did> <prompt>")
        return
    target, _, prompt = rest.partition(" ")
    ref = resolve(target, DID_MAP) if target.startswith("did:") else target
    cid = f"ask-{uuid.uuid4().hex[:10]}"
    with PENDING_LOCK:
        PENDING[cid] = {"sent_at": time.time(), "tool": "language.respond", "prompt": prompt}
    c.send_message(
        from_agent_id=ME, to_agent_id=ref,
        payload={"tool": "language.respond", "args": {"text": prompt}},
        message_type="tool_call", correlation_id=cid,
    )
    print(f"[-> {ref}] tool_call language.respond cid={cid} — awaiting tool_result")


def send_one(line: str) -> None:
    if line in ("/list", "/ls"):
        cmd_list()
        return
    if line.startswith("/ask "):
        cmd_ask(line[len("/ask "):].strip())
        return
    if line in ("/help", "/?"):
        print("  <text>                    broadcast to cluster (or fan out to peers)")
        print("  @<ref> <text>             direct send to one peer (ref or DID)")
        print("  /ask <ref> <prompt>       ask a peer to run language.respond and return result")
        print("  /list                     show cluster members + health")
        return
    if line.startswith("@"):
        token, _, body = line[1:].partition(" ")
        if not token:
            print("  usage: @<peer-ref-or-did> <message>")
            return
        send_direct(token, body)
        return
    if CLUSTER_ID:
        result = c.broadcast(
            cluster_id=CLUSTER_ID,
            from_agent_id=ME,
            payload={"text": line},
            message_type="event",
        )
        print(f"[-> cluster {CLUSTER_ID}] sent={result['sent']} failed={result['failed']}")
        return
    for peer in PEERS:
        send_direct(peer, line)


def main() -> None:
    print(f"me      = {ME}  (did={LOCAL_DID})")
    print(f"peers   = {PEERS or '(via cluster)'}")
    print(f"cluster = {CLUSTER_ID or '(none)'}")
    print(f"aoc     = {os.environ.get('AOC_BASE_URL')}")
    responder = "TrustedAgent (real language model)" if USE_TRUSTED_AGENT else "stub (echo)"
    print(f"tool    = language.respond → {responder}")
    print("Commands: <text> broadcast | @<ref> <text> direct | /ask <ref> <prompt> | /list | /help | Ctrl-D quit\n")

    if USE_TRUSTED_AGENT and EAGER_LOAD:
        print("[loading model into GPU…]", flush=True)
        a = get_local_agent()
        # prepare_runtime() only stages artifacts on disk — the actual
        # model.to(device) happens inside _run_language. Force the real
        # load with a dummy .run() so first /ask doesn't pay the cliff.
        try:
            a.run("warmup", model_kind="language")
        except Exception as exc:
            print(f"[warmup .run failed: {type(exc).__name__}: {exc}]", flush=True)
        # Verify true GPU residency. The previous "ready" marker was
        # printing after prepare_runtime() returned, before any weights
        # touched CUDA — nvidia-smi showed zero Python processes.
        device_info = "device unknown"
        try:
            import torch
            if torch.cuda.is_available():
                allocated_mib = torch.cuda.memory_allocated() // (1024 * 1024)
                if allocated_mib > 0:
                    device_info = f"cuda:{torch.cuda.current_device()} ({allocated_mib} MiB allocated)"
                else:
                    device_info = "CPU (CUDA available but 0 MiB allocated)"
            else:
                device_info = "CPU (torch.cuda.is_available() = False)"
        except Exception as exc:
            device_info = f"unverified ({type(exc).__name__})"
        # Sentinel prefix the demo driver's wait_for_b_ready greps for.
        # Keep "[ready:" stable — it's a contract with demo/lib.sh.
        print(f"[ready: model loaded on {device_info}]", flush=True)

    threading.Thread(target=receive_loop, daemon=True).start()

    try:
        while True:
            sys.stdout.write("> "); sys.stdout.flush()
            line = sys.stdin.readline()
            if not line:
                break
            line = line.rstrip("\n")
            if not line:
                continue
            send_one(line)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()


if __name__ == "__main__":
    main()
