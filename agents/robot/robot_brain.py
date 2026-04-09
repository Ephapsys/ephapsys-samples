#!/usr/bin/env python3

import asyncio
import os
import re
import sys
import time
import traceback
import unicodedata
import cv2
import numpy as np

import faiss
from PIL import Image
from ephapsys.agent import TrustedAgent
from robot_arch import (
    RobotGovernor,
    RobotToolbox,
    body_intent_for_world,
    classify_tool_intent,
    face_intent_for_state,
)
from robot_contracts import BodyAction, FaceAction, SpeakAction, SpeechFact, SystemFact, VisionFact
from robot_executors import ActionExecutor, BodyController, FaceController, SpeechController, ToolExecutor
from robot_state import RobotStateStore


class RobotBrain:
    def __init__(self, face, body, channel, shutdown_event):
        self.face = face
        self.body = body
        self.channel = channel
        self.shutdown_event = shutdown_event
        os.environ.setdefault("AOC_LANGUAGE_DO_SAMPLE", "0")
        os.environ.setdefault("AOC_LANGUAGE_TEMPERATURE", "0.2")
        os.environ.setdefault("AOC_LANGUAGE_TOP_P", "0.9")
        os.environ.setdefault("AOC_MAX_NEW_TOKENS", "64")
        self.agent = TrustedAgent.from_env()
        self.governor = RobotGovernor(
            allow_body_control=os.getenv("ROBOT_ALLOW_BODY_CONTROL", "1").lower() not in ("0", "false", "no"),
            allow_tools=os.getenv("ROBOT_ALLOW_TOOLS", "1").lower() not in ("0", "false", "no"),
        )
        self.toolbox = RobotToolbox()
        self.tool_executor = ToolExecutor(self.toolbox, self.face, self.governor, self.set_governor_state)
        self.action_executor = ActionExecutor(
            BodyController(self.channel, self.face),
            FaceController(self.face),
            SpeechController(self.channel, self.face),
        )
        self._sessions: dict = {"default": RobotStateStore()}
        self._current_user_id: str = "default"
        self.language_warm_task = None
        self.reasoning_queue: asyncio.Queue = asyncio.Queue()
        self.output_queue: asyncio.Queue = asyncio.Queue()
        self.tts_done_event = asyncio.Event()
        self.turn_active = False
        self.body_mode = os.getenv("ROBOT_BODY_MODE", "local").strip().lower()
        # Live per-turn vision is enabled by default again for the robot demo.
        # It can still be disabled explicitly via env when debugging other paths.
        live_vision_default = "0" if self.body_mode == "remote" else "1"
        self.live_vision_enabled = os.getenv("ROBOT_ENABLE_LIVE_VISION", live_vision_default).lower() not in ("0", "false", "no")
        self.world_enabled = os.getenv("ROBOT_ENABLE_WORLD_MODEL", "1").lower() not in ("0", "false", "no")

    def build_language_prompt(self, text_input: str, vision_label: str | None, world_summary: str | None, prior_memory: str) -> str:
        parts = [
            "You are Asimov, a trusted multimodal robot speaking to a nearby human.",
            "Reply in plain natural English only.",
            "Keep the reply to one or two short spoken sentences.",
            "Do not output code, file paths, markup, role labels, foreign scripts, or mixed-language text.",
            "If the input is unclear, ask one short clarifying question.",
            f"Human said: {text_input.strip()}",
        ]
        if vision_label:
            parts.append(f"Scene: {vision_label.strip()}")
        if world_summary and world_summary != "-":
            parts.append(f"World state: {world_summary.strip()}")
        if prior_memory:
            parts.append(f"Recent memory: {prior_memory.strip()}")
        parts.append("Answer:")
        return "\n".join(parts)

    def response_looks_invalid(self, text: str) -> bool:
        cleaned = (text or "").strip()
        if not cleaned:
            return True
        if len(cleaned) < 2:
            return True
        invalid_patterns = (
            r"\.\./",
            r"[A-Za-z0-9_]+://",
            r"</?[A-Za-z][^>]*>",
            r"[{}[\]<>]{2,}",
        )
        if any(re.search(pattern, cleaned) for pattern in invalid_patterns):
            return True
        script_families = set()
        for ch in cleaned:
            if not ch.isalpha():
                continue
            try:
                name = unicodedata.name(ch)
            except ValueError:
                continue
            for family in ("LATIN", "CYRILLIC", "CJK", "HIRAGANA", "KATAKANA", "HANGUL", "ARABIC", "HEBREW", "GREEK"):
                if family in name:
                    script_families.add(family)
                    break
        if len(script_families) > 1:
            return True
        ascii_ratio = sum(1 for ch in cleaned if ord(ch) < 128) / max(len(cleaned), 1)
        if ascii_ratio < 0.85:
            return True
        return False

    @staticmethod
    def safe_language_fallback(text_input: str) -> str:
        cleaned = (text_input or "").strip()
        if not cleaned:
            return "I didn't catch that. Could you say it again?"
        return "I want to answer clearly. Could you say that again in a short sentence?"

    @property
    def state(self) -> RobotStateStore:
        return self._sessions.get(self._current_user_id) or self._sessions["default"]

    def get_or_load_session(self, user_id: str) -> RobotStateStore:
        """Return existing session state or load from disk if SOCIAL_LOGIN_ENABLED."""
        if user_id not in self._sessions:
            from robot_auth import SOCIAL_LOGIN_ENABLED
            memory_dir = os.getenv("ROBOT_MEMORY_DIR", "memory")
            path = os.path.join(memory_dir, user_id)
            if SOCIAL_LOGIN_ENABLED and os.path.exists(path):
                self._sessions[user_id] = RobotStateStore.load(path)
                self.face.console_log.log(f"Loaded session for user={user_id} ({len(self._sessions[user_id].stored_responses)} memories)")
            else:
                self._sessions[user_id] = RobotStateStore()
        return self._sessions[user_id]

    def set_current_user(self, user_id: str, user_info: dict = None) -> None:
        """Switch active session to the given user."""
        self.get_or_load_session(user_id)
        self._current_user_id = user_id
        name = (user_info or {}).get("name", "")
        self.face.console_log.log(f"Active session: user_id={user_id}" + (f" name={name}" if name else ""))

    def save_session(self, user_id: str) -> None:
        """Persist session state to disk (only when SOCIAL_LOGIN_ENABLED)."""
        from robot_auth import SOCIAL_LOGIN_ENABLED
        if not SOCIAL_LOGIN_ENABLED:
            return
        session = self._sessions.get(user_id)
        if session is None:
            return
        memory_dir = os.getenv("ROBOT_MEMORY_DIR", "memory")
        path = os.path.join(memory_dir, user_id)
        session.save(path)
        self.face.console_log.log(f"Saved session for user={user_id} ({len(session.stored_responses)} memories)")

    def set_governor_state(self, decision):
        self.face.set_state(governor=f"{'ALLOW' if decision.allowed else 'BLOCK'}: {decision.reason}")

    async def dispatch_body_intent(self, world_summary: str):
        intent = body_intent_for_world(world_summary)
        decision = self.governor.approve(intent)
        self.set_governor_state(decision)
        if not decision.allowed:
            return
        action = intent.payload.get("action", "idle")
        self.face.set_state(body=str(action))
        await self.action_executor.execute(BodyAction(action=action, source=intent.source))

    async def maybe_use_tool(self, transcript: str):
        intent = classify_tool_intent(transcript)
        if intent is None:
            self.face.set_state(tools="Idle")
            return None
        decision = self.governor.approve(intent)
        self.set_governor_state(decision)
        if not decision.allowed:
            self.face.set_state(tools=f"Blocked: {intent.payload.get('tool', 'tool')}")
            return f"I am not allowed to use the {intent.payload.get('tool', 'requested')} tool right now."
        return await self.tool_executor.execute(intent)

    async def emit_reasoning_event(self, fact):
        await self.reasoning_queue.put(fact)

    async def emit_output_event(self, action):
        coalesce_key = getattr(action, "coalesce_key", None)
        if coalesce_key:
            self._coalesce_output_kind(coalesce_key)
        await self.output_queue.put(action)

    def _coalesce_output_kind(self, kind: str):
        queue_items = list(self.output_queue._queue)
        filtered = [item for item in queue_items if getattr(item, "coalesce_key", None) != kind]
        if len(filtered) == len(queue_items):
            return
        self.output_queue._queue.clear()
        self.output_queue._queue.extend(filtered)

    async def emit_face_intent(self, *, world_summary: str = "", reasoning: str = "", speaking: str = "", event: str = ""):
        intent = face_intent_for_state(
            world_summary=world_summary,
            reasoning=reasoning,
            speaking=speaking,
            event=event,
        )
        decision = self.governor.approve(intent)
        self.set_governor_state(decision)
        if not decision.allowed:
            return
        self.state.latest_expression = str(intent.payload.get("expression", "neutral"))
        self.state.latest_gaze = str(intent.payload.get("gaze", "center"))
        await self.emit_output_event(
            FaceAction(
                expression=self.state.latest_expression,
                gaze=self.state.latest_gaze,
                source=intent.source,
            )
        )

    def log_stage(self, label: str, started_at: float):
        self.face.console_log.log(f"[brain] {label} in {(time.perf_counter() - started_at):.2f}s")

    async def run_blocking(self, fn, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    def build_startup_scene_observation(self):
        if self.body_mode == "remote":
            return None
        vision_label = None
        try:
            self.face.set_state(vision="Looking for a first impression", reasoning="Observing the scene")
            frame = self.body.capture_startup_frame()
            if frame is not None:
                self.state.latest_camera_frame = frame
                t0 = time.perf_counter()
                self.face.console_log.log("[brain] Loading startup vision model: Robot Vision Model (hustvl/yolos-base)")
                vision_input = Image.fromarray(frame)
                vision_raw = self.agent.run(vision_input, model_kind="vision")
                self.log_stage("Startup vision ready", t0)
                vision_label = str(vision_raw).strip() if vision_raw is not None else None
        except Exception as exc:
            self.face.console_log.log(f"Startup vision observation fallback: {exc}")
        return vision_label

    def compute_world_summary(self, frame, vision_label):
        # --- Temporal pixel-diff averaged across the frame buffer ---
        self.state.add_frame(frame)
        motion_score = 0.0
        if len(self.state.frame_buffer) >= 2:
            diffs = []
            for prev_f, curr_f in zip(self.state.frame_buffer[:-1], self.state.frame_buffer[1:]):
                prev_small = cv2.resize(prev_f, (64, 64), interpolation=cv2.INTER_AREA)
                curr_small = cv2.resize(curr_f, (64, 64), interpolation=cv2.INTER_AREA)
                diffs.append(
                    float(np.mean(np.abs(curr_small.astype("float32") - prev_small.astype("float32"))) / 255.0)
                )
            motion_score = float(np.mean(diffs))

        # --- V-JEPA world delta averaged across the embedding buffer ---
        world_delta = None
        if self.world_enabled:
            try:
                current_embedding = np.asarray(
                    self.agent.run(Image.fromarray(frame), model_kind="world"),
                    dtype="float32",
                ).flatten()
                if current_embedding is not None and current_embedding.size > 0:
                    self.state.add_embedding(current_embedding)
                    if len(self.state.embedding_buffer) >= 2:
                        deltas = []
                        for prev_e, curr_e in zip(self.state.embedding_buffer[:-1], self.state.embedding_buffer[1:]):
                            denom = (np.linalg.norm(prev_e) * np.linalg.norm(curr_e)) or 1.0
                            deltas.append(float(1.0 - np.dot(prev_e, curr_e) / denom))
                        world_delta = float(np.mean(deltas))
            except Exception as exc:
                self.face.console_log.log(f"World summary fallback: {exc}")

        activity_score = max(motion_score, world_delta or 0.0)

        # Adaptive thresholds: self-calibrate to the deployment environment
        self.state.update_activity(activity_score)
        low_thresh, high_thresh = self.state.adaptive_thresholds()

        if activity_score >= high_thresh:
            movement_phrase = "significant movement"
        elif activity_score >= low_thresh:
            movement_phrase = "movement detected"
        else:
            movement_phrase = "scene steady"

        vision_text = (vision_label or "").strip().lower()
        if vision_text and vision_text != "no objects detected":
            if "person" in vision_text and activity_score >= low_thresh:
                return "person moving in view"
            if "person" in vision_text:
                return "person present"
            if activity_score >= low_thresh:
                return f"{vision_label}; {movement_phrase}"
            return str(vision_label)
        if activity_score >= low_thresh:
            return movement_phrase
        return "scene clear"

    @staticmethod
    def build_startup_greeting(vision_label, *, ready: bool):
        readiness_suffix = "I'm ready when you are." if ready else "I'm still warming up my language model."
        if vision_label and vision_label.strip() and vision_label.strip().lower() != "no objects detected":
            return f"Hello. I can see {vision_label}. {readiness_suffix}"
        return f"Hello. {readiness_suffix}"

    async def warm_language_runtime(self):
        if self.state.language_warm_done:
            return
        started = time.perf_counter()
        try:
            self.face.set_runtime_state("language", "warming")
            self.face.clear_latest_reply("Warming language model...")
            self.face.console_log.log("[brain] Warming language model: Robot Language Model")
            await self.run_blocking(self.agent.run, "Hello.", model_kind="language")
            self.log_stage("Language model warmup ready", started)
            self.state.language_warm_done = True
            self.face.set_runtime_state("language", "ready")
            if str(self.face.latest.get("reply", "")).startswith("Turn failed:"):
                self.face.clear_latest_reply("-")
        except Exception as exc:
            self.face.set_runtime_state("language", "error")
            self.face.console_log.log(f"Language warmup failed: {exc}")
            self.face.console_log.log(traceback.format_exc())

    async def startup(self):
        self.face.startup()
        self.face.clear_latest_reply("-")
        self.face.set_state(
            hearing="Stand by",
            vision="Stand by",
            reasoning="Verifying agent",
            expression=self.state.latest_expression,
            gaze=self.state.latest_gaze,
            body="Idle",
            tools="Idle",
            governor="Ready",
            speaking="Stand by",
            event="Starting brain",
        )
        try:
            self.face.set_state(event="Verifying agent", reasoning="Checking trusted state")
            ok, _ = await self.run_blocking(self.agent.verify)
        except RuntimeError as exc:
            if "404" in str(exc):
                self.face.console_live.print(f"[red]❌ Agent template '{self.agent.agent_id}' not found in backend.[/red]")
                self.face.console_live.print("[yellow]Please create it in the AOC before running this sample.[/yellow]")
                sys.exit(1)
            raise

        if not ok:
            status = await self.run_blocking(self.agent.get_status)
            is_personalized = status.get("state", {}).get("personalized", False) or status.get("personalized", False)
            if not is_personalized:
                anchor = os.getenv("PERSONALIZE_ANCHOR")
                self.face.set_state(event="Personalizing agent instance", reasoning="Binding device identity")
                self.face.console_live.print(
                    f"[yellow]Agent not personalized; running personalize(anchor={anchor})...[/yellow]"
                )
                await self.run_blocking(self.agent.personalize, anchor=anchor)
                self.face.console_live.print("[green]✅ Agent personalized (instance registered in AOC).[/green]")
                for _ in range(5):
                    ok, _ = await self.run_blocking(self.agent.verify)
                    if ok:
                        break
                    self.face.console_live.print("[yellow]...waiting for agent to become ready...[/yellow]")
                    await asyncio.sleep(1)
            if not ok:
                self.face.console_live.print("[red]❌ Agent not ready after personalization.[/red]")
                sys.exit(1)

        status = await self.run_blocking(self.agent.get_status)
        is_enabled = status.get("enabled", False) or (status.get("status", "").lower() == "enabled")
        is_revoked = status.get("state", {}).get("revoked", False)
        self.face.agent_status.update({"verified": ok, "enabled": is_enabled, "revoked": is_revoked})
        self.face.set_state(event="Agent verified", reasoning="Trusted runtime ready")
        self.face.console_live.print("[green]✅ Agent personalized and verified.[/green]")
        self.face.console_live.print(f"[dim]Instance DID: {self.agent.agent_id}[/dim]")

        self.face.set_state(event="Preparing runtime bundles", reasoning="Loading secure model runtimes")
        t0 = time.perf_counter()
        self.face.console_log.log("[brain] Preparing runtime bundles")
        runtimes = await self.run_blocking(self.agent.prepare_runtime)
        self.log_stage("Runtime bundles prepared", t0)
        runtime_state = {
            "stt": "ready" if runtimes.get("stt") else "unavailable",
            "vision": "ready" if runtimes.get("vision") else "unavailable",
            "world": "ready" if runtimes.get("world") else "unavailable",
            "language": "ready" if runtimes.get("language") else "unavailable",
            "embedding": "ready" if runtimes.get("embedding") else "unavailable",
            "tts": "ready" if runtimes.get("tts") else "unavailable",
            "vocoder": "ready" if runtimes.get("vocoder") else "unavailable",
        }
        self.face.set_state(runtime=runtime_state)
        if runtimes.get("world") is None:
            self.world_enabled = False
        tts_path = (runtimes.get("tts") or {}).get("model_path")
        self.body.tts_available = await self.run_blocking(self.body.ensure_preprocessor, tts_path) if tts_path else False
        self.face.set_state(
            hearing="Listening on microphone",
            vision="Scanning scene",
            world="Scanning scene dynamics",
            expression="warm",
            gaze="engage",
            body="Idle",
            tools="Idle",
            governor="Ready",
            reasoning="Preparing greeting",
            speaking="Preparing greeting" if self.body.tts_available else "Unavailable",
            memory="0 memories",
            latency={"turn": None, "stt": None, "vision": None, "language": None, "embedding": None, "tts": None},
            event=f"Runtime ready: {', '.join(sorted(runtimes.keys()))}",
        )
        self.face.set_latest(hearing="-", vision="-", world="-", reply="Preparing greeting...")
        self.face.console_live.print(
            f"[green]✅ Runtime prepared[/green] "
            f"(voice={'ready' if self.body.tts_available else 'unavailable'}, models={', '.join(sorted(runtimes.keys()))})"
        )

        if self.language_warm_task is None:
            self.language_warm_task = asyncio.create_task(self.warm_language_runtime())
        asyncio.create_task(self.periodic_verify())

        self.face.set_state(event="Observing startup scene", reasoning="Warming language model")
        startup_vision = await self.run_blocking(self.build_startup_scene_observation)
        if self.state.latest_camera_frame is not None:
            self.state.latest_world_summary = self.compute_world_summary(self.state.latest_camera_frame, startup_vision)
        self.state.latest_scene_summary = (
            self.state.latest_world_summary if self.state.latest_world_summary != "-" else (startup_vision or "-")
        )
        greeting = self.build_startup_greeting(
            startup_vision,
            ready=bool(self.state.language_warm_done or (self.language_warm_task and self.language_warm_task.done())),
        )
        self.state.startup_vision_label = startup_vision or "-"
        self.face.set_latest(
            hearing="-",
            vision=startup_vision or "-",
            world=self.state.latest_world_summary,
            reply=greeting,
        )
        if startup_vision:
            self.face.set_state(
                vision=self.face.clip_text(startup_vision, 64),
                world=self.face.clip_text(self.state.latest_world_summary, 64),
            )
        if startup_vision:
            self.face.console_live.print(f"[cyan]👁️ Startup vision: {startup_vision}[/cyan]")
        if self.body.tts_available and self.body_mode != "remote":
            self.body.speech_enabled = False
            self.face.set_state(
                reasoning="Greeting ready" if self.state.language_warm_done else "Warming language model",
                speaking="Queued for startup greeting",
                event="Greeting",
            )
            await self.emit_face_intent(
                world_summary=self.state.latest_world_summary,
                reasoning="Greeting ready" if self.state.language_warm_done else "Warming language model",
                speaking="Queued for startup greeting",
                event="Greeting",
            )
            self.tts_done_event.clear()
            await self.channel.send_command("speak", text=greeting)
            try:
                await asyncio.wait_for(self.tts_done_event.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                self.face.console_log.log("Startup greeting did not receive tts_done before timeout")
        elif self.body_mode == "remote":
            self.face.set_state(
                reasoning="Waiting for speech" if self.state.language_warm_done else "Warming language model",
                speaking="Remote face will render greeting",
                event="Live interaction loop started" if self.state.language_warm_done else "Warming language model",
            )
        self.body.speech_enabled = True

        self.face.set_state(
            hearing="Listening on microphone",
            vision="Scanning scene",
            world=self.face.clip_text(self.state.latest_world_summary or "Scanning scene dynamics", 64),
            expression="neutral",
            gaze="center",
            body="Idle",
            tools="Idle",
            reasoning="Waiting for speech" if self.state.language_warm_done else "Warming language model",
            speaking="Idle" if self.body.tts_available else "Unavailable",
            event="Live interaction loop started" if self.state.language_warm_done else "Warming language model",
        )
        self.face.console_live.print("[blue]Entering live interaction loop...[/blue]")
        return greeting

    async def periodic_verify(self):
        last_snapshot = None
        while not self.shutdown_event.is_set():
            await asyncio.sleep(5)
            try:
                ok, _ = await self.run_blocking(self.agent.verify)
                status = await self.run_blocking(self.agent.get_status)
                is_enabled = status.get("enabled", False) or (status.get("status", "").lower() == "enabled")
                is_revoked = status.get("state", {}).get("revoked", False)
                self.face.agent_status.update({"verified": ok, "enabled": is_enabled, "revoked": is_revoked})
                snapshot = (ok, is_enabled, is_revoked)
                if snapshot != last_snapshot:
                    if last_snapshot is not None:
                        prev_ok, prev_enabled, prev_revoked = last_snapshot
                        if is_revoked and not prev_revoked:
                            self.face.set_state(
                                event="Agent certificate REVOKED",
                                expression="distress", gaze="down",
                                reasoning="Terminated", speaking="Muted",
                            )
                            self.face.set_latest(reply="My certificate has been revoked. I am being terminated. Goodbye.")
                        elif not is_enabled and prev_enabled:
                            self.face.set_state(
                                event="Agent DISABLED by operator",
                                expression="distress", gaze="down",
                                reasoning="Shutting down", speaking="Muted",
                            )
                            self.face.set_latest(reply="I have been disabled by my operator. Shutting down.")
                        elif is_enabled and not prev_enabled:
                            self.face.set_state(
                                event="Agent ENABLED by operator",
                                expression="happy", gaze="attentive",
                                reasoning="Coming back online", speaking="Idle",
                            )
                            self.face.set_latest(reply="I have been re-enabled. Hello again, I am back online.")
                    else:
                        self.face.set_state(event="Verification state updated")
                    self.face.console_log.log(f"Periodic verify={self.face.agent_status}")
                    last_snapshot = snapshot
            except Exception as exc:
                self.face.set_state(event=f"Verification failed: {exc}")
                self.face.console_log.log(f"⚠️ Verification failed: {exc}")
                self.face.agent_status.update({"enabled": False, "revoked": True})

    async def ingest_channel_events(self):
        while not self.shutdown_event.is_set():
            try:
                event = await self.channel.next_event(timeout=0.2)
            except Exception:
                event = None
            if event is None:
                continue
            try:
                if event.kind == "tts_done":
                    self.tts_done_event.set()
                    await self.emit_reasoning_event(SystemFact(name="tts_done", payload=event.payload))
                elif event.kind == "body_control_done":
                    await self.emit_reasoning_event(SystemFact(name="body_control_done", payload=event.payload))
                elif event.kind == "camera":
                    if self.turn_active:
                        frame = event.payload.get("frame")
                        if frame is not None:
                            self.state.latest_camera_frame = frame
                        continue
                    await self.emit_reasoning_event(SystemFact(name="camera", payload=event.payload, source="camera"))
                elif event.kind == "microphone":
                    await self.emit_reasoning_event(SystemFact(name="microphone", payload=event.payload, source="microphone"))
                else:
                    await self.emit_reasoning_event(SystemFact(name=event.kind, payload=event.payload))
            finally:
                self.channel.event_done()

    async def output_arbiter(self):
        while not self.shutdown_event.is_set():
            action = await self.output_queue.get()
            try:
                if isinstance(action, SpeakAction):
                    self.state.awaiting_tts_done = True
                    self.body.speech_enabled = False
                    defer = self.governor.should_defer(
                        face_intent_for_state(event="Reply ready"),
                        speaking_active=self.state.awaiting_tts_done,
                    )
                    if defer.allowed:
                        self.face.set_state(event=defer.reason)
                    await self.emit_face_intent(
                        world_summary=self.state.latest_world_summary,
                        reasoning=self.face.ui_state.get("reasoning", ""),
                        speaking="Queued for playback",
                        event="Reply ready",
                    )
                await self.action_executor.execute(action)
            finally:
                self.output_queue.task_done()

    async def publish_camera_fact(self, frame, latest_vision_label, awaiting_tts_done: bool):
        vision_label = latest_vision_label
        vision_ms = 0
        latest_world_summary = self.state.latest_world_summary or "-"
        if self.live_vision_enabled and frame is not None:
            camera_state = {
                "vision": "Analyzing scene",
                "event": "Camera update received",
            }
            if not awaiting_tts_done:
                camera_state["reasoning"] = "Waiting for speech"
            self.face.set_state(**camera_state)
            vision_started = time.perf_counter()
            vision_input = Image.fromarray(frame)
            vision_raw = await self.run_blocking(self.agent.run, vision_input, model_kind="vision")
            vision_ms = (time.perf_counter() - vision_started) * 1000
            vision_label = str(vision_raw).strip() if vision_raw is not None else None
            vision_label = vision_label or latest_vision_label
            latest_world_summary = self.compute_world_summary(frame, vision_label)
        await self.emit_reasoning_event(
            VisionFact(
                frame=frame,
                vision_label=vision_label or "-",
                world_summary=latest_world_summary or "-",
                vision_ms=vision_ms,
            )
        )

    async def publish_microphone_fact(self, mic_audio, heard_summary):
        self.face.set_latest(
            hearing=heard_summary,
            vision=self.face.latest.get("vision", "-"),
            world=self.face.latest.get("world", "-"),
            reply=self.face.latest.get("reply", "-"),
        )
        self.face.set_state(hearing="Transcribing speech", event="Processing microphone input")
        self.face.set_runtime_state("stt", "running")
        self.face.console_log.log("[brain] Running STT")
        stt_started = time.perf_counter()
        text_input = await self.run_blocking(self.agent.run, mic_audio, model_kind="stt")
        stt_ms = (time.perf_counter() - stt_started) * 1000
        self.face.set_runtime_state("stt", "ready")
        self.face.console_log.log(f"[brain] STT completed in {stt_ms:.0f}ms")
        transcript = self.face.clip_text(text_input or heard_summary or "No speech detected", 64)
        self.face.console_log.log(f"[brain] Transcript: {transcript}")
        await self.emit_reasoning_event(
            SpeechFact(
                transcript=transcript,
                stt_ms=stt_ms,
                heard_summary=heard_summary,
            )
        )

    async def process_task(self, live=None):
        last_render_key = None
        latest_camera_frame = None
        latest_vision_label = self.state.startup_vision_label or "-"
        latest_world_summary = self.state.latest_world_summary or "-"
        latest_scene_summary = self.state.latest_scene_summary or latest_vision_label
        while not self.shutdown_event.is_set():
            if not self.face.agent_status.get("enabled", False) or self.face.agent_status.get("revoked", False):
                self.face.set_state(event="Agent disabled or revoked", reasoning="Paused", speaking="Muted")
                self.face.set_latest(hearing="-", vision="-", world="-", reply="-")
                if live is not None:
                    panel = self.face.render_status("-", "-", "-")
                    key = self.face.render_key("-", "-", "-")
                    if key != last_render_key:
                        live.update(panel, refresh=True)
                        last_render_key = key
                await asyncio.sleep(1)
                continue

            try:
                item = await asyncio.wait_for(self.reasoning_queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                item = None

            if item is None:
                self.face.set_state(vision="Scanning", reasoning="Waiting for speech")
                if live is not None:
                    panel = self.face.render_status(
                        self.face.latest.get("hearing", "-"),
                        self.face.latest.get("vision", "-"),
                        self.face.latest.get("reply", "-"),
                    )
                    key = self.face.render_key(
                        self.face.latest.get("hearing", "-"),
                        self.face.latest.get("vision", "-"),
                        self.face.latest.get("reply", "-"),
                    )
                    if key != last_render_key:
                        live.update(panel)
                        last_render_key = key
                continue

            try:
                if isinstance(item, SystemFact) and item.name == "camera":
                    latest_camera_frame = item.payload.get("frame")
                    if latest_camera_frame is not None:
                        await self.publish_camera_fact(
                            latest_camera_frame,
                            latest_vision_label,
                            self.state.awaiting_tts_done,
                        )
                    continue

                if isinstance(item, SystemFact) and item.name == "tts_done":
                    self.turn_active = False
                    self.state.awaiting_tts_done = False
                    self.body.speech_enabled = True
                    self.face.set_state(
                        expression="neutral",
                        gaze="center",
                        body=self.face.ui_state.get("body", "Idle"),
                        tools="Idle",
                        speaking="Idle" if self.body.tts_available else "Unavailable",
                        reasoning="Waiting for speech",
                        event="Ready for next interaction",
                    )
                    continue

                if isinstance(item, SystemFact) and item.name == "body_control_done":
                    self.face.set_state(event=f"Body ready: {item.payload.get('action', 'idle')}")
                    continue

                if isinstance(item, VisionFact):
                    latest_camera_frame = item.frame
                    latest_vision_label = item.vision_label or latest_vision_label
                    latest_world_summary = item.world_summary or latest_world_summary
                    latest_scene_summary = latest_world_summary or latest_vision_label or "-"
                    self.state.latest_camera_frame = latest_camera_frame
                    self.state.latest_world_summary = latest_world_summary
                    self.state.latest_scene_summary = latest_scene_summary
                    intent = body_intent_for_world(latest_world_summary)
                    decision = self.governor.approve(intent)
                    self.set_governor_state(decision)
                    if decision.allowed:
                        action = intent.payload.get("action", "idle")
                        self.face.set_state(body=str(action))
                        await self.emit_output_event(BodyAction(action=action, source=intent.source))
                    await self.emit_face_intent(
                        world_summary=latest_world_summary,
                        reasoning=self.face.ui_state.get("reasoning", "Waiting for speech"),
                        speaking=self.face.ui_state.get("speaking", "Idle"),
                        event="Speaking reply" if self.state.awaiting_tts_done else "Waiting for speech",
                    )
                    self.face.set_state(
                        vision=self.face.clip_text(latest_vision_label or "No scene update", 64),
                        world=self.face.clip_text(latest_world_summary or "Scene clear", 64),
                        latency={"vision": item.vision_ms or None},
                        event="Speaking reply" if self.state.awaiting_tts_done else "Waiting for speech",
                    )
                    self.face.set_latest(
                        hearing=self.face.latest.get("hearing", "-"),
                        vision=latest_vision_label or "-",
                        world=latest_world_summary or "-",
                        reply=self.face.latest.get("reply", "-"),
                    )
                    if live is not None:
                        panel = self.face.render_status(
                            self.face.latest.get("hearing", "-"),
                            self.face.latest.get("vision", "-"),
                            self.face.latest.get("reply", "-"),
                        )
                        key = self.face.render_key(
                            self.face.latest.get("hearing", "-"),
                            self.face.latest.get("vision", "-"),
                            self.face.latest.get("reply", "-"),
                        )
                        if key != last_render_key:
                            live.update(panel)
                            last_render_key = key
                    continue

                if isinstance(item, SystemFact) and item.name == "microphone":
                    self.turn_active = True
                    self.body.speech_enabled = False
                    mic_audio = item.payload.get("audio")
                    heard_summary = item.payload.get("summary") or "No speech"
                    await self.publish_microphone_fact(mic_audio, heard_summary)
                    continue

                if not isinstance(item, SpeechFact):
                    continue

                turn_started = time.perf_counter()
                stt_ms = float(item.stt_ms or 0)
                vision_ms = 0
                language_ms = 0
                embedding_ms = 0

                text_input = item.transcript or "No speech detected"
                self.face.set_state(hearing=text_input)
                await self.emit_face_intent(
                    world_summary=latest_world_summary,
                    reasoning="Processing speech",
                    speaking=self.face.ui_state.get("speaking", "Idle"),
                    event="Processing microphone input",
                )
                if live is not None:
                    panel = self.face.render_status(
                        text_input,
                        latest_vision_label or "-",
                        self.face.latest.get("reply", "-"),
                    )
                    key = self.face.render_key(
                        text_input,
                        latest_vision_label or "-",
                        self.face.latest.get("reply", "-"),
                    )
                    if key != last_render_key:
                        live.update(panel)
                        last_render_key = key
                self.face.set_latest(
                    hearing=text_input,
                    vision=latest_vision_label or "-",
                    world=latest_world_summary or "-",
                    reply=self.face.latest.get("reply", "-"),
                )

                vision_label = latest_vision_label if latest_vision_label != "-" else None
                if self.live_vision_enabled and latest_camera_frame is not None:
                    self.face.set_state(vision="Analyzing scene", event="Running vision model")
                    self.face.set_runtime_state("vision", "running")
                    if self.world_enabled:
                        self.face.set_runtime_state("world", "running")
                    self.face.console_log.log("[brain] Running vision/world update")
                    vision_started = time.perf_counter()
                    vision_input = Image.fromarray(latest_camera_frame)
                    vision_raw = await self.run_blocking(self.agent.run, vision_input, model_kind="vision")
                    vision_ms = (time.perf_counter() - vision_started) * 1000
                    self.face.set_runtime_state("vision", "ready")
                    if self.world_enabled:
                        self.face.set_runtime_state("world", "ready")
                    self.face.console_log.log(f"[brain] Vision completed in {vision_ms:.0f}ms")
                    vision_label = str(vision_raw).strip() if vision_raw is not None else None
                    latest_vision_label = vision_label or latest_vision_label
                    latest_world_summary = self.compute_world_summary(latest_camera_frame, latest_vision_label)
                    latest_scene_summary = latest_world_summary or latest_vision_label or "-"
                    self.state.latest_camera_frame = latest_camera_frame
                    self.state.latest_world_summary = latest_world_summary
                    self.state.latest_scene_summary = latest_scene_summary
                    intent = body_intent_for_world(latest_world_summary)
                    decision = self.governor.approve(intent)
                    self.set_governor_state(decision)
                    if decision.allowed:
                        action = intent.payload.get("action", "idle")
                        self.face.set_state(body=str(action))
                        await self.emit_output_event(BodyAction(action=action, source=intent.source))
                    await self.emit_face_intent(
                        world_summary=latest_world_summary,
                        reasoning=self.face.ui_state.get("reasoning", "Waiting for speech"),
                        speaking=self.face.ui_state.get("speaking", "Idle"),
                        event="Running vision model",
                    )
                    self.face.set_state(
                        vision=self.face.clip_text(latest_vision_label or "No scene update", 64),
                        world=self.face.clip_text(latest_world_summary or "Scene clear", 64),
                    )

                tool_response = await self.maybe_use_tool(text_input)
                if tool_response is not None:
                    response_text = str(tool_response).strip()
                    self.face.set_state(reasoning="Tool response ready")
                    language_ms = 0
                else:
                    prior_memory = self.state.latest_memory_context()
                    if str(self.face.latest.get("reply", "")).startswith("Turn failed:"):
                        self.face.clear_latest_reply("-")
                    self.face.set_latest(
                        hearing=text_input,
                        vision=latest_vision_label or "-",
                        world=latest_world_summary or "-",
                        reply="Thinking...",
                    )
                    self.face.set_state(
                        tools="Idle",
                        reasoning="Composing response",
                        event="Running language model",
                        speaking="Thinking",
                    )
                    self.face.set_runtime_state("language", "running")
                    self.face.console_log.log("[brain] Running language model")
                    await self.emit_face_intent(
                        world_summary=latest_world_summary,
                        reasoning="Composing response",
                        speaking="Thinking",
                        event="Running language model",
                    )
                    if self.language_warm_task is not None and not self.language_warm_task.done():
                        self.face.set_state(event="Waiting for language model warmup")
                        await self.language_warm_task
                        self.state.language_warm_done = True
                    language_started = time.perf_counter()
                    language_prompt = self.build_language_prompt(
                        text_input=text_input,
                        vision_label=vision_label,
                        world_summary=latest_world_summary,
                        prior_memory=prior_memory,
                    )
                    try:
                        response_text = str(await asyncio.wait_for(
                            self.run_blocking(self.agent.run, language_prompt, model_kind="language"),
                            timeout=float(os.getenv("ROBOT_LANGUAGE_TIMEOUT_S", "45")),
                        )).strip()
                    except Exception as exc:
                        self.face.set_runtime_state("language", "error")
                        self.face.console_log.log(f"Language generation failed: {exc}")
                        self.face.console_log.log(traceback.format_exc())
                        raise
                    language_ms = (time.perf_counter() - language_started) * 1000
                    if self.response_looks_invalid(response_text):
                        self.face.console_log.log(f"Language generation failed: malformed text: {response_text!r}")
                        response_text = self.safe_language_fallback(text_input)
                        self.face.console_log.log(f"[brain] Using safe fallback reply: {response_text}")
                    self.face.set_runtime_state("language", "ready")
                    self.face.console_log.log(f"[brain] Language completed in {language_ms:.0f}ms")
                    self.face.set_state(reasoning=self.face.clip_text(response_text or "No response generated", 64))
                    if str(self.face.latest.get("reply", "")).startswith("Turn failed:"):
                        self.face.clear_latest_reply("-")
                    await self.emit_face_intent(
                        world_summary=latest_world_summary,
                        reasoning=response_text or "No response generated",
                        speaking="Queued for playback" if self.body.tts_available else "Idle",
                        event="Reply ready",
                    )

                self.face.set_state(event="Updating memory")
                self.face.set_runtime_state("embedding", "running")
                self.face.console_log.log("[brain] Running embedding/memory update")
                try:
                    embedding_out = await self.run_blocking(self.agent.run, response_text, model_kind="embedding")
                    if embedding_out is None:
                        raise RuntimeError("Embedding model returned no vector")
                    vec = np.asarray(embedding_out, dtype="float32")
                    if vec.size == 0:
                        raise RuntimeError("Embedding model returned an empty vector")
                    vec = vec.reshape(1, -1)
                    if vec.size > 0:
                        if self.state.faiss_index is None:
                            self.state.faiss_index = faiss.IndexFlatL2(vec.shape[1])
                            self.face.console_log.log(f"Initialized memory index dim={vec.shape[1]}")
                        elif self.state.faiss_index.d != vec.shape[1]:
                            self.face.console_log.log(
                                f"Embedding dim changed {self.state.faiss_index.d} to {vec.shape[1]}; resetting index."
                            )
                            self.state.faiss_index = faiss.IndexFlatL2(vec.shape[1])
                            self.state.faiss_responses = []
                        self.state.faiss_index.add(vec)
                        self.state.faiss_responses.append(response_text)
                        self.face.set_state(memory=f"{self.state.faiss_index.ntotal} memories")
                        self.face.set_runtime_state("embedding", "ready")
                    else:
                        self.face.set_state(event="Embedding unavailable")
                        self.face.set_runtime_state("embedding", "unavailable")
                except Exception as exc:
                    self.face.set_runtime_state("embedding", "error")
                    self.face.console_log.log(f"FAISS memory error: {exc}")
                    self.face.console_log.log(traceback.format_exc())
                if not self.response_looks_invalid(response_text):
                    self.state.append_response(response_text)
                    self.face.set_state(memory=f"{len(self.state.stored_responses)} memories")

                augmented_text = response_text
                turn_ms = (time.perf_counter() - turn_started) * 1000
                latency = {
                    "turn": turn_ms,
                    "stt": stt_ms,
                    "vision": vision_ms if vision_ms > 0 else None,
                    "language": language_ms,
                    "embedding": None,
                }
                self.face.set_state(latency=latency)
                self.face.console_log.log(
                    f"Latency turn={turn_ms:.0f} stt={stt_ms:.0f} vision={vision_ms:.0f} "
                    f"lang={language_ms:.0f}"
                )

                if self.body.tts_available:
                    await self.emit_output_event(SpeakAction(text=augmented_text))
                else:
                    self.turn_active = False
                    self.body.speech_enabled = True

                self.face.set_latest(
                    hearing=text_input,
                    vision=latest_vision_label or "-",
                    world=latest_world_summary or "-",
                    reply=augmented_text,
                )
                if live is not None:
                    panel = self.face.render_status(text_input, latest_vision_label or "-", augmented_text)
                    key = self.face.render_key(text_input, latest_vision_label or "-", augmented_text)
                    if key != last_render_key:
                        live.update(panel)
                        last_render_key = key
            except Exception as exc:
                detail = str(exc).strip() or exc.__class__.__name__
                if isinstance(exc, asyncio.TimeoutError):
                    detail = "Language model timed out"
                self.face.set_runtime_state("stt", "ready")
                self.face.set_runtime_state("vision", "ready")
                if self.world_enabled:
                    self.face.set_runtime_state("world", "ready")
                current_language_state = str(self.face.ui_state.get("runtime", {}).get("language", "")).lower()
                if current_language_state in {"running", "warming", "error"}:
                    self.face.set_runtime_state("language", "error")
                self.face.set_runtime_state("embedding", "ready")
                self.face.set_state(event=f"Processing error: {detail}", reasoning="Error")
                await self.emit_face_intent(
                    world_summary=self.face.latest.get("world", "-"),
                    reasoning="Error",
                    speaking="Idle",
                    event=f"Processing error: {detail}",
                )
                self.face.set_latest(
                    hearing=self.face.latest.get("hearing", "-"),
                    vision=self.face.latest.get("vision", "-"),
                    world=self.face.latest.get("world", "-"),
                    reply=f"Turn failed: {detail}",
                )
                self.face.console_log.log(f"Processing error: {detail}")
                self.face.console_log.log(traceback.format_exc())
                if not self.state.awaiting_tts_done:
                    self.turn_active = False
                    self.body.speech_enabled = True
            finally:
                if item is not None:
                    self.reasoning_queue.task_done()

            await asyncio.sleep(0.1)
