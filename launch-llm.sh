#!/bin/bash
set -euo pipefail

# ── launch-llm.sh ──────────────────────────────────────────────────
# Submit a vLLM or Ollama job on Bouchet and open an SSH tunnel.
#
# Usage:
#   ./launch-llm.sh                                          # GLM-5 defaults
#   ./launch-llm.sh --model zai-org/GLM-5 --gpus 4 --time 06:00:00
#   ./launch-llm.sh --model zai-org/GLM-5 --gpus 4 --time 2-00:00:00
#   ./launch-llm.sh --ollama --model llama3.1:70b --gpus 1
#   ./launch-llm.sh --cancel                                 # Cancel running job

REMOTE="bouchet"
REMOTE_BASE="~/project_pi_cc572/ngw23/project/llm-serve"

# ── Defaults ────────────────────────────────────────────────────────
ENGINE="vllm"
MODEL="zai-org/GLM-5"
NUM_GPUS=4
WALLTIME="06:00:00"
PARTITION=""
QUANTIZATION="fp8"
MAX_MODEL_LEN=32768
LOCAL_PORT=""
CANCEL=false

# ── Parse args ──────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --ollama)       ENGINE="ollama"; shift ;;
        --model)        MODEL="$2"; shift 2 ;;
        --gpus)         NUM_GPUS="$2"; shift 2 ;;
        --time)         WALLTIME="$2"; shift 2 ;;
        --partition)    PARTITION="$2"; shift 2 ;;
        --quant)        QUANTIZATION="$2"; shift 2 ;;
        --max-len)      MAX_MODEL_LEN="$2"; shift 2 ;;
        --local-port)   LOCAL_PORT="$2"; shift 2 ;;
        --cancel)       CANCEL=true; shift ;;
        -h|--help)
            echo "Usage: launch-llm.sh [options]"
            echo ""
            echo "Options:"
            echo "  --model <id>       Model name (default: zai-org/GLM-5)"
            echo "  --gpus <n>         Number of GPUs (default: 4)"
            echo "  --time <HH:MM:SS>  Wall time (default: 06:00:00)"
            echo "  --partition <name>  SLURM partition (auto-selected if omitted)"
            echo "  --quant <type>     Quantization: fp8, none (default: fp8)"
            echo "  --max-len <n>      Max context length (default: 32768)"
            echo "  --local-port <n>   Local port for tunnel (default: same as remote)"
            echo "  --ollama           Use Ollama instead of vLLM"
            echo "  --cancel           Cancel the running LLM job"
            echo "  -h, --help         Show this help"
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ── Cancel mode ─────────────────────────────────────────────────────
if $CANCEL; then
    echo "Cancelling LLM jobs on Bouchet..."
    ssh "$REMOTE" "scancel --name=vllm-serve --name=ollama-serve 2>/dev/null; echo 'Done.'"
    exit 0
fi

# ── Auto-select partition ───────────────────────────────────────────
if [ -z "$PARTITION" ]; then
    # gpu_devel: max 2 GPUs, 12 CPUs, 120G mem, 6hr
    # gpu_h200: up to 16 GPUs, 2-day max
    if [ "$NUM_GPUS" -le 2 ]; then
        # Parse walltime to check if it fits gpu_devel (6hr)
        if [[ "$WALLTIME" == *-* ]]; then
            DAYS="${WALLTIME%%-*}"
            HMS="${WALLTIME#*-}"
        else
            DAYS=0
            HMS="$WALLTIME"
        fi
        IFS=: read -r H M S <<< "$HMS"
        TOTAL_SECS=$(( DAYS*86400 + 10#$H*3600 + 10#$M*60 + 10#$S ))

        if [ "$TOTAL_SECS" -le 21600 ]; then
            PARTITION="gpu_devel"
        else
            PARTITION="gpu_h200"
        fi
    else
        PARTITION="gpu_h200"
    fi
    echo "Auto-selected partition: $PARTITION"
fi

# ── Compute resource limits per partition ────────────────────────────
# gpu_devel: max 2 GPUs, 12 CPUs, 120G mem
# gpu_h200: generous limits
if [ "$PARTITION" = "gpu_devel" ]; then
    CPUS=$((NUM_GPUS * 4))
    [ "$CPUS" -gt 12 ] && CPUS=12
    MEM=$((NUM_GPUS * 60))G
    [ "$NUM_GPUS" -gt 2 ] && echo "WARNING: gpu_devel only supports 2 GPUs max"
else
    CPUS=$((NUM_GPUS * 4))
    MEM=$((NUM_GPUS * 64))G
fi

# ── Submit the SLURM job ────────────────────────────────────────────
echo "Submitting ${ENGINE} job: model=${MODEL} gpus=${NUM_GPUS} cpus=${CPUS} mem=${MEM} time=${WALLTIME} partition=${PARTITION}"

if [ "$ENGINE" = "vllm" ]; then
    SBATCH_SCRIPT="sbatch/vllm-serve.sbatch"
    EXPORT_VARS="MODEL=${MODEL},TP_SIZE=${NUM_GPUS},QUANTIZATION=${QUANTIZATION},MAX_MODEL_LEN=${MAX_MODEL_LEN}"
    SUBMIT_CMD="cd ${REMOTE_BASE} && sbatch \
        --partition=${PARTITION} \
        --time=${WALLTIME} \
        --gres=gpu:h200:${NUM_GPUS} \
        --cpus-per-task=${CPUS} \
        --mem=${MEM} \
        --export=ALL,${EXPORT_VARS} \
        ${SBATCH_SCRIPT}"
else
    SBATCH_SCRIPT="sbatch/ollama-serve.sbatch"
    EXPORT_VARS="OLLAMA_MODEL=${MODEL}"
    SUBMIT_CMD="cd ${REMOTE_BASE} && sbatch \
        --partition=${PARTITION} \
        --time=${WALLTIME} \
        --gres=gpu:h200:${NUM_GPUS} \
        --cpus-per-task=${CPUS} \
        --mem=${MEM} \
        --export=ALL,${EXPORT_VARS} \
        ${SBATCH_SCRIPT}"
fi

JOB_OUTPUT=$(ssh "$REMOTE" "$SUBMIT_CMD")
JOB_ID=$(echo "$JOB_OUTPUT" | grep -oE '[0-9]+$')

if [ -z "$JOB_ID" ]; then
    echo "Failed to submit job. Output:"
    echo "$JOB_OUTPUT"
    exit 1
fi
echo "Job submitted: ${JOB_ID}"

# ── Wait for job to start ───────────────────────────────────────────
echo -n "Waiting for job to start"
CONNECTION_FILE="${REMOTE_BASE}/logs/connection-${JOB_ID}.json"

for i in $(seq 1 120); do
    STATE=$(ssh "$REMOTE" "squeue -j ${JOB_ID} -h -o %T 2>/dev/null || echo 'UNKNOWN'")
    if [ "$STATE" = "RUNNING" ]; then
        echo ""
        echo "Job is RUNNING."
        break
    elif [ "$STATE" = "UNKNOWN" ] || [ -z "$STATE" ]; then
        echo ""
        echo "Job failed or was cancelled."
        ssh "$REMOTE" "cat ${REMOTE_BASE}/logs/vllm-${JOB_ID}.err 2>/dev/null || true"
        exit 1
    fi
    echo -n "."
    sleep 5
done

# ── Read connection info ────────────────────────────────────────────
echo "Reading connection info..."
for i in $(seq 1 30); do
    CONN_JSON=$(ssh "$REMOTE" "cat ${CONNECTION_FILE} 2>/dev/null || echo ''")
    if [ -n "$CONN_JSON" ]; then
        break
    fi
    sleep 2
done

if [ -z "$CONN_JSON" ]; then
    echo "Timed out waiting for connection file."
    echo "Check logs: ssh ${REMOTE} 'cat ${REMOTE_BASE}/logs/vllm-${JOB_ID}.out'"
    exit 1
fi

REMOTE_NODE=$(echo "$CONN_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['node'])")
REMOTE_PORT=$(echo "$CONN_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['port'])")
LOCAL_PORT="${LOCAL_PORT:-${REMOTE_PORT}}"

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  LLM Server Ready"
echo "═══════════════════════════════════════════════════════════════"
echo "  Job ID:    ${JOB_ID}"
echo "  Node:      ${REMOTE_NODE}"
echo "  Model:     ${MODEL}"
echo "  Engine:    ${ENGINE}"
echo "  Remote:    ${REMOTE_NODE}:${REMOTE_PORT}"
echo "  Local:     http://localhost:${LOCAL_PORT}/v1"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "Test with:"
echo "  curl http://localhost:${LOCAL_PORT}/v1/models"
echo ""
echo "Chat completion:"
echo "  curl http://localhost:${LOCAL_PORT}/v1/chat/completions \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"model\": \"${MODEL}\", \"messages\": [{\"role\": \"user\", \"content\": \"Hello!\"}]}'"
echo ""
echo "For coding tools, set:"
echo "  Base URL: http://localhost:${LOCAL_PORT}/v1"
echo "  Model:    ${MODEL}"
echo ""
echo "Press Ctrl+C to close the tunnel and end the session."
echo "(The SLURM job will continue running. Use --cancel to stop it.)"
echo ""

# ── Open SSH tunnel (blocks until Ctrl+C) ───────────────────────────
ssh -N -L "${LOCAL_PORT}:${REMOTE_NODE}:${REMOTE_PORT}" "$REMOTE"
