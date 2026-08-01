# LLMCtl：通用 vLLM 多 GPU 集群部署器

[![CI](https://github.com/chatop2020/LLMCtl/actions/workflows/ci.yml/badge.svg)](https://github.com/chatop2020/LLMCtl/actions/workflows/ci.yml)

在 Ubuntu 24.04 裸机上，从 Hugging Face 或 ModelScope 搜索模型，按本机 NVIDIA GPU/显存保守筛选并规划拓扑，然后自动部署多个 vLLM Worker、LiteLLM 负载均衡和 Web 管理后台。

项目不使用 Conda，也不改 NVIDIA 驱动。推理依赖位于固定版本的 Docker 镜像中；安装时可以临时使用局域网代理，运行期默认完全离线且不会自动更新。

> 当前为面向真实多 GPU 主机验收的预发布版本。代码由
> [GitHub 仓库](https://github.com/chatop2020/LLMCtl)统一管理；每次提交和拉取请求都会运行自动测试。

## 主要能力

- 同时搜索 Hugging Face 和 ModelScope，也可只搜索其中一个。
- 只显示通过以下门禁的模型：生成式任务、完整权重、vLLM 0.22.1 架构清单、本机至少 8K 上下文可容纳。
- 按权重、运行时预留、KV Cache、最低单卡显存和 GPU 数自动推荐 TP、实例数、上下文与 `max-num-seqs`。
- 下载前在固定 vLLM 容器的 `ModelRegistry` 中再次核验架构；下载后校验配置、权重存在性和体积。
- 模型能力匹配时才启用图片/OCR、OpenAI 工具调用、思考解析和请求级思考关闭。
- 一个 GPU 或一个 TP 分组对应一个 Worker；LiteLLM 使用 `least-busy` 按未完成请求数路由，并设置每 Worker 的并发上限。
- systemd 开机自启；Worker 可分批并行加载，SSH 断开不影响后台启动。
- 启动和卸载提供聚合进度：逐 Worker 状态、GPU 显存、活动 systemd 单元与容器；SSH 重连后可用 `llmctl startup watch` 继续观察。
- 管理命令支持部分/全部启动、停止、重启、激活、缩容、日志、健康检查、OCR、压力测试、代理与离线包。

## 重要边界

“目录可安装”是保守预检，不是绝对运行保证。自定义模型代码、Tokenizer、量化内核、模型仓库内容和 vLLM 本身仍可能存在运行期兼容问题。因此安装链路还有三道真实验收：固定镜像架构核验、完整模型加载、能力感知的 API 冒烟测试。任何一道失败都会停止并取消开机自启。

功能调用、思考和 OCR 不能从“模型名字看起来像”就保证。脚本只为已知解析协议启用相应参数；未知模型仍可作为普通文本/图片模型部署，不会虚构能力。

## 文件

| 文件 | 用途 |
|---|---|
| `install-llm-cluster.sh` | 首次安装或重新选择模型/拓扑 |
| `llmctl.sh` | 安装为全局命令 `/usr/local/sbin/llmctl` |
| `lib/model_catalog.py` | Hub 搜索、能力识别、显存估算和部署计划 |
| `tests/test_model_catalog.py` | 目录与硬件规划单元测试 |
| `USAGE.md` | 日常使用、API 和故障排查手册 |

## 默认值

| 项目 | 默认值 |
|---|---|
| 模型目录 | `/data/llm-cluster/models` |
| vLLM 镜像 | `vllm/vllm-openai:v0.22.1` |
| LiteLLM 镜像 | `ghcr.io/berriai/litellm:v1.94.0` |
| PostgreSQL | `postgres:16-alpine` |
| API | `http://服务器IP:8000/v1` |
| Web UI | `http://服务器IP:8000/ui` |
| 管理员用户名 | `admin` |
| 初始通用密码 | `llm-admin` |
| 路由策略 | `least-busy` |
| GPU 显存利用率 | `0.92` |

初始 Web 密码按你的要求保留为可公开的通用密码。它不是安全密码，安装后应立即运行：

```bash
sudo llmctl admin set-password
```

## 快速开始

把整个目录复制到服务器，进入目录后运行：

```bash
chmod +x install-llm-cluster.sh llmctl.sh lib/model_catalog.py
sudo bash install-llm-cluster.sh
```

交互流程会依次询问：

1. 搜索 Hugging Face、ModelScope、两者，或直接输入模型。
2. 搜索关键词和用途（文本/视觉/自动）。
3. 从本机可部署模型中选择。
4. 模型目录，默认 `/data/llm-cluster/models`。
5. 是否接受推荐 TP、每实例序列数、激活实例数和并行启动数。
6. 需要国际资源时才询问代理 IP 和端口；代理可选择保存供维护使用。

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

重新选择模型会重新计算 TP/上下文/能力参数，并保留已下载模型与 LiteLLM 数据库：

```bash
sudo bash install-llm-cluster.sh --force-reconfigure
```

旧版 `/etc/ornith` 集群不会被通用版自动覆盖。安装器发现旧服务时会拒绝继续并给出提示，避免两套 Worker 抢占同一组 GPU 和 8100–8107 端口。当前已运行的 Ornith 服务可继续使用；在没有完成配置、模型和 LiteLLM 数据库备份前，不要直接迁移。

不要用 `llmctl download` 直接切换到不同模型。不同模型可能需要不同 TP 和解析器；管理器会拒绝这种不安全切换，应使用安装器的 `--force-reconfigure` 流程。

## 网络与代理

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

## 模型目录命令

安装后仍可检查硬件和浏览候选模型：

```bash
sudo llmctl models hardware
sudo llmctl models search Qwen3 --source all --task auto --limit 10
sudo llmctl models search OCR --source modelscope --task vision
sudo llmctl models inspect huggingface Qwen/Qwen3-8B
sudo llmctl models current
```

目录结果中的计划例如 `TP1×8 ctx=32768 seq=7`，表示 8 个独立 Worker、每个 Worker 最多 7 个 vLLM 调度序列；这不是“每个请求都能同时占满 32K/256K”的保证。请求越长、图片越多，KV Cache 压力越高，实际可持续并发越低。

更多日常命令和 API 示例见 [USAGE.md](USAGE.md)。

## 服务与数据位置

| 路径/服务 | 内容 |
|---|---|
| `/etc/llm-cluster/cluster.env` | 非敏感模型、拓扑和能力配置 |
| `/etc/llm-cluster/secrets.env` | API key、数据库和 Web 管理凭据（0600） |
| `/etc/llm-cluster/workers/*.env` | Worker 与 GPU 映射 |
| `/usr/local/lib/llm-cluster/model_catalog.py` | 已安装目录助手 |
| `/var/lib/llm-cluster/cache` | 可再生成的 vLLM 缓存 |
| `/data/llm-cluster/models` | 默认模型根目录 |
| `llm-cluster.service` | 总控 oneshot 服务 |
| `llm-worker@N.service` | vLLM Worker |
| `llm-router.service` | LiteLLM API/UI |
| `llm-database.service` | LiteLLM PostgreSQL |

## 安全说明

- API 默认监听 `0.0.0.0:8000`，应使用主机防火墙限制到可信局域网；PostgreSQL 只监听 `127.0.0.1`。
- 模型 revision 会写入清单。Hugging Face 默认固定 commit；ModelScope 默认 `master`，若要求可复现部署，请显式提供 tag 或 commit hash。
- 只有模型配置声明 `auto_map` 时才启用 `--trust-remote-code`。这仍意味着会执行仓库代码，只选择经过审核且固定 revision 的模型。
- 外部图片域名默认不放行；OCR 示例使用 `data:` base64，降低 SSRF 风险。
- 脚本不会自动升级镜像。`llmctl update` 只在管理员显式执行时拉取并验收。

## 开发验证

```bash
bash -n install-llm-cluster.sh llmctl.sh
python3 -m py_compile lib/model_catalog.py
python3 -m unittest discover -s tests -v
```

真实发布前还应在目标服务器执行完整安装、`llmctl smoke --full` 和符合业务请求分布的 `llmctl bench`。40 token/s、25 并发或 256K 上下文是性能目标，不能仅靠显存估算承诺，必须用实际模型、输入长度、图片数量和输出长度压测。
