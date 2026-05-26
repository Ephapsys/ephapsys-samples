#!/usr/bin/env python3
"""
Trainer script for Reinforcement Learning (RL) with ephaptic coupling integration.

This trainer streams per-episode metrics (reward, success_rate) back to the AOC,
so the frontend UI can render live charts during training.

Usage flow:
- Minimal CLI args: --base_url, --api_key, --model_template_id, --outdir
- All hyperparameters, environment config, and model_id are fetched dynamically
  from the backend template created in the UI.
- The trainer does not accept manual tuning flags for variant, epsilon, dataset, etc.;
  these must be specified in the Modulation config of the template.

Before starting a job in the UI:

1. Create a Model Template (via the Create Model page):
   - Source: Custom or External
   - Provider: Hugging Face or custom repo
   - Repository ID: <your-rl-model-repo>
   - Model Kind: rl
   - Register immediately (so a provenance certificate is issued)

2. Go to the Modulator page for this template:
   - Variant: additive or multiplicative
   - Hyperparameters: epsilon (ε), lambda0 (λ₀), phi (activation), ecm_init
   - MaxSteps: number of episodes to evaluate
   - KPI Targets: enable RL KPIs (reward, success_rate)
"""


import os, sys, json, datetime, argparse
from ephapsys.modulation import ModulatorClient, compute_indispensability_loss, run_ablation_probe

GREEN = "\033[92m"
YELLOW = "\033[93m"
RESET = "\033[0m"


class _RLPlaceholderArtifact:
    """Minimal artifact shim so finalize_and_certify can export RL sample outputs."""

    def named_parameters(self):
        return []

    def save_pretrained(self, outdir: str):
        os.makedirs(outdir, exist_ok=True)
        payload = {
            "kind": "rl",
            "artifact": "placeholder",
            "note": "Current RL sample streams synthetic metrics and does not persist a real policy checkpoint yet.",
        }
        with open(os.path.join(outdir, "placeholder_rl_artifact.json"), "w") as f:
            json.dump(payload, f, indent=2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base_url", type=str, default=os.getenv("AOC_BASE_URL", os.getenv("BASE_URL", "http://localhost:7001")))
    parser.add_argument("--api_key", type=str, default=os.getenv("AOC_MODULATION_TOKEN", ""))
    parser.add_argument("--model_template_id", type=str, required=True)   # <- still required
    parser.add_argument("--outdir", type=str, default="./out")
    parser.add_argument("--auto_start", type=int, default=int(os.getenv("AUTO_START", "1")),
        help="1=auto-call /modulation/start (default), 0=manual mode (UI must start job)")
    args = parser.parse_args()

    if not args.api_key:
        raise RuntimeError("API token missing. Provide --api_key or set AOC_MODULATION_TOKEN in the environment")


    # --- Create base outdir + timestamped run subdir ---
    os.makedirs(args.outdir, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(args.outdir, f"run_{timestamp}")
    os.makedirs(run_dir, exist_ok=True)
    print(f"[INFO] Run directory created: {run_dir}")

    # --- Setup client ---
    mc = ModulatorClient(args.base_url, args.api_key)

    if args.auto_start:
        print("[INFO] Auto-starting modulation job with full config...")

        # --- Check for existing running job ---
        tpl_existing = mc.get_template_or_die(args.model_template_id)
        mod = tpl_existing.get("Modulation") or {}
        if mod.get("status") == "running":
            old_job = mod.get("job_id")
            print(f"[WARN] Previous job still running (job_id={old_job}). Stopping it first...")
            try:
                mc.stop_job(job_id=old_job, model_template_id=args.model_template_id)
                tpl_existing = mc.get_template_or_die(args.model_template_id)
                mod = tpl_existing.get("Modulation")
            except Exception as e:
                print(f"[WARN] Failed to stop old job cleanly: {e}")
                exit(1)

        # maxSteps precedence (#119): explicit AOC_STEPS_PER_TRIAL env
        # (headless/CI override) > UI-configured DesiredModulation.kpi.maxSteps
        # > built-in default. For RL, maxSteps is the number of episodes.
        _env_steps = os.getenv("AOC_STEPS_PER_TRIAL")
        _ui_steps = ((tpl_existing.get("DesiredModulation") or {}).get("kpi") or {}).get("maxSteps")
        if _env_steps is not None:
            max_steps_per_trial = int(_env_steps)
        elif _ui_steps:
            max_steps_per_trial = int(_ui_steps)
            print(f"[INFO] Using UI-configured maxSteps={max_steps_per_trial} from template DesiredModulation")
        else:
            max_steps_per_trial = 10
        kpi = {
            "targets": [
                {"name": "reward", "direction": "max", "weight": 1},
                {"name": "success_rate", "direction": "max", "weight": 1},
            ],
            "maxSteps": max_steps_per_trial,
        }
        search = {
            "algo": "bayes",
            "budget": 1,
            "parallel": 1,
            "multi_objective": True,
            "space": {
                "epsilon": {"low": 0.0, "high": 2.0},
                "lambda0": {"low": 0.0, "high": 0.5},
                "phi": ["identity", "relu", "tanh", "silu", "gelu"],
                "ecm_init": ["transpose", "identity", "random"],
                "variant": ["additive", "multiplicative"],
            },
        }

        # --- Start a fresh job ---
        # RL is environment-based, not dataset-based — omit `dataset` (start_job
        # treats it as optional and the RL trainer doesn't read it from the recipe).
        mc.start_job(
            args.model_template_id,
            variant="additive",
            kpi=kpi,
            mode="auto",
            search=search,
        )
    else:
        print("[INFO] AUTO_START=0 → skipping /modulation/start, waiting for UI job...")

    # --- Block until job_id is available ---
    tpl, job_id = mc.wait_for_job_id(args.model_template_id)
    recipe = tpl.get("DesiredModulation") or {}

    # --- Extract config from recipe ---
    mode = recipe.get("mode") or "manual"
    variant = recipe.get("variant")
    episodes = int((recipe.get("kpi") or {}).get("maxSteps") or 10)
    if not variant:
        raise ValueError("Trainer requires 'variant' in recipe (additive or multiplicative).")

    # --- Governance mode and indispensability config ---
    governance_mode = recipe.get("governance_mode", "standard")
    indisp_cfg = recipe.get("indispensability") or {}
    mod_block = tpl.get("Modulation") or {}
    if not indisp_cfg and mod_block.get("indispensability"):
        indisp_cfg = mod_block["indispensability"]
    if not governance_mode or governance_mode == "standard":
        governance_mode = mod_block.get("governance_mode", "standard")
    is_indispensable = governance_mode == "indispensable" or indisp_cfg.get("enabled", False)

    print("=== JOB CONFIG FROM BACKEND ===")
    print(f"Job ID:      {job_id}")
    print(f"Mode:        {mode}")
    print(f"Variant:     {variant}")
    print(f"Episodes:    {episodes}")
    print(f"Run Dir:     {run_dir}")
    print("================================")

    summary = {
        "job_id": job_id,
        "mode": mode,
        "variant": variant,
        "episodes": episodes,
        "run_dir": run_dir,
    }

    # --- Run evaluation with streaming metrics ---
    last = None
    all_metrics = []
    for update in mc.compute_rl_metrics_stream(
        args.model_template_id,
        episodes=episodes
    ):
        last = update
        all_metrics.append(update)

    print(f"{GREEN}Final aggregated metrics: {last}{RESET}")

    # --- Indispensability ablation probe ---
    indisp_metrics = None
    if is_indispensable:
        print("[INDISPENSABLE] Running ablation probe (skipped for RL — no direct model input).")

    # --- Report back to backend ---
    placeholder = _RLPlaceholderArtifact()
    mc.finalize_and_certify(
        run_dir,
        placeholder,
        placeholder,
        last,
        variant,
        job_id,
        args.model_template_id,
        all_metrics=all_metrics,
        indispensability_metrics=indisp_metrics,
    )
    print(f"{GREEN}Reported metrics to backend and certified results.{RESET}")

    # --- Always write summary.json ---
    summary_path = os.path.join(run_dir, "summary.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"[INFO] Summary written to {summary_path}")


if __name__ == "__main__":
    main()
