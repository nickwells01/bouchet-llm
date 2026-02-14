# bouchet-llm

Run [Claude Code](https://docs.anthropic.com/en/docs/claude-code) backed by open-source models on Yale's [Bouchet HPC cluster](https://docs.ycrc.yale.edu/clusters/bouchet/) (H200 GPUs).

## How it works

```
Claude Code  →  LiteLLM proxy  →  SSH tunnel  →  vLLM on Bouchet H200
  (local)         (local)           (local)          (SLURM job)
```

1. **vLLM** serves an open-source model on a Bouchet GPU node via SLURM
2. **SSH tunnel** forwards the remote port to localhost
3. **LiteLLM proxy** translates Claude API calls → OpenAI-compatible vLLM calls
4. **Claude Code** connects to LiteLLM thinking it's talking to Anthropic's API

## Quick start

```bash
# Launch a model and start Claude Code
bouchet --launch --model Qwen/Qwen3-32B

# Or connect to an already-running job
bouchet
```

## Commands

| Command | Description |
|---------|-------------|
| `bouchet` | Connect to running vLLM job + launch Claude Code |
| `bouchet --launch [opts]` | Submit SLURM job, wait, tunnel, launch Claude Code |
| `bouchet --status` | Show job info (model, node, tunnel status) |
| `bouchet --models` | Show model catalog with download status |
| `bouchet --download <id>` | Download a model to Bouchet via HuggingFace |
| `bouchet --chat` | Simple interactive chat mode (no Claude Code) |
| `bouchet --tunnel` | Open tunnel only (no Claude Code) |
| `bouchet --cancel` | Cancel running vLLM job |

### Launch options

```bash
bouchet --launch \
  --model Qwen/Qwen3-32B \    # Model ID (default from catalog)
  --gpus 1 \                   # Number of GPUs (default from catalog)
  --time 06:00:00 \            # Wall time
  --partition gpu_h200 \       # SLURM partition (auto-selected if omitted)
  --quant fp8 \                # Quantization: fp8, none (default from catalog)
  --max-len 40960              # Max context length (default from catalog)
```

## Model catalog

Models are defined in `models.yaml`. The catalog provides defaults for GPU count, context length, quantization, and extra vLLM args per model.

```bash
bouchet --models
```

```
MODEL                               GPUs     CTX QUANT  STATUS    DESCRIPTION
Qwen/Qwen3-Coder-Next                  2     40k   fp8  ready     80B MoE, 256k ctx, 70% SWE-bench
Qwen/Qwen3-32B                         1     40k   fp8  ready     32B dense, fast general-purpose
Qwen/Qwen2.5-72B-Instruct              1     32k   fp8  ready     72B dense, strong reasoning
Qwen/Qwen2.5-1.5B-Instruct             1     32k  none  ready     1.5B dense, testing only
moonshotai/Kimi-Dev-72B                 1     40k   fp8  ready     72B dense, 60% SWE-bench
```

To add a model, add an entry to `models.yaml` and run `bouchet --download <model-id>`.

## Architecture

### Files

| File | Description |
|------|-------------|
| `bouchet` | Main CLI script — job management, tunneling, LiteLLM proxy, Claude Code launch |
| `models.yaml` | Model catalog with per-model defaults (GPUs, context, quant, extra vLLM args) |
| `litellm_config.yaml` | LiteLLM proxy config template (placeholders filled at runtime) |
| `litellm_clamp.py` | LiteLLM callback — clamps output tokens, disables Qwen3 thinking mode, strips `<think>` tags |
| `chat.py` | Simple interactive chat client (no Claude Code dependency) |
| `chat.sh` | Shell wrapper for chat.py |
| `launch-llm.sh` | Legacy standalone launcher (predates `bouchet` CLI) |

### Remote files (on Bouchet)

| File | Description |
|------|-------------|
| `sbatch/vllm-serve.sbatch` | SLURM batch script — starts vLLM, writes connection JSON, runs keepalive |
| `scripts/keepalive.sh` | Periodic health check to prevent idle-GPU kill |
| `logs/connection-<jobid>.json` | Connection info (node, port, model) written by vLLM on startup |

### LiteLLM proxy

The proxy maps Claude model names (e.g. `claude-sonnet-4-5-20250929`) to the actual vLLM model, so Claude Code works without modification. It also:

- Clamps `max_tokens` / `max_completion_tokens` to fit the model's context window
- Injects `chat_template_kwargs: {enable_thinking: false}` to suppress Qwen3 `<think>` blocks
- Strips any remaining `<think>...</think>` tags from responses as a safety net

### Partition auto-selection

The `--launch` command auto-selects a SLURM partition based on GPU count and wall time:

- **gpu_devel**: ≤1 GPU, ≤6 hours (fast queue, 1 job limit)
- **gpu_h200**: default for >1 GPU or longer jobs
- Override with `--partition <name>`

## Requirements

### Local (macOS)
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (`npm install -g @anthropic-ai/claude-code`)
- [LiteLLM](https://docs.litellm.ai/) (`pip install litellm`)
- SSH access to Bouchet configured in `~/.ssh/config` as host `bouchet`

### Remote (Bouchet)
- Conda env `vllm_env` with vLLM, PyTorch, huggingface_hub
- CUDA 12.6
- Models downloaded to `~/project_pi_cc572/ngw23/hf_cache/hub/`

## Known limitations

- **Qwen3-Coder-Next** requires vLLM ≥0.14 (currently upgrading from 0.11.2)
- **Kimi-Dev-72B** tends to over-think on simple prompts (generates long reasoning chains)
- **gpu_devel** partition limited to 1 GPU and 1 job per user
- The model identifies itself as Claude (expected — LiteLLM maps model names)
- Qwen3 thinking mode (`<think>` tags) disabled via LiteLLM proxy; server-side disable requires vLLM ≥0.12
