#!/usr/bin/env python3
"""
HelloWorld Agent using Ephapsys SDK.

Workflow:
1. Load agent from environment (agent_id, API base/key).
2. Verify agent (status, certs, models) and personalize agent if required.
3. Prepare runtime (download/cache artifacts + decrypt ECM).
4. Enter a loop:
   - Re-verify agent status each cycle.
   - Prompt the user for text input.
   - Send input to the agent and print its response.
   - Exit gracefully on 'exit' or Ctrl+C.
"""

import os, sys, time, warnings, logging
from ephapsys.agent import TrustedAgent

# Suppress HF generation warnings for cleaner demo output
try:
    from transformers.utils import logging as hf_logging
    hf_logging.set_verbosity_error()
except Exception:
    pass
logging.getLogger("transformers").setLevel(logging.ERROR)
logging.getLogger("transformers.generation.utils").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", message="Setting `pad_token_id` to `eos_token_id`.*")

# Colors
BOLD = "\033[1m"
DIM = "\033[2m"
BLUE = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
GOLD = "\033[38;5;220m"
CYAN = "\033[38;5;87m"
MAGENTA = "\033[38;5;213m"
RESET = "\033[0m"

# Gradient colors for progress bar (blue → cyan → green → gold)
_GRAD = [
    "\033[38;5;27m",   # blue
    "\033[38;5;33m",   # blue-cyan
    "\033[38;5;39m",   # cyan
    "\033[38;5;45m",   # cyan-green
    "\033[38;5;48m",   # green-cyan
    "\033[38;5;42m",   # green
    "\033[38;5;46m",   # bright green
    "\033[38;5;118m",  # lime
    "\033[38;5;190m",  # yellow-green
    "\033[38;5;220m",  # gold
]


def phase(msg):
    print(f"\n  {GOLD}>>>{RESET} {BOLD}{msg}{RESET}")


def ok(msg):
    print(f"  {GREEN}+{RESET} {msg}")


def info(msg):
    print(f"  {BLUE}>{RESET} {msg}")


def main():
    agent = TrustedAgent.from_env()

    # ── Phase 1: Verification & Personalization ───────────────────
    phase("Verifying agent identity")
    t0 = time.perf_counter()

    # Suppress SDK warnings during verification (expected for first-time personalization)
    _sdk_logger = logging.getLogger("ephapsys.sdk")
    _prev_level = _sdk_logger.level
    _sdk_logger.setLevel(logging.ERROR)

    try:
        verified, report = agent.verify()
    except RuntimeError as e:
        if "404" in str(e):
            print(f"  {YELLOW}!{RESET} Agent template '{agent.agent_id}' not found in AOC.")
            print(f"  {DIM}Create it in the AOC before running this sample.{RESET}")
            sys.exit(1)
        else:
            raise

    if not verified:
        status = agent.get_status()
        is_personalized = status.get("state", {}).get("personalized", False) or status.get("personalized", False)
        if not is_personalized:
            anchor = os.getenv("PERSONALIZE_ANCHOR", "tpm")
            info(f"Personalizing agent (anchor={anchor})...")
            agent.personalize(anchor=anchor)
            info(f"Instance ID: {DIM}{agent.agent_id}{RESET}")
            for _ in range(5):
                verified, report = agent.verify()
                if verified:
                    break
                time.sleep(1)

        if not verified:
            print(f"  {YELLOW}!{RESET} Agent not ready after personalization.")
            sys.exit(1)

    _sdk_logger.setLevel(_prev_level)  # restore SDK logging
    verify_ms = (time.perf_counter() - t0) * 1000
    ok(f"Agent verified and personalized {DIM}({verify_ms:.0f}ms){RESET}")

    # ── Phase 2: Runtime preparation ─────────────────────────────
    phase("Preparing runtime")

    # Show expected download size upfront
    try:
        manifest = agent._fetch_manifest()
        total_size = 0
        for m in (manifest or {}).get("models", []):
            for art_meta in (m.get("artifact_urls") or {}).values():
                if isinstance(art_meta, dict):
                    total_size += int(art_meta.get("size") or 0)
        if total_size > 0:
            info(f"Downloading {total_size / (1024*1024):.0f} MB of model artifacts")
        else:
            info("Downloading model artifacts")
    except Exception:
        info("Downloading model artifacts")

    _t0 = time.perf_counter()
    _bar_width = 40
    _last_render = [0]

    def _gradient_bar(filled, total_w):
        """Build a gradient-colored progress bar."""
        bar = ""
        for i in range(total_w):
            if i < filled:
                color_idx = int((i / max(total_w - 1, 1)) * (len(_GRAD) - 1))
                bar += f"{_GRAD[color_idx]}{'█'}"
            else:
                bar += f"{DIM}{'░'}"
        return bar + RESET

    def _bouncing_bar(elapsed, total_w):
        """Build a colorful bouncing bar for unknown total."""
        bar = ""
        pos = int(elapsed * 4) % (total_w * 2)
        if pos >= total_w:
            pos = total_w * 2 - pos
        for i in range(total_w):
            if pos <= i < pos + 6:
                offset = i - pos
                color_idx = int((offset / 5) * (len(_GRAD) - 1))
                bar += f"{_GRAD[color_idx]}{'█'}"
            else:
                bar += f"{DIM}{'░'}"
        return bar + RESET

    def _render_progress(downloaded, total):
        now = time.perf_counter()
        if now - _last_render[0] < 0.15 and (total <= 0 or downloaded < total):
            return
        _last_render[0] = now
        elapsed = max(0.001, now - _t0)
        speed = (downloaded / (1024 * 1024)) / elapsed
        dl_mb = downloaded / (1024 * 1024)
        if total > 0:
            pct = min(100, int((downloaded * 100) / total))
            filled = (pct * _bar_width) // 100
            tot_mb = total / (1024 * 1024)
            eta = int((total - downloaded) / max(1, downloaded / elapsed)) if downloaded > 0 else 0
            bar = _gradient_bar(filled, _bar_width)
            pct_color = GREEN if pct > 80 else GOLD if pct > 40 else BLUE
            line = f"\r  {bar}  {pct_color}{pct:3d}%{RESET}  {dl_mb:.0f}/{tot_mb:.0f} MB  {DIM}{speed:.1f} MB/s  ETA {eta}s{RESET}    "
        else:
            bar = _bouncing_bar(elapsed, _bar_width)
            line = f"\r  {bar}  {dl_mb:.0f} MB  {DIM}{speed:.1f} MB/s  {elapsed:.0f}s{RESET}    "
        sys.stdout.write(line)
        sys.stdout.flush()

    try:
        try:
            rt = agent.prepare_runtime(progress_cb=_render_progress)
        except TypeError:
            # Fallback for SDK versions without progress_cb
            rt = agent.prepare_runtime()
    except Exception as e:
        sys.stdout.write("\r\033[2K")
        print(f"  {YELLOW}!{RESET} Runtime preparation failed: {e}")
        sys.exit(1)

    # Show completed bar
    sys.stdout.write("\r\033[2K")
    full_bar = "".join(f"{_GRAD[int(i / max(_bar_width-1,1) * (len(_GRAD)-1))]}█" for i in range(_bar_width))
    sys.stdout.write(f"  {full_bar}{RESET}  {GREEN}100%{RESET}  {DIM}done{RESET}\n")
    sys.stdout.flush()
    runtime_s = time.perf_counter() - _t0
    ok(f"Runtime ready {DIM}({runtime_s:.0f}s){RESET}")

    # ── Phase 3: Interactive chat ─────────────────────────────────
    phase("HelloWorld Chatbot")
    print(f"  {DIM}Type your message and press Enter. Type 'exit' to quit.{RESET}\n")

    turn_count = 0
    while True:
        try:
            verified, report = agent.verify()
            if not verified:
                print(f"  {YELLOW}!{RESET} Agent disabled or revoked. Waiting...")
                time.sleep(5)
                continue

            user_input = input(f"\n  {BOLD}You >{RESET} ").strip()
            if user_input.lower() in ("exit", "quit"):
                print(f"\n  {DIM}Goodbye.{RESET}")
                break
            if not user_input:
                continue

            t0 = time.perf_counter()
            result = agent.run(user_input, model_kind="language")
            inference_ms = (time.perf_counter() - t0) * 1000
            turn_count += 1

            reply = result.strip() if isinstance(result, str) else str(result)
            print(f"\n  {GREEN}{BOLD}Agent >{RESET} {reply}")
            print(f"  {DIM}turn {turn_count} | {inference_ms:.0f}ms{RESET}")

        except KeyboardInterrupt:
            print(f"\n  {DIM}Shutting down.{RESET}")
            sys.exit(0)
        except Exception as e:
            print(f"\n  {YELLOW}!{RESET} Error: {e}")
            time.sleep(2)

if __name__ == "__main__":
    main()
