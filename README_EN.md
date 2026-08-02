# LLMCtl: General-Purpose Multi-GPU vLLM Cluster Deployer

**Language:** [中文](README.md) | English

[![CI](https://github.com/chatop2020/LLMCtl/actions/workflows/ci.yml/badge.svg)](https://github.com/chatop2020/LLMCtl/actions/workflows/ci.yml)

LLMCtl searches Hugging Face or ModelScope for models on a bare-metal Ubuntu 24.04 host, conservatively filters them according to the local NVIDIA GPUs and VRAM, plans the deployment topology, and automatically deploys multiple vLLM workers plus one of four gateways: New API, LiteLLM, Bifrost, or OmniRoute. New API is the default recommendation; OmniRoute is intended for installations that need company self-registration, personal API keys, recurring quotas, and a model portal.

The project does not use Conda or modify the NVIDIA driver. Inference dependencies are contained in pinned Docker image versions. A LAN proxy may be used temporarily during installation; runtime is fully offline by default and does not update automatically.

> This is a prerelease intended for validation on real multi-GPU hosts. The
> [GitHub repository](https://github.com/chatop2020/LLMCtl) is the source of truth. Tests run locally by default, and the same checks can be started manually from GitHub Actions when needed.

## Key Features

- Search Hugging Face and ModelScope together or search either source independently.
- Run a read-only host preflight before recommendations: OS/architecture, CPU/cores/threads, memory/swap, NVIDIA GPUs/VRAM/driver/compute capability, current and maximum PCIe links, GPU/NUMA/NVLink topology, and model-filesystem capacity.
- Only display models that pass all of these gates: generative task, complete weights, non-platform-specific format, an architecture supported by vLLM 0.22.1, and capacity for at least an 8K context window on the local host.
- Explicitly reject Apple MLX conversions such as `mlx-community/*`. They target MLX on Apple Silicon and are not NVIDIA CUDA/vLLM weights. Native Ornith FP8/AWQ/GPTQ variants remain eligible.
- Automatically recommend TP, instance count, context length, `max-num-seqs`, and startup parallelism from weight size, runtime reserve, KV cache, minimum per-GPU VRAM, GPU count, CPU, available memory, PCIe/topology, and disk capacity.
- After candidate selection, show VRAM, topology, host-memory and disk budgets, itemized recommendation reasons, and warnings. The user can confirm, return to the list, search again, or quit.
- Recheck the architecture with `ModelRegistry` inside the pinned vLLM container before download, then verify configuration, weight presence, and size after download.
- Enable image/OCR input, OpenAI tool calling, reasoning parsing, and per-request reasoning disable controls only when the model capability matches.
- Map one GPU or one TP group to each worker. The installer configures all workers, authentication, and database state for the selected gateway and exposes a consistent Nginx-fronted `:8000/v1` endpoint.
- Install or reuse Nginx automatically. `/v1/` and `/ui/` are the consistent public entry points; inference goes directly to the gateway without passing through the portal; OmniRoute's native troubleshooting UI remains available at `/base_ui/`. LLMCtl owns an isolated config, validates it with `nginx -t`, rolls back failed changes, and preserves the package and unrelated sites on uninstall.
- In OmniRoute mode, deploy a Vue 3 company portal with email verification, an exact corporate-domain allowlist, registration and SMTP controls, users and groups, personal API keys, money balances, additional recurring token grants, per-model input/output/cache/reasoning prices, a usage ledger, and audit events.
- Use native OmniRoute APIs for model-ID mappings, Combos, per-key access, and free-tier resources. A free model must be discovered, configured, currently available, live-tested, and explicitly published. User keys authorize only the public model ID, so underlying model or Combo IDs cannot bypass portal policy.
- Start automatically through systemd. Workers can load concurrently in batches, and an SSH disconnect does not terminate background startup.
- Show aggregated startup and uninstall progress, including per-worker state, GPU memory, active systemd units, and containers. After reconnecting through SSH, continue observing with `llmctl startup watch`.
- Manage partial or full start, stop, restart, activation, scaling, logs, health checks, OCR, benchmarks, proxies, and offline bundles.
- Use `llmctl info` as a categorized recovery inventory covering public/internal endpoints, hardware, images, services, autostart, workers, databases, administrators, every key/password, SMTP, proxy state, model details, and file paths. Plaintext is the root-terminal default; use `--redact` before sharing it.
- Use `llmctl optimize` to collect streaming TTFT/ITL/E2E, aggregate throughput, GPU/VRAM/temperature, CPU/memory/swap, and vLLM KV-cache/queue/preemption/prefix-cache metrics. It explains every candidate's rationale, tradeoffs, and boundaries before consent, then backs up configuration, restarts and tests candidates, selects the objective-specific winner, runs full smoke acceptance, and rolls back on failure or interruption.

## Important Boundaries

“Installable according to the catalog” is a conservative preflight result, not an absolute runtime guarantee. Custom model code, tokenizers, quantization kernels, model repository contents, and vLLM itself may still have runtime compatibility issues. Installation therefore includes three real acceptance gates: architecture verification in the pinned image, full model loading, and capability-aware API smoke tests. Failure at any gate stops the installation and disables autostart.

Tool calling, reasoning, and OCR cannot be guaranteed merely because a model name suggests those capabilities. The scripts enable parameters only for recognized parser protocols. An unknown model may still be deployed as a regular text or image model, but LLMCtl does not claim capabilities it cannot verify.

## Files

| File | Purpose |
|---|---|
| `install-llm-cluster.sh` | Initial installation or model/topology reselection |
| `llmctl.sh` | Installed as the global `/usr/local/sbin/llmctl` command |
| `lib/model_catalog.py` | Hub search, capability detection, VRAM estimation, and deployment planning |
| `lib/runtime_optimizer.py` | Streaming benchmarks, GPU/vLLM metrics, conservative candidates, and objective scoring |
| `lib/gateway_config.py` | Secret-free configuration for all four gateways and New API/OmniRoute reconciliation |
| `lib/account_portal.py` | OmniRoute company account portal, verification, quotas, and model catalog |
| `portal-ui/` | Vue 3 company-portal source and frontend tests |
| `lib/account_portal_ui/` | Built portal assets copied directly by the installer |
| `tests/test_model_catalog.py` | Model catalog and hardware planning unit tests |
| `tests/test_runtime_optimizer.py` | Tuning advice, scoring, metrics parsing, and streaming-latency tests |
| `README.md` / `README_EN.md` | Chinese and English project overview |
| `USAGE.md` / `USAGE_EN.md` | Chinese and English operations, API, and troubleshooting manual |

## Defaults

| Setting | Default |
|---|---|
| Model directory | `/data/llm-cluster/models` |
| vLLM image | `vllm/vllm-openai:v0.22.1` |
| Gateway | New API (recommended) |
| New API image | `calciumion/new-api:v1.0.0-rc.22` |
| LiteLLM image | `ghcr.io/berriai/litellm:v1.94.0` |
| Bifrost image | `maximhq/bifrost:v1.6.7` |
| OmniRoute image | `diegosouzapw/omniroute:3.8.48` |
| PostgreSQL | `postgres:16-alpine` |
| Unified API | `http://SERVER_IP:8000/v1` |
| Unified Web UI | `http://SERVER_IP:8000/ui/` |
| OmniRoute native UI | `http://SERVER_IP:8000/base_ui/` |
| Internal listeners | Gateway `127.0.0.1:18000`; OmniRoute portal `127.0.0.1:8001` |
| Administrator username | `admin` |
| Initial password | `llm-admin` by default; OmniRoute generates a strong random value when omitted |
| Routing | Equal-weight healthy workers with failover; LiteLLM uses `least-busy` |
| GPU memory utilization | `0.92` |

The initial shared password for New API, LiteLLM, and Bifrost is intentionally simple as requested. Change it immediately. OmniRoute generates a strong random password by default, which should still be managed and rotated carefully:

```bash
sudo llmctl admin set-password
```

### Gateway Selection

The wizard offers four choices before image download. For unattended installs, use `--gateway`:

| Gateway | Best fit | Fully automated configuration |
|---|---|---|
| New API (default) | Friendly Chinese administration, channels, keys, and usage | Initializes the administrator, creates one equal-weight channel per healthy worker, and creates a root-only API token |
| LiteLLM | Broad provider compatibility and established proxy configuration | Generates the model list, `least-busy` routing, master key, and PostgreSQL settings |
| Bifrost | Efficient forwarding, observability, and virtual-key governance | Generates eight vLLM keys, equal-weight routing, a virtual key, admin authentication, and PostgreSQL log storage |
| OmniRoute | Local SQLite gateway plus a company account portal | Creates eight provider nodes and one equal-weight Combo; deploys a separate portal database, email registration, personal keys, recurring quotas, usage, and a model catalog |

All four use `llm-router.service`, but the actual gateway listens only on `127.0.0.1:18000`. Nginx publishes the consistent OpenAI-compatible `/v1/` and `/ui/` entry points on port `8000`; the root-only maintenance key is stored in `GATEWAY_API_KEY`. New API, LiteLLM, and Bifrost use PostgreSQL through `llm-database.service`. OmniRoute uses its own SQLite database, does not start PostgreSQL, and adds `llm-account.service` on loopback port `8001`. There is no online migration between gateway types: select one during a clean install after old service configuration has been removed. Existing model files and exact local Docker images are verified and reused. Review the upstream licenses for your distribution and modification model.

## Quick Start

Copy the entire directory to the server, enter it, and run:

```bash
chmod +x install-llm-cluster.sh llmctl.sh lib/model_catalog.py lib/runtime_optimizer.py lib/gateway_config.py lib/account_portal.py
sudo bash install-llm-cluster.sh
```

The interactive workflow asks you to:

1. Select 中文 or English. Chinese is the default; subsequent installer and catalog interaction uses the selection.
2. Immediately test international access against the Hugging Face model-catalog API. On failure, the installer explicitly offers proxy setup and retests it; unattended failure requires an explicit `--proxy`.
3. Select New API (default), LiteLLM, Bifrost, or OmniRoute. OmniRoute then asks about company registration, email domains, and SMTP.
4. Review the read-only OS, CPU, memory, GPU/driver, PCIe/topology/NUMA, and disk preflight.
5. Search Hugging Face, ModelScope, both sources, or enter a model directly, then provide a term and task.
6. Select a gated candidate. Enter `0`/`b` to go back or `q` to quit.
7. Review the detailed plan, itemized recommendation reasons, and warnings. Catalog candidates, validated profiles, and manually entered models all require confirmation. Press `Y` to accept, `b` to go back, `s` to search again, or `q` to quit.
8. Select the model directory and review TP, sequences per replica, active replicas, and the host-planned startup parallelism.

The wizard language can also be selected directly:

```bash
sudo bash install-llm-cluster.sh --lang en
```

Typical unattended ModelScope installation:

```bash
sudo bash install-llm-cluster.sh \
  --yes \
  --model-source modelscope \
  --catalog-query Qwen3 \
  --catalog-task text \
  --model-root /data/llm-cluster/models
```

Specify an exact Hugging Face model. If no revision is supplied, LLMCtl resolves and pins the current commit SHA:

```bash
sudo bash install-llm-cluster.sh \
  --yes \
  --model-source huggingface \
  --model-id Qwen/Qwen3-8B \
  --proxy http://10.1.0.6:7890
```

Use the retained Ornith-compatible profile:

```bash
sudo bash install-llm-cluster.sh --yes --model-source validated \
  --proxy http://10.1.0.6:7890
```

### Reusing Existing Weights and Skipping Downloads

If a retained 36 GiB Ornith model is stored under `/data/ornith/models`, select the validated `protoLabsAI/Ornith-1.0-35B-FP8` profile. LLMCtl checks its legacy manifest and actual files. On an exact match, that directory becomes the default and the installer reports that the download and weight copy are skipped. The 36 GiB directory does not need to be moved.

Reuse requires matching Hub, model ID, revision, `config.json` architecture, complete weight files, and a conservative size floor. Legacy Ornith manifests without a Hub field receive a narrow compatibility exception only for the pinned Hugging Face Ornith identities. Similar names or sizes are not identity: the ModelScope `deepreinforce-ai/Ornith-1.0-35B-FP8` cannot impersonate the Hugging Face `protoLabsAI/Ornith-1.0-35B-FP8`. A mismatch is never mixed in place; LLMCtl downloads the selected version normally.

For unattended reuse, select the retained root explicitly and add `--skip-download`. This option does not bypass the checks above:

```bash
sudo bash install-llm-cluster.sh --yes --model-source validated \
  --model-root /data/ornith/models --skip-download
```

Reselecting a model recalculates TP, context length, and capability parameters while retaining downloaded models and gateway database state:

```bash
sudo bash install-llm-cluster.sh --force-reconfigure
```

The general-purpose installer never overwrites a legacy `/etc/ornith` cluster automatically. It stops if old services are found so two worker sets cannot compete for the same GPUs and ports 8100–8107. This release does not implement online migration; remove the old services, retain the model files, and perform a clean install.

Do not use `llmctl download` to switch directly to a different model. Different models may require different TP and parsers. The manager rejects this unsafe switch; use the installer's `--force-reconfigure` workflow instead.

## Networking and Proxies

Immediately after language selection—and before gateway or model selection—the installer requests the Hugging Face model-catalog API directly. If that fails it explains the loss of catalog coverage, offers proxy setup, and requires the proxy retest to pass. Declining is allowed with an explicit warning. `--yes` and `--non-interactive` never hang on hidden input; they require `--proxy http://IP:PORT` explicitly.

`llmctl models search --source all|huggingface` performs the same preflight: direct access first, then a validated saved maintenance proxy, then a new prompt if needed. Health, status, and offline inference commands never prompt merely because international access is absent.

A proxy is used only for installation, model downloads, or explicit maintenance commands. Before installation finishes, the script:

1. Clears proxy variables from the current process.
2. Removes Docker's temporary proxy drop-in.
3. Restarts Docker so the temporary proxy is no longer effective.
4. Starts vLLM with `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`.

Save a maintenance proxy:

```bash
sudo llmctl proxy set 10.1.0.6 7890 http
sudo llmctl proxy test
sudo llmctl proxy show
sudo llmctl proxy clear
```

Private or gated Hugging Face models require `HF_TOKEN` when the installer runs, and you must accept the model license first. Private ModelScope models use `MODELSCOPE_API_TOKEN`. Tokens are not written to the cluster configuration.

## Upgrade the LLMCtl Control Plane

`llmctl upgrade` upgrades only LLMCtl's own programs: the manager, model-catalog/runtime-optimization/gateway helpers, and the account-portal backend and built Vue assets. It does not rerun the installer or modify/restart model workers, the router, Nginx, Docker, model weights, runtime configuration, secrets, or databases.

```bash
sudo llmctl upgrade
```

The command first asks whether to fetch the latest `chatop2020/LLMCtl` `main` from GitHub, then pins the download to one exact commit. If GitHub is not directly reachable, it validates the saved maintenance proxy first and asks for a new proxy only when necessary. The proxy is limited to this maintenance operation and may optionally be saved; it is never injected into inference services. The entire old control plane is backed up before replacement. If the account portal is running, only that service is stopped briefly for health acceptance, with automatic rollback on failure.

For a fully offline server, upload a repository ZIP and run:

```bash
sudo llmctl upgrade --from-zip /root/LLMCtl-main.zip
```

For the first upgrade from an older release that does not yet have the `upgrade` command, extract the new ZIP and run its bootstrap upgrader:

```bash
python3 -m zipfile -e /root/LLMCtl-main.zip /root/llmctl-upgrade-bootstrap
sudo bash /root/llmctl-upgrade-bootstrap/LLMCtl-main/upgrade-llmctl.sh \
  --from-zip /root/LLMCtl-main.zip
```

Add `--check` to download and validate without replacing files. Upgrade metadata is stored in `/var/lib/llm-cluster/control-plane-version.env`, backups are kept under `/var/backups/llmctl/`, and `llmctl info` reports the installed control-plane commit.

## Model Catalog Commands

After installation, you can still inspect the hardware and browse candidate models:

```bash
sudo llmctl models hardware
sudo llmctl models search Qwen3 --source all --task auto --limit 10
sudo llmctl models search OCR --source modelscope --task vision
sudo llmctl models inspect huggingface Qwen/Qwen3-8B
sudo llmctl models current
```

A catalog plan such as `TP1×8 ctx=32768 seq=7 start=8` means eight independent workers, up to seven vLLM scheduling sequences per worker, and a recommendation to load up to eight workers concurrently. It does not guarantee that every request can simultaneously occupy a 32K or 256K context. Longer requests and additional images increase KV cache pressure and reduce sustainable concurrency. PCIe/topology data is a capability snapshot, not an NCCL bandwidth test; use workload benchmarks for real performance.

For daily commands and API examples, see [USAGE_EN.md](USAGE_EN.md).

## Service and Data Locations

| Path or service | Contents |
|---|---|
| `/etc/llm-cluster/cluster.env` | Non-secret model, topology, and capability configuration |
| `/etc/llm-cluster/secrets.env` | API key, database credentials, and web administration credentials (mode 0600) |
| `/etc/llm-cluster/workers/*.env` | Worker-to-GPU mappings |
| `/usr/local/lib/llm-cluster/model_catalog.py` | Installed model catalog helper |
| `/usr/local/lib/llm-cluster/gateway_config.py` | Gateway configuration and New API reconciliation helper |
| `/usr/local/lib/llm-cluster/account_portal.py` | LLMCtl account portal backend |
| `/usr/local/lib/llm-cluster/account_portal_ui` | Built Vue 3 portal assets |
| `/usr/local/lib/llm-cluster/upgrade-llmctl.sh` | LLMCtl control-plane upgrader |
| `/etc/nginx/conf.d/llm-cluster.conf` | Isolated LLMCtl Nginx front-door configuration |
| `/var/lib/llm-cluster/cache` | Regenerable vLLM cache |
| `/var/lib/llm-cluster/omniroute/gateway/storage.sqlite` | OmniRoute's SQLite database |
| `/var/lib/llm-cluster/omniroute/portal/account-portal.db` | Separate account-portal SQLite database |
| `/data/llm-cluster/models` | Default model root |
| `llm-cluster.service` | Top-level oneshot service |
| `llm-worker@N.service` | vLLM worker |
| `llm-router.service` | Selected New API, LiteLLM, Bifrost, or OmniRoute API and UI |
| `llm-database.service` | PostgreSQL for non-OmniRoute gateways |
| `llm-account.service` | Company account portal in OmniRoute mode only |
| `nginx.service` | Public `/v1/`, `/ui/`, and optional `/base_ui/` front door |

## Portal Model Metadata and Ledgers

- The model editor reads context windows, maximum output limits, and detectable capabilities from the active AI gateway. For a multi-target routing combo, the portal shows the conservative usable value and lists every resolved target.
- When an administrator changes the context or output limit, LLMCtl writes it through the gateway's native API for every resolvable target. Partial failures remain visible, name the failed targets, and are audited; they are never presented as a successful sync.
- Model descriptions, OCR labels, and access scopes are LLMCtl publication metadata. They appear in the administration list and user catalog but are not misrepresented as gateway-native parameters.
- Billing separates request usage from monetary balance transactions. An empty money ledger is expected when grants cover all requests; token, model, user, and retained request-text records remain visible.
- `llmctl upgrade` upgrades only the LLMCtl control plane, portal assets, and maintenance scripts, applying in-place database migrations. It does not rebuild or replace existing workers or model weights.

## Security Notes

- Nginx listens on `0.0.0.0:8000` by default; gateways, the portal, workers, and PostgreSQL are constrained to loopback or the internal Docker network, and the account portal rejects any bind address other than `127.0.0.1`. A service may listen on `0.0.0.0` inside its container, but its host publication remains fixed to `127.0.0.1` and is not directly exposed. Production deployments should still restrict the public listener with a firewall and configure HTTPS. The installer can reuse existing Nginx, but it does not issue TLS certificates.
- Model revisions are recorded in the manifest. Hugging Face defaults to a pinned commit; ModelScope defaults to `master`. For reproducible deployments, explicitly provide a tag or commit hash.
- `--trust-remote-code` is enabled only when the model configuration declares `auto_map`. This still executes repository code, so select only reviewed models at pinned revisions.
- External image domains are denied by default. OCR examples use base64 `data:` URLs to reduce SSRF exposure.
- The scripts never upgrade images automatically. `llmctl update` pulls and validates images only when an administrator runs it explicitly.

## Development Validation

```bash
bash -n install-llm-cluster.sh llmctl.sh
python3 -m py_compile lib/*.py
python3 -m unittest discover -s tests -v
cd portal-ui
npm ci
npm test
npm run build
npm audit --audit-level=high
```

Before a real release, also perform a complete installation on the target server, run `llmctl smoke --full`, and execute `llmctl bench` with the actual request distribution. Targets such as 40 tokens/s, 25 concurrent requests, or a 256K context cannot be promised from VRAM estimates alone. They must be benchmarked with the actual model, input lengths, image counts, and output lengths.
