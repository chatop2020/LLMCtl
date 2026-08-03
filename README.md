# LLMCtl：通用 vLLM 多 GPU 集群部署器

**语言：** 中文 | [English](README_EN.md)

[![CI](https://github.com/chatop2020/LLMCtl/actions/workflows/ci.yml/badge.svg)](https://github.com/chatop2020/LLMCtl/actions/workflows/ci.yml)

在 Ubuntu 24.04 裸机上，从 Hugging Face 或 ModelScope 搜索模型，按本机 NVIDIA GPU/显存保守筛选并规划拓扑，然后自动部署多个 vLLM Worker，以及 New API、LiteLLM、Bifrost、OmniRoute 四选一的接入层。默认推荐 New API；需要公司内部注册、独立 API key、周期额度和模型门户时可选择 OmniRoute。

项目不使用 Conda，也不改 NVIDIA 驱动。推理依赖位于固定版本的 Docker 镜像中；安装时可以临时使用局域网代理，运行期默认完全离线且不会自动更新。

> 当前为面向真实多 GPU 主机验收的预发布版本。代码由
> [GitHub 仓库](https://github.com/chatop2020/LLMCtl)统一管理；测试默认在本地执行，需要时可从 GitHub Actions 手动运行同一套检查。

## 主要能力

- 同时搜索 Hugging Face 和 ModelScope，也可只搜索其中一个。
- 推荐前执行只读本机体检：操作系统/架构、CPU/核/线程、内存/Swap、NVIDIA GPU/显存/驱动/计算能力、PCIe 当前与最大链路、GPU/NUMA/NVLink 拓扑及模型盘空间。
- 只显示通过以下门禁的模型：生成式任务、完整权重、非平台专用转换格式、vLLM 0.22.1 架构清单、本机至少 8K 上下文可容纳。
- 明确排除 `mlx-community/*` 等 Apple MLX 转换权重；它们面向 MLX/Apple Silicon，不是 NVIDIA CUDA/vLLM 权重。原生 Ornith FP8/AWQ/GPTQ 等版本不受影响。
- 按权重、运行时预留、KV Cache、最低单卡显存、GPU 数、CPU、可用内存、PCIe/拓扑和磁盘自动推荐 TP、实例数、上下文、`max-num-seqs` 与启动并行度。
- 选择候选后显示显存预算、拓扑链路、主机内存/磁盘预算、逐项推荐原因和风险提示；可确认、返回列表、重新搜索或退出。
- 下载前在固定 vLLM 容器的 `ModelRegistry` 中再次核验架构；下载后校验配置、权重存在性和体积。
- 模型能力匹配时才启用图片/OCR、OpenAI 工具调用、思考解析和请求级思考关闭。
- 一个 GPU 或一个 TP 分组对应一个 Worker；安装器为所选接入层自动生成全部 Worker、鉴权和数据库配置，并通过 Nginx 统一的 `:8000/v1` 提供服务。
- 自动安装或复用现有 Nginx：公开入口统一为 `/v1/` 和 `/ui/`；推理请求直接转发到网关，不经过门户；OmniRoute 原生排障界面保留在 `/base_ui/`。LLMCtl 使用独立配置，写入前执行 `nginx -t`，失败回滚，卸载时保留软件包和其他站点。
- OmniRoute 模式额外部署 Vue 3 企业门户：支持可配置门户品牌、可选公开基准地址（仅用于生成链接）、邮箱验证/精确企业邮箱后缀/注册开关/SMTP 在线配置、用户与用户组、用户独立且登录后保持不变的 API Key（仅手工轮换时更换）、原生活跃会话上限、金额余额、额外 token 赠额及周期自动重置、逐模型输入/输出/缓存/思考定价、用量账本和审计。
- 企业门户通过 OmniRoute API 原生管理模型 ID 映射、Combo、用户 key 权限和免费层资源；免费模型必须经过“发现、已配置、可用、实时测试、管理员发布”门禁。用户只获得公开模型 ID 权限，不能用底层模型或 Combo ID 绕过映射。
- 在线测试支持图片、PDF 和常见文本附件；图片直接作为多模态内容发送，PDF 在浏览器内按页转为图片，不上传到门户后端。流式结果明确标注输入、输出和合计 Token，并展示思考内容、首 Token 延迟与输出速度。
- 管理端提供服务器后台压测：浏览器只提交和观察任务，独立执行器按并发数与目标输入长度生成有意义的提示词，并实时汇总成功率、RPS、聚合输出 tok/s、TTFT、端到端延迟和 p50/p95/p99；同时记录每个请求命中的 Worker，并每秒采样各 GPU 的利用率、显存、功耗及峰值并行卡数，用于识别路由偏斜。高负载档位需要二次确认。
- systemd 开机自启；Worker 可分批并行加载，SSH 断开不影响后台启动。
- 启动和卸载提供聚合进度：逐 Worker 状态、GPU 显存、活动 systemd 单元与容器；SSH 重连后可用 `llmctl startup watch` 继续观察。
- 管理命令支持部分/全部启动、停止、重启、激活、缩容、日志、健康检查、OCR、压力测试、代理与离线包。
- `llmctl info` 提供分门别类的完整灾备清单：公开/内部入口、硬件、镜像、服务、自启、Worker、数据库、管理员、全部密钥/密码、SMTP、代理、模型与文件路径；默认仅供可信 root 终端明文查看，分享时用 `--redact`。
- `llmctl optimize` 可采集流式 TTFT/ITL/E2E、聚合吞吐、GPU/显存/温度、CPU/内存/Swap 和 vLLM KV Cache/排队/抢占/前缀缓存指标；先解释候选原因、代价和边界，经用户确认后才备份配置、逐项重启试验、自动择优、完整冒烟，并在失败或中断时回滚。

## 重要边界

“目录可安装”是保守预检，不是绝对运行保证。自定义模型代码、Tokenizer、量化内核、模型仓库内容和 vLLM 本身仍可能存在运行期兼容问题。因此安装链路还有三道真实验收：固定镜像架构核验、完整模型加载、能力感知的 API 冒烟测试。任何一道失败都会停止并取消开机自启。

功能调用、思考和 OCR 不能从“模型名字看起来像”就保证。脚本只为已知解析协议启用相应参数；未知模型仍可作为普通文本/图片模型部署，不会虚构能力。

## 文件

| 文件 | 用途 |
|---|---|
| `install-llm-cluster.sh` | 首次安装或重新选择模型/拓扑 |
| `llmctl.sh` | 安装为全局命令 `/usr/local/sbin/llmctl` |
| `lib/model_catalog.py` | Hub 搜索、能力识别、显存估算和部署计划 |
| `lib/runtime_optimizer.py` | 流式基准、GPU/vLLM 指标采集、保守候选生成与目标评分 |
| `lib/gateway_config.py` | 四种接入层的无密钥配置生成及 New API/OmniRoute 状态同步 |
| `lib/account_portal.py` | OmniRoute 企业账户门户、邮箱验证、额度和模型目录 |
| `lib/llm_benchmark.py` | 门户管理端的后台并发压测与流式性能指标执行器 |
| `portal-ui/` | Vue 3 企业门户源码与前端测试 |
| `lib/account_portal_ui/` | 已构建、安装时直接复制的门户静态资源 |
| `tests/test_model_catalog.py` | 目录与硬件规划单元测试 |
| `tests/test_runtime_optimizer.py` | 调优建议、评分、指标解析与流式时延测试 |
| `README.md` / `README_EN.md` | 中英文项目说明 |
| `USAGE.md` / `USAGE_EN.md` | 中英文日常使用、API 和故障排查手册 |

## 默认值

| 项目 | 默认值 |
|---|---|
| 模型目录 | `/data/llm-cluster/models` |
| vLLM 镜像 | `vllm/vllm-openai:v0.22.1` |
| 接入层 | New API（推荐） |
| New API 镜像 | `calciumion/new-api:v1.0.0-rc.22` |
| LiteLLM 镜像 | `ghcr.io/berriai/litellm:v1.94.0` |
| Bifrost 镜像 | `maximhq/bifrost:v1.6.7` |
| OmniRoute 镜像 | `diegosouzapw/omniroute:3.8.48` |
| PostgreSQL | `postgres:16-alpine` |
| 统一 API | `http://服务器IP:8000/v1` |
| 统一 Web UI | `http://服务器IP:8000/ui/` |
| OmniRoute 原生 UI | `http://服务器IP:8000/base_ui/` |
| 内部监听 | 网关 `127.0.0.1:18000`；OmniRoute 门户 `127.0.0.1:8001` |
| 管理员用户名 | `admin` |
| 初始密码 | 默认 `llm-admin`；OmniRoute 未指定时生成强随机值 |
| 路由 | 8 个健康 Worker 等权分发并故障切换；LiteLLM 使用 `least-busy` |
| GPU 显存利用率 | `0.92` |

New API、LiteLLM 和 Bifrost 的初始 Web 密码按你的要求保留为通用值。它不是安全密码，安装后应立即运行；OmniRoute 默认已生成强随机密码，也建议妥善轮换：

```bash
sudo llmctl admin set-password
```

### 接入层选择

安装向导在下载镜像前提供四个选项；无人值守时用 `--gateway`：

| 接入层 | 适用场景 | 自动配置内容 |
|---|---|---|
| New API（默认） | 中文管理体验、渠道/令牌/用量管理 | 初始化管理员；为每个健康 Worker 创建等权渠道；创建 root-only 调用令牌 |
| LiteLLM | 更广的供应商兼容与成熟代理配置 | 生成模型列表、`least-busy` 路由、主密钥和 PostgreSQL |
| Bifrost | 高效转发、可观测与虚拟密钥治理 | 生成 8 个 vLLM key、等权路由、虚拟密钥、管理认证和 PostgreSQL 日志存储 |
| OmniRoute | 本地 SQLite 网关及公司账户门户 | 创建 8 个 Provider 节点和一个等权 Combo；部署独立门户数据库、邮箱注册、用户 key、周期额度、用量与模型目录 |

四者都使用 `llm-router.service`，实际网关只监听回环地址 `127.0.0.1:18000`；Nginx 在公开 `8000` 端口统一提供 OpenAI 兼容 `/v1/` 和 `/ui/`。统一维护密钥保存在 root-only 的 `GATEWAY_API_KEY`。New API、LiteLLM、Bifrost 使用 `llm-database.service` 的 PostgreSQL；OmniRoute 使用自己的 SQLite，不启动 PostgreSQL，并额外启动只监听回环 `8001` 的 `llm-account.service`。本版不做在线迁移；切换接入层时应使用没有旧服务配置的全新安装。本地模型和已存在的精确 Docker 镜像会分别核验后复用。部署前还应按使用方式审查各上游项目许可证。

### HTTPS 公网发布

公网域名、证书和端口映射由现有边缘 Nginx、负载均衡器或防火墙负责。OmniRoute 门户管理员可在“发布、注册与 SMTP”中配置：

- **门户品牌名称**：替换页眉、登录页和浏览器标题中的 `LLMCtl`，默认仍为 `LLMCtl`。
- **公开基准地址**：例如 `https://llm.zjguardian.com`。它只决定验证邮件、门户/API 地址、curl 示例和 `llmctl key show` 中生成的链接；不会让 LLMCtl 绑定域名、监听 80/443、申请证书或配置 TLS。留空继续使用原安装地址。

对外只开放 Nginx 统一入口，不要公开 `8001`、`18000` 或 `8100-8107`。控制面升级保留现有 Nginx 软件和其他站点；升级成功后仅在确认 `/etc/nginx/conf.d/llm-cluster.conf` 是 LLMCtl 生成时事务式刷新该文件并平滑 reload。它不会生成域名、80/443、证书或 TLS。也可随时手工重新应用并校验：

```bash
sudo llmctl nginx apply
sudo llmctl nginx test
sudo llmctl info --redact
```

`/base_ui/` 是底层网关管理/排障界面，不是普通用户页面；公网部署应通过边缘 ACL、VPN 或管理网限制它。详细检查清单见 [USAGE.md](USAGE.md)。

## 快速开始

把整个目录复制到服务器，进入目录后运行：

```bash
chmod +x install-llm-cluster.sh llmctl.sh lib/model_catalog.py lib/runtime_optimizer.py lib/gateway_config.py lib/account_portal.py lib/llm_benchmark.py
sudo bash install-llm-cluster.sh
```

交互流程会依次询问：

1. 选择中文或 English；默认中文，后续安装向导和模型目录使用所选语言。
2. 立即直连 Hugging Face 模型目录执行国际网络测试。失败时明确询问是否配置代理，填写后立即复测；无人值守失败必须显式提供 `--proxy`。
3. 选择 New API（默认）、LiteLLM、Bifrost 或 OmniRoute；OmniRoute 会继续询问企业注册、邮箱域名和 SMTP 设置。
4. 只读显示本机 OS、CPU、内存、GPU/驱动、PCIe/拓扑/NUMA 与磁盘体检。
5. 搜索 Hugging Face、ModelScope、两者，或直接输入模型；再输入关键词和用途。
6. 从通过门禁的候选中选择。输入 `0`/`b` 返回，`q` 退出。
7. 阅读模型详细计划、逐项推荐原因和风险；目录候选、已验证配置和手工输入模型都必须先确认。`Y` 确认、`b` 返回、`s` 重新搜索、`q` 退出。
8. 选择模型目录，确认 TP、每实例序列数、激活实例数和按本机资源规划的并行启动数。

也可用参数直接选择安装向导语言：

```bash
sudo bash install-llm-cluster.sh --lang en
```

典型的 ModelScope 无人值守安装：

```bash
sudo bash install-llm-cluster.sh \
  --yes \
  --model-source modelscope \
  --catalog-query Qwen3 \
  --catalog-task text \
  --model-root /data/llm-cluster/models
```

指定确切 Hugging Face 模型；未写 revision 时会解析并固定当前 commit SHA：

```bash
sudo bash install-llm-cluster.sh \
  --yes \
  --model-source huggingface \
  --model-id Qwen/Qwen3-8B \
  --proxy http://10.1.0.6:7890
```

保留的 Ornith 兼容配置：

```bash
sudo bash install-llm-cluster.sh --yes --model-source validated \
  --proxy http://10.1.0.6:7890
```

### 复用已有模型、跳过下载

如果之前保留的 36G Ornith 位于 `/data/ornith/models`，选择已验证的 `protoLabsAI/Ornith-1.0-35B-FP8` 配置后，安装器会检查旧清单和实际文件；完全匹配时自动把该目录作为默认值，随后显示“跳过下载且不复制权重”。无需手工搬运 36G 文件。

复用必须同时满足：Hub、模型 ID、revision、`config.json` 架构、完整权重文件和保守体积下限。旧 Ornith 清单没有 Hub 字段时，只对已固定的 Hugging Face Ornith 身份作有限兼容。名字或文件大小相似不算匹配；例如 ModelScope 的 `deepreinforce-ai/Ornith-1.0-35B-FP8` 不能直接冒充 Hugging Face 的 `protoLabsAI/Ornith-1.0-35B-FP8`。不匹配时安装器拒绝混用，再正常下载所选版本。

无人值守复用也可以显式指定原目录并加 `--skip-download`；该参数不会绕过上述核验：

```bash
sudo bash install-llm-cluster.sh --yes --model-source validated \
  --model-root /data/ornith/models --skip-download
```

重新选择模型会重新计算 TP/上下文/能力参数，并保留已下载模型与接入层数据库：

```bash
sudo bash install-llm-cluster.sh --force-reconfigure
```

旧版 `/etc/ornith` 集群不会被通用版自动覆盖。安装器发现旧服务时会拒绝继续，避免两套 Worker 抢占同一组 GPU 和 8100–8107 端口。本版不实现在线迁移；先卸载旧服务、保留模型，再执行全新安装。

不要用 `llmctl download` 直接切换到不同模型。不同模型可能需要不同 TP 和解析器；管理器会拒绝这种不安全切换，应使用安装器的 `--force-reconfigure` 流程。

## 网络与代理

安装器在语言选择后、任何网关或模型选择之前，直接请求 Hugging Face 模型目录 API。直连失败时会显示原因并询问代理，代理填写后必须复测成功才继续；拒绝代理可以继续，但会明确提示 Hugging Face 结果可能缺失。`--yes` 或 `--non-interactive` 不会卡在输入框，而是要求显式添加 `--proxy http://IP:PORT`。

`llmctl models search --source all|huggingface` 同样先测试网络：优先直连，其次验证保存的维护代理，仍失败才重新询问。健康检查、状态查询和离线推理不会因为没有国际网络而弹出代理问题。

代理只用于安装、模型下载或显式维护命令。安装结束前脚本会：

1. 清除当前进程的代理变量。
2. 删除 Docker 的临时 proxy drop-in。
3. 重启 Docker 使临时代理失效。
4. 以 `HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1` 启动 vLLM。

保存维护代理：

```bash
sudo llmctl proxy set 10.1.0.6 7890 http
sudo llmctl proxy test
sudo llmctl proxy show
sudo llmctl proxy clear
```

Hugging Face 私有或 gated 模型需要在运行安装器时提供 `HF_TOKEN` 并先接受模型许可。ModelScope 私有模型使用 `MODELSCOPE_API_TOKEN`。令牌不会被写入集群配置。

## 升级 LLMCtl 控制面

`llmctl upgrade` 只升级 LLMCtl 自身程序：管理命令、模型目录/调优/网关配置工具，以及账户门户后端和 Vue 静态资源。它不会重跑安装器，也不会修改或重启模型 Worker、Router、Docker、模型权重、运行配置、密钥或数据库。若存在 LLMCtl 自己生成的 Nginx 入口配置，升级验收完成后会事务式刷新并平滑 reload Nginx；其他站点保持不变。

```bash
sudo llmctl upgrade
```

命令会先询问是否从 GitHub 获取 `chatop2020/LLMCtl` 的最新 `main`，锁定到明确提交后下载。网络预检会同时检查 GitHub API 与实际归档下载站点；即使预检通过，只要提交信息或 ZIP 的真实传输失败，也会依次尝试已保存的维护代理、询问新的代理并重试。代理只用于本次维护，可选择保存，不会注入推理服务。替换前会备份完整旧控制面；账户门户如正在运行，只短暂停止该服务并执行健康验收，失败自动回滚。

服务器完全离线时，也可以上传仓库 ZIP 后执行：

```bash
sudo llmctl upgrade --from-zip /root/LLMCtl-main.zip
```

从尚未带有 `upgrade` 命令的旧版本首次引导时，解压新版 ZIP，并运行其中的升级器：

```bash
python3 -m zipfile -e /root/LLMCtl-main.zip /root/llmctl-upgrade-bootstrap
sudo bash /root/llmctl-upgrade-bootstrap/LLMCtl-main/upgrade-llmctl.sh \
  --from-zip /root/LLMCtl-main.zip
```

仅下载/校验而不替换文件可加 `--check`。升级记录保存在 `/var/lib/llm-cluster/control-plane-version.env`，备份保存在 `/var/backups/llmctl/`，并由 `llmctl info` 显示当前控制面提交。

## 模型目录命令

安装后仍可检查硬件和浏览候选模型：

```bash
sudo llmctl models hardware
sudo llmctl models search Qwen3 --source all --task auto --limit 10
sudo llmctl models search OCR --source modelscope --task vision
sudo llmctl models inspect huggingface Qwen/Qwen3-8B
sudo llmctl models current
```

目录结果中的计划例如 `TP1×8 ctx=32768 seq=7 start=8`，表示 8 个独立 Worker、每个 Worker 最多 7 个 vLLM 调度序列，并建议最多同时加载 8 个 Worker；这不是“每个请求都能同时占满 32K/256K”的保证。请求越长、图片越多，KV Cache 压力越高，实际可持续并发越低。PCIe/拓扑体检是能力快照，不是 NCCL 带宽测试，真实性能仍以业务压测为准。

更多日常命令和 API 示例见 [USAGE.md](USAGE.md)。

## 服务与数据位置

| 路径/服务 | 内容 |
|---|---|
| `/etc/llm-cluster/cluster.env` | 非敏感模型、拓扑和能力配置 |
| `/etc/llm-cluster/secrets.env` | API key、数据库和 Web 管理凭据（0600） |
| `/etc/llm-cluster/workers/*.env` | Worker 与 GPU 映射 |
| `/usr/local/lib/llm-cluster/model_catalog.py` | 已安装目录助手 |
| `/usr/local/lib/llm-cluster/gateway_config.py` | 接入层配置与 New API 同步助手 |
| `/usr/local/lib/llm-cluster/account_portal.py` | LLMCtl 账户门户后端 |
| `/usr/local/lib/llm-cluster/account_portal_ui` | Vue 3 门户静态资源 |
| `/usr/local/lib/llm-cluster/upgrade-llmctl.sh` | LLMCtl 控制面升级器 |
| `/etc/nginx/conf.d/llm-cluster.conf` | LLMCtl 独立 Nginx 统一入口配置 |
| `/var/lib/llm-cluster/cache` | 可再生成的 vLLM 缓存 |
| `/var/lib/llm-cluster/omniroute/gateway/storage.sqlite` | OmniRoute 自身 SQLite 数据库 |
| `/var/lib/llm-cluster/omniroute/portal/account-portal.db` | 账户门户独立 SQLite 数据库 |
| `/data/llm-cluster/models` | 默认模型根目录 |
| `llm-cluster.service` | 总控 oneshot 服务 |
| `llm-worker@N.service` | vLLM Worker |
| `llm-router.service` | 所选 New API/LiteLLM/Bifrost/OmniRoute API/UI |
| `llm-database.service` | 非 OmniRoute 接入层 PostgreSQL |
| `llm-account.service` | 仅 OmniRoute 模式的企业账户门户 |
| `nginx.service` | `/v1/`、`/ui/` 和可选 `/base_ui/` 统一公开入口 |

## 门户中的模型参数与账本

- 模型编辑器会从当前 AI 接入层读取底层模型的上下文窗口、最大输出和可识别能力。路由组合包含多个目标时，门户显示其中的保守可用值，并列出每个底层目标。
- 管理员修改最大上下文或最大输出后，LLMCtl 通过接入层原生 API 同步到所有可解析目标；部分失败会保留门户配置、显示失败目标并写入审计，不会把失败伪装成成功。
- 模型描述、OCR 标签和开放范围属于 LLMCtl 发布元数据；它们会显示在管理列表和用户模型广场，但不会伪装成接入层原生参数。
- 计费页将请求用量与金额流水分开。赠送 Token 足以覆盖请求时，金额流水为空是正常状态。用户只能查看自己的请求输入；管理员还可查看接入层已保留的最终模型输出，未启用响应保留时界面会明确说明。
- `llmctl upgrade` 只升级 LLMCtl 控制面、门户静态资源和维护脚本，并执行原地数据库迁移；不会重建或替换现有 Worker 与模型权重。

## 安全说明

- Nginx 默认监听 `0.0.0.0:8000`；网关、门户、Worker 和 PostgreSQL 均强制只绑定回环或内部 Docker 网络，企业门户拒绝使用非 `127.0.0.1` 的监听地址。容器内服务监听 `0.0.0.0` 时，宿主机发布仍固定为 `127.0.0.1`，不会直接暴露。生产环境仍应使用防火墙限制来源并配置 HTTPS。安装器可利旧已有 Nginx，但不会自动签发 TLS 证书。
- 模型 revision 会写入清单。Hugging Face 默认固定 commit；ModelScope 默认 `master`，若要求可复现部署，请显式提供 tag 或 commit hash。
- 只有模型配置声明 `auto_map` 时才启用 `--trust-remote-code`。这仍意味着会执行仓库代码，只选择经过审核且固定 revision 的模型。
- 外部图片域名默认不放行；OCR 示例使用 `data:` base64，降低 SSRF 风险。
- 脚本不会自动升级镜像。`llmctl update` 只在管理员显式执行时拉取并验收。

## 开发验证

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

真实发布前还应在目标服务器执行完整安装、`llmctl smoke --full` 和符合业务请求分布的 `llmctl bench`。40 token/s、25 并发或 256K 上下文是性能目标，不能仅靠显存估算承诺，必须用实际模型、输入长度、图片数量和输出长度压测。
