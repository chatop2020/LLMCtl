# Deploy Qwen3.8 Flash Next on 8×RTX PRO 6000D

LLMCtl supports the dedicated Qwen3.8 preview runtime, TP2, PLE CPU offload, expert parallelism, MTP, selectable KV-cache dtype, and guarded static YaRN.

> Qwen3.8 Flash Next and its vLLM integration are still preview software. The one-click path deploys four identical TP2 replicas but publishes them only after per-instance inference acceptance. Failures restore the previous runtime automatically, and successful deployments retain an explicit restore point. The generic form remains available for a staged canary rollout.

## Recommended one-click deployment

After upgrading to LLMCtl 3.6.2, open Model Deployments and use the Qwen3.8 Flash Next one-click panel at the top of the page. It reads the eight-GPU topology, shows four TP2 groups and the recommended preset, then performs image/model checks, fixed-revision download, backup, deployment, per-instance inference acceptance, and `gdn-inside` publication from one button. Failures after runtime changes restore the backup automatically; a successful run exposes a prominent restore button.

The commands below remain available for advanced manual control.

## Recommended production profile

For 8×84GB RTX PRO 6000D, 512GB RAM, PCIe 4.0, and no NVLink, use four identical TP2 replicas:

| Setting | Value |
| --- | --- |
| Model | `Inferact/Qwen3.8-Flash-Next-NVFP4` |
| Revision | `103a7608316173ca6edd49929544244de7ffda70` |
| Runtime | `vllm/vllm-openai:qwen38-flash-next` |
| Topology | 4×TP2 with expert parallelism |
| Context | native `262144`, YaRN=`1` |
| Scheduler slots | `8` per replica |
| PLE | CPU offload enabled |
| KV cache | `auto` / BF16 |
| MTP | `0` for the baseline; A/B test `2` later |
| Prefix cache | disabled until the preview stability fixes are validated |
| GPU utilization | `0.92` |
| Batched tokens | `8192` |
| Startup parallelism | `1` |

Inspect `nvidia-smi topo -m`, P2P read/write topology, and `numactl -H` before assigning pairs. Prefer the same PCIe switch, root complex, and NUMA node. Do not assume adjacent GPU numbers are the best pairs.

## Install

```bash
sudo bash install-llm-cluster.sh \
  --yes \
  --model-source huggingface \
  --model-id Inferact/Qwen3.8-Flash-Next-NVFP4 \
  --model-revision 103a7608316173ca6edd49929544244de7ffda70 \
  --tp-size 2 \
  --max-model-len 262144 \
  --max-num-seqs 8 \
  --active-instances 1 \
  --startup-parallelism 1 \
  --gpu-memory-utilization 0.92 \
  --max-num-batched-tokens 8192 \
  --ple-cpu-offload enabled \
  --expert-parallel enabled \
  --prefix-caching disabled \
  --flashinfer-autotune disabled \
  --mtp-speculative-tokens 0 \
  --kv-cache-dtype auto \
  --yarn-factor 1
```

On an existing cluster, open Model Deployments, apply the “Qwen3.8 NVFP4” preset, and first select only the best-topology GPU pair. After the canary passes, edit the same deployment, select all eight GPUs, and verify the four identical TP2 groups before confirming the read-only plan.

## Tune and roll back

Run `llmctl smoke --full` and benchmark C1/C2/C4/C8 with MTP disabled. Then change only the MTP draft-token count to `2` and repeat the exact workload. Keep MTP2 only when latency or throughput improves without quality, stability, KV-preemption, or tail-latency regressions.

For a fresh install, expand the accepted canary with `sudo llmctl scale 4`, then run the full smoke test again.

The production profile deliberately stays at native 262K. Static YaRN 4× is available as an all-replica opt-in, but a 1M window is estimated at roughly one full-window request per TP2 replica and applies the scaling to short prompts as well. It requires a separate quality and capacity acceptance.

`KV_CACHE_DTYPE=nvfp4` is also guarded. The current dedicated QSA backend declares only `auto/bfloat16`; LLMCtl reads the selected image's capability list before downloading weights. Future images can enable the option without changing the registry schema, but must still pass performance and accuracy checks.

Every deployment saves the registry, affected Worker environments, and gateway plan before changes. Failures roll back automatically. A successful deployment can be restored with:

```bash
sudo llmctl model rollback DEPLOYMENT_JOB_ID
```

Ornith deployments keep their own TP and runtime fields. Rolling back or redeploying Ornith 1.5 does not inherit Qwen-specific PLE, EP, MTP, KV-cache, or YaRN settings.
