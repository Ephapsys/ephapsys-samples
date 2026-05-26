#!/usr/bin/env python3
"""
Trainer script for World models (e.g. V-JEPA 2) with ephaptic coupling integration.

This trainer evaluates video classification models by processing video clips through
the model's video processor and measuring top-1/top-5 accuracy. It supports both
manual and auto (Bayesian search) modes, plus indispensable governance.

Usage flow:
- Minimal CLI args: --base_url, --api_key, --model_template_id, --outdir
- All training hyperparameters, dataset config, and model_id are fetched dynamically
  from the backend template created in the UI.

Before starting a job in the UI:

1. Create a Model Template (via the Create Model page):
   - Source: External repository
   - Provider: Hugging Face
   - Repository ID: facebook/vjepa2-vitl-fpc64-256
   - Model Kind: world
   - Revision: main
   - Hugging Face Token: hf_xxxxxxxx
   - Register immediately (so a provenance certificate is issued)

2. Go to the Modulator page for this template:
   - Variant: additive or multiplicative
   - Hyperparameters: epsilon, lambda0, phi, ecm_init
   - MaxSteps: number of video clips to evaluate
   - Dataset: name (e.g., kinetics700), config (e.g., default), split (e.g., test[:100])
   - KPI Targets: accuracy, top5_accuracy
"""

import os, sys, json, datetime, argparse, time
import torch
from copy import deepcopy

from ephapsys.modulation import ModulatorClient, compute_indispensability_loss, run_ablation_probe

BOLD = "\033[1m"
DIM = "\033[2m"
BLUE = "\033[36m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
GOLD = "\033[38;5;220m"
RESET = "\033[0m"


def phase(msg):
    print(f"\n  {GOLD}>>>{RESET} {BOLD}{msg}{RESET}")


def main():
    start_time = time.time()

    parser = argparse.ArgumentParser()
    parser.add_argument("--base_url", type=str, default=os.getenv("AOC_BASE_URL", os.getenv("BASE_URL", "http://localhost:7001")))
    parser.add_argument("--api_key", type=str, default=os.getenv("AOC_MODULATION_TOKEN", ""))
    parser.add_argument("--model_template_id", type=str, required=True)
    parser.add_argument("--outdir", type=str, default="./artifacts_world")
    parser.add_argument("--auto_start", type=int, default=int(os.getenv("AUTO_START", "1")),
        help="1=auto-call /modulation/start (default), 0=manual mode (UI must start job)")
    parser.add_argument("--num_frames", type=int, default=int(os.getenv("NUM_FRAMES", "16")),
        help="Number of frames to sample per video clip (default: 16, max: 64)")
    args = parser.parse_args()

    if not args.api_key:
        raise RuntimeError("API token missing. Provide --api_key or set AOC_MODULATION_TOKEN in the environment")

    os.makedirs(args.outdir, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(args.outdir, f"run_{timestamp}")
    os.makedirs(run_dir, exist_ok=True)
    print(f"[INFO] Run directory created: {run_dir}")

    mc = ModulatorClient(args.base_url, args.api_key)
    _active_job_id = [None]

    def _cleanup_on_exit(signum=None, frame=None):
        job = _active_job_id[0]
        if job:
            print(f"\n  {YELLOW}>{RESET} Stopping modulation job {DIM}({job}){RESET}")
            try:
                mc.stop_job(job_id=job, model_template_id=args.model_template_id)
            except Exception:
                pass
        sys.exit(1)

    import signal
    signal.signal(signal.SIGINT, _cleanup_on_exit)
    signal.signal(signal.SIGTERM, _cleanup_on_exit)
    try:
        signal.signal(signal.SIGQUIT, _cleanup_on_exit)
    except (OSError, AttributeError):
        pass

    if args.auto_start:
        tpl_existing = mc.get_template_or_die(args.model_template_id)
        mod = tpl_existing.get("Modulation") or {}
        if mod.get("status") == "running":
            old_job = mod.get("job_id")
            print(f"  {YELLOW}>{RESET} Stopping previous job {DIM}({old_job}){RESET}")
            try:
                mc.stop_job(job_id=old_job, model_template_id=args.model_template_id)
            except Exception as e:
                print(f"  {YELLOW}!{RESET} Failed to stop old job: {e}")
                exit(1)

        # maxSteps precedence (#119): an explicitly-set AOC_STEPS_PER_TRIAL env
        # wins (headless/CI override), else the operator's UI-configured value
        # from the template's DesiredModulation.kpi.maxSteps, else the built-in
        # default. Previously this always used the env default and the
        # start_job() below then overwrote the UI-configured value.
        _env_steps = os.getenv("AOC_STEPS_PER_TRIAL")
        _ui_steps = ((tpl_existing.get("DesiredModulation") or {}).get("kpi") or {}).get("maxSteps")
        if _env_steps is not None:
            max_steps_per_trial = int(_env_steps)
        elif _ui_steps:
            max_steps_per_trial = int(_ui_steps)
            print(f"[INFO] Using UI-configured maxSteps={max_steps_per_trial} from template DesiredModulation")
        else:
            max_steps_per_trial = 50
        search_budget = int(os.getenv("AOC_SEARCH_BUDGET", "2"))

        dataset = {
            "kind": "repo",
            "source": "external",
            "name": os.getenv("AOC_DATASET_NAME", "kinetics700"),
            "config": os.getenv("AOC_DATASET_CONFIG", "default"),
            "split": os.getenv("AOC_DATASET_SPLIT", "test[:100]"),
        }

        kpi = {
            "targets": [
                {"name": "accuracy", "direction": "max", "weight": 1},
                {"name": "top5_accuracy", "direction": "max", "weight": 1},
            ],
            "maxSteps": max_steps_per_trial,
        }

        search = {
            "algo": "bayes",
            "budget": search_budget,
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

        mc.start_job(
            args.model_template_id,
            variant="additive",
            kpi=kpi,
            mode="auto",
            dataset=dataset,
            search=search,
        )
    else:
        print("[INFO] AUTO_START=0 -> waiting for UI job...")

    tpl, job_id = mc.wait_for_job_id(args.model_template_id)
    _active_job_id[0] = job_id
    recipe = tpl.get("DesiredModulation") or {}

    # --- Download model snapshot ---
    phase("Downloading model snapshot")
    local_model_dir = mc.download_and_extract_model(args.model_template_id, run_dir)

    # --- Load model ---
    phase("Loading world model")
    device = "cuda" if torch.cuda.is_available() else "cpu"

    from transformers import AutoConfig, AutoVideoProcessor, AutoModelForVideoClassification

    config = AutoConfig.from_pretrained(local_model_dir, local_files_only=True)
    processor = AutoVideoProcessor.from_pretrained(local_model_dir, local_files_only=True)
    model = AutoModelForVideoClassification.from_pretrained(
        local_model_dir, local_files_only=True
    ).to(device)

    hidden_dim = getattr(config, "hidden_size", None)
    print(f"  {BLUE}>{RESET} Model type: {BOLD}{config.model_type}{RESET}, hidden_dim={hidden_dim}")

    # --- Extract recipe config ---
    mode = recipe.get("mode") or "auto"
    variant = recipe.get("variant") or "additive"
    steps = int((recipe.get("kpi") or {}).get("maxSteps") or 50)
    dataset_cfg = recipe.get("dataset") or {}
    ds_name = dataset_cfg.get("name") or "kinetics700"
    ds_config = dataset_cfg.get("config") or "default"
    ds_split = dataset_cfg.get("split") or "test[:100]"
    num_frames = args.num_frames

    # --- Governance mode and indispensability config ---
    governance_mode = recipe.get("governance_mode", "standard")
    indisp_cfg = recipe.get("indispensability") or {}
    mod_block = tpl.get("Modulation") or {}
    if not indisp_cfg and mod_block.get("indispensability"):
        indisp_cfg = mod_block["indispensability"]
    if not governance_mode or governance_mode == "standard":
        governance_mode = mod_block.get("governance_mode", "standard")
    is_indispensable = governance_mode == "indispensable" or indisp_cfg.get("enabled", False)

    phase("Modulation job")
    print(f"  {DIM}Job ID:    {job_id}{RESET}")
    print(f"  {DIM}Mode:      {mode} | Variant: {variant} | Steps: {steps}{RESET}")
    print(f"  {DIM}Dataset:   {ds_name}/{ds_config}/{ds_split}{RESET}")
    print(f"  {DIM}Frames:    {num_frames}{RESET}")
    if is_indispensable:
        print(f"  {BOLD}\033[91m>>{RESET} {BOLD}INDISPENSABLE MODE{RESET}")
    else:
        print(f"  {DIM}Governance: {governance_mode}{RESET}")

    # --- Baseline evaluation ---
    phase("Baseline evaluation (no ECM)")
    baseline_model = deepcopy(model)
    baseline_stream = []
    for update in mc.compute_world_metrics_stream(
        baseline_model, processor, args.model_template_id,
        ds_name=ds_name, ds_config=ds_config, ds_split=ds_split,
        steps=steps, num_frames=num_frames,
    ):
        baseline_stream.append(update)
        step = update.get("step", len(baseline_stream))
        acc = update.get("accuracy", 0)
        top5 = update.get("top5_accuracy", 0)
        bar_w = 20
        filled = int(bar_w * step / max(steps, 1))
        bar = "\u2588" * filled + "\u2591" * (bar_w - filled)
        sys.stdout.write(
            f"\r  [{bar}] {step}/{steps}  "
            f"acc={GREEN}{acc:.4f}{RESET}  top5={GREEN}{top5:.4f}{RESET}   "
        )
        sys.stdout.flush()
    if baseline_stream:
        sys.stdout.write("\n")

    baseline_metrics = baseline_stream[-1] if baseline_stream else {}
    print(f"  {YELLOW}[BASELINE]{RESET} {baseline_metrics}")

    try:
        metric_keys = tuple(
            k for k, v in baseline_metrics.items()
            if isinstance(v, (int, float)) and k not in ("step", "total")
        )
        mc.upload_baseline_metrics(args.model_template_id, baseline_stream, kpis=metric_keys or ("accuracy", "top5_accuracy"))
    except Exception as e:
        print(f"[WARN] Baseline upload error: {e}")

    del baseline_model

    # --- Auto mode trial loop ---
    phase("Ephaptic modulation (auto mode)")
    print(f"  {BLUE}>{RESET} Running Bayesian search over EC-ANN configurations")
    best_score, best_metrics, best_variant, best_stream = None, None, None, None
    last_cfg, last_score = None, None
    trial_num = 0
    budget = int((recipe.get("search") or {}).get("budget", 0) or 20)

    while True:
        trial_cfg = mc.inject_ecm_from_trial(
            job_id, model, hidden_dim=hidden_dim,
            last_cfg=last_cfg, last_score=last_score,
        )
        if not trial_cfg:
            print("\n[INFO] No more trials. Auto mode loop finished.")
            break

        trial_num += 1
        print(f"\n[TRIAL {trial_num}/{budget}] Config -> {trial_cfg}")

        metrics_stream = []
        for update in mc.compute_world_metrics_stream(
            model, processor, args.model_template_id,
            ds_name=ds_name, ds_config=ds_config, ds_split=ds_split,
            steps=steps, num_frames=num_frames,
        ):
            metrics_stream.append(update)
            step = update.get("step", len(metrics_stream))
            acc = update.get("accuracy", 0)
            top5 = update.get("top5_accuracy", 0)
            bar_w = 20
            filled = int(bar_w * step / max(steps, 1))
            bar = "\u2588" * filled + "\u2591" * (bar_w - filled)
            sys.stdout.write(
                f"\r  [{bar}] {step}/{steps}  "
                f"acc={GREEN}{acc:.4f}{RESET}  top5={GREEN}{top5:.4f}{RESET}   "
            )
            sys.stdout.flush()
        if metrics_stream:
            sys.stdout.write("\n")

        last = metrics_stream[-1] if metrics_stream else {}
        score = last.get("accuracy", 0.0)
        last_cfg, last_score = trial_cfg, score
        print(f"[RESULT] Trial {trial_num}/{budget} score={score:.4f}, metrics={last}")

        if best_score is None or score > best_score:
            best_score, best_metrics, best_variant = score, last, trial_cfg
            best_stream = list(metrics_stream)
            print(f"{GREEN}[BEST] Updated best score={best_score:.4f}, config={best_variant}{RESET}")

    if best_metrics:
        total_runtime = time.time() - start_time

        exp_cfg = {
            "variant": best_variant.get("variant"),
            "epsilon": float(best_variant.get("epsilon")),
            "lambda0": float(best_variant.get("lambda0")),
            "phi": best_variant.get("phi"),
            "ecm_init": best_variant.get("ecm_init"),
            "runtime": total_runtime,
            "maxSteps": steps,
            "num_frames": num_frames,
        }

        # --- Ablation probe if indispensable ---
        indisp_metrics = None
        if is_indispensable:
            phase("Ablation probe (indispensability)")
            try:
                probe_video = torch.randn(1, num_frames, 3, 256, 256)
                probe_inputs = processor(probe_video, return_tensors="pt")
                probe_inputs = {k: v.to(device) for k, v in probe_inputs.items()}
                probe_inputs["labels"] = torch.tensor([0], device=device)
                indisp_metrics = run_ablation_probe(model, probe_inputs)
                strength = indisp_metrics.get("governance_strength", "unknown")
                sep = indisp_metrics.get("separation_ratio", 0)
                print(f"  {BOLD}Governance Strength: {strength.upper()}{RESET}")
                print(f"  Authorized PPL:   {indisp_metrics.get('authorized_ppl')}")
                print(f"  Unauthorized PPL: {indisp_metrics.get('unauthorized_ppl')}")
                print(f"  Separation:       {sep}x")
            except Exception as e:
                print(f"[WARN] Ablation probe failed: {e}")

        mc.finalize_and_certify(
            run_dir,
            model,
            processor,
            best_metrics,
            exp_cfg["variant"],
            job_id,
            args.model_template_id,
            all_metrics=best_stream,
            baseline_metrics=baseline_metrics,
            exp_config=exp_cfg,
            indispensability_metrics=indisp_metrics,
        )

        phase("Modulation complete")
        print(f"  {GREEN}+{RESET} Best trial: score={BOLD}{best_score:.4f}{RESET} "
              f"acc={best_metrics.get('accuracy', 0):.4f} "
              f"top5={best_metrics.get('top5_accuracy', 0):.4f}")
    else:
        print("[WARN] No valid trials executed.")

    summary = {
        "job_id": job_id,
        "model_template_id": args.model_template_id,
        "model_kind": "world",
        "mode": mode,
        "variant": variant,
        "dataset": f"{ds_name}/{ds_config}/{ds_split}",
        "steps": steps,
        "num_frames": num_frames,
        "run_dir": run_dir,
        "best_score": best_score,
        "best_metrics": best_metrics,
        "runtime_secs": round(time.time() - start_time, 2),
    }
    summary_path = os.path.join(run_dir, "summary.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"[INFO] Summary written to {summary_path}")


if __name__ == "__main__":
    main()
