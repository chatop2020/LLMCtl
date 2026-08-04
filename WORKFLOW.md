# LLMCtl 可插拔工作流与远程资源池

**语言：** 中文 | [English](WORKFLOW_EN.md)

LLMCtl 3.3 增加了一个可选的 Go 工作流数据面，用来把联网搜索、文生图、图片编辑、多参考图、音频和视频等能力组合到一个公开模型中。它不是 Python 门户里的转发器，也不会接管现有模型流量：只有管理员显式配置、验证并发布的工作流模型才经过该进程。

## 数据路径与性能边界

默认路径保持不变：

```text
客户端 -> Nginx /v1 -> 当前 AI 接入层 -> 现有 vLLM Worker
```

显式发布工作流模型后，该模型使用：

```text
客户端 -> Nginx /v1 -> 当前 AI 接入层
       -> llm-workflowd（Go）
       -> 文本资源池 / 搜索适配器 / 图片适配器 / 其他适配器
```

- Vue/Python 门户只负责管理配置，不承载 `/v1` 推理流量。
- Go 数据面使用长连接、连接池和 P2C 最少在途请求调度；透明文本路由会逐块转发真实 SSE，不缓存整个回答。
- Agent 路由会缓冲内部的“规划 -> 工具 -> 总结”轮次，再返回最终结果。所有内部模型轮次的输入、输出、缓存和思考 Token 都会合并到最终 `usage`，便于现有接入层统一计费。
- 图片等大对象应由适配器写入对象存储并返回 HTTPS 签名 URL；不要在模型响应中传输巨大的 Base64 数据。

## 配置模型

配置版本固定为 `version: 1`，位于：

```text
/var/lib/llm-cluster/workflow/workflow.json
```

密钥和代理位于 root-only 的：

```text
/etc/llm-cluster/workflow.env
```

核心对象：

- `models`：公开工作流模型 ID、底层文本模型、模式、资源池、允许调用的工具。
- `pools`：显式 URL 目标列表，可位于本机或其他服务器，不依赖 Docker 自动发现。
- `adapters`：HTTP JSON 工具适配器；每个适配器拥有独立端点、密钥、超时、响应大小和工具参数 Schema。
- `allowed_purposes`：约束适配器允许的操作模式，例如 `text-to-image`、`image-edit`、`multi-reference`、`web-search`、`audio-transcribe` 或 `video-generate`。

采样器、步数、尺寸、质量、随机种子和参考图等参数应写在适配器的工具 JSON Schema 中，由用户在聊天请求中表达、文本模型生成结构化工具参数、适配器执行最终范围校验。它们不是编译进 LLMCtl 的固定字段。

## 新服务器或远程 Worker

以下示例在控制节点创建一个默认关闭的路由，并将两台远程 vLLM 服务器加入资源池：

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

`--gateway-base-url` 必须是当前 AI 接入层能够访问的数据面地址。数据面与网关同机时可省略；分离部署时应填写内网地址，并用防火墙只允许网关访问。所有远程 Worker 应启用独立 API Key；跨不可信网络时必须使用 TLS 或受保护的私网/VPN。

在线增删目标不会重启现有 GPU Worker：

```bash
sudo llmctl workflow target add text-generation gpu-4090-0 http://10.0.2.10:9000/v1 IMAGE_POOL_KEY
sudo llmctl workflow target remove text-generation remote-worker-0
sudo llmctl workflow reload
```

## 增加一个可配置适配器

工具定义文件使用 OpenAI function tool 格式，例如 `/root/image-tool.json`：

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

注册适配器和启用 Agent 路由：

```bash
sudo llmctl workflow secret set IMAGE_ADAPTER_KEY
sudo llmctl workflow adapter set image-main http://10.0.2.20:19000/invoke /root/image-tool.json IMAGE_ADAPTER_KEY
sudo llmctl workflow model set llmctl-workflow-gdn-inside ornith-1.0-35b-fp8 text-generation agent image-main
sudo llmctl workflow check
sudo llmctl workflow reload
```

适配器接收：

```json
{
  "tool": "generate_image",
  "arguments": {"purpose": "text-to-image", "prompt": "..."},
  "context": {"request_id": "...", "model": "llmctl-workflow-gdn-inside"}
}
```

建议结果至少包含 `url`、`mime_type`、`width`、`height`、`seed`、实际采样参数和安全审查状态。LLMCtl 不限定适配器内部使用 HiDream、其他扩散模型、外部 API 或多个 GPU 实例。

## 网络代理

维护代理和运行时代理相互隔离。联网搜索或外部多媒体适配器需要国际出口时，使用运行时代理；它同时注入 OmniRoute/当前 Router 和 Go Workflow，默认绕过本机及 RFC1918 内网，不会注入 vLLM Worker。配置变化只短暂重启 Router 与已启用的 Workflow，不重启 Docker 或 GPU Worker：

```bash
sudo llmctl runtime-proxy set 192.168.9.104 1082 http
sudo llmctl runtime-proxy test
sudo llmctl runtime-proxy show
```

如资源端点位于非标准内网，可把第 5 个参数指定为自定义、逗号分隔的 `NO_PROXY`。不要删除 `127.0.0.1` 和内部 Worker 地址。

生产环境应为搜索适配器设置域名允许列表、下载大小、内容类型、重定向次数和超时，防止 SSRF、内网探测和无限下载。代理只解决网络连通性，不等于内容安全策略。

## 发布与回滚边界

工作流启用后，在门户管理端“工作流”页面执行“发布到 AI 接入层”。发布使用独立的 `llmctl-workflow-*` Combo 名称，并在写入前检查名称冲突；不会覆盖 `gdn-inside` 或任何现有公开映射。确认测试通过后，管理员再明确把所需公开 ID 指向该 Combo。

从 3.2.x 原地执行 `llmctl upgrade` 时：

- 只更新 LLMCtl 控制面、门户静态资源和可选运行时文件。
- 不创建、不启用 `llm-workflow.service`，也不生成工作流配置。
- 不重建、不停止、不重启 Router、Docker 或任何 `llm-worker@N.service`。
- 不修改现有模型映射、Worker URL、Key、余额、计费或用户权限。
- 账户门户会短暂停止并重新加载；如果工作流以前已经由管理员启用，则只重启工作流自身。
- 升级前自动备份控制面；本次功能开发前的仓库回滚点为 Git tag `pre-workflowd-20260804`。

常用检查和关闭命令：

```bash
sudo llmctl workflow status
sudo llmctl workflow health
sudo llmctl workflow logs -f
sudo llmctl workflow disable
```

关闭工作流只停止可选服务并保留配置，不影响原有模型服务。

## 在 macOS 构建 Linux 二进制

macOS 可以直接交叉编译 Linux x86_64 和 arm64，无需虚拟机：

```bash
brew install go
./scripts/build-workflowd.sh
```

脚本实际使用：

```bash
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build ...
CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build ...
```

产物位于 `lib/workflowd/`，是静态链接 ELF。服务器安装时的启动包装器会按 `uname -m` 选择对应文件，Linux 服务器不需要 Go 编译器。
