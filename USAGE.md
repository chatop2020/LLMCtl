# llmctl 使用手册

**语言：** 中文 | [English](USAGE_EN.md)

## 安装后的第一组命令

```bash
sudo llmctl status
sudo llmctl health
sudo llmctl smoke --full
sudo llmctl key show
sudo llmctl admin show
sudo llmctl admin set-password
```

服务器从 UTC 改为中国标准时间：

```bash
sudo llmctl timezone set Asia/Shanghai
```

Web 管理界面默认地址为 `http://服务器IP:8000/ui`，管理员用户名 `admin`，初始通用密码 `llm-admin`。密码会写在 root-only 的 `/etc/llm-cluster/secrets.env`，也可用 `sudo llmctl admin show` 查看。首次登录后请修改。

## SSH 断开或安装窗口看起来不动

systemd 接管后，SSH 会话断开不会终止 Worker。重新登录后运行：

```bash
systemctl status llm-cluster.service --no-pager -l
sudo llmctl startup watch
sudo llmctl status
sudo llmctl health
journalctl -u llm-cluster.service -f
```

`startup watch` 每 10 秒汇总一次 `healthy/loading/pending/failed`、每个 Worker 状态和每张卡显存。Worker 加载大模型时，`GPU-Util` 可能接近 0%，但显存会逐步增长。看单个 Worker：

```bash
sudo llmctl logs worker 0 -f
nvidia-smi
```

`sudo llmctl logs` 默认汇总 Router、数据库和全部激活 Worker；也可使用 `llmctl logs router`、`llmctl logs database` 或 `llmctl logs worker 0` 精确查看。能力冒烟失败时，响应摘要会显示 `finish_reason` 与各字段长度，完整固定测试响应保存在 root-only 的 `/var/lib/llm-cluster/diagnostics/smoke`，用于区分长度截断、协议解析和模型答案错误。

若总控显示 `active (exited)` 或 `Finished llm-cluster.service`，这是正常的 `oneshot + RemainAfterExit` 状态；真正的推理进程位于 `llm-worker@N.service`。

## Worker 管理

```bash
sudo llmctl start all
sudo llmctl start 0,2,4
sudo llmctl stop all
sudo llmctl stop 1,3
sudo llmctl restart all
sudo llmctl restart 0,1
sudo llmctl shutdown
```

`start/stop/restart` 默认不改变下次开机列表；`shutdown` 并发停止 Router、数据库和全部 Worker，也不改变下次开机列表。持久管理：

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

- `enable/disable`：只改开机列表。
- `activate/deactivate`：同时改开机列表、运行状态和 LiteLLM 后端。
- `scale N`：持久设置为前 N 个实例。
- `all` 对 `start/restart` 表示持久激活列表；`stop all` 会停止所有可能实例。

并行启动数量：

```bash
sudo llmctl tune set startup-parallelism 8
sudo llmctl restart all
```

8 个并行加载启动更快，但 CPU、系统内存和磁盘读取峰值更高。安装器会根据本机 CPU 线程、可用内存、模型大小和实例数给出默认值；手工调高后应观察内存与磁盘。启动期间每个 Worker 分别加载一份模型；这不是 GPU 间权重共享。

启动命令按批次并发发起，并持续输出整个批次的聚合进度，不会再逐个 Worker 静默等待。直接执行 `systemctl start llm-cluster.service` 时，systemd 客户端本身不会转发服务日志；请另开窗口或 SSH 重连后运行 `sudo llmctl startup watch`。

## LiteLLM 路由

```bash
sudo llmctl router status
sudo llmctl router restart
sudo llmctl database status
```

默认 `least-busy` 根据每个后端未完成请求数选择 Worker，并结合 `max_parallel_requests=max-num-seqs` 限流。它不会直接读取 GPU 显存、KV Cache 或图片大小，因此不是完美的“按工作量”调度。大请求仍可能造成负载不均；需要用业务压测观察，再考虑请求分类、不同模型别名或独立长上下文池。

## OpenAI 兼容 API

先获取参数：

```bash
sudo llmctl key show
```

文本请求：

```bash
curl http://服务器IP:8000/v1/chat/completions \
  -H 'Authorization: Bearer YOUR_KEY' \
  -H 'Content-Type: application/json' \
  -d '{
    "model":"安装摘要中的模型名",
    "messages":[{"role":"user","content":"你好"}],
    "max_tokens":256
  }'
```

支持请求级关闭思考的模型可加入：

```json
{
  "reasoning_effort": "none",
  "chat_template_kwargs": {"enable_thinking": false}
}
```

不支持思考开关的模型不要发送这些字段。查看当前能力：

```bash
sudo llmctl status
```

工具调用遵循 OpenAI `tools`/`tool_choice` 格式。模型只会返回工具调用意图；业务程序仍必须执行函数，并把 `tool` 消息返回模型。脚本只对识别出的模型协议开启 vLLM tool parser。

## 图片与 OCR

只有 `llmctl status` 显示支持图片时才能使用：

```bash
sudo llmctl ocr /path/to/image.png '逐字识别全部内容'
```

默认每请求上限 8 张，完整验收包含单请求 6 张：

```bash
sudo llmctl smoke --full
sudo llmctl tune set max-images 8
sudo llmctl restart all
```

API 使用 OpenAI `image_url` 内容块。默认推荐 `data:image/...;base64,...`；服务没有放行任意外部媒体域名。

## 参数调整

```bash
sudo llmctl tune show
sudo llmctl tune set max-model-len 32768
sudo llmctl tune set max-num-seqs 7
sudo llmctl tune set max-num-batched-tokens 8192
sudo llmctl tune set gpu-memory-utilization 0.92
sudo llmctl tune set routing-strategy least-busy
```

修改显存或 Worker 参数后需 `restart all`。提高 `max-model-len`、`max-num-seqs` 或图片上限会增加峰值显存；不要只因为空闲时还有几 GiB 就盲目打满，CUDA Graph、临时张量、图片编码和请求长度波动都需要余量。

## 性能验收

```bash
sudo llmctl bench --concurrency 25 --requests 50 --max-tokens 512
```

输出包括聚合 token/s、单请求有效 token/s p50/p95 和请求耗时。对“25 平均并发、40 token/s”这类目标，至少分别测试短文本、长 prompt、思考、工具调用和 6 图请求。256K 是单请求最大窗口，不等于 25 个请求都能同时使用 256K。

## 自动测试、建议与调优

只测试当前配置并获得建议，不写配置或重启：

```bash
sudo llmctl optimize analyze --profile balanced
```

经确认后自动试验并应用：

```bash
sudo llmctl optimize run --profile balanced
```

可选目标：

- `latency`：优先 p95 TTFT 与 ITL。
- `balanced`：默认，综合聚合吞吐、p95 TTFT 和 ITL。
- `throughput`：优先聚合输出 token/s，同时保留尾延迟约束。

流程先执行能力冒烟和当前配置基线，再采集流式 TTFT、ITL、E2E、聚合输出 token/s、GPU 利用率/显存/温度、CPU/内存/Swap，以及每个 vLLM Worker 的 KV Cache 峰值、排队、抢占和前缀缓存命中。随后逐项展示候选参数、推荐原因、可能代价和不可由本测试证明的边界。关键 GPU、主机或 vLLM 指标缺失，以及 CPU/内存/Swap 压力过高时，流程不会生成向上扩容候选。

在用户输入 `y` 前不会写配置或重启。确认后，LLMCtl 会把完整 `cluster.env` 备份到 `/var/lib/llm-cluster/optimization/backups`，最多测试两个保守候选。候选必须没有请求失败、不能增加 KV Cache 抢占，并且针对所选目标的综合得分通常至少提高 5% 才会保留。每次候选试验都需要重启激活 Worker，因此 API 会短暂不可用。最后还会运行包含文本、思考、工具调用及模型支持时 OCR/6 图的完整冒烟测试；启动、冒烟、Ctrl+C 或信号中断都会触发原配置恢复。

快速模式减少请求量和候选数，适合初筛但证据较弱：

```bash
sudo llmctl optimize analyze --profile throughput --quick
sudo llmctl optimize run --profile throughput --quick
```

查看报告或显式恢复调优前配置：

```bash
sudo llmctl optimize report
sudo llmctl optimize report --json
sudo llmctl optimize restore latest
# 或使用报告中的 RUN_ID
sudo llmctl optimize restore 20260801T120000Z
```

自动负载是可复现的合成文本，不代表真实 prompt、长上下文、输出长度、图片比例或工具调用分布。它不会自动改变模型、TP、上下文上限、路由语义、量化格式或驱动；上线前仍需用业务样本复测。`--yes` 只适合已经阅读候选和停机影响的无人值守维护窗口。

## 模型搜索与重配

```bash
sudo llmctl models hardware
sudo llmctl models search Qwen --source all --task auto
sudo llmctl models inspect modelscope Qwen/Qwen3-8B
sudo llmctl models current
```

安装向导会先做只读体检，再搜索模型。体检包括操作系统/架构、CPU/核/线程、内存/Swap、GPU/显存/驱动/计算能力、PCIe 当前与最大链路、GPU/NUMA/NVLink 拓扑和模型盘空间。PCIe/拓扑是能力快照，不是主动 NCCL 带宽测试。

目录会排除 Apple MLX 等平台专用转换权重；`mlx-community/*` 面向 MLX/Apple Silicon，不能作为 NVIDIA CUDA/vLLM 权重。选择候选后会展开显存预算、TP 链路、主机内存、磁盘、启动并行度和逐项推荐理由。此时可确认、返回候选列表或重新搜索。

ModelScope 下载器固定安装在独立虚拟环境，实际命令为 `/opt/llm-cluster/hub-venv/bin/ms`。安装器会在下载大权重前验证 `ms download --help`；`.partial` 目录用于断点续传，下载错误时不要手工删除。

若所选模型已存在，只有 Hub、模型 ID、revision、配置架构、完整权重和体积全部匹配时才跳过下载，且不会复制权重。保留的 `/data/ornith/models` 可被自动识别；不同来源或模型 ID 即使名称相似也不会混用。

切换到不同模型：回到项目目录，重新运行安装器。

```bash
sudo bash install-llm-cluster.sh --force-reconfigure
```

这样会重新检查权重、架构、TP、上下文和能力解析器。直接 `llmctl download` 只允许重新核验/补齐当前模型，不允许绕过规划切换模型。

## 镜像维护、代理和离线包

脚本不会自动更新。显式维护：

```bash
sudo llmctl proxy set 10.1.0.6 7890 http
sudo llmctl update --vllm-image vllm/vllm-openai:v0.22.1
sudo llmctl proxy clear
```

导出/导入：

```bash
sudo llmctl offline export /data/offline/llm-bundle
sudo llmctl offline import /data/offline/llm-bundle
```

## 日志

```bash
sudo llmctl logs worker 0 -f
sudo llmctl logs router -f
sudo llmctl logs database -f
journalctl -u llm-cluster.service -n 300 --no-pager
```

常见判断：

- `curl: (7) ... 810N` 在 Worker 加载阶段只是健康检查尚未通过，不代表已失败。
- Worker 显存停止增长且日志长期无变化：查看是否在编译 CUDA Graph、CPU 内存/磁盘是否饱和，以及服务是否仍 active。
- Worker 退出：日志末尾通常会给出 OOM、架构、量化内核或模型代码错误。
- LiteLLM 不健康但 Worker 健康：检查 `llm-router` 和 `llm-database` 日志。

## 卸载

默认保留模型与 LiteLLM 数据库：

```bash
sudo llmctl uninstall
```

卸载会并发停止 Router、数据库和全部 Worker，并每 5 秒显示仍活动的 systemd 单元、容器和 GPU 显存。正常停止总等待上限为 180 秒；超时后只对 `llm-*` 命名的本集群单元/容器执行一次有界强制停止，仍失败则保留配置并明确退出，不会边运行边删除。删除包含大量小文件的编译缓存、以及显式 `--purge-model` 的模型目录时也会持续输出心跳，不会再长时间静默。

可选永久删除：

```bash
sudo llmctl uninstall --purge-model --purge-images --purge-database
```

`--purge-model` 只有在模型目录存在安装器标记时才允许执行；`--purge-database` 会永久删除 Web UI 数据、虚拟 key 和管理记录。
