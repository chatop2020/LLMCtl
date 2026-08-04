# LLMCtl Pluggable Workflows and Remote Resource Pools

**Language:** [中文](WORKFLOW.md) | English

LLMCtl 3.3 adds an optional Go workflow data plane for composing web search, text-to-image, image editing, multi-reference generation, audio, video, and future capabilities behind a public model. It is not a Python portal proxy and does not take over existing model traffic. Only a workflow model that an administrator explicitly configures, validates, and publishes uses this process.

## Data path and performance boundary

The default path remains unchanged:

```text
Client -> Nginx /v1 -> current AI gateway -> existing vLLM Workers
```

An explicitly published workflow model uses:

```text
Client -> Nginx /v1 -> current AI gateway
       -> llm-workflowd (Go)
       -> text pool / search adapter / image adapter / other adapters
```

- The Vue/Python portal manages configuration only; it never carries `/v1` inference traffic.
- The Go data plane uses persistent connections, connection pooling, and power-of-two-choices least-inflight scheduling. Transparent text routes forward real SSE chunks without buffering the entire response.
- Agent routes buffer internal planning, tool, and summarization rounds before returning the final result. Input, output, cache, and reasoning tokens from every internal model round are merged into the final `usage`, allowing the existing gateway to bill the whole workflow.
- Image and other large artifacts should be placed in object storage by the adapter and returned as signed HTTPS URLs rather than large Base64 response bodies.

## Configuration model

The explicit `version: 1` configuration is stored at:

```text
/var/lib/llm-cluster/workflow/workflow.json
```

Root-only secrets and proxy variables are stored in:

```text
/etc/llm-cluster/workflow.env
```

Core objects:

- `models`: public workflow ID, underlying text model, mode, resource pool, and permitted tools.
- `pools`: explicit URL targets on the local host or another server; no Docker discovery is required.
- `adapters`: HTTP JSON tools with independent endpoints, secrets, timeouts, response limits, and argument schemas.
- `allowed_purposes`: permitted modes such as `text-to-image`, `image-edit`, `multi-reference`, `web-search`, `audio-transcribe`, or `video-generate`.

Sampler, step count, dimensions, quality, seed, and reference images belong in the adapter's tool JSON Schema. Users express the intent in chat, the text model produces structured tool arguments, and the adapter enforces final bounds. These fields are not hard-coded into LLMCtl.

## A separate controller or remote Workers

This creates a disabled-by-default route and adds two remote vLLM servers:

```bash
sudo llmctl workflow init \
  --listen 127.0.0.1:18100 \
  --gateway-base-url http://10.0.0.20:18100/v1 \
  --route-model llmctl-workflow-gdn-inside \
  --base-model ornith-1.0-35b-fp8 \
  --target http://10.0.1.11:8000/v1 \
  --target http://10.0.1.12:8000/v1

sudo llmctl workflow secret set BACKEND_API_KEY
sudo llmctl workflow target discover http://10.0.1.11:8000/v1 BACKEND_API_KEY ornith-1.0-35b-fp8
sudo llmctl workflow model enable llmctl-workflow-gdn-inside
sudo llmctl workflow check
sudo llmctl workflow enable
```

`--gateway-base-url` must be reachable by the current AI gateway. Omit it when the gateway and data plane share a host. For a separate host, use an internal address and allow only the gateway through the firewall. Every remote Worker should use its own API key; use TLS or a protected private network/VPN across untrusted networks.

Targets can be changed online without restarting existing GPU Workers:

```bash
sudo llmctl workflow target add text-generation gpu-4090-0 http://10.0.2.10:9000/v1 IMAGE_POOL_KEY
sudo llmctl workflow target remove text-generation remote-worker-0
sudo llmctl workflow reload
```

## Add a configurable adapter

An OpenAI function-tool definition such as `/root/image-tool.json` can expose runtime parameters:

```json
{
  "type": "function",
  "function": {
    "name": "generate_image",
    "description": "Generate or edit an image with an approved image pool",
    "parameters": {
      "type": "object",
      "properties": {
        "purpose": {"enum": ["text-to-image", "image-edit", "multi-reference"]},
        "prompt": {"type": "string"},
        "steps": {"type": "integer", "minimum": 1, "maximum": 100},
        "seed": {"type": "integer"},
        "sampler": {"type": "string"},
        "reference_urls": {"type": "array", "items": {"type": "string", "format": "uri"}}
      },
      "required": ["purpose", "prompt"]
    }
  }
}
```

Register it and switch the route to Agent mode:

```bash
sudo llmctl workflow secret set IMAGE_ADAPTER_KEY
sudo llmctl workflow adapter set image-main http://10.0.2.20:19000/invoke /root/image-tool.json IMAGE_ADAPTER_KEY
sudo llmctl workflow model set llmctl-workflow-gdn-inside ornith-1.0-35b-fp8 text-generation agent image-main
sudo llmctl workflow check
sudo llmctl workflow reload
```

The adapter receives:

```json
{
  "tool": "generate_image",
  "arguments": {"purpose": "text-to-image", "prompt": "..."},
  "context": {"request_id": "...", "model": "llmctl-workflow-gdn-inside"}
}
```

A recommended response includes `url`, `mime_type`, dimensions, seed, actual sampling parameters, and safety status. LLMCtl does not require the adapter to use HiDream, any particular diffusion model, an external API, or a specific number of GPU instances.

## Network proxy

Maintenance and runtime proxies are isolated. When search or external media adapters need international access, configure the runtime proxy. It is injected into OmniRoute/the selected Router and the Go Workflow, bypasses loopback and RFC1918 networks by default, and is never injected into vLLM Workers. A change briefly restarts only a running Router and Workflow—not Docker or GPU Workers:

```bash
sudo llmctl runtime-proxy set 192.168.9.104 1082 http
sudo llmctl runtime-proxy test
sudo llmctl runtime-proxy show
```

For resource endpoints on other private ranges, pass a custom comma-separated `NO_PROXY` as the fifth argument. Keep loopback and internal Worker addresses excluded.

Production search adapters must enforce host allowlists, download limits, content types, redirect limits, and timeouts to prevent SSRF, private-network probing, and unbounded downloads. A proxy provides connectivity, not a content-security policy.

## Publishing and rollback boundary

After enabling the workflow, use the portal's workflow administration page to publish it to the AI gateway. Publication uses a collision-checked `llmctl-workflow-*` Combo and never overwrites `gdn-inside` or an existing public mapping. After testing it, explicitly map the desired public ID to that Combo.

An in-place `llmctl upgrade` from 3.2.x:

- updates only LLMCtl control-plane files, portal assets, and the optional runtime;
- does not create or enable `llm-workflow.service`, and does not create workflow configuration;
- does not rebuild, stop, or restart the Router, Docker, or any `llm-worker@N.service`;
- does not change existing model mappings, Worker URLs, keys, balances, pricing, or permissions;
- briefly restarts the account portal; if workflow was previously enabled, only workflow itself is restarted;
- backs up the existing control plane. The repository rollback tag created before this feature is `pre-workflowd-20260804`.

Operations:

```bash
sudo llmctl workflow status
sudo llmctl workflow health
sudo llmctl workflow logs -f
sudo llmctl workflow disable
```

Disabling workflow preserves its configuration and does not affect the original model service.

## Build Linux binaries on macOS

macOS can cross-compile Linux x86_64 and arm64 directly, without a VM:

```bash
brew install go
./scripts/build-workflowd.sh
```

The script uses:

```bash
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build ...
CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build ...
```

The artifacts under `lib/workflowd/` are statically linked ELF files. At installation time, the launcher selects one according to `uname -m`; the Linux server does not need a Go compiler.
