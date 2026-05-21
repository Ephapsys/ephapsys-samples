#!/usr/bin/env python3
"""
Trainer script for Language models (e.g., Qwen, Gemma, GPT-2) with ephaptic coupling integration.

This trainer streams per-step metrics (accuracy, loss, perplexity) back to the AOC,
so the frontend UI can render live charts during evaluation.
It supports both manual and auto modes:

- Manual mode: inject ECM once with config from the Modulator page, then evaluate.
- Auto mode: run multiple trials with configs suggested by the backend (Bayesian search),
  scoring each trial and finalizing the best one.

Usage flow:
- Minimal CLI args: --base_url, --api_key, --model_template_id, --outdir
- All training hyperparameters, dataset config, and model_id are fetched dynamically
  from the backend template created in the UI.
- The trainer does not accept manual tuning flags; these must be specified in the Modulation config.

Before starting a job in the UI:

1. Create a Model Template (via the Create Model page):
   - Source: External repository
   - Provider: Hugging Face
   - Repository ID: Qwen/Qwen3.5-0.8B, google/gemma-3-270m, openai-community/gpt2
   - Model Kind: language
   - Revision: main
   - Hugging Face Token: hf_xxxxxxxx
   - Register immediately (so a provenance certificate is issued)

2. Go to the Modulator page for this template:
   - Variant: additive or multiplicative
   - Hyperparameters: epsilon (ε), lambda0 (λ₀), phi (activation), ecm_init
   - MaxSteps: number of samples/steps to evaluate
   - Dataset: name (e.g., wiki), config (e.g., wikitext-103-raw-v1), split (e.g., train[:1%])
   - KPI Targets: enable at least one KPI relevant to Language (accuracy, loss, perplexity)
"""

import os, sys, json, datetime, argparse, time
import math
from ephapsys.modulation import ModulatorClient, compute_indispensability_loss, run_ablation_probe

BOLD = "\033[1m"
DIM = "\033[2m"
BLUE = "\033[36m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
GOLD = "\033[38;5;220m"
RESET = "\033[0m"

import threading

def _spinner(msg, stop_event):
    """Background spinner for long-running operations."""
    frames = ["   ", ".  ", ".. ", "..."]
    i = 0
    t0 = time.time()
    while not stop_event.is_set():
        elapsed = int(time.time() - t0)
        sys.stdout.write(f"\r  {GOLD}>{RESET} {msg}{frames[i % len(frames)]} {DIM}({elapsed}s){RESET}  ")
        sys.stdout.flush()
        stop_event.wait(0.4)
        i += 1
    elapsed = int(time.time() - t0)
    sys.stdout.write(f"\r  {GREEN}+{RESET} {msg} {DIM}({elapsed}s){RESET}                    \n")
    sys.stdout.flush()

def with_spinner(msg, fn, *args, no_spinner=False, **kwargs):
    """Run fn with a spinner, return its result.
    Pass no_spinner=True to skip the spinner (e.g. when fn has its own progress bar).
    """
    if no_spinner:
        print(f"  {GOLD}>{RESET} {msg}")
        return fn(*args, **kwargs)
    stop = threading.Event()
    t = threading.Thread(target=_spinner, args=(msg, stop), daemon=True)
    t.start()
    try:
        result = fn(*args, **kwargs)
    finally:
        stop.set()
        t.join()
    return result

def phase(msg):
    print(f"\n  {GOLD}>>>{RESET} {BOLD}{msg}{RESET}")

def evaluate_baseline(mc, model, tokenizer, model_template_id, ds_name, ds_config, ds_split, steps):
    """Run a baseline (unmodulated) evaluation for comparison."""
    phase("Baseline evaluation (no ECM)")
    print(f"  {YELLOW}>{RESET} Running standard evaluation...")
    baseline_stream = []

    # === Use full compute_language_metrics_stream (includes ROUGE/BLEU/BERTScore) ===
    for update in mc.compute_language_metrics_stream(
        model, tokenizer, model_template_id,
        ds_name=ds_name, ds_config=ds_config, ds_split=ds_split, steps=steps
    ):
        baseline_stream.append(update)
        step = update.get("step", len(baseline_stream))
        acc = update.get("accuracy", 0)
        loss = update.get("loss", 0)
        ppl = update.get("perplexity", 0)
        bar_w = 20
        filled = int(bar_w * step / max(steps, 1))
        bar = "█" * filled + "░" * (bar_w - filled)
        acc_c = GREEN if acc >= 0.5 else YELLOW
        sys.stdout.write(
            f"\r  [{bar}] {step}/{steps}  "
            f"loss={loss:.4f}  ppl={ppl:.2f}  acc={acc_c}{acc:.4f}{RESET}   "
        )
        sys.stdout.flush()
    if baseline_stream:
        sys.stdout.write("\n")

    baseline = baseline_stream[-1] if baseline_stream else {}
    print(f"{YELLOW}[BASELINE] Results: {baseline}{RESET}")

    # === Upload baseline to backend so AOC baseline matches DOCX & UI ===
    try:
        print(f"[BASELINE] Uploading baseline metrics for {model_template_id} to AOC...")

        # Dynamically detect all numeric KPI keys (auto-expands for ROUGE/BLEU/BERTScore)
        metric_keys = tuple(
            k for k, v in baseline.items()
            if isinstance(v, (int, float)) and k not in ("step", "total")
        )

        if not metric_keys:
            metric_keys = (
                "accuracy", "loss", "perplexity",
                "rouge1", "rouge2", "rougeL", "bleu", "bertscore_f1"
            )

        mc.upload_baseline_metrics(
            model_template_id,
            baseline_stream,
            kpis=metric_keys,
        )

        # --- Trigger immediate baseline re-emit so frontend refreshes dashed lines ---
        import requests
        resp = requests.post(
            f"{mc.base_url}/modulation/baseline_emit",
            headers={"Authorization": f"Bearer {mc.api_key}"},
            json={"model_template_id": model_template_id},
            timeout=10,
        )
        if resp.ok:
            print(f"[BASELINE] Baseline (Standard) re-emitted for {model_template_id}")
        else:
            print(f"[WARN] Baseline re-emit failed: {resp.status_code} {resp.text}")

        # --- Log summary of uploaded KPI keys ---
        uploaded_keys = [
            k for k in baseline.keys()
            if isinstance(baseline.get(k), (int, float))
        ]
        print(f"[BASELINE] Uploaded baseline curves: {uploaded_keys}")

    except Exception as e:
        print(f"[WARN] Baseline upload error: {e}")
    # ================================================================

    return baseline, baseline_stream

# Inspect the current ephaptic coupling matrix (Λ)
def inspect_lambda(model, label="Λ"):
    """Print diagnostics for the ephaptic coupling matrix if it exists."""
    import torch

    for name, p in model.named_parameters():
        if "lambda_ecm" in name:
            with torch.no_grad():
                norm = torch.linalg.norm(p).item()
                minv, maxv, meanv = p.min().item(), p.max().item(), p.mean().item()
                print(f"[{label}] Norm={norm:.6f}, min={minv:.6f}, max={maxv:.6f}, mean={meanv:.6f}")
            return p.detach().clone()
    print(f"[WARN] No ephaptic Λ found in model during {label} inspection.")
    return None

def main():
    import torch
    from transformers import (
        AutoConfig,
        AutoTokenizer,
        AutoModelForSeq2SeqLM,
        AutoModelForCausalLM,
    )

    def _make_optimizer(params, lr, memory_efficient):
        if memory_efficient:
            try:
                import bitsandbytes as bnb
            except ImportError as e:
                raise RuntimeError(
                    "--memory-efficient requires bitsandbytes. "
                    "Install with: pip install bitsandbytes"
                ) from e
            return bnb.optim.AdamW8bit(params, lr=lr)
        return torch.optim.AdamW(params, lr=lr)

    def _trainable_ecm_params(model):
        trainable = []
        for name, p in model.named_parameters():
            is_ecm = "lambda_ecm" in name
            p.requires_grad = is_ecm
            if is_ecm:
                trainable.append(p)
        if not trainable:
            raise RuntimeError(
                "No lambda_ecm parameters found — ECM must be injected before training."
            )
        return trainable

    def _set_memory_efficient_training(model, enable, memory_efficient):
        if not memory_efficient:
            return
        if enable:
            model.gradient_checkpointing_enable()
            model.config.use_cache = False
        else:
            model.gradient_checkpointing_disable()
            model.config.use_cache = True

    best_score, best_metrics, best_variant, best_stream = None, {}, {"variant": "additive"}, []
    start_time = time.time()  # Track total runtime
    parser = argparse.ArgumentParser()
    parser.add_argument("--base_url", type=str, default=os.getenv("AOC_BASE_URL", os.getenv("BASE_URL", "http://localhost:7001")))
    parser.add_argument("--api_key", type=str, default=os.getenv("AOC_MODULATION_TOKEN", ""))
    parser.add_argument("--model_template_id", type=str, required=True)   # <- still required
    parser.add_argument("--outdir", type=str, default="./out")
    # --- Train is an option/flag (not a mode) ---
    parser.add_argument("--train", action="store_true", help="Enable gradient updates during per-step loop")
    # Backward compatibility with old flag name
    parser.add_argument("--train_mode", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--auto_start", type=int, default=int(os.getenv("AUTO_START", "1")),
        help="1=auto-call /modulation/start (default), 0=manual mode (UI must start job)")
    parser.add_argument("--verbose", action="store_true", default=os.getenv("TRAINER_VERBOSE", "0") == "1",
        help="Show debug output")
    parser.add_argument("--memory-efficient", action="store_true",
        default=os.getenv("MEMORY_EFFICIENT", "").lower() in ("1", "true", "yes"),
        help="Enable bf16 weights, gradient checkpointing, and 8-bit AdamW (bitsandbytes). "
             "Reduces VRAM ~3x for training; use on <12 GB GPUs.")
    parser.add_argument("--freeze-base", action="store_true",
        default=os.getenv("FREEZE_BASE", "").lower() in ("1", "true", "yes"),
        help="Freeze base model weights and train only lambda_ecm parameters. "
             "Drastically reduces trainable params and VRAM.")
    args = parser.parse_args()
    VERBOSE = args.verbose
    # Merge old flag into the new one
    args.train = bool(args.train or args.train_mode)

    if args.memory_efficient:
        try:
            import bitsandbytes  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "--memory-efficient requires bitsandbytes. "
                "Install with: pip install bitsandbytes"
            ) from e

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
    _active_job_id = [None]  # mutable for cleanup handler

    def _cleanup_on_exit(signum=None, frame=None):
        job = _active_job_id[0]
        if job:
            print(f"\n  {YELLOW}>{RESET} Stopping modulation job {DIM}({job}){RESET}")
            try:
                mc.stop_job(job_id=job, model_template_id=args.model_template_id)
                print(f"  {GREEN}+{RESET} Job stopped cleanly")
            except Exception:
                pass
        sys.exit(1)

    import signal
    signal.signal(signal.SIGINT, _cleanup_on_exit)
    signal.signal(signal.SIGTERM, _cleanup_on_exit)
    # Ctrl+\ (SIGQUIT) as a fallback when Ctrl+C can't interrupt torch ops
    try:
        signal.signal(signal.SIGQUIT, _cleanup_on_exit)
    except (OSError, AttributeError):
        pass

    if args.auto_start:

        # --- Check for existing running job ---
        tpl_existing = mc.get_template_or_die(args.model_template_id)
        mod = tpl_existing.get("Modulation") or {}

        if VERBOSE:
            print(f"  {DIM}[verbose] Modulation state: {json.dumps(mod, indent=2)}{RESET}")

        if mod.get("status") == "running":
            old_job = mod.get("job_id")
            print(f"  {YELLOW}>{RESET} Stopping previous job {DIM}({old_job}){RESET}")
            try:
                mc.stop_job(job_id=old_job, model_template_id=args.model_template_id)
                tpl_existing = mc.get_template_or_die(args.model_template_id)
                mod = tpl_existing.get("Modulation")
                if VERBOSE:
                    print(f"  {DIM}[verbose] State after stop: {json.dumps(mod, indent=2)}{RESET}")
            except Exception as e:
                print(f"  {YELLOW}!{RESET} Failed to stop old job: {e}")
                exit(1)

        # --- Define dataset, KPIs, search config ---
        # Dataset can be overridden via env vars:
        #   AOC_DATASET_NAME=wikitext  AOC_DATASET_CONFIG=wikitext-103-raw-v1  AOC_DATASET_SPLIT=train[:1%]
        #   AOC_DATASET_PATH=/path/to/uploaded.jsonl  (for uploaded datasets)
        ds_path = os.getenv("AOC_DATASET_PATH", "").strip()
        if ds_path:
            dataset = {
                "kind": "file",
                "source": "uploaded",
                "path": ds_path,
                "name": os.path.basename(ds_path),
            }
        else:
            dataset = {
                "kind": "repo",
                "source": "external",
                "name": os.getenv("AOC_DATASET_NAME", "wikitext"),
                "config": os.getenv("AOC_DATASET_CONFIG", "wikitext-103-raw-v1"),
                "split": os.getenv("AOC_DATASET_SPLIT", "train[:1%]"),
            }

        max_steps_per_trial = int(os.getenv("AOC_STEPS_PER_TRIAL", "10"))
        search_budget = int(os.getenv("AOC_SEARCH_BUDGET", "2"))

        # Governance-mode-aware variant restriction.
        # The SDK's start_job() doesn't currently carry governance_mode to AOC
        # (only variant + search + kpi + mode), so we can't tell the backend
        # which mode we require. As a worker-side workaround, we read the
        # operator's intent from AOC_GOVERNANCE_MODE and tailor the search
        # space locally. When indispensability is required, additive variant
        # is removed from the search because additive's `y = f(S + ε·φ(Λ·x))`
        # makes W's gradient INDEPENDENT of Λ — empirically every additive
        # trial produces a model where Λ is decorative and 0/3 adversaries
        # can be blocked. Multiplicative `y = f(S · (1 + ε·φ(Λ·x)))` scales
        # W's gradient by (1 + ε·φ(Λ·x)) which forces W↔Λ coupling — the
        # mathematical foundation of indispensability. For governance="standard"
        # (no indispensability requirement), both variants stay searchable
        # because additive is still a valid governance signaling mechanism.
        gov_mode = os.getenv("AOC_GOVERNANCE_MODE", "standard").lower().strip()
        _indisp_modes = {"indispensable", "indispensability", "strict"}
        if gov_mode in _indisp_modes:
            _allowed_variants = ["multiplicative"]
            print(f"[INFO] AOC_GOVERNANCE_MODE={gov_mode} → restricting variant search to multiplicative-only "
                  "(additive is excluded because its W-gradient is independent of Λ → no indispensability)")
        else:
            _allowed_variants = ["additive", "multiplicative"]
            print(f"[INFO] AOC_GOVERNANCE_MODE={gov_mode} → variant search includes both additive and multiplicative")

        kpi = {
            "targets": [
                {"name": "accuracy", "direction": "max", "weight": 1},
                {"name": "loss", "direction": "min", "weight": 1},
                {"name": "perplexity", "direction": "min", "weight": 1},
            ],
            "maxSteps": max_steps_per_trial,
        }

        # Search space includes the Family D indispensability hyperparameters
        # (alpha, beta) — these were missed when indispensability training
        # was integrated into the modulator. Without searching alpha/beta,
        # AOC could only optimize the *shape* of Λ (variant/ε/λ₀/φ/init);
        # the strength of the indispensability penalty stayed at whatever
        # the model template said (default 10.0), causing the held-out PPL
        # collapse documented in the security paper EXPERIMENTS.md.
        #
        # alpha range chosen to span "almost no penalty" (0.5) → "very
        # aggressive" (20). The paper §5.6.4 ran α=10 and got severe
        # held-out collapse; AOC should now find a softer point.
        # beta range stays small — it's the stability term, doesn't need
        # wide exploration.
        search = {
            "algo": "bayes",
            "budget": search_budget,
            "parallel": 1,
            "multi_objective": True,
            "space": {
                # Continuous ranges anchored to the canonical paper values
                # (ε=0.5, λ₀=0.2296, α=10.0, β=0.01 from .env.experiment §5.6.4).
                # Earlier ranges were guesses with ±1 order of magnitude headroom
                # — empirically those let AOC converge to destructive regions:
                #   α=18.8 + small ε → catastrophic over-specialization
                #   β=0.001         → no Λ-norm constraint, no AOC signal anyway
                #   ε=0.05–0.33     → decorative Λ, indispensability fails
                # Tightened to ±50% around canonical, which keeps AOC inside
                # the empirically-validated sweet spot while still letting it
                # find better-than-canonical configurations.
                "epsilon": {"low": 0.3, "high": 0.8},   # was [0.0, 2.0]; canonical 0.5
                "lambda0": {"low": 0.1, "high": 0.4},   # was [0.0, 0.5]; canonical 0.2296
                "phi": ["identity", "relu", "tanh", "silu", "gelu"],
                "ecm_init": ["transpose", "identity", "random"],
                # Variant set is governance-mode-dependent (see _allowed_variants
                # logic above): multiplicative-only when AOC_GOVERNANCE_MODE
                # requires indispensability, both variants otherwise.
                "variant": _allowed_variants,
                "alpha": {"low": 5.0, "high": 15.0},    # was [0.5, 20.0]; canonical 10.0
                "beta":  {"low": 0.005, "high": 0.02},  # was [0.001, 0.1]; canonical 0.01
            },
        }

        # --- Start a fresh job ---
        # NOTE: mode is an AOC/UI concept (manual vs auto). Training is now a *flag* here, not a mode.
        # governance_mode (added in SDK 0.2.80) carries operator intent to
        # the backend, which then coerces the search space accordingly:
        #   "indispensable" → backend restricts variant→multiplicative,
        #                     ecm_init excludes random, ε∈[0.3,0.8]
        #   "standard"      → no extra constraints (backend pass-through)
        # Reading from AOC_GOVERNANCE_MODE env (same source the local
        # search-space restriction logic uses above) keeps both layers
        # consistent.
        mc.start_job(
            args.model_template_id,
            variant="additive",
            kpi=kpi,
            mode="auto",  # carry search behavior; training is controlled locally by --train
            dataset=dataset,
            search=search,
            governance_mode=gov_mode,
        )
    else:
        print("[INFO] AUTO_START=0 → skipping /modulation/start, waiting for UI job...")

    # --- Block until job_id is available ---
    tpl, job_id = mc.wait_for_job_id(args.model_template_id)
    _active_job_id[0] = job_id
    recipe = tpl.get("DesiredModulation") or {}

    # --- Download model snapshot into run_dir ---
    phase("Downloading model snapshot")
    local_model_dir = with_spinner("Downloading model from AOC",
        mc.download_and_extract_model, args.model_template_id, run_dir, no_spinner=True)

    # --- Load model (Seq2Seq or Causal) from local snapshot ---
    phase("Loading model")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model_id = recipe.get("SourceRepo")

    config = AutoConfig.from_pretrained(local_model_dir, local_files_only=True)
    tokenizer = AutoTokenizer.from_pretrained(local_model_dir, local_files_only=True)

    load_dtype = torch.bfloat16 if args.memory_efficient else None
    if args.memory_efficient:
        print()
        print(f"  {GOLD}━━━ MEMORY_EFFICIENT mode enabled ━━━{RESET}")
        print(f"  {DIM}Reduces VRAM ~3x so training fits on <12 GB GPUs (e.g. RTX 4070 8 GB).{RESET}")
        print(f"  {DIM}Enable via:  --memory-efficient   OR   MEMORY_EFFICIENT=true{RESET}")
        print()
        print(f"  {BOLD}Active optimizations:{RESET}")
        print(f"    {GREEN}+{RESET} {BOLD}bf16 weights{RESET}           {DIM}halves model + activation memory (3 GB → 1.5 GB for 0.8B model){RESET}")
        print(f"    {GREEN}+{RESET} {BOLD}gradient checkpointing{RESET} {DIM}re-computes activations in backward pass instead of storing{RESET}")
        print(f"    {GREEN}+{RESET} {BOLD}8-bit AdamW{RESET}            {DIM}stores optimizer state in int8 via bitsandbytes{RESET}")
        print()
        if args.freeze_base:
            print(f"  {GREEN}+ --freeze-base active:{RESET} {DIM}only lambda_ecm params train — optimizer state ~6 GB → ~1.5 GB.{RESET}")
        else:
            print(f"  {YELLOW}!{RESET} {BOLD}--freeze-base not set.{RESET} {DIM}Full base model is trainable; optimizer state stays large.{RESET}")
            print(f"     {DIM}For the advertised ~3x VRAM reduction, add {BOLD}--freeze-base{RESET}{DIM} (or FREEZE_BASE=true).{RESET}")
        print()
        print(f"  {YELLOW}Tradeoffs:{RESET} {DIM}~15-25% slower per step (grad ckpt); minor precision drift from bf16.{RESET}")
        print(f"  {YELLOW}Requires:{RESET}  {DIM}bitsandbytes (pip install bitsandbytes), CUDA GPU, bf16-compatible model.{RESET}")
        print()
    else:
        hint = "--memory-efficient" + ("" if args.freeze_base else " --freeze-base")
        env_hint = "MEMORY_EFFICIENT=true" + ("" if args.freeze_base else " FREEZE_BASE=true")
        print(f"  {DIM}[HINT] Running in fp32. If OOM on GPU, try {hint} (or {env_hint}){RESET}")

    # Detect model type
    if config.model_type in ["t5", "bart", "mbart", "pegasus", "mt5"]:
        print(f"  {BLUE}>{RESET} Detected Seq2Seq model: {BOLD}{config.model_type}{RESET}")
        model = with_spinner("Loading model weights",
            lambda: AutoModelForSeq2SeqLM.from_pretrained(local_model_dir, local_files_only=True, torch_dtype=load_dtype).to(device))
        encoder = model.get_encoder()
        is_seq2seq = True
    else:
        print(f"  {BLUE}>{RESET} Detected Causal LM: {BOLD}{config.model_type}{RESET}")
        model = with_spinner("Loading model weights",
            lambda: AutoModelForCausalLM.from_pretrained(local_model_dir, local_files_only=True, torch_dtype=load_dtype).to(device))
        encoder = model  # causal LMs have no separate encoder
        tokenizer.pad_token = tokenizer.eos_token
        model.config.pad_token_id = model.config.eos_token_id
        is_seq2seq = False

    # Clone a baseline copy before ECM injection
    from copy import deepcopy
    baseline_model = deepcopy(model)

    # --- Extract config from recipe ---
    mode = recipe.get("mode") or "manual"
    variant = recipe.get("variant")
    steps = int((recipe.get("kpi") or {}).get("maxSteps") or 0)
    dataset_cfg = recipe.get("dataset", {})
    ds_kind = dataset_cfg.get("kind", "repo")
    ds_path = dataset_cfg.get("path", "")
    ds_name, ds_config, ds_split = dataset_cfg.get("name"), dataset_cfg.get("config"), dataset_cfg.get("split")

    # AOC_DATASET_PATH env var WINS over whatever dataset config is baked
    # into the AOC model template. Reason: model templates accumulate
    # stale dataset references across runs (e.g. /tmp/identity_dataset.jsonl
    # registered from a previous worker that no longer exists), and re-
    # registering the template just to update one field is heavy. The env
    # override lets the caller (e.g. run_experiment.sh's Phase 0) point
    # the trial at whatever local JSONL got uploaded to the current worker.
    env_ds_path = os.getenv("AOC_DATASET_PATH", "").strip()
    if env_ds_path:
        if env_ds_path != ds_path:
            print(f"[INFO] AOC_DATASET_PATH env override: "
                  f"template={ds_path or '(none)'} → env={env_ds_path}")
        ds_path = env_ds_path
        ds_kind = "file"
        ds_name = None
        ds_config = None
        ds_split = None

    # For file-based datasets, use the path as the name for load_dataset("json", ...)
    if ds_kind == "file" and ds_path:
        ds_name = ds_path
        ds_config = None
        ds_split = None

    if not variant:
        raise ValueError("Trainer requires 'variant' in recipe (additive or multiplicative).")

    # --- Governance mode and indispensability config ---
    governance_mode = recipe.get("governance_mode", "standard")
    indisp_cfg = recipe.get("indispensability") or {}
    # Also check Modulation block (set by backend during /start)
    mod_block = tpl.get("Modulation") or {}
    if not indisp_cfg and mod_block.get("indispensability"):
        indisp_cfg = mod_block["indispensability"]
    if not governance_mode or governance_mode == "standard":
        governance_mode = mod_block.get("governance_mode", "standard")

    is_indispensable = governance_mode == "indispensable" or indisp_cfg.get("enabled", False)
    indisp_alpha = float(indisp_cfg.get("alpha", 10.0))
    indisp_beta = float(indisp_cfg.get("beta", 0.01))
    indisp_joint = bool(indisp_cfg.get("joint_training", True))
    indisp_min_steps = int(indisp_cfg.get("min_steps", 1000))

    phase("Modulation job")
    print(f"  {DIM}Job ID:    {job_id}{RESET}")
    print(f"  {DIM}Mode:      {mode} | Variant: {variant} | Steps: {steps}{RESET}")
    print(f"  {DIM}Dataset:   {ds_name}/{ds_config}/{ds_split}{RESET}")
    if is_indispensable:
        print(f"  {BOLD}\033[91m>>{RESET} {BOLD}INDISPENSABLE MODE{RESET} "
              f"(alpha={indisp_alpha}, beta={indisp_beta}, joint={indisp_joint}, min_steps={indisp_min_steps})")
    else:
        print(f"  {DIM}Governance: {governance_mode}{RESET}")

    # --- Initialize baseline placeholders to avoid UnboundLocalError ---
    baseline_metrics, baseline_stream = {}, []

    # =========================
    #  Base baseline (for both manual and auto flows)
    # =========================
    baseline_metrics, baseline_stream = evaluate_baseline(
        mc, baseline_model, tokenizer, args.model_template_id, ds_name, ds_config, ds_split, steps
    )
    summary = {
        "job_id": job_id,
        "mode": mode,
        "variant": variant,
        "dataset": f"{ds_name}/{ds_config}/{ds_split}",
        "steps": steps,
        "run_dir": run_dir,
    }

    # Helper to build training dataset when training is enabled
    def build_training_ds(ds_name, ds_config, ds_split):
        from datasets import load_dataset
        if ds_kind == "file" and ds_path:
            # Load from uploaded JSONL file
            ds = load_dataset("json", data_files=ds_path, split="train")
        else:
            ds = load_dataset(ds_name, ds_config, split=ds_split)
        def sample_at(i):
            return ds[int(i % len(ds))]
        return ds, sample_at

    # =========================
    # MANUAL MODE
    # =========================
    if mode == "manual":
        phase("Ephaptic modulation (manual mode)")

        # Inject ECM once with config from recipe
        trial_cfg = {
            "variant": variant,
            "epsilon": recipe.get("epsilon"),
            "lambda0": recipe.get("lambda0"),
            "phi": recipe.get("phi"),
            "ecm_init": recipe.get("ecm_init"),
            "maxSteps": steps,
        }

        trial_cfg = mc.inject_ecm_from_trial(job_id, encoder, last_cfg=trial_cfg, last_score=None)
        if not trial_cfg:
            raise RuntimeError("[MANUAL] inject_ecm_from_trial returned no config")

        # Inspect Λ before modulation/training
        lambda_before = inspect_lambda(model, label="Λ (before)")

        metrics_stream = []

        # Indispensable mode forces training — ECM must become load-bearing.
        use_training = args.train or is_indispensable
        if use_training:
            # --- Optimizer + (optional) loss (seq2seq uses model.loss; causal uses CE through labels) ---
            print("[TRAIN] Training enabled in manual mode — running gradient updates per step.")
            ds, sample_at = build_training_ds(ds_name, ds_config, ds_split)
            _set_memory_efficient_training(model, enable=True, memory_efficient=args.memory_efficient)
            if args.freeze_base:
                trainable = _trainable_ecm_params(model)
                print(f"  {DIM}Training {sum(p.numel() for p in trainable):,} ECM params (base model frozen){RESET}")
            else:
                trainable = list(model.parameters())
            optimizer = _make_optimizer(trainable, lr=1e-4, memory_efficient=args.memory_efficient)
            model.train()

            for step_idx in range(steps):
                sample = sample_at(step_idx)
                text = (sample.get("text") or "").strip() or "Hello world."

                inputs = tokenizer(
                    text,
                    return_tensors="pt",
                    truncation=True,
                    max_length=128,
                    padding=True
                ).to(device)

                if is_seq2seq:
                    labels = inputs["input_ids"]  # define labels first
                    outputs = model(
                        input_ids=inputs["input_ids"],
                        attention_mask=inputs["attention_mask"],
                        labels=labels
                    )

                    loss = outputs.loss
                    logits = outputs.logits
                    pad_id = tokenizer.pad_token_id if tokenizer.pad_token_id is not None else -100
                    mask = (labels != pad_id)
                    preds = logits.argmax(dim=-1)
                    correct = (preds.eq(labels) & mask).sum().item()
                    total = mask.sum().item()

                else:
                    labels = inputs["input_ids"]
                    outputs = model(
                        input_ids=inputs["input_ids"],
                        attention_mask=inputs["attention_mask"],
                        labels=labels
                    )

                    loss = outputs.loss
                    logits = outputs.logits
                    shift_logits = logits[:, :-1, :].contiguous()
                    shift_labels = labels[:, 1:].contiguous()
                    with torch.no_grad():
                        pred_ids = shift_logits.argmax(dim=-1)
                        mask = (shift_labels != (tokenizer.pad_token_id or -100))
                        correct = (pred_ids.eq(shift_labels) & mask).sum().item()
                        total = mask.sum().item()

                acc = (correct / total) if total > 0 else 0.0
                ppl = math.exp(loss.item())

                # --- Indispensable mode: use Family D loss ---
                if is_indispensable and step_idx >= indisp_min_steps:
                    indisp_result = compute_indispensability_loss(
                        model, inputs, alpha=indisp_alpha, beta=indisp_beta,
                    )
                    final_loss = indisp_result["total_loss"]
                    indisp_val = indisp_result["indispensability_loss"].item()
                else:
                    final_loss = loss
                    indisp_val = None

                optimizer.zero_grad()
                final_loss.backward()
                optimizer.step()

                metric = {
                    "step": step_idx + 1,
                    "total": steps,
                    "accuracy": float(acc),
                    "loss": float(loss.item()),
                    "perplexity": float(ppl),
                }
                if indisp_val is not None:
                    metric["indispensability"] = float(indisp_val)
                metrics_stream.append(metric)

                # Live inline progress (colored, same line)
                acc_color = GREEN if acc >= 0.5 else YELLOW
                loss_color = "\033[91m" if loss.item() > 5 else GREEN  # red if high loss
                indisp_str = f" | indisp={indisp_val:.4f}" if indisp_val is not None else ""
                sys.stdout.write(
                    f"\r{YELLOW}[MANUAL]{RESET} {GREEN}Step {step_idx + 1:03d}/{steps}{RESET} "
                    f"| acc={acc_color}{acc:.4f}{RESET} | loss={loss_color}{loss.item():.4f}{RESET} | ppl={ppl:.2f}{indisp_str}   "
                )
                sys.stdout.flush()


                # newline only after final step for clean formatting
                if step_idx + 1 == steps:
                    sys.stdout.write("\n")

                # stream live to AOC
                report = {"accuracy": acc, "loss": loss.item(), "perplexity": ppl}
                if indisp_val is not None:
                    report["indispensability"] = indisp_val
                mc._report_model_metrics(
                    args.model_template_id,
                    report,
                    step=step_idx + 1,
                )

            _set_memory_efficient_training(model, enable=False, memory_efficient=args.memory_efficient)

        else:
            # --- Run evaluation with streaming metrics (includes language-quality KPIs) ---
            for update in mc.compute_language_metrics_stream(
                model, tokenizer, args.model_template_id,
                ds_name=ds_name, ds_config=ds_config, ds_split=ds_split, steps=steps
            ):
                metrics_stream.append(update)
                step = update.get("step") or len(metrics_stream)
                acc = update.get("accuracy", 0)
                loss = update.get("loss", 0)
                ppl = update.get("perplexity", 0)

                # --- Inline progress ---
                bar_w = 20
                filled = int(bar_w * step / max(steps, 1))
                bar = "█" * filled + "░" * (bar_w - filled)
                acc_c = GREEN if acc >= 0.5 else YELLOW
                sys.stdout.write(
                    f"\r  [{bar}] {step}/{steps}  "
                    f"loss={loss:.4f}  ppl={ppl:.2f}  acc={acc_c}{acc:.4f}{RESET}   "
                )
                sys.stdout.flush()

                # --- Report live core metrics ---
                mc._report_model_metrics(
                    args.model_template_id,
                    {"accuracy": acc, "loss": loss, "perplexity": ppl},
                    step=step,
                )

                # --- Report language-quality metrics in real time ---
                quality_metrics = {
                    k: update.get(k, 0.0)
                    for k in ("rouge1", "rouge2", "rougeL", "bleu", "bertscore_f1")
                    if k in update
                }
                if any(v != 0 for v in quality_metrics.values()):
                    mc._report_model_metrics(args.model_template_id, quality_metrics, step=step)

            if metrics_stream:
                sys.stdout.write("\n")

        # Inspect Λ after modulation/training
        lambda_after = inspect_lambda(model, label="Λ (after)")
        if lambda_before is not None and lambda_after is not None:
            delta = torch.linalg.norm(lambda_after - lambda_before).item()
            print(f"[ΔΛ] Frobenius difference: {delta:.6f}")

        last = metrics_stream[-1] if metrics_stream else {}
        print(f"[RESULT] Manual run metrics: {last}")

        total_runtime = time.time() - start_time
        summary["runtime_secs"] = round(total_runtime, 2)

        # --- Finalize with rich metrics stream ---
        print("[DIAGNOSTIC] Final Λ state before certification:")
        inspect_lambda(model, label="Λ (final)")

        # --- Preserve language-quality metrics from last stream (no extra eval) ---
        if metrics_stream and any(k in metrics_stream[-1] for k in ("rouge1", "rouge2", "rougeL", "bleu", "bertscore_f1")):
            last.update({
                k: metrics_stream[-1][k]
                for k in ("rouge1", "rouge2", "rougeL", "bleu", "bertscore_f1")
                if k in metrics_stream[-1]
            })
            print("[EVALUATE] Added language-quality metrics to final report:",
                  {k: last.get(k) for k in ("rouge1","rouge2","rougeL","bleu","bertscore_f1")})

        # --- Report final language-quality metrics so dashboard sees them ---
        for k in ("rouge1", "rouge2", "rougeL", "bleu", "bertscore_f1"):
            if k in last:
                mc._report_model_metrics(args.model_template_id, {k: last[k]}, step=steps)

        # --- Run ablation probe if indispensable mode ---
        indisp_metrics = None
        if is_indispensable:
            phase("Ablation probe (indispensability)")
            probe_inputs = tokenizer(
                "What is your name?", return_tensors="pt", truncation=True, max_length=64
            ).to(device)
            probe_inputs["labels"] = probe_inputs["input_ids"].clone()
            indisp_metrics = run_ablation_probe(model, probe_inputs, tokenizer)
            strength = indisp_metrics.get("governance_strength", "unknown")
            sep = indisp_metrics.get("separation_ratio", 0)
            print(f"  {BOLD}Governance Strength: {strength.upper()}{RESET}")
            print(f"  Authorized PPL:   {indisp_metrics.get('authorized_ppl')}")
            print(f"  Unauthorized PPL: {indisp_metrics.get('unauthorized_ppl')}")
            print(f"  Separation:       {sep}x")
            print(f"  KL Divergence:    {indisp_metrics.get('kl_divergence')}")

        mc.finalize_and_certify(
            run_dir,
            model,
            tokenizer,
            last,
            trial_cfg["variant"],
            job_id,
            args.model_template_id,
            all_metrics=metrics_stream,  # ✅ include per-step data for PNG/CSV
            baseline_metrics=baseline_metrics,  # ✅ include comparison table
            exp_config={**trial_cfg, "runtime": total_runtime},  # ✅ pass ephaptic config + runtime
            indispensability_metrics=indisp_metrics,
        )
        print(f"[INFO] Reports saved under: {run_dir}")
        print("[DONE] Manual mode finished successfully.")

    # =========================
    # AUTO MODE
    # =========================
    else:
        phase("Ephaptic modulation (auto mode)")
        print(f"  {BLUE}>{RESET} Running Bayesian search over EC-ANN configurations")
        best_score, best_metrics, best_variant, best_stream = None, None, None, None
        last_cfg, last_score = None, None
        trial_num = 0
        budget = int((recipe.get("search") or {}).get("budget", 0) or 20)

        while True:
            trial_cfg = mc.inject_ecm_from_trial(
                job_id, encoder,
                last_cfg=last_cfg, last_score=last_score
            )
            if not trial_cfg:
                print("\n[INFO] No more trials. Auto mode loop finished.")
                break

            trial_num += 1
            print(f"\n[TRIAL {trial_num}/{budget}] Config → {trial_cfg}")

            # Per-trial Family D hyperparameters: AOC may now propose alpha/beta
            # in trial_cfg (search space added 2026-04-30). Fall back to the
            # template-level indisp_cfg defaults if AOC didn't return them
            # (e.g. older AOC backends or when these fields aren't in the space).
            trial_alpha = float(trial_cfg.get("alpha", indisp_alpha))
            trial_beta  = float(trial_cfg.get("beta",  indisp_beta))
            if "alpha" in trial_cfg or "beta" in trial_cfg:
                print(f"  {DIM}[INDISP] trial α={trial_alpha} β={trial_beta} (AOC-proposed){RESET}")

            # For training-enabled trials, we can (optionally) isolate updates by copying the model.
            # This avoids cross-trial contamination of weights.
            # Indispensable mode forces training — ECM must become load-bearing.
            use_training = args.train or is_indispensable
            model_trial = deepcopy(model) if use_training else model
            encoder_trial = model_trial.get_encoder() if is_seq2seq else model_trial

            # Inspect Λ before modulation/training
            lambda_before = inspect_lambda(model_trial, label=f"Λ (trial {trial_num} before)")

            metrics_stream = []

            if use_training:
                # --- Show how many trials we expect overall (once, before the first) ---
                if trial_num == 1:
                    print(f"{YELLOW}[INFO] Preparing to run {budget} ephaptic trials (auto mode){RESET}")

                print(f"[TRAIN] Training enabled for trial {trial_num} — running gradient updates per step.")
                ds, sample_at = build_training_ds(ds_name, ds_config, ds_split)
                _set_memory_efficient_training(model_trial, enable=True, memory_efficient=args.memory_efficient)
                if args.freeze_base:
                    trainable = _trainable_ecm_params(model_trial)
                    if trial_num == 1:
                        print(f"  {DIM}Training {sum(p.numel() for p in trainable):,} ECM params per trial (base model frozen){RESET}")
                else:
                    trainable = list(model_trial.parameters())
                optimizer = _make_optimizer(trainable, lr=1e-4, memory_efficient=args.memory_efficient)
                model_trial.train()

                for step_idx in range(steps):
                    sample = sample_at(step_idx)
                    text = (sample.get("text") or "").strip() or "Hello world."

                    inputs = tokenizer(
                        text,
                        return_tensors="pt",
                        truncation=True,
                        max_length=128,
                        padding=True
                    ).to(device)

                    # --- forward + loss ---
                    if is_seq2seq:
                        outputs = model_trial(
                            input_ids=inputs["input_ids"],
                            attention_mask=inputs["attention_mask"],
                            labels=inputs["input_ids"]
                        )
                        loss = outputs.loss
                        logits = outputs.logits
                        labels = inputs["input_ids"]
                        pad_id = tokenizer.pad_token_id if tokenizer.pad_token_id is not None else -100
                        mask = (labels != pad_id)
                        preds = logits.argmax(dim=-1)
                        correct = (preds.eq(labels) & mask).sum().item()
                        total = mask.sum().item()
                    else:
                        labels = inputs["input_ids"]
                        outputs = model_trial(
                            input_ids=inputs["input_ids"],
                            attention_mask=inputs["attention_mask"],
                            labels=labels
                        )
                        loss = outputs.loss
                        logits = outputs.logits
                        shift_logits = logits[:, :-1, :].contiguous()
                        shift_labels = labels[:, 1:].contiguous()
                        with torch.no_grad():
                            pred_ids = shift_logits.argmax(dim=-1)
                            mask = (shift_labels != (tokenizer.pad_token_id or -100))
                            correct = (pred_ids.eq(shift_labels) & mask).sum().item()
                            total = mask.sum().item()

                    acc = (correct / total) if total > 0 else 0.0
                    ppl = math.exp(loss.item())

                    # --- Indispensable mode: use Family D loss ---
                    if is_indispensable and step_idx >= indisp_min_steps:
                        # trial_alpha/trial_beta carry AOC's per-trial proposal
                        # (or fall back to indisp_alpha/indisp_beta defaults).
                        indisp_result = compute_indispensability_loss(
                            model_trial, inputs, alpha=trial_alpha, beta=trial_beta,
                        )
                        final_loss = indisp_result["total_loss"]
                        indisp_val = indisp_result["indispensability_loss"].item()
                    else:
                        final_loss = loss
                        indisp_val = None

                    optimizer.zero_grad()
                    final_loss.backward()
                    optimizer.step()

                    metric = {
                        "step": step_idx + 1,
                        "total": steps,
                        "accuracy": float(acc),
                        "loss": float(loss.item()),
                        "perplexity": float(ppl),
                    }
                    if indisp_val is not None:
                        metric["indispensability"] = float(indisp_val)
                    metrics_stream.append(metric)

                    # Live inline colored progress (single line)
                    acc_color = GREEN if acc >= 0.5 else YELLOW
                    loss_color = "\033[91m" if loss.item() > 5 else GREEN
                    indisp_str = f" | indisp={indisp_val:.4f}" if indisp_val is not None else ""
                    sys.stdout.write(
                        f"\r{YELLOW}[TRIAL {trial_num}/{budget}] {GREEN}Step {step_idx + 1:03d}/{steps}{RESET} "
                        f"| acc={acc_color}{acc:.4f}{RESET} | loss={loss_color}{loss.item():.4f}{RESET} | ppl={ppl:.2f}{indisp_str}   "
                    )
                    sys.stdout.flush()

                    # newline only after the last step so the next print starts cleanly
                    if step_idx + 1 == steps:
                        sys.stdout.write("\n")

                    # stream live to AOC
                    report = {"accuracy": acc, "loss": loss.item(), "perplexity": ppl}
                    if indisp_val is not None:
                        report["indispensability"] = indisp_val
                    mc._report_model_metrics(
                        args.model_template_id,
                        report,
                        step=step_idx + 1,
                    )

                _set_memory_efficient_training(model_trial, enable=False, memory_efficient=args.memory_efficient)

            else:
                # --- Run evaluation with streaming metrics (includes language-quality KPIs) ---
                for update in mc.compute_language_metrics_stream(
                    model_trial, tokenizer, args.model_template_id,
                    ds_name=ds_name, ds_config=ds_config, ds_split=ds_split, steps=steps
                ):
                    metrics_stream.append(update)
                    step = update.get("step") or len(metrics_stream)
                    acc = update.get("accuracy", 0)
                    loss = update.get("loss", 0)
                    ppl = update.get("perplexity", 0)

                    # --- Report live core metrics ---
                    mc._report_model_metrics(
                        args.model_template_id,
                        {"accuracy": acc, "loss": loss, "perplexity": ppl},
                        step=step,
                    )

                    # --- 🆕 Report language-quality metrics in real time ---
                    quality_metrics = {
                        k: update.get(k, 0.0)
                        for k in ("rouge1", "rouge2", "rougeL", "bleu", "bertscore_f1")
                        if k in update
                    }
                    if any(v != 0 for v in quality_metrics.values()):
                        mc._report_model_metrics(args.model_template_id, quality_metrics, step=step)

                    # Inline progress
                    bar_w = 20
                    filled = int(bar_w * step / max(steps, 1))
                    bar = "█" * filled + "░" * (bar_w - filled)
                    acc_c = GREEN if acc >= 0.5 else YELLOW
                    sys.stdout.write(
                        f"\r  [{bar}] {step}/{steps}  "
                        f"loss={loss:.4f}  ppl={ppl:.2f}  acc={acc_c}{acc:.4f}{RESET}   "
                    )
                    sys.stdout.flush()

                if metrics_stream:
                    sys.stdout.write("\n")

            # Inspect Λ after modulation/training
            lambda_after = inspect_lambda(model_trial, label=f"Λ (trial {trial_num} after)")
            if lambda_before is not None and lambda_after is not None:
                delta = torch.linalg.norm(lambda_after - lambda_before).item()
                print(f"[ΔΛ] Change during trial {trial_num}: {delta:.6f}")

            last = metrics_stream[-1] if metrics_stream else {}

            # ── Held-out evaluation (was: in-distribution score) ─────
            # Originally the trial score was computed from the LAST training-
            # stream metric, i.e. on the same WikiText slice the model just
            # trained on. That rewarded configurations that memorize the
            # training set and silently penalized ones that preserved general
            # language ability — the exact failure mode that produced the
            # ε=0.5/α=10 hyperparameters which collapsed authorized PPL on
            # held-out WikiText to 488,917 (vs vanilla 31). See security paper
            # EXPERIMENTS.md for context.
            #
            # Now we evaluate on the held-out test split (test[:200] for
            # wikitext, otherwise the configured split with a small step
            # budget) and report THAT to AOC. AOC's Bayesian search will then
            # converge toward configurations that generalize, not overfit.
            #
            # If the held-out eval fails for any reason, fall back to the
            # original behavior so the trial still reports something rather
            # than crashing the search loop.
            # Anchor held-out on WikiText test regardless of what training
            # data was used. This gives a *generalization* signal that's
            # invariant to the (possibly narrow) task-specific training set.
            # Configurations that memorize the training data will look great
            # on the in-distribution training stream but blow up on this
            # held-out PPL — exactly the failure mode we want AOC to avoid.
            held_out_ds_name   = "wikitext"
            held_out_ds_config = "wikitext-103-raw-v1"
            held_out_split     = "test[:200]"
            held_out_steps     = 20  # quick — ~30 sec extra per trial
            held_out_last = None
            try:
                held_out_stream = []
                for upd in mc.compute_language_metrics_stream(
                    model_trial, tokenizer, args.model_template_id,
                    ds_name=held_out_ds_name, ds_config=held_out_ds_config,
                    ds_split=held_out_split, steps=held_out_steps,
                ):
                    held_out_stream.append(upd)
                if held_out_stream:
                    held_out_last = held_out_stream[-1]
                    last["held_out_loss"] = held_out_last.get("loss")
                    last["held_out_perplexity"] = held_out_last.get("perplexity")
                    last["held_out_accuracy"] = held_out_last.get("accuracy")
            except Exception as e:
                print(f"[WARN] held-out eval failed ({e}); falling back to in-distribution score")

            # ── Indispensability eval: re-evaluate the trial with Λ zeroed ──
            # The previous scoring (held-out PPL only) is anti-correlated
            # with indispensability: small ε produces minimal perturbation,
            # cleanest held-out PPL, AOC scores it best — but Λ is decorative
            # and adversaries trivially recover the trained behavior. We saw
            # this empirically across multiple AOC runs (Apr 30 / May 1):
            # AOC selected ε≈0.05–0.33 → 0/3 adversaries blocked. Hand-tuned
            # ε=0.5 → 2/3 blocked with 12-orders-of-magnitude PPL gap.
            #
            # Fix: run held-out eval a SECOND time with Λ zeroed, compute
            # the loss gap, and add it to the score. AOC will then prefer
            # configurations where (a) the model works with Λ AND (b) the
            # model breaks without Λ — i.e., Λ is genuinely load-bearing.
            #
            # Math check: Λ=0 reduces both variants to W-only:
            #   multiplicative: f(S · (1 + ε·φ(0))) = f(S · 1) = f(S)
            #   additive:       f(S + ε·φ(0))      = f(S + 0) = f(S)
            # So zero-Λ is functionally equivalent to ECM-removed.
            held_out_no_lambda_last = None
            saved_lambda = {}
            try:
                for name, param in model_trial.named_parameters():
                    if "lambda_ecm" in name:
                        saved_lambda[name] = param.data.clone()
                        param.data.zero_()
                if saved_lambda:
                    nl_stream = []
                    for upd in mc.compute_language_metrics_stream(
                        model_trial, tokenizer, args.model_template_id,
                        ds_name=held_out_ds_name, ds_config=held_out_ds_config,
                        ds_split=held_out_split, steps=held_out_steps,
                    ):
                        nl_stream.append(upd)
                    if nl_stream:
                        held_out_no_lambda_last = nl_stream[-1]
                        last["nolambda_loss"] = held_out_no_lambda_last.get("loss")
                        last["nolambda_perplexity"] = held_out_no_lambda_last.get("perplexity")
            except Exception as e:
                print(f"[WARN] zero-Λ eval failed ({e}); indispensability gap unavailable")
            finally:
                # Always restore Λ — even if the eval crashed, the trial's
                # original Λ values must come back so subsequent trials and
                # the final report see the trained tensor, not zeros.
                for name, param in model_trial.named_parameters():
                    if name in saved_lambda:
                        param.data.copy_(saved_lambda[name])

            if held_out_last is not None:
                # Held-out score: same accuracy-minus-loss form, but on text
                # the trial never trained on. Higher = better generalization.
                base_score = held_out_last.get("accuracy", 0.0) - held_out_last.get("loss", 0.0)

                # Indispensability bonus: how much worse is the model without Λ?
                # Positive = Λ contributes; negative = Λ is harmful.
                # Combined: AOC picks configs that maximize
                #   (acc_with − loss_with) + α·(loss_without − loss_with)
                # which expands to acc_with − (1+α)·loss_with + α·loss_without
                # — naturally double-weights "model works with Λ" while still
                # rewarding "model breaks without Λ". α is configurable so we
                # can tune the trade-off without code changes.
                indisp_gap = 0.0
                if held_out_no_lambda_last is not None:
                    loss_with = held_out_last.get("loss", 0.0)
                    loss_without = held_out_no_lambda_last.get("loss", 0.0)
                    indisp_gap = loss_without - loss_with
                    last["indispensability_gap"] = indisp_gap
                alpha_indisp = float(os.getenv("AOC_INDISPENSABILITY_WEIGHT", "1.0"))
                score = base_score + alpha_indisp * indisp_gap
                score_source = "held_out+indispensability" if held_out_no_lambda_last is not None else "held_out"
            else:
                score = last.get("accuracy", 0.0) - last.get("loss", 0.0)
                score_source = "in_distribution_fallback"

            # Sanitize NaN/Inf before reporting to AOC. Random-init Λ at high
            # norms can diverge during training (loss → NaN); the SDK's
            # report_metrics() then fails JSON serialization, which the SDK's
            # auto-mode loop treats as a fatal error and exits — losing the
            # rest of the search budget. Convert NaN/Inf to a "very bad"
            # finite score so AOC's Bayesian prior learns to avoid that
            # region and the loop continues. See ephapsys-research#6.
            NAN_SENTINEL_SCORE = -1e9
            def _sanitize(v):
                if v is None:
                    return v
                if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                    return NAN_SENTINEL_SCORE
                return v
            trial_was_nan = False
            if isinstance(score, float) and (math.isnan(score) or math.isinf(score)):
                trial_was_nan = True
                score = NAN_SENTINEL_SCORE
            last = {k: _sanitize(v) for k, v in last.items()}
            if trial_was_nan:
                print(f"{YELLOW}[WARN] Trial {trial_num}/{budget} produced NaN/Inf — sanitized to score={NAN_SENTINEL_SCORE} so AOC can continue. Config: {trial_cfg}{RESET}")

            last_cfg, last_score = trial_cfg, score
            print(f"[RESULT] Trial {trial_num}/{budget} score={score:.3f} ({score_source}), metrics={last}")

            if best_score is None or score > best_score:
                best_score, best_metrics, best_variant = score, last, trial_cfg
                best_stream = list(metrics_stream)
                print(f"{GREEN}[BEST] Updated best score={best_score:.3f}, config={best_variant}{RESET}")

        if best_metrics:
            total_runtime = time.time() - start_time
            summary["runtime_secs"] = round(total_runtime, 2)

            print("[DIAGNOSTIC] Final Λ (best variant):")
            inspect_lambda(model if not args.train else model, label="Λ (final best)")

            #  Build a strict exp_config for provenance (no None fields)
            exp_cfg = {
                "variant": best_variant.get("variant"),
                "epsilon": float(best_variant.get("epsilon")),
                "lambda0": float(best_variant.get("lambda0")),
                "phi": best_variant.get("phi"),
                "ecm_init": best_variant.get("ecm_init"),
                "runtime": total_runtime,
                "maxSteps": steps,
            }
            # Family D hyperparameters added to AOC search 2026-04-30. Persist
            # them in the summary when present so the security paper's Phase 0
            # parser can pick them up alongside (variant, ε, λ₀, φ, init).
            if "alpha" in best_variant:
                exp_cfg["alpha"] = float(best_variant.get("alpha"))
            if "beta" in best_variant:
                exp_cfg["beta"] = float(best_variant.get("beta"))
            print(f"[INFO] Final exp_config for report: {json.dumps(exp_cfg, indent=2)}")

            # Normalize naming for report compatibility
            # --- Guard against None trial_cfg on faster GCP runs ---
            if best_variant is not None:
                best_variant["maxSteps"] = best_variant.get("maxSteps", steps)
                best_variant.pop("timesteps", None)
            else:
                if VERBOSE:
                    print(f"  {DIM}[verbose] best_variant is None at summary stage — skipping patch{RESET}")


            # --- Preserve language-quality metrics from last stream (no extra eval) ---
            if best_stream and any(k in best_stream[-1] for k in ("rouge1", "rouge2", "rougeL", "bleu", "bertscore_f1")):
                best_metrics.update({
                    k: best_stream[-1][k]
                    for k in ("rouge1", "rouge2", "rougeL", "bleu", "bertscore_f1")
                    if k in best_stream[-1]
                })
                print("[EVALUATE] Added language-quality metrics to final best metrics:",
                      {k: best_metrics.get(k) for k in ("rouge1","rouge2","rougeL","bleu","bertscore_f1")})

            for k in ("rouge1", "rouge2", "rougeL", "bleu", "bertscore_f1"):
                if k in best_metrics:
                    mc._report_model_metrics(args.model_template_id, {k: best_metrics[k]}, step=steps)

            # --- Run ablation probe if indispensable mode ---
            indisp_metrics = None
            if is_indispensable:
                phase("Ablation probe (indispensability)")
                probe_inputs = tokenizer(
                    "What is your name?", return_tensors="pt", truncation=True, max_length=64
                ).to(device)
                probe_inputs["labels"] = probe_inputs["input_ids"].clone()
                indisp_metrics = run_ablation_probe(model, probe_inputs, tokenizer)
                strength = indisp_metrics.get("governance_strength", "unknown")
                sep = indisp_metrics.get("separation_ratio", 0)
                print(f"  {BOLD}Governance Strength: {strength.upper()}{RESET}")
                print(f"  Authorized PPL:   {indisp_metrics.get('authorized_ppl')}")
                print(f"  Unauthorized PPL: {indisp_metrics.get('unauthorized_ppl')}")
                print(f"  Separation:       {sep}x")
                print(f"  KL Divergence:    {indisp_metrics.get('kl_divergence')}")

            mc.finalize_and_certify(
                run_dir,
                model,            # keep main model artifact; Λ digests are uploaded separately
                tokenizer,
                best_metrics,
                exp_cfg["variant"],
                job_id,
                args.model_template_id,
                all_metrics=best_stream,
                baseline_metrics=baseline_metrics,
                exp_config=exp_cfg,
                indispensability_metrics=indisp_metrics,
            )

            summary["best_variant"] = exp_cfg  # keep summary.json consistent
            summary["best_score"] = best_score
            summary["best_metrics"] = best_metrics
            summary["timesteps"] = steps
            phase("Modulation complete")
            print(f"  {GREEN}+{RESET} Best trial: score={BOLD}{best_score:.3f}{RESET} acc={best_metrics.get('accuracy', 0):.4f} loss={best_metrics.get('loss', 0):.4f} ppl={best_metrics.get('perplexity', 0):.1f}")
        else:
            print("[WARN] No valid trials executed in auto mode.")

    # --- Always write summary.json ---
    summary_path = os.path.join(run_dir, "summary.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"[INFO] Summary written to {summary_path}")


if __name__ == "__main__":
    main()
