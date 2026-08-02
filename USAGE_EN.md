# llmctl User Guide

**Language:** [中文](USAGE.md) | English

## First Commands After Installation

```bash
sudo llmctl status
sudo llmctl health
sudo llmctl info --redact
sudo llmctl smoke --full
sudo llmctl key show
sudo llmctl admin show
sudo llmctl admin set-password
```

Change the server timezone from UTC to China Standard Time:

```bash
sudo llmctl timezone set Asia/Shanghai
```

Nginx provides one public front door: `http://SERVER_IP:8000/v1/` for the API and `http://SERVER_IP:8000/ui/` for the daily web interface. In OmniRoute mode, `/ui/` is the company portal and the native troubleshooting UI remains at `/base_ui/`; for other gateways `/ui/` is their native interface. The actual gateway listens only on `127.0.0.1:18000`, and the company portal listens only on `127.0.0.1:8001`, so users do not need a second public port.

The administrator username defaults to `admin`. OmniRoute generates a strong random password when none is provided; other gateways initially use `llm-admin`. Credentials are stored in root-only `/etc/llm-cluster/secrets.env`. `sudo llmctl admin show` prints routine sign-in details. `sudo llmctl info` is the disaster-recovery inventory and, by default in a root terminal, prints all plaintext passwords, API keys, database and SMTP credentials, public/internal endpoints, model/runtime state, services, files, and SQLite checks. Always use `sudo llmctl info --redact` before copying output to a ticket.

Choose the gateway during a clean install. New API is the default, or select one explicitly:

```bash
sudo bash install-llm-cluster.sh --gateway newapi
sudo bash install-llm-cluster.sh --gateway litellm
sudo bash install-llm-cluster.sh --gateway bifrost
sudo bash install-llm-cluster.sh --gateway omniroute
```

The installer pulls only the selected gateway. An exact local image is reused without a pull. Model download is also skipped when identity, revision, architecture, completeness, and size all match. All four maintain the root-only `GATEWAY_API_KEY`, and there is no online migration between them.

## SSH Disconnected or the Installation Window Appears Stuck

After systemd takes control, disconnecting the SSH session does not terminate workers. Log in again and run:

```bash
systemctl status llm-cluster.service --no-pager -l
sudo llmctl startup watch
sudo llmctl status
sudo llmctl health
journalctl -u llm-cluster.service -f
```

`startup watch` reports `healthy/loading/pending/failed`, each worker's state, and memory use on every GPU once every 10 seconds. While a worker loads a large model, `GPU-Util` may be close to 0% even though GPU memory continues to grow. To inspect an individual worker:

```bash
sudo llmctl logs worker 0 -f
nvidia-smi
```

`sudo llmctl logs` aggregates the router, database, and all active workers by default. Use `llmctl logs router`, `llmctl logs database`, or `llmctl logs worker 0` to narrow the view. When a capability smoke test fails, the response summary reports `finish_reason` and field lengths; the complete fixed-test response is stored root-only under `/var/lib/llm-cluster/diagnostics/smoke` to distinguish length truncation, protocol parsing, and an incorrect model answer.

If the top-level unit shows `active (exited)` or `Finished llm-cluster.service`, that is the expected `oneshot + RemainAfterExit` state. The actual inference processes run in `llm-worker@N.service` units.

## Worker Management

```bash
sudo llmctl start all
sudo llmctl start 0,2,4
sudo llmctl stop all
sudo llmctl stop 1,3
sudo llmctl restart all
sudo llmctl restart 0,1
sudo llmctl shutdown
```

By default, `start`, `stop`, and `restart` do not change the list used at the next boot. `shutdown` stops the router, database, and all workers concurrently, also without changing the next-boot list. For persistent management:

```bash
sudo llmctl enable 0,1,2,3
sudo llmctl disable 6,7
sudo llmctl activate 4,5
sudo llmctl deactivate 4,5
sudo llmctl scale 6
sudo llmctl autostart status
sudo llmctl autostart enable
sudo llmctl autostart disable
```

- `enable/disable`: change only the next-boot list.
- `activate/deactivate`: change the next-boot list, runtime state, and selected-gateway backends together.
- `scale N`: persistently select the first N instances.
- For `start/restart`, `all` means the persistently active list. `stop all` stops every possible instance.

Configure startup parallelism:

```bash
sudo llmctl tune set startup-parallelism 8
sudo llmctl restart all
```

Loading eight workers concurrently is faster, but it increases peak CPU, system memory, and disk-read demand. The installer derives its default from host CPU threads, available memory, model size, and replica count; watch memory and disk activity after increasing it manually. During startup, every worker loads its own model copy; weights are not shared between GPUs.

Startup requests are submitted concurrently in batches, and LLMCtl continuously reports aggregated progress for the entire batch instead of silently waiting for each worker in sequence. When `systemctl start llm-cluster.service` is invoked directly, the systemd client does not forward service logs. Open another terminal or reconnect through SSH and run `sudo llmctl startup watch`.

## Gateway and Routing

```bash
sudo llmctl router status
sudo llmctl router restart
sudo llmctl database status
```

`llmctl router restart` rediscovers healthy workers, renders the selected gateway configuration, restarts it, waits for process health, and only then verifies authenticated `/v1/models`. New API initializes the administrator, creates an equal-weight channel per healthy worker, and creates a managed token. Bifrost generates equal-weight vLLM keys, a virtual key, and PostgreSQL log storage. LiteLLM uses `least-busy` plus a per-worker concurrency limit. OmniRoute creates one provider node per worker and an equal-weight Combo, then synchronizes model metadata and the maintenance key.

The installer installs Nginx or reuses an existing installation. LLMCtl manages only `/etc/nginx/conf.d/llm-cluster.conf`, requires `nginx -t` before reload, and restores the prior content on failure. If a same-name config existed before installation it is restored on uninstall; the Nginx package, other virtual hosts, and certificate configuration are always preserved. `/v1/` disables proxy buffering and goes straight to the gateway rather than the Python portal, preserving streaming and high-throughput behavior.

None of these routers directly knows how much GPU memory, KV cache, or image processing a particular request will consume. Long-context and multi-image requests may still create transient imbalance. Run `llmctl bench` and `llmctl optimize analyze` with a realistic workload before tuning.

Key and administrator maintenance:

```bash
sudo llmctl key show
sudo llmctl key rotate
sudo llmctl admin show
sudo llmctl admin set-username NEW_ADMIN
sudo llmctl admin set-password
```

New API and OmniRoute create maintenance tokens in their databases, so `key rotate` does not accept a caller-supplied value. LiteLLM and Bifrost accept an optional value; a Bifrost key must start with `sk-bf-`. OmniRoute users rotate their personal keys in the account portal; the administrator command does not replace those keys.

### LLMCtl account portal

OmniRoute does not provide a complete company-registration and administrator-friendly billing flow. LLMCtl therefore deploys a separate Vue 3 portal and lightweight `llm-account.service`. The portal uses OmniRoute HTTP APIs for mappings, Combos, and user-key permissions, while ordinary `/v1` requests still go directly from Nginx to OmniRoute. They share a state root but never a database file:

```text
/var/lib/llm-cluster/omniroute/gateway/storage.sqlite
/var/lib/llm-cluster/omniroute/portal/account-portal.db
```

The first file is owned entirely by OmniRoute. The second stores portal users/groups, verification state, sessions, published models, access rules, price versions, balances, token grants, the usage ledger, and portal audit events. The portal never stores a plaintext user API key: a newly issued key is displayed once after email verification, and later it can only be rotated. It periodically reconciles OmniRoute call logs by unique request ID. Entitlement-changing reconciliation disables the user key first, commits the ledger, then publishes the new permission set; a failed sync remains closed.

Public registration is disabled by default. Enabling it requires an exact email-domain allowlist, the public portal origin, and external SMTP:

```bash
sudo bash install-llm-cluster.sh \
  --gateway omniroute \
  --registration enabled \
  --allowed-email-domains example.com,subsidiary.example.com \
  --account-public-url https://llm.example.com \
  --account-api-public-url https://llm-api.example.com \
  --account-admin-email llm-admin@example.com \
  --account-default-quota 1000000 \
  --account-quota-reset monthly \
  --smtp-host smtp.example.com \
  --smtp-port 587 \
  --smtp-security starttls \
  --smtp-username llm@example.com \
  --smtp-password APP_PASSWORD \
  --smtp-from llm@example.com
```

The allowlist matches the complete part after `@`; allowing `example.com` does not allow `evil-example.com` or `dept.example.com`. It is checked at both registration and verification. Administrators can configure and test SMTP online, enable/disable registration, change the allowlist and welcome grant, disable users, manage groups, adjust money balances, and issue generic or model-specific token grants. Grants support `daily`, `weekly`, and `monthly` resets at an explicit time. A background reset runs independently, so an exhausted and disabled key can become eligible again on schedule.

The model page maps a public ID such as `gdn-inside` to `ornith-1.0-35b-fp8` with OmniRoute's native Combo mapping or model alias. User keys authorize only the public ID—not the underlying model or Combo ID. Each model can have separate input, output, cache-read, and reasoning-token prices per million tokens and multiple `all`, group, or individual access rules. Token grants are consumed first; only the excess debits the money balance. Prices are versioned and snapshotted into the ledger, so later edits never reprice old calls.

The free-resource page reads OmniRoute's catalog plus configured/currently-available provider rankings. A resource can be published only after discovery, provider configuration, current availability, a real portal live test, and explicit administrator approval. Published free models are retested every 15 minutes. After three consecutive failures the portal disables keys first, withdraws the native mapping, and removes the model from effective permissions.

```bash
sudo llmctl account status
sudo llmctl account url
sudo llmctl account restart
sudo llmctl logs account -f
```

The public portal URL is `http://SERVER_IP:8000/ui/`; port `8001` is loopback-only. Users see balances, grants, per-request usage, and transactions. The catalog shows only their effective models with prices/capabilities, copyable IDs, endpoints, and curl examples. The browser playground calls public `/v1` directly; the personal key stays in browser `sessionStorage` and does not pass through the portal backend. OCR, vision, and tool labels come from verified or administrator-confirmed metadata and do not alter vLLM behavior.

If OmniRoute is temporarily unavailable, the portal's local administration pages remain accessible with a degradation warning so users, SMTP settings, ledgers, and audits can still be inspected. Operations that depend on the gateway—models, keys, permissions, and live reconciliation—fail explicitly rather than reporting false success. `llmctl startup status` reports this state as `degraded`, while full startup acceptance still requires the portal `/ready` endpoint and OmniRoute to recover together.

In production, protect only public port `8000` and terminate HTTPS in an existing Nginx/TLS site or upstream load balancer; never expose loopback ports `8001`, `18000`, or `810x`. `--account-public-url` may be the public origin or its `/ui` path, while `--account-api-public-url` is a path-free origin. SMTP and management credentials are in root-only configuration; the portal SQLite file also stores runtime SMTP settings and must be backed up and protected as a sensitive file.

## OpenAI-Compatible API

First obtain the parameters:

```bash
sudo llmctl key show
```

Text request:

```bash
curl http://SERVER_IP:8000/v1/chat/completions \
  -H 'Authorization: Bearer YOUR_KEY' \
  -H 'Accept: application/json' \
  -H 'Content-Type: application/json' \
  -d '{
    "model":"MODEL_NAME_FROM_INSTALL_SUMMARY",
    "messages":[{"role":"user","content":"Hello"}],
    "max_tokens":256,
    "stream":false
  }'
```

Synchronous JSON clients should send `"stream":false` explicitly. For legacy-client compatibility, OmniRoute may return SSE when that field is omitted and the request does not declare a JSON-only response; streaming clients should instead send `"stream":true` and parse each `data:` event.

For models that support disabling reasoning per request, add:

```json
{
  "reasoning_effort": "none",
  "chat_template_kwargs": {"enable_thinking": false}
}
```

Do not send these fields to models that do not support the reasoning switch. View the currently detected capabilities with:

```bash
sudo llmctl status
```

Tool calls follow the OpenAI `tools` and `tool_choice` format. The model only returns its intent to call a tool; the application must execute the function and return a `tool` message to the model. The scripts enable a vLLM tool parser only for recognized model protocols.

## Images and OCR

Use image input only when `llmctl status` reports image support:

```bash
sudo llmctl ocr /path/to/image.png 'Transcribe all content verbatim'
```

The default limit is eight images per request, and full acceptance includes a request containing six images:

```bash
sudo llmctl smoke --full
sudo llmctl tune set max-images 8
sudo llmctl restart all
```

The API uses OpenAI `image_url` content blocks. Base64 `data:image/...;base64,...` input is recommended by default; the service does not allow arbitrary external media domains.

## Parameter Tuning

```bash
sudo llmctl tune show
sudo llmctl tune set max-model-len 32768
sudo llmctl tune set max-num-seqs 7
sudo llmctl tune set max-num-batched-tokens 8192
sudo llmctl tune set gpu-memory-utilization 0.92
sudo llmctl tune set routing-strategy least-busy
```

Run `restart all` after changing GPU memory or worker parameters. Increasing `max-model-len`, `max-num-seqs`, or the image limit raises peak GPU memory demand. Do not fill every remaining GiB merely because memory is available while idle. CUDA Graphs, temporary tensors, image encoding, and request-length variation all need headroom.

## Performance Acceptance

```bash
sudo llmctl bench --concurrency 25 --requests 50 --max-tokens 512
```

The output includes aggregate tokens/s, p50/p95 effective tokens/s per request, and request latency. For targets such as “25 average concurrent requests at 40 tokens/s,” test short text, long prompts, reasoning, tool calls, and six-image requests separately. A 256K context is the maximum window for one request; it does not mean that 25 requests can all use 256K simultaneously.

## Automated Testing, Advice, and Optimization

Test the current configuration and generate advice without writing configuration or restarting services:

```bash
sudo llmctl optimize analyze --profile balanced
```

After consent, test candidates and apply the selected result automatically:

```bash
sudo llmctl optimize run --profile balanced
```

Available objectives:

- `latency`: prioritize p95 TTFT and ITL.
- `balanced`: the default; combine aggregate throughput, p95 TTFT, and ITL.
- `throughput`: prioritize aggregate output tokens/s while retaining tail-latency constraints.

The flow first runs capability smoke tests and a current-configuration baseline. It then collects streaming TTFT, ITL, E2E, aggregate output tokens/s, GPU utilization/VRAM/temperature, CPU/memory/swap, plus each vLLM worker's peak KV-cache use, queueing, preemptions, and prefix-cache hits. Every proposed parameter is shown with its rationale, possible cost, and the boundaries this test cannot prove. Missing critical GPU, host, or vLLM metrics—and excessive CPU, memory, or swap pressure—prevent upward-scaling candidates.

No configuration is written and no service is restarted before the user enters `y`. After consent, LLMCtl stores a complete `cluster.env` backup under `/var/lib/llm-cluster/optimization/backups` and tests at most two conservative candidates. A candidate must have no request failures, must not increase KV-cache preemptions, and normally must improve the selected objective's composite score by at least 5%. Every trial restarts active workers, so the API is briefly unavailable. Final acceptance covers text, reasoning, tool calling, and, when supported, OCR/six-image input. Startup failure, smoke-test failure, Ctrl+C, or a termination signal restores the original configuration.

Quick mode reduces requests and candidate count. It is useful for screening but produces weaker evidence:

```bash
sudo llmctl optimize analyze --profile throughput --quick
sudo llmctl optimize run --profile throughput --quick
```

Inspect the report or explicitly restore the pre-optimization configuration:

```bash
sudo llmctl optimize report
sudo llmctl optimize report --json
sudo llmctl optimize restore latest
# Or use the RUN_ID from the report
sudo llmctl optimize restore 20260801T120000Z
```

The automatic workload is reproducible synthetic text, not a representation of real prompts, long contexts, output lengths, image ratios, or tool-call distribution. It never changes the model, TP, context limit, routing semantics, quantization format, or driver automatically. Replay production-like samples before rollout. Use `--yes` only in an unattended maintenance window after reviewing candidate and downtime implications.

## Model Search and Reconfiguration

```bash
sudo llmctl models hardware
sudo llmctl models search Qwen --source all --task auto
sudo llmctl models inspect modelscope Qwen/Qwen3-8B
sudo llmctl models current
```

Immediately after language selection, before gateway or model selection, the installer directly probes `https://huggingface.co/api/models?limit=1`. If it fails, it offers proxy setup and retests after entry; declining produces an explicit warning that Hugging Face candidates may be absent. With `--yes` or `--non-interactive`, it never waits for input and requires `--proxy http://IP:PORT`. After installation, `llmctl models search --source all|huggingface` also validates direct access or a saved proxy first. ModelScope-only search, health checks, and offline inference do not prompt for international networking.

The installer then performs a read-only host preflight before model search. It covers OS/architecture, CPU/cores/threads, memory/swap, GPUs/VRAM/driver/compute capability, current and maximum PCIe links, GPU/NUMA/NVLink topology, and model-filesystem capacity. PCIe/topology information is a capability snapshot, not an active NCCL bandwidth test.

The catalog excludes platform-specific conversions such as Apple MLX weights. `mlx-community/*` targets MLX on Apple Silicon and cannot be used as NVIDIA CUDA/vLLM weights. Selecting a candidate expands its VRAM budget, TP links, host memory, disk, startup parallelism, and itemized recommendation reasons. You can then confirm, return to the candidate list, or search again.

The ModelScope downloader is pinned in an isolated virtual environment, and its actual command is `/opt/llm-cluster/hub-venv/bin/ms`. Before downloading large weights, the installer validates `ms download --help`. A `.partial` directory is the resumable download target and should not be removed after a download error.

If the selected model already exists locally, the installer skips its download and does not copy weights only when the Hub, model ID, revision, configuration architecture, complete weight set, and size all match. A retained `/data/ornith/models` root can be detected automatically. Similar model names from different sources or IDs are never mixed.

To switch to a different model, return to the project directory and rerun the installer:

```bash
sudo bash install-llm-cluster.sh --force-reconfigure
```

This rechecks weights, architecture, TP, context, and capability parsers. Direct `llmctl download` is limited to revalidating or completing the current model and cannot bypass planning to switch models.

## Container Image Maintenance, Proxies, and Offline Bundles

Upgrade the LLMCtl control plane itself without reinstalling or restarting workers:

```bash
sudo llmctl upgrade
```

The command asks whether to fetch the latest `main` commit from GitHub. If direct access fails, it tries the saved maintenance proxy and then offers a new proxy; download starts only after the proxy retest succeeds. The upgrade is limited to `llmctl` and control-plane programs declared under `/usr/local/lib/llm-cluster/`. Models, workers, gateway runtime data, configuration, secrets, databases, and Nginx are preserved. A running account portal is stopped briefly for acceptance and automatically restored from `/var/backups/llmctl/` on failure.

```bash
sudo llmctl upgrade --proxy http://192.168.9.104:1082 --save-proxy
sudo llmctl upgrade --from-zip /root/LLMCtl-main.zip
sudo llmctl upgrade --check
```

`llmctl update` and `llmctl upgrade` are intentionally different: the former explicitly updates container images, while the latter updates only LLMCtl control-plane programs. Image maintenance commands follow:

```bash
sudo llmctl proxy set 10.1.0.6 7890 http
sudo llmctl update --vllm-image vllm/vllm-openai:v0.22.1
sudo llmctl update --gateway-image calciumion/new-api:v1.0.0-rc.22
sudo llmctl proxy clear
```

The installer defaults to the published OmniRoute image `diegosouzapw/omniroute:3.8.48`. If an image tag is absent or the registry is unreachable, the installer preserves Docker's original error and prints the corresponding `--*-image` override instead of reporting only a script line number. For example:

```bash
sudo ./install-llm-cluster.sh --omniroute-image diegosouzapw/omniroute:3.8.48
```

Export or import an offline bundle:

```bash
sudo llmctl offline export /data/offline/llm-bundle
sudo llmctl offline import /data/offline/llm-bundle
```

An offline bundle records the selected gateway kind and image. Bundles cannot be mixed between New API, LiteLLM, Bifrost, and OmniRoute plans. An OmniRoute bundle does not require the PostgreSQL image.

## Logs

```bash
sudo llmctl logs worker 0 -f
sudo llmctl logs router -f
sudo llmctl logs database -f
journalctl -u llm-cluster.service -n 300 --no-pager
```

Common interpretations:

- `curl: (7) ... 810N` during worker loading means only that the health probe has not passed yet; it is not itself a failure.
- If worker GPU memory stops growing and logs remain unchanged for a long time, check whether CUDA Graph compilation is in progress, whether CPU memory or disk is saturated, and whether the service is still active.
- If a worker exits, the final log lines usually identify an out-of-memory error, architecture mismatch, quantization-kernel problem, or model-code error.
- If the gateway is unhealthy while workers are healthy, inspect the `llm-router` and `llm-database` logs. For New API reconciliation failures, the `llm-cluster.service` log also reports the exact setup, login, channel, or token error after the router log.
- If OmniRoute is healthy but the portal is not, run `llmctl account status` and `llmctl logs account`. For missing mail, check the SMTP host, TLS mode, sender, and spam policy; the portal never bypasses verification to issue a key.

## Uninstall

By default, uninstall retains the models, gateway PostgreSQL/SQLite state, and root-only recovery credentials:

```bash
sudo llmctl uninstall
```

Uninstall stops the router, database, and all workers concurrently, reporting active systemd units, containers, and GPU memory every five seconds. Graceful shutdown has a total limit of 180 seconds. After a timeout, LLMCtl performs one bounded forced-stop pass against only this cluster's `llm-*` units and containers. If they still cannot be stopped, LLMCtl exits explicitly while retaining the configuration; it never deletes files while services are still running. Deleting a compilation cache containing many small files, or a model directory selected with `--purge-model`, also emits periodic heartbeat messages instead of remaining silent for a long time.

Optional permanent deletion:

```bash
sudo llmctl uninstall --purge-model --purge-images --purge-database
```

`--purge-model` is allowed only when the model directory contains the installer's marker. `--purge-database` permanently deletes web UI data, virtual keys, New API/Bifrost state, and both independent OmniRoute SQLite files including account audit. Do not use `--purge-model` when existing weights must be retained.

Back up both `gateway/storage.sqlite` and `portal/account-portal.db` for OmniRoute. The simplest consistent method is to stop the portal and gateway briefly, copy both files, then restart them. Backing up only one can leave portal users unmatched with OmniRoute keys and limits.
