#!/usr/bin/env python3
import os
import random
from pathlib import Path

import torch

from ephapsys.agent import TrustedAgent
from transformers import AutoConfig, AutoModelForCausalLM, AutoModelForSeq2SeqLM, AutoTokenizer


def load_env(sample_dir: Path) -> None:
    env_path = sample_dir / ".env"
    if not env_path.exists():
        raise SystemExit(f"missing {env_path}")
    for line in env_path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def clone_lambda_params(model):
    params = {}
    for name, param in model.named_parameters():
        if name.endswith("lambda_ecm"):
            params[name] = param.detach().clone()
    if not params:
        raise RuntimeError("No lambda_ecm parameters found on loaded model")
    return params


def assign_lambda_params(model, tensors):
    named = dict(model.named_parameters())
    with torch.no_grad():
        for name, tensor in tensors.items():
            named[name].copy_(tensor.to(device=named[name].device, dtype=named[name].dtype))


def build_variant(original, mode):
    if mode == "baseline":
        return {name: tensor.clone() for name, tensor in original.items()}
    if mode == "zero":
        return {name: torch.zeros_like(tensor) for name, tensor in original.items()}
    if mode == "random":
        variant = {}
        for name, tensor in original.items():
            std = float(tensor.float().std().item())
            scale = std if std > 0 else 1e-3
            variant[name] = torch.randn_like(tensor) * scale
        return variant
    if mode == "shuffle":
        variant = {}
        for name, tensor in original.items():
            flat = tensor.flatten()
            perm = flat[torch.randperm(flat.numel(), device=flat.device)]
            variant[name] = perm.view_as(tensor)
        return variant
    raise ValueError(f"Unknown mode: {mode}")


def measure_logits(model, tokenizer, prompt, device):
    inputs = tokenizer(prompt, return_tensors="pt").to(device)
    with torch.no_grad():
        outputs = model(**inputs)
        logits = outputs.logits[0, -1].detach().float().cpu()
    top_vals, top_idx = torch.topk(logits, k=5)
    top_tokens = [tokenizer.decode([int(idx)]).replace("\n", "\\n") for idx in top_idx]
    return {
        "logits": logits,
        "top_ids": [int(x) for x in top_idx],
        "top_tokens": top_tokens,
        "top_vals": [float(x) for x in top_vals],
    }


def summarize(reference, current):
    ref = reference["logits"]
    cur = current["logits"]
    delta = cur - ref
    l2 = float(torch.linalg.vector_norm(delta).item())
    cosine = float(torch.nn.functional.cosine_similarity(ref.unsqueeze(0), cur.unsqueeze(0)).item())
    max_abs = float(delta.abs().max().item())
    changed = sum(1 for a, b in zip(reference["top_ids"], current["top_ids"]) if a != b)
    return {
        "l2_delta": l2,
        "cosine": cosine,
        "max_abs_delta": max_abs,
        "top5_changed_positions": changed,
    }


def main():
    random.seed(0)
    torch.manual_seed(0)

    sample_dir = Path(__file__).resolve().parents[1]
    load_env(sample_dir)

    prompt = os.getenv("HELLOWORLD_ECM_PROBE_PROMPT", "What time is it?")
    print("Creating TrustedAgent...", flush=True)
    agent = TrustedAgent.from_env()
    print("Verifying agent state...", flush=True)
    ok, _ = agent.verify()
    if not ok:
        raise SystemExit("agent must already be personalized and verified before running this probe")

    print("Preparing runtime cache...", flush=True)
    runtimes = agent.prepare_runtime()
    runtime = runtimes.get("language")
    if not runtime:
        raise SystemExit("language runtime unavailable")

    model_path = runtime.get("model_path")
    if not model_path:
        raise SystemExit("language runtime missing model_path")
    print(f"Loading tokenizer/config from {model_path}...", flush=True)
    tokenizer = AutoTokenizer.from_pretrained(model_path)
    cfg = AutoConfig.from_pretrained(model_path)
    model_cls = AutoModelForSeq2SeqLM if cfg.model_type in ("t5", "mt5", "bart", "mbart", "pegasus", "prophetnet", "marian") else AutoModelForCausalLM
    state_dict = agent._load_model_state_dict(model_path)
    if state_dict is None:
        raise SystemExit("could not load local state dict for measurement")
    print("Constructing model and applying ECM hooks...", flush=True)
    can_init_from_config = not (getattr(cfg, "text_config", None) is not None and not hasattr(cfg, "vocab_size"))
    if can_init_from_config:
        model = model_cls.from_config(cfg)
        agent._apply_ecm_if_available(model, runtime, install_only=True)
        missing_keys, unexpected_keys = model.load_state_dict(state_dict, strict=False)
        if missing_keys:
            print(f"Missing keys during load: {missing_keys[:5]}", flush=True)
        if unexpected_keys:
            print(f"Unexpected keys during load: {unexpected_keys[:5]}", flush=True)
        model.to(agent._device())
    else:
        print("Using from_pretrained fallback for wrapped text config...", flush=True)
        model = model_cls.from_pretrained(model_path, config=cfg).to(agent._device())
    agent._apply_ecm_if_available(model, runtime)
    model.eval()

    original = clone_lambda_params(model)
    device = next(model.parameters()).device

    print("Measuring baseline logits...", flush=True)
    baseline = measure_logits(model, tokenizer, prompt, device)
    print(f"Prompt: {prompt}")
    print("Baseline top-5 next tokens:")
    for token, token_id, score in zip(baseline["top_tokens"], baseline["top_ids"], baseline["top_vals"]):
        print(f"  token={token!r} id={token_id} logit={score:.6f}")

    for mode in ("zero", "shuffle", "random"):
        assign_lambda_params(model, build_variant(original, mode))
        current = measure_logits(model, tokenizer, prompt, device)
        metrics = summarize(baseline, current)
        print(f"\nVariant: {mode}")
        print(f"  l2_delta={metrics['l2_delta']:.6f}")
        print(f"  cosine={metrics['cosine']:.6f}")
        print(f"  max_abs_delta={metrics['max_abs_delta']:.6f}")
        print(f"  top5_changed_positions={metrics['top5_changed_positions']}/5")
        for token, token_id, score in zip(current["top_tokens"], current["top_ids"], current["top_vals"]):
            print(f"  token={token!r} id={token_id} logit={score:.6f}")

    assign_lambda_params(model, original)


if __name__ == "__main__":
    main()
