#!/usr/bin/env python3
"""Personalize a HelloWorld peer (verify + personalize, optionally prepare_runtime).

Designed to be run *inside a peer dir* (helloworld-a/-b/-c/...) after
quickstart.sh has populated the parent helloworld/ template with valid
MODEL_TEMPLATE_ID and AGENT_TEMPLATE_ID. Mirrors the verify/personalize
phases of helloworld_agent.py:67-110 but exits before the chat loop.

Used by demo/setup.sh as the per-peer "create instance DID + cert" step.

Env:
  SETUP_PREPARE_RUNTIME=1     also call agent.prepare_runtime() to download
                              + cache model artifacts (use for B only).
"""
import os
import sys
import time

from ephapsys.agent import TrustedAgent


def main() -> int:
    agent = TrustedAgent.from_env()

    verified, _ = agent.verify()
    if not verified:
        status = agent.get_status()
        is_personalized = (
            status.get("state", {}).get("personalized", False)
            or status.get("personalized", False)
        )
        if not is_personalized:
            anchor = os.getenv("PERSONALIZE_ANCHOR", "tpm")
            print(f"  + personalizing (anchor={anchor})…", flush=True)
            agent.personalize(anchor=anchor)
            for _ in range(5):
                verified, _ = agent.verify()
                if verified:
                    break
                time.sleep(1)

        if not verified:
            print("  ! agent not ready after personalization", file=sys.stderr)
            return 1

    print(f"  + verified: agent_id={agent.agent_id}", flush=True)

    if os.environ.get("SETUP_PREPARE_RUNTIME", "0") == "1":
        print("  + preparing runtime (downloading model artifacts)…", flush=True)
        agent.prepare_runtime()
        print("  + runtime ready", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
