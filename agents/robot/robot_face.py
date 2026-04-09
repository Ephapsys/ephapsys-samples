#!/usr/bin/env python3

import asyncio
import json
import os
import re
import time

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text


class SilentConsole:
    def print(self, *args, **kwargs):
        return None

    def log(self, *args, **kwargs):
        return None


class FaceLogConsole:
    def __init__(self, face, *, stderr=True):
        self.face = face
        self.console = Console(stderr=stderr)

    def _record(self, *args):
        text = " ".join(str(arg) for arg in args if arg is not None).strip()
        if text:
            self.face.add_debug_log(text)
        return text

    def print(self, *args, **kwargs):
        text = self._record(*args)
        if text:
            self.console.print(*args, **kwargs)

    def log(self, *args, **kwargs):
        text = self._record(*args)
        if text:
            self.console.log(*args, **kwargs)


class FaceLogRecorder:
    def __init__(self, face, sink=None):
        self.face = face
        self.sink = sink

    def _record(self, *args):
        text = " ".join(str(arg) for arg in args if arg is not None).strip()
        if text:
            self.face.add_debug_log(text)
            if self.sink is not None:
                try:
                    self.sink.write(f"{text}\n")
                    self.sink.flush()
                except Exception:
                    pass

    def print(self, *args, **kwargs):
        self._record(*args)

    def log(self, *args, **kwargs):
        self._record(*args)


class RobotFaceBase:
    def __init__(self):
        self.agent_status = {"verified": False, "enabled": False, "revoked": False}
        self.ui_state = {
            "hearing": "Stand by",
            "vision": "Stand by",
            "world": "Stand by",
            "expression": "neutral",
            "gaze": "center",
            "body": "Stand by",
            "tools": "Idle",
            "governor": "Ready",
            "reasoning": "Starting brain",
            "speaking": "Stand by",
            "memory": "0 memories",
            "latency": {
                "turn": None,
                "stt": None,
                "vision": None,
                "language": None,
                "embedding": None,
                "tts": None,
            },
            "runtime": {
                "stt": "loading",
                "vision": "loading",
                "world": "loading",
                "language": "loading",
                "embedding": "loading",
                "tts": "loading",
                "vocoder": "loading",
            },
            "event": "Starting brain",
        }
        self.latest = {"hearing": "-", "vision": "-", "world": "-", "reply": "-"}
        self.activity_log = []
        self.debug_log = []
        self._activity_frames = ["◜", "◠", "◝", "◞", "◡", "◟"]
        self.console_log = FaceLogConsole(self, stderr=True)

    def add_activity(self, label, value, limit=12):
        entry = f"{label}: {self.clip_text(value, 100)}"
        if self.activity_log and self.activity_log[-1] == entry:
            return
        self.activity_log.append(entry)
        if len(self.activity_log) > limit:
            self.activity_log = self.activity_log[-limit:]

    def add_debug_log(self, value, limit=10):
        entry = self.clip_text(re.sub(r"\s+", " ", str(value or "").strip()), 160)
        if not entry:
            return
        if self.debug_log and self.debug_log[-1] == entry:
            return
        self.debug_log.append(entry)
        if len(self.debug_log) > limit:
            self.debug_log = self.debug_log[-limit:]

    def set_state(self, **kwargs):
        for key, value in kwargs.items():
            if key in self.ui_state and value is not None:
                previous = self.ui_state.get(key)
                if key == "latency" and isinstance(value, dict):
                    merged = dict(self.ui_state.get("latency", {}))
                    merged.update(value)
                    self.ui_state[key] = merged
                elif key == "runtime" and isinstance(value, dict):
                    merged = dict(self.ui_state.get("runtime", {}))
                    merged.update({k: str(v) for k, v in value.items()})
                    self.ui_state[key] = merged
                else:
                    self.ui_state[key] = str(value)
                if key == "event" and str(value) != str(previous):
                    self.add_activity("Event", value)

    def set_runtime_state(self, key: str, value: str):
        runtime = dict(self.ui_state.get("runtime", {}))
        runtime[str(key)] = str(value)
        self.ui_state["runtime"] = runtime

    def set_latest(self, hearing=None, vision=None, world=None, reply=None):
        if hearing is not None:
            hearing = str(hearing)
            if hearing != self.latest["hearing"] and hearing != "-":
                self.add_activity("Heard", hearing)
            self.latest["hearing"] = hearing
        if vision is not None:
            vision = str(vision)
            if vision != self.latest["vision"] and vision != "-":
                self.add_activity("Vision", vision)
            self.latest["vision"] = vision
        if world is not None:
            world = str(world)
            if world != self.latest["world"] and world != "-":
                self.add_activity("World", world)
            self.latest["world"] = world
        if reply is not None:
            reply = str(reply)
            if reply != self.latest["reply"] and reply != "-":
                self.add_activity("Reply", reply)
            self.latest["reply"] = reply

    def clear_latest_reply(self, value: str = "-"):
        self.latest["reply"] = str(value)

    @staticmethod
    def clip_text(value, limit=88):
        text = str(value or "-").strip()
        if len(text) <= limit:
            return text
        return text[: limit - 1].rstrip() + "…"

    @staticmethod
    def inline_text(value, width):
        return RobotFaceBase.clip_text(value, width).ljust(width)

    def in_startup_transition(self):
        event = str(self.ui_state.get("event", "")).lower()
        reasoning = str(self.ui_state.get("reasoning", "")).lower()
        speaking = str(self.ui_state.get("speaking", "")).lower()
        transitional_tokens = (
            "starting",
            "verifying",
            "personalizing",
            "preparing",
            "loading",
            "observing",
            "warming",
            "greeting",
            "binding",
            "checking trusted state",
            "trusted runtime ready",
            "waiting for language model warmup",
            "processing",
            "analyzing",
            "scanning",
            "transcribing",
            "queued",
        )
        combined = f"{event} {reasoning} {speaking}"
        return any(token in combined for token in transitional_tokens)

    def animated_value(self, key, value, width):
        text = str(value or "-")
        lowered = text.lower()
        spinner = self.pulse(self._activity_frames, 10)
        if key in {"vision", "world"} and lowered == "stand by" and self.in_startup_transition():
            text = f"warming {spinner}"
        elif key == "hearing" and lowered == "stand by" and self.in_startup_transition():
            text = f"arming sensors {spinner}"
        elif key == "speaking" and lowered == "stand by" and self.in_startup_transition():
            text = f"warming voice {spinner}"
        elif key in {"reasoning", "event"} and self.in_startup_transition():
            if not re.search(r"[◜◠◝◞◡◟◴◷◶◵▖▘▝▗]$", text):
                text = f"{text} {spinner}"
        return self.inline_text(text, width)

    def activity_pulse_line(self, width):
        spinner = self.pulse(self._activity_frames, 10)
        candidates = [
            ("Event", self.ui_state.get("event")),
            ("Reasoning", self.ui_state.get("reasoning")),
            ("Speaking", self.ui_state.get("speaking")),
            ("Vision", self.ui_state.get("vision")),
            ("World", self.ui_state.get("world")),
            ("Hearing", self.ui_state.get("hearing")),
        ]
        for label, value in candidates:
            text = str(value or "").strip()
            if not text or text == "-" or text.lower() == "stand by":
                continue
            return self.inline_text(f"{spinner} {label}: {text}", width)
        return self.inline_text(f"{spinner} Waiting for robot activity", width)

    def format_status(self):
        if not self.agent_status.get("verified", False) and self.ui_state.get("event") in {
            "Starting brain",
            "Booting robot runtime",
            "Verifying agent",
            "Personalizing agent instance",
            "Preparing runtime bundles",
            "Agent verified",
        }:
            return ("STARTING", "cyan")
        if self.agent_status.get("revoked", False):
            return ("REVOKED", "red")
        if not self.agent_status.get("enabled", False):
            return ("DISABLED", "red")
        if not self.agent_status.get("verified", False):
            return ("VERIFYING", "yellow")
        return ("ENABLED", "green")

    def snapshot(self):
        return {
            "agent_status": dict(self.agent_status),
            "ui_state": {
                **self.ui_state,
                "latency": dict(self.ui_state.get("latency", {})),
                "runtime": dict(self.ui_state.get("runtime", {})),
            },
            "latest": dict(self.latest),
            "activity_log": list(self.activity_log),
            "debug_log": list(self.debug_log),
        }

    def startup(self):
        return None

    def live(self, greeting):
        raise NotImplementedError("Only interactive robot faces implement live rendering")

    @staticmethod
    def pulse(frames, speed=6):
        idx = int(time.time() * speed) % len(frames)
        return frames[idx]

    def format_presence(self):
        hearing = self.ui_state.get("hearing", "").lower()
        speaking = self.ui_state.get("speaking", "").lower()
        reasoning = self.ui_state.get("reasoning", "").lower()
        debug_tail = " ".join(self.debug_log[-6:]).lower()
        event = self.ui_state.get("event", "").lower()
        if "warming language model" in debug_tail and "language model warmup ready" not in debug_tail and "language warmup skipped" not in debug_tail:
            return f"[yellow]{self.pulse(['◴', '◷', '◶', '◵'], 8)} warming brain[/yellow]"
        if self.in_startup_transition():
            return f"[cyan]{self.pulse(['◜', '◠', '◝', '◞', '◡', '◟'], 10)} booting[/cyan]"
        if "playing" in speaking or "synthesizing" in speaking or "queued" in speaking:
            return f"[magenta]{self.pulse(['◜', '◠', '◝', '◞', '◡', '◟'], 10)} speaking[/magenta]"
        if "transcribing" in hearing or "speech detected" in hearing or "capturing" in self.ui_state.get("event", "").lower():
            return f"[cyan]{self.pulse(['▖', '▘', '▝', '▗'], 12)} listening[/cyan]"
        if "composing" in reasoning or "running language" in self.ui_state.get("event", "").lower():
            return f"[yellow]{self.pulse(['◴', '◷', '◶', '◵'], 8)} thinking[/yellow]"
        if "waiting for speech" in event and self.agent_status.get("enabled", False):
            return "[green]● ready[/green]"
        if self.agent_status.get("enabled", False):
            return "[green]● active[/green]"
        return "[cyan]◌ starting[/cyan]"

    def latency_text(self):
        latency = self.ui_state.get("latency", {})
        if not isinstance(latency, dict):
            return Text(str(latency), style="dim")
        text = Text()
        for label, key, style in (
            ("Turn", "turn", "bright_white"),
            ("STT", "stt", "cyan"),
            ("Vision", "vision", "green"),
            ("Language", "language", "yellow"),
            ("Embed", "embedding", "white"),
            ("TTS", "tts", "magenta"),
        ):
            value = latency.get(key)
            text.append(f"{label:<9}", style=f"bold {style}" if style != "white" else "bold white")
            if value is None:
                text.append("—\n", style="dim")
            else:
                text.append(f"{int(value)} ms\n", style=style)
        return text

    def runtime_text(self):
        runtime = self.ui_state.get("runtime", {})
        if not isinstance(runtime, dict):
            return Text(str(runtime), style="dim")
        text = Text()
        for label, key in (
            ("STT", "stt"),
            ("Vision", "vision"),
            ("World", "world"),
            ("Language", "language"),
            ("Embed", "embedding"),
            ("TTS", "tts"),
            ("Vocoder", "vocoder"),
        ):
            value = str(runtime.get(key, "loading")).upper()
            style = "green" if value == "READY" else "yellow" if value in {"LOADING", "WARMING", "RUNNING"} else "red" if value in {"ERROR", "UNAVAILABLE"} else "white"
            text.append(f"{label:<9}", style="bold white")
            text.append(f"{value}\n", style=style)
        return text


class RobotFace(RobotFaceBase):
    def __init__(self):
        super().__init__()
        self.console_live = Console(force_terminal=True)

    def render_status(self, hearing_text, vision_text, response_text):
        status_width = 88
        latest_width = 100
        activity_width = 110

        header = Text("ASIMOV", style="bold bright_cyan")
        header.append("  trusted multimodal robot", style="dim")
        header.append("   ")
        header.append_text(Text.from_markup(self.format_presence()))

        state = Text()
        status_label, status_style = self.format_status()
        state.append("Status    ", style="bold white")
        state.append(status_label, style=status_style)
        state.append("\n")
        state.append("Listening ", style="bold cyan")
        state.append(f"{self.animated_value('hearing', self.ui_state['hearing'], status_width)}\n")
        state.append("Vision    ", style="bold green")
        state.append(f"{self.animated_value('vision', self.ui_state['vision'], status_width)}\n")
        state.append("World     ", style="bold bright_green")
        state.append(f"{self.animated_value('world', self.ui_state['world'], status_width)}\n")
        state.append("Expression", style="bold magenta")
        state.append(f" {self.inline_text(self.ui_state['expression'], status_width - 1)}\n")
        state.append("Gaze      ", style="bold blue")
        state.append(f"{self.inline_text(self.ui_state['gaze'], status_width)}\n")
        state.append("Body      ", style="bold blue")
        state.append(f"{self.animated_value('body', self.ui_state['body'], status_width)}\n")
        state.append("Tools     ", style="bold white")
        state.append(f"{self.animated_value('tools', self.ui_state['tools'], status_width)}\n")
        state.append("Governor  ", style="bold red")
        state.append(f"{self.inline_text(self.ui_state['governor'], status_width)}\n")
        state.append("Reasoning ", style="bold yellow")
        state.append(f"{self.animated_value('reasoning', self.ui_state['reasoning'], status_width)}\n")
        state.append("Speaking  ", style="bold magenta")
        state.append(f"{self.animated_value('speaking', self.ui_state['speaking'], status_width)}\n")
        state.append("Event     ", style="bold white")
        state.append(f"{self.animated_value('event', self.ui_state['event'], status_width)}\n")
        state.append("Memory    ", style="bold white")
        state.append(f"{self.inline_text(self.ui_state['memory'], 24)}\n")
        turn = self.ui_state.get("latency", {}).get("turn")
        stt = self.ui_state.get("latency", {}).get("stt")
        language = self.ui_state.get("latency", {}).get("language")
        vision = self.ui_state.get("latency", {}).get("vision")
        tts = self.ui_state.get("latency", {}).get("tts")
        state.append("Latency   ", style="bold bright_white")
        if all(v is None for v in (turn, stt, language, vision, tts)):
            state.append("No turns yet", style="dim")
        else:
            parts = []
            if turn is not None:
                parts.append(f"turn {int(turn)}ms")
            if stt is not None:
                parts.append(f"stt {int(stt)}")
            if language is not None:
                parts.append(f"lang {int(language)}")
            if vision is not None:
                parts.append(f"vision {int(vision)}")
            if tts is not None:
                parts.append(f"tts {int(tts)}")
            state.append(" | ".join(parts))

        latest = Text()
        latest.append("Heard  ", style="bold cyan")
        latest.append(f"{self.inline_text(hearing_text or '-', latest_width)}\n")
        latest.append("Vision ", style="bold green")
        latest.append(f"{self.inline_text(vision_text or '-', latest_width)}\n")
        latest.append("World  ", style="bold bright_green")
        latest.append(f"{self.inline_text(self.latest.get('world', '-') or '-', latest_width)}\n")
        latest.append("Reply  ", style="bold yellow")
        latest.append(f"{self.inline_text(response_text or '-', latest_width)}")

        activity = Text()
        activity.append(self.activity_pulse_line(activity_width), style="bright_cyan")
        activity.append("\n")
        activity.append("─" * activity_width, style="dim")
        activity.append("\n")
        if self.activity_log:
            for entry in self.activity_log[-8:]:
                activity.append("• ", style="dim")
                activity.append(f"{self.inline_text(entry, activity_width)}\n")
        else:
            activity.append(self.inline_text("No activity yet", activity_width), style="dim")

        footer = Text()
        footer.append("Ctrl+C", style="bold")
        footer.append(" to exit", style="dim")

        debug = Text()
        if self.debug_log:
            for entry in self.debug_log[-6:]:
                debug.append("› ", style="dim")
                debug.append(f"{self.inline_text(entry, activity_width)}\n")
        else:
            debug.append(self.inline_text("No backend logs yet", activity_width), style="dim")

        return Panel(
            Group(
                header,
                Text(""),
                Panel(state, title="Status", border_style="cyan", padding=(1, 1)),
                Text(""),
                Panel(self.runtime_text(), title="Runtime", border_style="blue", padding=(1, 1)),
                Text(""),
                Panel(latest, title="Latest", border_style="green", padding=(1, 1)),
                Text(""),
                Panel(activity, title="Recent Activity", border_style="yellow", padding=(1, 1)),
                Text(""),
                Panel(debug, title="Live Brain Log", border_style="magenta", padding=(1, 1)),
                Text(""),
                footer,
            ),
            title="Robot Console",
            border_style="bright_blue",
            padding=(1, 2),
        )

    def render_key(self, hearing_text, vision_text, response_text):
        return (
            hearing_text or "-",
            vision_text or "-",
            response_text or "-",
            self.format_status(),
            self.ui_state["hearing"],
            self.ui_state["vision"],
            self.ui_state["expression"],
            self.ui_state["gaze"],
            self.ui_state["reasoning"],
            self.ui_state["speaking"],
            self.ui_state["memory"],
            self.ui_state["latency"],
            tuple(sorted(self.ui_state.get("runtime", {}).items())),
            self.ui_state["event"],
            tuple(self.debug_log[-6:]),
            int(time.time() * 4) if self.in_startup_transition() else 0,
        )

    def startup(self):
        self.console_live.print(Panel.fit("Asimov local runtime startup", border_style="bright_blue"))
        self.console_live.print("[bold cyan]Step 1[/bold cyan] Verify agent and prepare runtime")

    def live(self, greeting):
        return Live(
            self.render_status("-", "-", greeting),
            refresh_per_second=2,
            console=self.console_live,
            screen=False,
            auto_refresh=False,
        )


class RobotStateFace(RobotFaceBase):
    def __init__(self):
        super().__init__()
        self.console_live = SilentConsole()
        self._log_path = os.getenv("ROBOT_BRAIN_LOG_PATH", ".ephapsys_state/robot_brain.log")
        self._log_sink = None
        try:
            from pathlib import Path
            log_path = Path(self._log_path)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_sink = log_path.open("a", encoding="utf-8")
        except Exception:
            self._log_sink = None
        self.console_log = FaceLogRecorder(self, sink=self._log_sink)


async def run_terminal_face(ws_url: str):
    import websockets

    face = RobotFace()
    face.startup()
    last_key = None

    try:
        async with websockets.connect(ws_url, max_size=None) as websocket:
            first = json.loads(await websocket.recv())
            snapshot = first.get("snapshot", first)
            greeting = snapshot.get("latest", {}).get("reply") or "Asimov is starting..."
            with face.live(greeting) as live:
                while True:
                    try:
                        payload = json.loads(await asyncio.wait_for(websocket.recv(), timeout=0.25))
                        snapshot = payload.get("snapshot", payload)
                        face.agent_status.update(snapshot.get("agent_status", {}))
                        face.ui_state.update(snapshot.get("ui_state", {}))
                        face.latest.update(snapshot.get("latest", {}))
                        face.activity_log = snapshot.get("activity_log", [])
                        face.debug_log = snapshot.get("debug_log", [])
                    except asyncio.TimeoutError:
                        pass
                    key = face.render_key(
                        face.latest.get("hearing", "-"),
                        face.latest.get("vision", "-"),
                        face.latest.get("reply", "-"),
                    )
                    if key != last_key:
                        live.update(
                            face.render_status(
                                face.latest.get("hearing", "-"),
                                face.latest.get("vision", "-"),
                                face.latest.get("reply", "-"),
                            ),
                            refresh=True,
                        )
                        last_key = key
    except (asyncio.CancelledError, KeyboardInterrupt):
        return
    except Exception:
        raise
