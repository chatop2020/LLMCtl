# LLMCtl: General-Purpose Multi-GPU vLLM Cluster Deployer

**Language:** [中文](README.md) | English

[![CI](https://github.com/chatop2020/LLMCtl/actions/workflows/ci.yml/badge.svg)](https://github.com/chatop2020/LLMCtl/actions/workflows/ci.yml)

LLMCtl searches Hugging Face or ModelScope for models on a bare-metal Ubuntu 24.04 host, conservatively filters them according to the local NVIDIA GPUs and VRAM, plans the deployment topology, and automatically deploys multiple vLLM workers plus one of four gateways: New API, LiteLLM, Bifrost, or OmniRoute. New API is the default recommendation; OmniRoute is intended for installations that need company self-registration, personal API keys, prepaid balances, and a model portal.

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
- In OmniRoute mode, deploy a Vue 3 company portal with configurable branding and an optional published origin, email verification, an exact corporate-domain allowlist, registration and SMTP controls, users and groups, personal API keys that remain stable across sign-ins and change only on explicit rotation, native active-session and per-minute/per-day request limits, prepaid cash balances, a one-time registration credit, per-model input/output/cache/reasoning prices, per-request monetary ledgers, and audit events. Paid-model access stays disabled after the balance is exhausted and returns only after a top-up.
- Use native OmniRoute APIs for model-ID mappings, Combos, per-key access, and free-tier resources. A free model must be discovered, configured, currently available, live-tested, and explicitly published. User keys authorize only the public model ID, so underlying model or Combo IDs cannot bypass portal policy.
- Accept images, PDFs, and common text attachments in the playground. Images are sent as multimodal content, while PDFs are rendered to page images in the browser and are never uploaded to the portal backend. Streaming results label input, output, and total tokens explicitly and show reasoning content, TTFT, and output speed.
- Run administration benchmarks on the server. The browser only submits and observes a job; a separate process generates meaningful prompts for the selected concurrency and target input size, then reports success rate, RPS, aggregate output tok/s, TTFT, end-to-end latency, and p50/p95/p99 in real time. It also records the Worker selected for every request and samples per-GPU utilization, memory, power, and peak simultaneous active-GPU count once per second to expose routing skew. High-load plans require explicit confirmation.
- Provide an on-demand administrator system monitor for CPU, memory, swap, GPU/VRAM/temperature/power, network interfaces, disks, and a `top`-like process table. It samples every two seconds only while the monitor page is visible and uses a one-second shared backend cache across tabs. The monitor is read-only and administrator-only; common keys, tokens, passwords, and credential-bearing URLs are redacted from process arguments.
- Start automatically through systemd. Workers can load concurrently in batches, and an SSH disconnect does not terminate background startup.
- Show aggregated startup and uninstall progress, including per-worker state, GPU memory, active systemd units, and containers. After reconnecting through SSH, continue observing with `llmctl startup watch`.
- Manage partial or full start, stop, restart, activation, scaling, logs, health checks, OCR, benchmarks, proxies, and offline bundles.
- Optionally compose web search, image/audio/video HTTP adapters, and explicit local or remote URL pools behind a published model through the Go workflow data plane. Ordinary models retain the existing gateway-to-Worker path. See [WORKFLOW_EN.md](WORKFLOW_EN.md).
- Add models from the administration portal, reuse local weights, explicitly configure local or remote Workers, partition GPUs, run background download and acceptance, restart only affected Workers, and roll back individual jobs. The portal also exposes the same Ornith version-upgrade contract as `llmctl model upgrade`: pin the target revision, re-plan TP, retain old weights, and keep a pre-upgrade rollback point. See [MULTI_MODEL_EN.md](MULTI_MODEL_EN.md) for both GPU partitioning and version upgrades.
- Use `llmctl info` as a categorized recovery inventory covering public/internal endpoints, hardware, images, services, autostart, workers, databases, administrators, every key/password, SMTP, proxy state, model details, and file paths. Plaintext is the root-terminal default; use `--redact` before sharing it.
- Use `llmctl optimize` to collect streaming TTFT/ITL/E2E, aggregate throughput, GPU/VRAM/temperature, CPU/memory/swap, and vLLM KV-cache/queue/preemption/prefix-cache metrics. It explains every candidate's rationale, tradeoffs, and boundaries before consent, then backs up configuration, restarts and tests candidates, selects the objective-specific winner, runs full smoke acceptance, and rolls back on failure or interruption.

## Important Boundaries

“Installable according to the catalog” is a conservative preflight result, not an absolute runtime guarantee. Custom model code, tokenizers, quantization kernels, model repository contents, and vLLM itself may still have runtime compatibility issues. Installation therefore includes three real acceptance gates: architecture verification in the pinned image, full model loading, and capability-aware API smoke tests. Failure at any gate stops the installation and disables autostart.

Tool calling, reasoning, and OCR cannot be guaranteed merely because a model name suggests those capabilities. The scripts enable parameters only for recognized parser protocols. An unknown model may still be deployed as a regular text or image model, but LLMCtl does not claim capabilities it cannot verify.

## Files

| File | Purpose |
|---|---|
| `install-llm-cluster.sh` | Initial installation or model/topology reselection |
| `llmctl.sh` / `lib/llmctl/` | Thin global `/usr/local/sbin/llmctl` entrypoint plus command-domain modules |
| `lib/model_catalog.py` | Hub search, capability detection, VRAM estimation, and deployment planning |
| `lib/runtime_optimizer.py` | Streaming benchmarks, GPU/vLLM metrics, conservative candidates, and objective scoring |
| `lib/gateway_config.py` | Secret-free configuration for all four gateways and New API/OmniRoute reconciliation |
| `lib/account_portal.py` / `lib/account_portal_*.py` | OmniRoute portal composition entrypoint plus database, HTTP, gateway, monitoring, and policy modules |
| `lib/llm_benchmark.py` | Backend concurrent load generator and streaming performance metrics for the administration console |
| `workflowd/` / `lib/workflowd/` | Optional Go workflow source plus macOS-cross-compiled Linux amd64/arm64 static runtimes |
| `lib/workflow_config.py` | Explicit remote pools, model routes, and adapter configuration helper |
| `lib/model_deployment.py` | Multi-model registry, GPU/Worker ownership, background deployment, and job-level rollback controller |
| `lib/model_upgrade.py` | Ornith upgrade target, immutable revision, target-topology, and old-version retention rules |
| `portal-ui/` | Vue 3 company-portal source and frontend tests |
| `lib/account_portal_ui/` | Built portal assets copied directly by the installer |
| `tests/test_model_catalog.py` | Model catalog and hardware planning unit tests |
| `tests/test_runtime_optimizer.py` | Tuning advice, scoring, metrics parsing, and streaming-latency tests |
| `README.md` / `README_EN.md` | Chinese and English project overview |
| `USAGE.md` / `USAGE_EN.md` | Chinese and English operations, API, and troubleshooting manual |
| `WORKFLOW.md` / `WORKFLOW_EN.md` | Chinese and English workflow, remote-Worker, and upgrade-compatibility manuals |
| `MULTI_MODEL.md` / `MULTI_MODEL_EN.md` | Chinese and English multi-model installation, GPU partitioning, remote-instance, and rollback manuals |

## Defaults

| Setting | Default |
|---|---|
| Model directory | `/data/llm-cluster/models` |
| vLLM image | `vllm/vllm-openai:v0.22.1` |
| Gateway | New API (recommended) |
| New API image | `calciumion/new-api:v1.0.0-rc.22` |
| LiteLLM image | `ghcr.io/berriai/litellm:v1.94.0` |
| Bifrost image | `maximhq/bifrost:v1.6.7` |
| OmniRoute image | `diegosouzapw/omniroute:3.8.49` |
| PostgreSQL | `postgres:16-alpine` |
| Unified API | `http://SERVER_IP:8000/v1` |
| Unified Web UI | `http://SERVER_IP:8000/ui/` |
| OmniRoute native UI | `http://SERVER_IP:8000/base_ui/` |
| Internal listeners | Gateway `127.0.0.1:18000`; OmniRoute portal `127.0.0.1:8001` |
| Administrator username | `admin` |
| Initial password | `llm-admin` by default; OmniRoute generates a strong random value when omitted |
| Routing | Equal-weight healthy workers with failover; LiteLLM uses `least-busy` |
| GPU memory utilization | `0.92` |
| Worker keep-warm | Enabled by default for new installs; one direct 1-token inference per active Worker every 300 seconds |

The initial shared password for New API, LiteLLM, and Bifrost is intentionally simple as requested. Change it immediately. OmniRoute generates a strong random password by default, which should still be managed and rotated carefully:

```bash
sudo llmctl admin set-password
```

### Worker startup warm-up and periodic keep-warm

Normal idleness does not make vLLM unload model weights already resident in VRAM; memory is released only by an explicitly enabled mechanism such as vLLM sleep mode. The first real request can still be slower after process startup because of CUDA, compilation, and graph warm-up, and after a long idle period because the GPU can enter a lower power state.

After Worker health checks pass, LLMCtl concurrently performs a startup warm-up and can periodically send a 1-token Chat Completions request directly to every active Worker. This path bypasses the AI gateway, user quotas, billing, and user audit records. Fresh installations run it every 300 seconds by default. In-place upgrades from an older version leave it disabled until explicitly enabled, avoiding an unapproved increase in idle power use:

```bash
sudo llmctl keepwarm enable 300
sudo llmctl keepwarm status
```

The interval can be changed online, or keep-warm can be disabled, without restarting model processes:

```bash
sudo llmctl keepwarm interval 600
sudo llmctl keepwarm run all
sudo llmctl keepwarm disable
```

Results record success, time to first byte, and total duration for every Worker in the root-only `/var/lib/llm-cluster/keepwarm/last-run.json`. Shorter intervals generally make the first request after idleness more consistent, at the cost of higher standby power.

### Gateway Selection

The wizard offers four choices before image download. For unattended installs, use `--gateway`:

| Gateway | Best fit | Fully automated configuration |
|---|---|---|
| New API (default) | Friendly Chinese administration, channels, keys, and usage | Initializes the administrator, creates one equal-weight channel per healthy worker, and creates a root-only API token |
| LiteLLM | Broad provider compatibility and established proxy configuration | Generates the model list, `least-busy` routing, master key, and PostgreSQL settings |
| Bifrost | Efficient forwarding, observability, and virtual-key governance | Generates eight vLLM keys, equal-weight routing, a virtual key, admin authentication, and PostgreSQL log storage |
| OmniRoute | Local SQLite gateway plus a company account portal | Creates eight provider nodes and one equal-weight Combo; deploys a separate portal database, email registration, personal keys, prepaid billing, usage, and a model catalog |

All four use `llm-router.service`, but the actual gateway listens only on `127.0.0.1:18000`. Nginx publishes the consistent OpenAI-compatible `/v1/` and `/ui/` entry points on port `8000`; the root-only maintenance key is stored in `GATEWAY_API_KEY`. New API, LiteLLM, and Bifrost use PostgreSQL through `llm-database.service`. OmniRoute uses its own SQLite database, does not start PostgreSQL, and adds `llm-account.service` on loopback port `8001`. There is no online migration between gateway types: select one during a clean install after old service configuration has been removed. Existing model files and exact local Docker images are verified and reused. Review the upstream licenses for your distribution and modification model.

### Publishing over HTTPS

Your existing edge Nginx, load balancer, or firewall remains responsible for the public domain, certificate, and port mapping. In OmniRoute mode, the portal administrator can configure these fields under **Publishing, registration & SMTP**:

- **Portal brand name** replaces `LLMCtl` in the header, sign-in hero, and browser title. The default remains `LLMCtl`.
- **Published base URL**, for example `https://llm.zjguardian.com`, is metadata used only to generate verification-mail, portal, API, curl-demo, and `llmctl key show` links. It does not make LLMCtl bind the domain, listen on 80/443, obtain certificates, or configure TLS. Leaving it blank preserves the installed fallback address.

Expose only the Nginx front door. Never publish `8001`, `18000`, or `8100-8107`. Control-plane upgrades preserve the existing Nginx installation and every other site. After a successful upgrade, only an `/etc/nginx/conf.d/llm-cluster.conf` positively identified as LLMCtl-generated is transactionally refreshed with a graceful reload. LLMCtl never generates domains, ports 80/443, certificates, or TLS. You can also reapply and validate it manually:

```bash
sudo llmctl nginx apply
sudo llmctl nginx test
sudo llmctl info --redact
```

`/base_ui/` is the native gateway administration/troubleshooting surface, not an end-user page. Restrict it at the edge with an ACL, VPN, or management network. See [USAGE_EN.md](USAGE_EN.md) for the full checklist.

## Quick Start

Copy the entire directory to the server, enter it, and run:

```bash
chmod +x install-llm-cluster.sh llmctl.sh lib/model_catalog.py lib/runtime_optimizer.py lib/gateway_config.py lib/account_portal.py lib/llm_benchmark.py
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

The installation/maintenance proxy is strictly separate from the inference runtime proxy and is used only for installation, model downloads, or explicit maintenance commands. Before installation finishes, the script:

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

When web search or external media adapters require international access, enable the separate runtime proxy. It is injected only into the selected Router (including OmniRoute) and the optional Go Workflow, bypasses loopback and RFC1918 networks by default, and is never injected into vLLM Workers. Setting or clearing it briefly restarts only a running Router/Workflow—not Docker or GPU Workers:

```bash
sudo llmctl runtime-proxy set 192.168.9.104 1082 http
sudo llmctl runtime-proxy test
sudo llmctl runtime-proxy show
sudo llmctl runtime-proxy clear
```

Private or gated Hugging Face models require `HF_TOKEN` when the installer runs, and you must accept the model license first. Private ModelScope models use `MODELSCOPE_API_TOKEN`. Tokens are not written to the cluster configuration.

## Upgrade the LLMCtl Control Plane

`llmctl upgrade` upgrades only LLMCtl's own programs: the manager, model-catalog/runtime-optimization/gateway helpers, and the account-portal backend and built Vue assets. It does not rerun the installer or modify/restart model workers, Docker, model weights, worker configuration, or secrets. If a release needs a compatibility data migration (for example, promoting a public model ID to a native OmniRoute Combo so both `/v1/chat/completions` and `/v1/responses` work), the portal first uses SQLite's online backup API to snapshot the portal database, the OmniRoute database, and the legacy route records; migration is refused if that snapshot fails. If LLMCtl's generated Nginx front-door file exists, it is transactionally refreshed after acceptance and Nginx is gracefully reloaded; all other sites remain untouched.

After an upgrade, run `llmctl responses status` to verify that every published public model ID has a native Responses route. If it is not ready, run `llmctl responses repair`. The repair first creates consistent portal and OmniRoute SQLite snapshots, stops only the account portal briefly, creates the same-name native Combo, and resynchronizes user permissions. OmniRoute, Nginx, Docker, GPU workers, and the inference API remain online. The optional Go workflow runtime is bundled with the control-plane archive and is not an apt package; if an older updater omitted it, `llmctl upgrade --force` installs the complete runtime.

```bash
sudo llmctl upgrade
```

The command first asks whether to fetch the latest `chatop2020/LLMCtl` `main` from GitHub, then pins the download to one exact commit. Its preflight checks both the GitHub API and the archive download host. Even after preflight succeeds, a real metadata or ZIP transfer failure triggers the saved maintenance proxy and then an interactive new-proxy prompt before retrying. The proxy is limited to this maintenance operation and may optionally be saved; it is never injected into inference services. The entire old control plane is backed up before replacement. The account portal is stopped briefly for acceptance, and the model deployment controller is restarted to load new capabilities. A failed acceptance automatically restores both the control plane and any runtime-data snapshot created by this upgrade. A normal upgrade does not restart the router or GPU workers.

The upgrade output prints the exact backup directory. To roll back manually, run:

```bash
sudo llmctl rollback /var/backups/llmctl/control-plane-YYYYMMDDTHHMMSSZ
```

Rollback asks for confirmation and creates a `pre-rollback-*` safety copy of the current portal/gateway databases first. The router is stopped briefly only when an OmniRoute data snapshot must be restored; GPU workers and models remain untouched. Because rollback reverts portal and gateway configuration changes made after the snapshot, use the exact directory printed by the corresponding upgrade.

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

## Safe OmniRoute Upgrades and SQLite Maintenance

OmniRoute itself uses `gateway/storage.sqlite`. LLMCtl exposes one shared state machine through its existing root-only control service for CLI and WebUI assessment, online backup, online maintenance, maintenance-window compaction, image upgrades, and rollback. The account portal process cannot read the database directly or invoke Docker/systemd.

```bash
sudo llmctl omniroute status
sudo llmctl omniroute sqlite assess
sudo llmctl omniroute sqlite assess --deep
sudo llmctl omniroute backup
sudo llmctl omniroute sqlite maintain online
sudo llmctl omniroute sqlite maintain compact
sudo llmctl omniroute update diegosouzapw/omniroute:3.8.49
sudo llmctl omniroute backups
sudo llmctl omniroute rollback <backup-id>
```

Assessment covers quick/integrity checks, foreign keys, WAL, free-page ratio, disk headroom, backup age, configured image, and the image actually running. Every write operation first creates a consistent snapshot with SQLite's online backup API and records SHA256, size, quick_check, source image, and file ownership/mode. Online maintenance runs only `PRAGMA optimize` and a `PASSIVE checkpoint` without stopping the Router. `compact` briefly stops the Router in a maintenance window for WAL truncation, `VACUUM`, and integrity verification. Upgrades accept only a fixed version or digest; a failed new image, route reconciliation, or full model smoke test restores both the previous image and pre-upgrade database. Manual rollback also snapshots the current state first. GPU Workers are never restarted by these operations.

The WebUI exposes the same workflow, live phases, and backup inventory under “OmniRoute Maintenance”. Risky operations require the exact confirmation phrase shown on screen. Managed snapshots are stored under `/var/backups/llmctl/omniroute/` and are not deleted automatically; include them in disk-capacity and off-host backup planning.
The legacy `llmctl update --omniroute-image ...` entry point delegates to this safe upgrade workflow and cannot bypass the SQLite backup and rollback contract.

## Optional MySQL Portal Database

The LLMCtl account portal continues to use SQLite by default; small and medium deployments do not need to migrate merely because MySQL support is available. MySQL replaces only the portal users, permissions, billing, usage, and audit data stored in `/var/lib/llm-cluster/omniroute/portal/account-portal.db`. It does not replace OmniRoute's own `storage.sqlite` and is never inserted into the `/v1` inference data path.

First run `sudo llmctl upgrade` as described above. The only command-line step for MySQL is then to activate the portal driver:

```bash
sudo llmctl database enable-mysql
```

This command installs and enables the pinned PyMySQL driver and the runtime required by MySQL 8's default authentication only for `llm-account.service`, then briefly restarts the account portal. It does not install MySQL Server, create a database, migrate data, or restart OmniRoute, Nginx, Docker, or GPU workers. The administrator supplies MySQL 8.0 or later, an empty database, a dedicated user, and its privileges.

After activation, sign in to the `/ui/` administration console and open **Database**. Enter the host, port, database, username, password, and TLS options, save them, and run the connection test. The connection test validates the MySQL version, connectivity, and an empty target database. Table-creation, write, index, and foreign-key privileges are exercised by the real migration transaction; any failure keeps SQLite active and removes tables created by that migration attempt. After the test passes, enter the confirmation phrase shown by the page to start SQLite-to-MySQL migration. Backup, schema creation, batched copying, per-table row-count/digest validation, final cutover, and progress reporting are all performed through the Web UI. Portal administration enters maintenance mode during migration, while `/v1`, OmniRoute, and GPU workers remain online.

The original SQLite file and a pre-migration backup remain after a successful cutover. The Web UI provides an explicit emergency rollback to SQLite. It does not merge writes made to MySQL after cutover back into the old SQLite file, so use rollback only when immediate post-migration acceptance fails. Database passwords are never returned to the browser; leaving the password field blank preserves the stored value.

## Operations Metrics and Bulk Call Policies

In OmniRoute mode, the administrator home page aggregates the usage ledger from LLMCtl's separate portal database (SQLite by default, optionally MySQL) into Today (hourly), 7-day, 30-day, and 12-month views. It reports requests, active users, input/output/cached/reasoning tokens, cash charges, the top ten users by token usage, a server-paginated recent-active-user list, and per-user trends. Total tokens always means input plus output; cached tokens are a subset of input and reasoning tokens are a subset of output, so neither is counted twice. Every view supports model filtering and states its ledger source, timezone, and typical two-second settlement lag. Internal stress traffic or calls that cannot be mapped to a portal user are not presented as user usage.

The administrator navigation also has a dedicated **Usage reports** page for full-staff reporting rather than another Top 10 view. It supports daily, monthly, yearly, and custom date ranges, plus model, user-keyword, and account-status filters. Summary cards cover total staff, active/zero-usage staff, requests, input/output/total tokens, and cash charges; the page also includes time trends, model summaries, and a full-staff detail table that retains members with zero usage. Staff rows are paginated in the database instead of being loaded into the browser all at once. The current filtered population can be exported as a real `.xlsx` workbook with Summary, User usage, Time trend, and Model usage sheets; export includes every matching member regardless of the currently visible page.

User management can bulk-update the API-key active-session limit, requests per minute (RPM), and requests per day for either the current filtered result or explicitly selected users. The browser submits an explicit list of user IDs. The backend validates the entire scope, briefly disables the target keys, commits the changes atomically to the active portal database, and then synchronizes each key to the selected AI gateway. A failed synchronization remains fail-closed. Bulk policy updates do not change cash balances, account status, groups, or model permissions, and every operation is written to the audit log.

The **System Monitor** administrator page reads Linux `/proc`, mount statistics, and fixed-argument `nvidia-smi` queries directly. It never executes a browser-supplied command and does not send monitoring traffic through OmniRoute or GPU workers. It shows up to 60 in-page CPU/memory/GPU samples, GPU driver and PCI details, per-interface network rates, filesystem capacity, and a searchable, sortable, paginated view of the top 200 processes. Samples are intentionally not persisted to LLMCtl's database; this is an immediate operational view, not a replacement for long-retention monitoring such as Prometheus.

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
| `/var/lib/llm-cluster/omniroute-maintenance` | OmniRoute assessment results and background task state |
| `/var/backups/llmctl/omniroute` | Managed recovery snapshots with SHA256, integrity results, and image metadata |
| `/var/lib/llm-cluster/omniroute/portal/account-portal.db` | Default portal SQLite database; retained as the pre-migration rollback copy after MySQL cutover |
| `/data/llm-cluster/models` | Default model root |
| `llm-cluster.service` | Top-level oneshot service |
| `llm-worker@N.service` | vLLM worker |
| `llm-router.service` | Selected New API, LiteLLM, Bifrost, or OmniRoute API and UI |
| `llm-database.service` | PostgreSQL for non-OmniRoute gateways |
| `llm-account.service` | Company account portal in OmniRoute mode only |
| `nginx.service` | Public `/v1/`, `/ui/`, and optional `/base_ui/` front door |

## Portal Model Metadata and Ledgers

- The model editor reads context windows, maximum output limits, and detectable capabilities from the active AI gateway. For a multi-target routing combo, the portal shows the conservative usable value and lists every resolved target.
- LLMCtl hard-limits each generation to `32768` output tokens by default. The vLLM worker enforces this limit, so omitting `max_tokens` or requesting a larger value cannot consume the full 256K context window. Administrators may still choose a smaller public-model output limit in the portal.
- When an administrator changes the context or output limit, LLMCtl writes it through the gateway's native API for every resolvable target. Partial failures remain visible, name the failed targets, and are audited; they are never presented as a successful sync.
- Model descriptions, OCR labels, and access scopes are LLMCtl publication metadata. They appear in the administration list and user catalog but are not misrepresented as gateway-native parameters.
- Administrators can reveal, hide, or copy one ordinary user's current API key on demand from User Management. Plaintext is excluded from administration snapshots, the portal database, logs, audit details, and browser persistence; leaving the page or signing out immediately clears it from page memory.
- Administrators can resend verification mail for an unverified registration; the old link is invalidated only after the new message is delivered. If mailbox ownership was confirmed through another trusted channel, an administrator may type the exact full email and approve the registration manually. The server still enforces the registration-domain policy, creates the API key, applies the configured welcome balance, synchronizes model permissions, expires old verification links, and writes an audit event; any failed step revokes the partial provisioning. Administrators may also delete an unverified placeholder that has no API key, billing, grant, or usage history. Provisioned users can only be disabled through this flow.
- LLMCtl enables OmniRoute detailed request logging so new request inputs and final model outputs can be viewed under the existing permission boundary. Previously unretained history cannot be reconstructed. Router defaults retain summaries for 30 days, reserve 4 MiB per request artifact, and return up to 1,000,000 characters each for prompts and final responses through the portal.
- The audit page translates internal action identifiers into administrator-facing operations and expands the complete retained detail. Exceptional details beyond 1,000,000 characters are explicitly marked as truncated rather than silently shortened.
- Billing separates request usage from monetary balance transactions. Every request debits the cash balance from the model's input, output, cache-read, and reasoning-token prices and stores an immutable price snapshot. On upgrade to 3.0, every unspent legacy token grant is converted once at the highest current token-class price for its model; a unique transaction and audit record make the migration idempotent. Paid-model access stays disabled when balance reaches zero or below. Users can inspect only their own request input; administrators can additionally inspect the final model output retained by the gateway, with an explicit notice when response retention is unavailable.
- `llmctl upgrade` upgrades only the LLMCtl control plane, portal assets, and maintenance scripts, applying in-place database migrations. It does not rebuild or replace existing workers or model weights.
- When enabling detailed request logging for the first time, run `sudo llmctl router restart` so the Router loads the artifact-size and retention environment settings. GPU workers are not restarted.

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
