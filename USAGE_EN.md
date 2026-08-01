# llmctl User Guide

**Language:** [中文](USAGE.md) | English

## First Commands After Installation

```bash
sudo llmctl status
sudo llmctl health
sudo llmctl smoke --full
sudo llmctl key show
sudo llmctl admin show
sudo llmctl admin set-password
```

Change the server timezone from UTC to China Standard Time:

```bash
sudo llmctl timezone set Asia/Shanghai
```

The default web administration URL is `http://SERVER_IP:8000/ui`. The administrator username is `admin`, and the initial shared password is `llm-admin`. The password is stored in the root-only `/etc/llm-cluster/secrets.env` file and can also be displayed with `sudo llmctl admin show`. Change it after the first login.

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
- `activate/deactivate`: change the next-boot list, runtime state, and LiteLLM backends together.
- `scale N`: persistently select the first N instances.
- For `start/restart`, `all` means the persistently active list. `stop all` stops every possible instance.

Configure startup parallelism:

```bash
sudo llmctl tune set startup-parallelism 8
sudo llmctl restart all
```

Loading eight workers concurrently is faster, but it increases peak CPU, system memory, and disk-read demand. During startup, every worker loads its own model copy; weights are not shared between GPUs.

Startup requests are submitted concurrently in batches, and LLMCtl continuously reports aggregated progress for the entire batch instead of silently waiting for each worker in sequence. When `systemctl start llm-cluster.service` is invoked directly, the systemd client does not forward service logs. Open another terminal or reconnect through SSH and run `sudo llmctl startup watch`.

## LiteLLM Routing

```bash
sudo llmctl router status
sudo llmctl router restart
sudo llmctl database status
```

The default `least-busy` strategy selects a worker according to the number of unfinished requests on each backend and enforces `max_parallel_requests=max-num-seqs`. It does not directly inspect GPU memory, KV cache usage, or image size, so it is not a perfect workload-aware scheduler. Large requests may still produce imbalance. Observe the actual workload with benchmarks before considering request classification, separate model aliases, or a dedicated long-context pool.

## OpenAI-Compatible API

First obtain the parameters:

```bash
sudo llmctl key show
```

Text request:

```bash
curl http://SERVER_IP:8000/v1/chat/completions \
  -H 'Authorization: Bearer YOUR_KEY' \
  -H 'Content-Type: application/json' \
  -d '{
    "model":"MODEL_NAME_FROM_INSTALL_SUMMARY",
    "messages":[{"role":"user","content":"Hello"}],
    "max_tokens":256
  }'
```

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

## Model Search and Reconfiguration

```bash
sudo llmctl models hardware
sudo llmctl models search Qwen --source all --task auto
sudo llmctl models inspect modelscope Qwen/Qwen3-8B
sudo llmctl models current
```

To switch to a different model, return to the project directory and rerun the installer:

```bash
sudo bash install-llm-cluster.sh --force-reconfigure
```

This rechecks weights, architecture, TP, context, and capability parsers. Direct `llmctl download` is limited to revalidating or completing the current model and cannot bypass planning to switch models.

## Container Image Maintenance, Proxies, and Offline Bundles

The scripts do not update automatically. Run maintenance explicitly:

```bash
sudo llmctl proxy set 10.1.0.6 7890 http
sudo llmctl update --vllm-image vllm/vllm-openai:v0.22.1
sudo llmctl proxy clear
```

Export or import an offline bundle:

```bash
sudo llmctl offline export /data/offline/llm-bundle
sudo llmctl offline import /data/offline/llm-bundle
```

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
- If LiteLLM is unhealthy while workers are healthy, inspect the `llm-router` and `llm-database` logs.

## Uninstall

By default, uninstall retains the models and LiteLLM database:

```bash
sudo llmctl uninstall
```

Uninstall stops the router, database, and all workers concurrently, reporting active systemd units, containers, and GPU memory every five seconds. Graceful shutdown has a total limit of 180 seconds. After a timeout, LLMCtl performs one bounded forced-stop pass against only this cluster's `llm-*` units and containers. If they still cannot be stopped, LLMCtl exits explicitly while retaining the configuration; it never deletes files while services are still running. Deleting a compilation cache containing many small files, or a model directory selected with `--purge-model`, also emits periodic heartbeat messages instead of remaining silent for a long time.

Optional permanent deletion:

```bash
sudo llmctl uninstall --purge-model --purge-images --purge-database
```

`--purge-model` is allowed only when the model directory contains the installer's marker. `--purge-database` permanently deletes web UI data, virtual keys, and administration records.
