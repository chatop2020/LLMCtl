# LLMCtl Multi-Model Deployment and GPU Partitioning

**Language:** [中文](MULTI_MODEL.md) | English

Starting with LLMCtl 3.5, administrators can download, verify, and publish additional models from the Model Deployments page and can reassign GPUs owned by existing Workers. The model controller handles only configuration, downloads, acceptance, and rollback. Normal inference traffic still flows directly from Nginx to the selected AI gateway and then to vLLM Workers; it does not pass through the Python portal.

## Prerequisites

- Run `llmctl upgrade`, then confirm that `llm-model-control.service` is `active` with `llmctl model status`. On the first upgrade from a release without the multi-model controller, run `llmctl model init` once if the status reports `enabled=not-installed`. This registers the service and migrates existing configuration; it installs no additional package and does not restart the Router or Workers.
- Automatic publication of multiple public model IDs currently requires OmniRoute. New API, LiteLLM, and Bifrost may keep isolated resources that do not replace existing Workers, but LLMCtl will not claim that an unsupported gateway has been synchronized.
- The default local model root is `/data/llm-cluster/models`. A selected local directory containing complete weights is verified and reused without downloading it again.
- Every local instance must own unique Worker IDs, ports, and GPUs. A GPU cannot be assigned to two vLLM instances.
- Only the Workers listed as affected in the plan may restart. If download, architecture verification, or health acceptance fails, the controller restores the registry, Worker configuration, and gateway snapshot.

## Eight-GPU Split Example

Target topology:

| Public model ID | Model | GPUs | Workers |
|---|---|---|---|
| `gdn-inside-ornith` | Existing Ornith | 0–3 | 0–3 |
| `gdn-inside-qwen` | New Qwen | 4–7 | 4–7 |
| `gdn-inside` | Compatibility alias | Points to `gdn-inside-ornith` | No extra instance |

Use two operations so all eight existing instances are not interrupted together:

1. Create the Qwen deployment with GPUs 4–7 and Workers 4–7. Set its public ID to `gdn-inside-qwen`.
2. Review the plan. It must state that Qwen takes Workers 4–7 while the existing Ornith deployment temporarily retains Workers 0–3.
3. Submit and wait for download, load, health acceptance, and gateway synchronization.
4. From Current Deployments, choose Split and Rename for the legacy Ornith deployment. Retain GPUs 0–3 and Workers 0–3 and change its public ID to `gdn-inside-ornith`.
5. Preserve the legacy compatibility ID so current clients can keep using `gdn-inside` while new clients can explicitly choose either model.
6. Review the second plan. Only Workers 0–3 should restart. After completion, test all three public IDs.

For Qwen TP4, configure GPUs `4,5,6,7` as one instance. For four TP1 instances, configure four separate Workers. Instance count and `tensor_parallel_size` are different settings.

## Web Workflow

1. Select Hugging Face, ModelScope, or a local directory.
2. Enter the model ID, revision, public ID, and display name.
3. Configure image, TP, context length, GPU-memory utilization, maximum sequences, batched tokens, and image/OCR/tool/reasoning capabilities.
4. Add local or remote instances. A remote instance uses an explicit OpenAI-compatible `/v1` endpoint and does not depend on local Docker discovery.
5. Generate a plan. It checks GPUs, Workers, ports, model paths, public-ID conflicts, gateway capability, and disk capacity.
6. Confirm the impact and submit the background job. Jobs persist under `/var/lib/llm-cluster/model-control` and continue after leaving the page.
7. Use rollback from the job details on failure. Rollback creates its own safety snapshot first.

## Upgrade Ornith 1.0 to 1.5

Do not overwrite the 1.0 weight directory or execute a mutable Hub `main` revision. The **Ornith version upgrade** panel at the top of the Model Deployments page follows this flow:

1. Select the active Ornith deployment. The guided upgrade owns local Workers only; remote instances must be upgraded by their own control plane.
2. If Hugging Face is not directly reachable, enter a proxy under **Download environment and maintenance proxy**, select Hugging Face, and choose **Test and save**. The proxy is limited to model catalog, dependency, and weight downloads and is never injected into the Router or Workers. A pinned ModelScope downloader is prepared automatically when missing.
3. Select a target from the official native-GPU Ornith weights grouped by ModelScope and Hugging Face. The page follows the current deployment Hub by default and prefers the size-compatible `ornith-ai/Ornith-1.5-35B-A3B-FP8`. The revision may be blank during planning; LLMCtl resolves and displays the full immutable SHA.
4. Start conservatively with a `32768` context. The controller re-plans TP and replica count from the real GPUs, memory, and target weights instead of copying the 1.0 topology.
5. Review the pinned SHA, affected Workers, target TP, retained old-weight path, and rollback behavior. Planning does not download weights or stop services.
6. Confirm during a maintenance window. The page immediately replaces the confirmation plan with task phase, progress, message, and logs. After the new Workers become healthy, the controller runs one real text generation against every instance and only then switches the public route.
7. Download, load, generation, or route-publication failures restore the pre-upgrade snapshot automatically. After success, **Roll back before upgrade** reloads the retained 1.0 weights.

The CLI uses the same backend contract and does not require hand-written deployment JSON:

```bash
sudo llmctl model upgrade plan legacy --hub modelscope --model ornith-ai/Ornith-1.5-35B-A3B-FP8 --max-model-len 32768
sudo llmctl model upgrade apply legacy --hub modelscope --model ornith-ai/Ornith-1.5-35B-A3B-FP8 --max-model-len 32768
sudo llmctl model job <upgrade-job-id>
sudo llmctl model upgrade rollback <upgrade-job-id>
```

`apply` checks the registry revision again. If another administrator changed deployments after planning, submission is rejected and must be re-planned. `--yes` skips only the terminal prompt; it never bypasses immutable-revision, catalog, GPU, real-generation, or rollback gates.

## Command-Line Recovery Path

```bash
sudo llmctl model init
sudo llmctl model status
sudo llmctl model plan /root/deployment.json
sudo llmctl model deploy /root/deployment.json
sudo llmctl model job <job-id>
sudo llmctl model cancel <job-id>
sudo llmctl model rollback <job-id>
sudo llmctl logs model
```

`plan` validates without changing runtime state. After `deploy` returns a job ID, systemd continues downloads and restarts even if the SSH session disconnects.

## Remote Workers

The control plane may run separately from GPU hosts. A remote instance needs:

- a stable private-network `base_url`;
- the actual remote model ID;
- an optional API-key environment-variable reference;
- health-check and request timeouts.

Remote instances do not write local systemd Worker configuration or consume local GPU and Worker IDs. LLMCtl validates remote `/v1/models` and inference before publication, while the gateway remains responsible for production traffic scheduling.

## Compatibility and Rollback

- For legacy installations containing only `/etc/llm-cluster/cluster.env` and `llm-worker@N`, the controller synthesizes a registry without restarting the Router or Workers.
- When the first cross-version upgrade is performed by a legacy updater, it can copy the new control-plane files but cannot register a systemd unit unknown to that old updater. Run `llmctl model init` once in this state. Later upgrades maintain the registered unit automatically.
- `llmctl upgrade` updates the control plane and portal only. Existing models, Workers, and GPUs are not changed until an administrator submits a model deployment.
- Every job snapshots the registry, per-Worker environment files, and gateway database/configuration. Rollback restarts only affected Workers.
- Reusable model weights are not deleted by a control-plane rollback.
- Before production changes, save a redacted inventory with `llmctl info --redact` and retain `/var/backups/llmctl` and job backup directories.

## Security Boundaries

- Model paths must remain under an LLMCtl model root; deployment requests cannot read arbitrary host paths.
- Only root and the portal service in the `llm-account` group can access the model-control Unix socket.
- The portal administration API does not carry inference traffic, accept arbitrary shell commands, or execute browser-supplied programs.
- `trust_remote_code` is disabled by default. Pin and audit a revision and validate it on a test host before enabling it.
- Only Nginx should be exposed publicly. Keep the model-control socket, vLLM Workers, portal backend, and gateway-internal listeners local or on a controlled private network.
