# LLMCtl 多模型部署与 GPU 拆分

**语言：** 中文 | [English](MULTI_MODEL_EN.md)

LLMCtl 3.5 起可以在管理后台的“模型部署”页面下载、校验和发布新模型，也可以重新分配现有 Worker 的 GPU。模型控制器只处理配置、下载、验收和回滚；正常推理请求仍由 Nginx 直接进入当前 AI 接入层，再到 vLLM Worker，不经过门户 Python 进程。

## 上线前提

- 先执行 `llmctl upgrade`，再用 `llmctl model status` 确认 `llm-model-control.service` 为 `active`。
- 自动发布多个公开模型 ID 目前要求 OmniRoute。New API、LiteLLM 和 Bifrost 可以维护不占用现有 Worker 的隔离资源，但 LLMCtl 不会假装已经把它们写入接入层。
- 本机模型目录默认为 `/data/llm-cluster/models`。选择的本地目录已包含完整权重时会直接校验并复用，不重复下载。
- 每个本地实例必须独占 Worker ID、监听端口和 GPU；一个 GPU 不能同时分配给两个 vLLM 实例。
- 计划页显示的“受影响 Worker”是本次唯一允许重启的范围。下载、架构校验或健康验收失败时，控制器会恢复注册表、Worker 配置和接入层快照。

## 8 卡拆分示例

目标拓扑：

| 公开模型 ID | 模型 | GPU | Worker |
|---|---|---|---|
| `gdn-inside-ornith` | 现有 Ornith | 0–3 | 0–3 |
| `gdn-inside-qwen` | 新 Qwen | 4–7 | 4–7 |
| `gdn-inside` | 兼容别名 | 指向 `gdn-inside-ornith` | 不新增实例 |

推荐分两次操作，避免一次性打断全部八个现有实例：

1. 在“模型部署”中新建 Qwen，选择 GPU 4–7 和 Worker 4–7，公开 ID 填 `gdn-inside-qwen`。
2. 阅读计划。计划应明确 Qwen 接管 Worker 4–7，同时旧 Ornith 暂时保留 Worker 0–3。
3. 确认后提交，等待下载、加载、健康检查和接入层同步完成。
4. 对“当前部署”中的旧 Ornith 选择“拆分并改名”，保留 GPU 0–3 和 Worker 0–3，公开 ID 改为 `gdn-inside-ornith`。
5. 勾选“保留旧兼容 ID”，使现有客户端继续用 `gdn-inside`，新客户端可以显式选择两个模型。
6. 再次核对计划，只应重启 Worker 0–3；提交后分别调用三个公开 ID 验收。

如果 Qwen 使用 TP4，则把 GPU `4,5,6,7` 配成一个实例；如果计划使用四个 TP1 实例，则分别配置四个 Worker。不要把“实例数”和 `tensor_parallel_size` 混为一项。

## Web 操作流程

1. 选择 Hugging Face、ModelScope 或本地目录。
2. 填写模型 ID、revision、公开 ID 和显示名称。
3. 配置镜像、TP、上下文、显存利用率、最大并发序列、批处理 Token，以及图片、OCR、工具调用和思考解析能力。
4. 添加本地或远程实例。远程实例使用明确的 OpenAI 兼容 `/v1` 地址，不依赖本机 Docker 发现。
5. 点击“生成计划”。计划会检查 GPU、Worker、端口、模型路径、公开 ID 冲突、接入层能力和磁盘空间。
6. 确认影响范围后提交后台任务。页面可以离开，任务状态持久化在 `/var/lib/llm-cluster/model-control`。
7. 失败时在任务详情中执行回滚；回滚本身也先创建安全快照。

## 命令行兜底

```bash
sudo llmctl model init
sudo llmctl model status
sudo llmctl model plan /root/deployment.json
sudo llmctl model deploy /root/deployment.json
sudo llmctl model job <任务ID>
sudo llmctl model cancel <任务ID>
sudo llmctl model rollback <任务ID>
sudo llmctl logs model
```

`plan` 只校验，不修改运行状态。`deploy` 返回任务 ID 后，下载和重启在 systemd 控制服务中继续执行，SSH 断开不会终止任务。

## 远程 Worker

控制面和 GPU 主机可以分离。远程实例需要配置：

- 稳定且仅内网可访问的 `base_url`；
- 远端真实模型 ID；
- 可选的密钥环境变量引用；
- 健康检查和调用超时。

远程实例不写本机 systemd Worker 配置，也不占用本机 GPU/Worker ID。LLMCtl 只在发布前验证远程 `/v1/models` 和推理请求，接入层仍负责正式流量调度。

## 兼容与回滚

- 旧版本只有 `/etc/llm-cluster/cluster.env` 和 `llm-worker@N` 时，首次启动模型控制器会合成一份注册表，不重启 Router 或 Worker。
- `llmctl upgrade` 只更新控制面和门户；除非管理员提交模型部署任务，否则不会调整现有模型、Worker 或 GPU。
- 每个任务会保存注册表、逐 Worker 环境文件和接入层数据库/配置快照。只恢复该任务影响的 Worker。
- 模型权重属于可复用数据，不会因为控制面回滚而删除。
- 上线前可用 `llmctl info --redact` 保存脱敏环境清单，并保留 `/var/backups/llmctl` 与任务备份目录。

## 安全边界

- 模型路径必须位于 LLMCtl 配置的模型根目录，不能借部署请求读取任意主机路径。
- 只有 root 或 `llm-account` 组内的门户服务可以访问模型控制 Unix Socket。
- 门户管理 API 不承载推理流量，不接受任意 shell 命令，也不直接执行浏览器传入的程序。
- `trust_remote_code` 默认关闭。确需启用时应先固定 revision、审查仓库代码并在测试主机验收。
- 对公网开放的仍应只有 Nginx；模型控制 Socket、vLLM Worker、门户后端和网关内部端口保持本机或受控内网监听。
