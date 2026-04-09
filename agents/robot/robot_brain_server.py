#!/usr/bin/env python3

import asyncio
import json
import os
from typing import Optional

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect

from robot_body import RobotBody
from robot_channel import RobotChannel
from robot_brain import RobotBrain
from robot_contracts import ROBOT_PUBLIC_SCHEMAS, public_schema_bundle
from robot_face import RobotStateFace
from robot_remote_body import RemoteAudioSegmenter

app = FastAPI(title="Robot Brain")

# ---------------------------------------------------------------------------
# Francisca face protocol adapter
# Maps robot brain snapshot fields to Francisca's {type, payload} events.
# ---------------------------------------------------------------------------

_EXPRESSION_MAP: dict = {
    "neutral": [],
    "happy": [("smile", 0.7)],
    "surprised": [("brows", 0.8), ("ah", 0.5)],
    "sad": [("sneer", 0.4)],
    "thinking": [("brows", 0.3)],
    "excited": [("smile", 0.9), ("brows", 0.5)],
    "distress": [("sneer", 0.7), ("brows", 0.9), ("ah", 0.3)],
}

_GAZE_MAP: dict = {
    "center": {"gazex": 0.0, "gazey": 0.0},
    "left": {"gazex": -0.5, "gazey": 0.0},
    "right": {"gazex": 0.5, "gazey": 0.0},
    "up": {"gazex": 0.0, "gazey": -0.3},
    "down": {"gazex": 0.0, "gazey": 0.3},
    "attentive": {"gazex": 0.0, "gazey": -0.1},
}


async def _emit_francisca_events(ws: WebSocket, prev: dict, curr: dict) -> None:
    """Diff two brain snapshots and push matching Francisca protocol events."""
    # Agent status
    if curr.get("agent_status") != prev.get("agent_status"):
        await ws.send_text(json.dumps({"type": "agent_status", "payload": curr["agent_status"]}))

    # Speech: emit when reply changes
    prev_reply = (prev.get("latest") or {}).get("reply", "")
    curr_reply = (curr.get("latest") or {}).get("reply", "")
    if curr_reply and curr_reply != "-" and curr_reply != prev_reply:
        await ws.send_text(json.dumps({"type": "speech", "payload": {"text": curr_reply}}))

    curr_ui = curr.get("ui_state") or {}
    prev_ui = prev.get("ui_state") or {}

    # Expression
    expr_key = curr_ui.get("expression", "neutral")
    if expr_key != prev_ui.get("expression"):
        for expr, value in _EXPRESSION_MAP.get(expr_key, []):
            await ws.send_text(json.dumps({"type": "expression", "payload": {"expr": expr, "value": value}}))
        if not _EXPRESSION_MAP.get(expr_key):
            # Reset to neutral
            await ws.send_text(json.dumps({"type": "expression", "payload": {"expr": "smile", "value": 0.0}}))

    # Gaze / movement
    gaze_key = curr_ui.get("gaze", "center")
    if gaze_key != prev_ui.get("gaze"):
        movement = _GAZE_MAP.get(gaze_key, {"gazex": 0.0, "gazey": 0.0})
        await ws.send_text(json.dumps({"type": "movement", "payload": movement}))

shutdown_event = asyncio.Event()
state_face = RobotStateFace()
channel = RobotChannel()
body = RobotBody(state_face, shutdown_event, channel)
brain = RobotBrain(state_face, body, channel, shutdown_event)
remote_audio = RemoteAudioSegmenter(body, channel)
brain_task = None
brain_ready = asyncio.Event()
body_mode = os.getenv("ROBOT_BODY_MODE", "local").strip().lower()
remote_control_clients = set()
remote_control_lock = asyncio.Lock()


def _ensure_brain_task():
    global brain_task
    if brain_task is None:
        brain_task = asyncio.create_task(_run_brain())


async def _run_brain():
    try:
        mic_task = asyncio.create_task(body.mic_task()) if body_mode in {"local", "hybrid"} else None
        tts_task = asyncio.create_task(body.tts_worker(brain.agent)) if body_mode in {"local", "hybrid"} else None
        remote_tts_task = asyncio.create_task(remote_body_control_task()) if body_mode == "remote" else None
        ingest_task = asyncio.create_task(brain.ingest_channel_events())
        output_task = asyncio.create_task(brain.output_arbiter())
        await brain.startup()
        brain_ready.set()
        tasks = [
            brain.process_task(None),
            brain.periodic_verify(),
            ingest_task,
            output_task,
        ]
        if tts_task is not None:
            tasks.append(tts_task)
        if remote_tts_task is not None:
            tasks.append(remote_tts_task)
        if body_mode in {"local", "hybrid"}:
            tasks.extend([body.cam_task(), mic_task])
        await asyncio.gather(
            *tasks,
            return_exceptions=True,
        )
    except Exception as exc:
        exc_str = str(exc)
        event_msg = f"Startup failure: {exc_str}"

        # Detect stale local state: digest mismatch means the cached agent/model
        # artifacts no longer match their certificates — almost always caused by a
        # leftover .ephapsys_state from a previous agent template version.
        if "Digest mismatch" in exc_str or "Artifact preparation failed" in exc_str:
            import shutil
            state_dir = os.path.join(os.path.dirname(__file__), ".ephapsys_state")
            if os.path.isdir(state_dir):
                shutil.rmtree(state_dir, ignore_errors=True)
                event_msg = (
                    "Stale local agent state detected (artifact digest mismatch). "
                    "Local cache cleared — please restart the robot to re-provision."
                )
                state_face.console_log.log(
                    "[ALERT] Stale .ephapsys_state detected: artifact digests do not match "
                    "the current agent template. Cache cleared automatically. Restart to continue."
                )
            else:
                event_msg = (
                    "Artifact digest mismatch — model certificate does not match stored artifact. "
                    "Re-run push.sh to re-modulate, then restart."
                )

        # Detect expired or invalid provisioning token
        elif "invalid provisioning token" in exc_str.lower() or "provisioning token exchange failed" in exc_str.lower():
            event_msg = (
                "Provisioning token expired or invalid. "
                "Generate a new AOC_PROVISIONING_TOKEN and restart."
            )

        state_face.set_state(
            reasoning="Startup blocked",
            speaking="Unavailable",
            event=event_msg,
        )
        state_face.console_log.log(f"Robot brain startup failed: {exc_str}")
        raise
    finally:
        brain_ready.clear()


@app.on_event("shutdown")
async def shutdown_event_handler():
    shutdown_event.set()
    body.cleanup()
    if brain_task is not None:
        brain_task.cancel()


@app.get("/health")
async def health():
    _ensure_brain_task()
    return {"ok": True, "ready": brain_ready.is_set(), "body_mode": body_mode, "state": state_face.snapshot()}


async def _broadcast_remote_command(payload: dict) -> bool:
    async with remote_control_lock:
        clients = list(remote_control_clients)
    if not clients:
        return False
    stale = []
    delivered = False
    for ws in clients:
        try:
            await ws.send_text(json.dumps(payload))
            delivered = True
        except Exception:
            stale.append(ws)
    if stale:
        async with remote_control_lock:
            for ws in stale:
                remote_control_clients.discard(ws)
    return delivered


async def remote_body_control_task():
    while not shutdown_event.is_set():
        command = await channel.next_command()
        try:
            payload = {"type": "command", "kind": command.kind, "payload": command.payload}
            delivered = await _broadcast_remote_command(payload)
            if delivered:
                state_face.set_state(
                    speaking="Remote body active" if command.kind == "speak" else state_face.ui_state.get("speaking", "Idle"),
                    body="Remote body active" if command.kind == "body_control" else state_face.ui_state.get("body", "Idle"),
                    event=f"Remote command sent: {command.kind}",
                )
                continue
            if command.kind == "speak":
                await channel.emit_event("tts_done", text=command.payload.get("text", ""), duration_ms=0.0, error="No remote body client connected")
            elif command.kind == "body_control":
                await channel.emit_event("body_control_done", action=command.payload.get("action", "idle"), error="No remote body client connected")
            state_face.set_state(event=f"Remote body unavailable for {command.kind}")
        finally:
            channel.command_done()


@app.get("/schemas")
async def schemas():
    return public_schema_bundle()


@app.get("/schemas/{schema_name}")
async def schema_by_name(schema_name: str):
    schema = ROBOT_PUBLIC_SCHEMAS.get(schema_name)
    if schema is None:
        return {"ok": False, "error": f"unknown schema '{schema_name}'", "available": sorted(ROBOT_PUBLIC_SCHEMAS)}
    return {"ok": True, "schema_version": "robot-public-v1", "name": schema_name, "schema": schema}


@app.websocket("/ws/state")
async def ws_state(
    ws: WebSocket,
    token: Optional[str] = Query(default=None),
    session_id: Optional[str] = Query(default=None),
    protocol: str = Query(default="robot"),
):
    """State stream for connected faces.

    Query params:
      token       Google ID token (required when SOCIAL_LOGIN_ENABLED=1)
      session_id  Client-generated UUID (used as user_id when login is off)
      protocol    "robot" (default, snapshot diffs) | "francisca" (typed events)
    """
    from robot_auth import SOCIAL_LOGIN_ENABLED, resolve_session

    _ensure_brain_task()
    try:
        user_id, user_info = resolve_session(token, session_id)
    except ValueError as exc:
        await ws.accept()
        await ws.send_text(json.dumps({"error": str(exc)}))
        await ws.close(code=4001)
        return

    await ws.accept()
    brain.set_current_user(user_id, user_info)

    last_snapshot: dict = {}
    try:
        while True:
            snapshot = state_face.snapshot()
            if snapshot != last_snapshot:
                if protocol == "francisca":
                    await _emit_francisca_events(ws, last_snapshot, snapshot)
                else:
                    await ws.send_text(json.dumps({"snapshot": snapshot}))
                last_snapshot = snapshot
            await asyncio.sleep(0.1)
    except WebSocketDisconnect:
        if SOCIAL_LOGIN_ENABLED:
            brain.save_session(user_id)
        return


@app.websocket("/ws/body/audio")
async def ws_body_audio(ws: WebSocket):
    _ensure_brain_task()
    await ws.accept()
    try:
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                break
            data = message.get("bytes")
            if not data:
                continue
            await remote_audio.ingest(data)
            state_face.set_state(hearing="Remote microphone active", event="Remote body audio received")
    except WebSocketDisconnect:
        await remote_audio.flush()
        return


@app.websocket("/ws/body/video")
async def ws_body_video(ws: WebSocket):
    _ensure_brain_task()
    await ws.accept()
    try:
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                break
            data = message.get("bytes")
            if not data:
                continue
            frame = body.decode_image_bytes(data)
            if frame is None:
                continue
            await channel.emit_event("camera", frame=frame, source="remote_ws")
            state_face.set_state(vision="Remote camera active", event="Remote body video received")
    except WebSocketDisconnect:
        return


@app.websocket("/ws/body/control")
async def ws_body_control(ws: WebSocket):
    _ensure_brain_task()
    await ws.accept()
    async with remote_control_lock:
        remote_control_clients.add(ws)
    state_face.set_state(body="Remote body connected", event="Remote body control connected")
    try:
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                break
            text = message.get("text")
            if not text:
                continue
            payload = json.loads(text)
            if payload.get("type") != "event":
                continue
            kind = payload.get("kind")
            event_payload = payload.get("payload") or {}
            if kind == "tts_done":
                await channel.emit_event("tts_done", **event_payload)
            elif kind == "body_control_done":
                await channel.emit_event("body_control_done", **event_payload)
            else:
                await channel.emit_event(kind or "remote_event", **event_payload)
    except WebSocketDisconnect:
        return
    finally:
        async with remote_control_lock:
            remote_control_clients.discard(ws)
        state_face.set_state(body="Remote body disconnected", event="Remote body control disconnected")
