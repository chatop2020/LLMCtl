# llmctl 使用手册

**语言：** 中文 | [English](USAGE_EN.md)

## 安装后的第一组命令

```bash
sudo llmctl status
sudo llmctl health
sudo llmctl info --redact
sudo llmctl smoke --full
sudo llmctl key show
sudo llmctl admin show
sudo llmctl admin set-password
```

服务器从 UTC 改为中国标准时间：

```bash
sudo llmctl timezone set Asia/Shanghai
```

Nginx 统一公开入口：API 为 `http://服务器IP:8000/v1/`，日常 Web 界面为 `http://服务器IP:8000/ui/`。OmniRoute 模式下企业门户占用 `/ui/`，原生 OmniRoute 界面保留在 `/base_ui/`；其他网关的 `/ui/` 指向各自原生界面。实际网关只监听 `127.0.0.1:18000`，企业门户只监听 `127.0.0.1:8001`，不需要向用户开放第二个端口。

管理员用户名默认 `admin`。OmniRoute 未显式传入密码时生成强随机密码；其他接入层初始密码为 `llm-admin`。凭据写在 root-only 的 `/etc/llm-cluster/secrets.env`。`sudo llmctl admin show` 显示日常登录信息；`sudo llmctl info` 是维护兜底清单，默认在 root 终端显示全部明文密码、API key、数据库/SMTP 凭据、入口、内部地址、模型、服务、路径和 SQLite 检查结果，输出到工单前务必改用 `sudo llmctl info --redact`。

全新安装时选择接入层；默认 New API，也可显式指定：

```bash
sudo bash install-llm-cluster.sh --gateway newapi
sudo bash install-llm-cluster.sh --gateway litellm
sudo bash install-llm-cluster.sh --gateway bifrost
sudo bash install-llm-cluster.sh --gateway omniroute
```

安装器只拉取所选接入层。若精确镜像已经存在于本机，直接复用；模型身份、revision、架构、完整性和大小全部匹配时也跳过权重下载。四种接入层统一维护 root-only 的 `GATEWAY_API_KEY`，且不做在线迁移。

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
- `activate/deactivate`：同时改开机列表、运行状态和所选接入层的后端。
- `scale N`：持久设置为前 N 个实例。
- `all` 对 `start/restart` 表示持久激活列表；`stop all` 会停止所有可能实例。

并行启动数量：

```bash
sudo llmctl tune set startup-parallelism 8
sudo llmctl restart all
```

8 个并行加载启动更快，但 CPU、系统内存和磁盘读取峰值更高。安装器会根据本机 CPU 线程、可用内存、模型大小和实例数给出默认值；手工调高后应观察内存与磁盘。启动期间每个 Worker 分别加载一份模型；这不是 GPU 间权重共享。

启动命令按批次并发发起，并持续输出整个批次的聚合进度，不会再逐个 Worker 静默等待。直接执行 `systemctl start llm-cluster.service` 时，systemd 客户端本身不会转发服务日志；请另开窗口或 SSH 重连后运行 `sudo llmctl startup watch`。

## 接入层与路由

```bash
sudo llmctl router status
sudo llmctl router restart
sudo llmctl database status
```

`llmctl router restart` 会重新探测健康 Worker、生成所选接入层配置、重启并等待进程健康，然后才验证带密钥的 `/v1/models`。New API 自动初始化管理员、为每个健康 Worker 创建等权渠道并生成受管令牌；Bifrost 自动生成等权 vLLM keys、虚拟密钥和 PostgreSQL 日志存储；LiteLLM 使用 `least-busy` 与每 Worker 并发上限；OmniRoute 自动创建每 Worker 一个 Provider 节点及一个等权 Combo，并同步模型元信息和维护 key。LLMCtl 将 OmniRoute 的会话粘性和独立的粘性轮转批量分别关闭（`disableSessionStickiness=true`、`stickyRoundRobinLimit=1`）。每个受管连接和 Combo 允许 20 个在途请求，异常排队最多等待 1 秒；`MAX_NUM_SEQS` 仍是每个 vLLM Worker 的活动序列上限，超过部分由 vLLM 调度器排队。对于安装阶段已经核验原生图片输入能力的本地模型，LLMCtl 还会关闭 OmniRoute 的 Vision Bridge：OmniRoute 3.8.x 在检查自定义 Provider 的 Combo 成员时看不到已保存的 `supportsVision=true`，否则会错误调用默认 `openai/gpt-4o-mini` 并在 30 秒超时后才把原图转发给 vLLM。8 Worker 因而可在网关排队前容纳 160 个在途请求，突发请求会逐个推进到下一健康 Worker，原生多模态请求也不会经过冗余的外部视觉桥接。

只需把升级后的受管渠道/Combo 配置写入正在运行的接入层时，使用 `llmctl router reconcile`。它会重新探测健康 Worker、在线同步配置并验证 `/v1/models`，不会重启 Router 或 GPU Worker。

Nginx 由安装器自动安装，或发现已有安装时利旧。LLMCtl 只管理 `/etc/nginx/conf.d/llm-cluster.conf`，写入后必须通过 `nginx -t` 才 reload；失败会恢复修改前内容。若安装前同名配置已存在，卸载时恢复它；Nginx 软件包、其他虚拟主机和证书配置始终保留。推理路径 `/v1/` 关闭代理缓冲并直接到网关，不经过 Python 门户，因此流式响应和高并发不会增加门户中转开销。

这些路由都不会直接读取单个请求将占用的 GPU 显存、KV Cache 或图片大小。长上下文和多图请求仍可能造成瞬时不均；用真实业务分布运行 `llmctl bench` 和 `llmctl optimize analyze` 后再调优。

密钥与管理员维护：

```bash
sudo llmctl key show
sudo llmctl key rotate
sudo llmctl admin show
sudo llmctl admin set-username NEW_ADMIN
sudo llmctl admin set-password
sudo llmctl admin set-portal-username NEW_ADMIN
sudo llmctl admin set-portal-password
sudo llmctl admin set-gateway-username NEW_ADMIN
sudo llmctl admin set-gateway-password
```

`set-username`/`set-password` 修改所有适用的管理员入口；带 `portal` 或 `gateway` 的命令只修改指定入口。OmniRoute 原生界面只有管理员密码，没有用户名，因此 `set-gateway-username` 在 OmniRoute 模式会明确提示不适用，门户用户名仍可设为任意非空登录名，不要求是邮箱。门户/网关管理员密码只要求非空、不能全是数字，也不能含换行或空字符；普通注册用户密码仍要求 8–200 位且不能全是数字。

New API 和 OmniRoute 的维护调用令牌由各自数据库生成，所以 `key rotate` 不接受自定义值；LiteLLM 和 Bifrost 可选传入新值，Bifrost 值必须以 `sk-bf-` 开头。OmniRoute 普通用户自己的 key 应在账户门户内轮换，不受这个管理员命令影响。

### LLMCtl 账户门户

OmniRoute 自身没有完整的企业注册和易用计费流程。LLMCtl 因此部署独立的 Vue 3 企业门户与轻量 `llm-account.service`。门户通过 OmniRoute HTTP API 管理模型映射、Combo 和用户 key 权限，但普通 `/v1` 调用仍由 Nginx 直达 OmniRoute。两者使用同一状态根目录但绝不混表：

```text
/var/lib/llm-cluster/omniroute/gateway/storage.sqlite
/var/lib/llm-cluster/omniroute/portal/account-portal.db
```

第一项完全归 OmniRoute 管理；第二项保存门户用户/用户组、验证状态、会话、公开模型、授权、价格版本、金额流水、token 赠额、使用账本和门户审计。门户数据库不保存明文 API Key。用户验证邮箱时创建一把长期固定 Key；以后登录只通过受保护的 OmniRoute 管理接口读取同一把 Key，并暂存在当前浏览器会话，curl 示例也自动使用它。读取动作会审计但不会记录明文；只有用户主动点击“轮换”才会创建新 Key 并撤销旧 Key。门户定期读取 OmniRoute 调用日志，以请求 ID 幂等结算；进行结算和权限变更时先停用用户 Key，提交账本后再同步新权限，失败时保持关闭。

默认关闭公开注册。启用时必须配置精确邮箱域名白名单、公开门户地址和外部 SMTP。例如：

```bash
sudo bash install-llm-cluster.sh \
  --gateway omniroute \
  --registration enabled \
  --allowed-email-domains example.com,subsidiary.example.com \
  --account-public-url https://llm.example.com \
  --account-api-public-url https://llm-api.example.com \
  --account-admin-username llm-admin \
  --account-default-quota 1000000 \
  --account-quota-reset monthly \
  --smtp-host smtp.example.com \
  --smtp-port 587 \
  --smtp-security starttls \
  --smtp-username llm@example.com \
  --smtp-password APP_PASSWORD \
  --smtp-from llm@example.com
```

白名单按 `@` 后完整域名匹配；允许 `example.com` 不会自动允许 `evil-example.com` 或 `dept.example.com`。注册和验证两个阶段都会重新检查，注册页按后端当前配置显示为 `@example.com` 等允许邮箱后缀。门户管理员可在线配置并测试 SMTP、关闭/开启注册、调整白名单、新用户默认赠额和默认 API Key 活跃会话上限，也可按用户覆盖会话上限、禁用用户、分组、增减金额余额、发放通用或指定模型的额外 token。新用户默认上限为 `1`；`0` 表示不限制。该值直接同步到 OmniRoute 原生 `maxSessions`，按模型、系统提示、首条用户消息和工具等会话指纹限制活跃会话，闲置约 15 分钟后释放；它不是 HTTP 并发数，也不能绝对等同于真实人数。升级前已有用户保持 `0`，需由管理员按需改为 `1`。赠额支持 `daily`、`weekly`、`monthly` 及指定重置时间；后台会独立执行到期重置，即使额度耗尽导致 key 已关闭也能按时恢复。

管理员还可在“发布、注册与 SMTP”中单独保存门户品牌名称和公开访问源。公开访问源只能是无路径、无凭据、无 query/fragment 的 `http(s)` origin，例如 `https://llm.zjguardian.com`。配置后，它优先于安装期回退地址，用于验证邮件、页内链接、API Base、curl 示例、`llmctl account url`、`llmctl admin show`、`llmctl key show` 和 `llmctl info`；留空使用安装期回退地址。该字段仅是链接与显示元数据，不修改 Nginx、TLS、Cookie、登录回跳或当前访问方式。品牌名称为 1–40 个可见字符，用于页眉、登录页和浏览器标题。

模型管理页把公开 ID（例如 `gdn-inside`）通过 OmniRoute 原生 Combo mapping/model alias 映射到实际 `ornith-1.0-35b-fp8`。用户 key 只授权公开 ID，不授权底层模型或 Combo ID。管理员可为每个模型分别设置输入、输出、缓存读取和思考 token 的 `$/1M` 价格，并按“全体、多个用户组、多个指定用户”发布；token 赠额优先消耗，超出部分才扣金额余额。价格会按版本快照进账本，历史调用不会被新价格重算。

免费资源页读取 OmniRoute 的免费目录和已配置/当前可用排名。一个资源只有在“已发现、Provider 已配置、当前可用、门户真实请求测试成功、管理员明确发布”全部满足后才能开放。门户每 15 分钟复测已发布免费模型；连续三次失败会先关闭用户 key、撤销 OmniRoute 映射，再从授权目录中下线，避免免费上游消失后继续暴露失效模型。

```bash
sudo llmctl account status
sudo llmctl account url
sudo llmctl account restart
sudo llmctl logs account -f
```

门户公开地址是 `http://服务器IP:8000/ui/`，`8001` 只是回环内部监听。用户登录后可查看金额、token 赠额、逐请求用量和流水；模型广场只显示本人有效授权，提供价格/能力、可复制模型 ID、调用地址和 `curl` 示例。在线聊天窗口由浏览器直接调用公开 `/v1`，个人 key 只存在当前浏览器 sessionStorage，不经过门户后端。它支持图片、PDF、TXT、Markdown、CSV 和 JSON 附件：图片以 data URL 发给具备视觉能力的模型，PDF 最多取前 8 页在本地渲染为 JPEG，多余或超限文件会在发送前明确拒绝；附件不会保存进门户数据库。OCR/视觉/工具等标签来自安装时已核验或管理员确认的能力元数据，不改变 vLLM 的实际能力。

管理端“性能压测”由服务器上的 `llm_benchmark.py` 执行，页面不会自行创建并发请求。可选并发为 1–100 的预设档位，目标输入为 50–30K Token；执行器生成可复现且有语义的企业场景文本，通过公开模型 ID 发起真实流式请求，并以网关 `usage` 返回值作为实际 Token 统计。页面每两秒读取任务状态，展示成功率、总/成功 RPS、聚合输出 tok/s、单请求 tok/s、TTFT 与端到端延迟分位数、实际输入/输出 Token 和错误分类。每条结果还记录 OmniRoute 实际选择的 Worker；后台每秒运行一次固定的 `nvidia-smi` 查询，展示各卡平均/峰值利用率、活跃采样占比、峰值显存、平均/峰值功耗，以及同一采样时刻的峰值并行 GPU 数。这样可以区分“请求最终轮过所有卡”和“任何时刻只让两张卡工作”这两种完全不同的状态。采样不可用时压测继续运行并明确显示原因。并发不少于 20 或输入不少于 8K 时必须确认对在线用户和 GPU 的影响；同一时间只允许一个任务，API key 只通过进程环境传递，不写入命令行或结果文件。

OmniRoute 暂时不可用时，门户的本地管理页仍保持可登录，显示降级告警，并允许查看用户、SMTP、账本和审计；依赖网关的模型、Key、权限和实时对账操作会明确失败，不会伪装成功。`llmctl startup status` 会把这种状态标为 `degraded`，而完整启动验收仍要求门户 `/ready` 与 OmniRoute 一起恢复。

生产环境只需保护公开 `8000`，并在现有 Nginx/TLS 站点或上游负载均衡器终止 HTTPS；不要公开回环 `8001/18000/810x`。`--account-public-url` 可使用公开 origin 或其 `/ui` 路径，`--account-api-public-url` 使用无路径 origin。SMTP 密码和管理 key 位于 root-only 配置；当前门户 SQLite 也会保存运行期 SMTP 设置，因此应按敏感文件备份和保护，不要把凭据写入命令历史。

公网发布前执行以下安全清单：

1. 防火墙只允许外部进入 TLS 入口和所需的 Nginx 统一端口；用 `ss -ltnp` 确认账户门户、OmniRoute 和 Worker 仍只监听 `127.0.0.1`。
2. 使用受信任证书并强制用户从 HTTPS 域名访问；在门户管理端保存相同的“公开访问源”。LLMCtl 不自动申请证书，也不修改你的边缘端口映射。
3. 升级控制面后运行 `sudo llmctl nginx apply && sudo llmctl nginx test`。生成器先备份旧 LLMCtl 配置并执行 `nginx -t`，失败自动回滚；该配置覆盖客户端伪造的 `X-Forwarded-For`、限制登录/注册/验证请求，并增加 `nosniff`、Referrer 和 Permissions Policy 响应头。
4. `/base_ui/` 及其原生网关管理 API 仅供排障。公网域名应在最外层反向代理对该路径使用 VPN、IP ACL 或独立管理认证；普通用户只需要 `/ui/`、`/portal-api/` 和 `/v1/`。
5. 不要公开或复制 `/etc/llm-cluster/secrets.env`、两个 SQLite 文件和未脱敏的 `llmctl info`。工单中只使用 `sudo llmctl info --redact`。
6. 开启企业注册时保留精确邮箱后缀白名单和 SMTP 验证；定期检查门户审计、失败登录和 Nginx 429 日志，并备份两个 SQLite 文件。

## OpenAI 兼容 API

先获取参数：

```bash
sudo llmctl key show
```

文本请求：

```bash
curl http://服务器IP:8000/v1/chat/completions \
  -H 'Authorization: Bearer YOUR_KEY' \
  -H 'Accept: application/json' \
  -H 'Content-Type: application/json' \
  -d '{
    "model":"安装摘要中的模型名",
    "messages":[{"role":"user","content":"你好"}],
    "max_tokens":256,
    "stream":false
  }'
```

同步 JSON 客户端应显式发送 `"stream":false`。OmniRoute 为兼容旧客户端，在省略该字段且请求没有声明只接受 JSON 时可能返回 SSE；流式客户端则应显式使用 `"stream":true` 并逐条解析 `data:` 事件。

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

安装器选择语言后立即直连 `https://huggingface.co/api/models?limit=1` 做国际网络预检，早于网关和模型选择。失败时询问代理并在填写后复测；拒绝时明确提示 HF 候选可能缺失。`--yes`/`--non-interactive` 下不会等待输入，必须显式添加 `--proxy http://IP:PORT`。安装后的 `llmctl models search --source all|huggingface` 也会先验证直连或已保存代理；只搜 ModelScope、健康检查和离线推理不会弹出该问题。

随后安装向导做只读体检，再搜索模型。体检包括操作系统/架构、CPU/核/线程、内存/Swap、GPU/显存/驱动/计算能力、PCIe 当前与最大链路、GPU/NUMA/NVLink 拓扑和模型盘空间。PCIe/拓扑是能力快照，不是主动 NCCL 带宽测试。

目录会排除 Apple MLX 等平台专用转换权重；`mlx-community/*` 面向 MLX/Apple Silicon，不能作为 NVIDIA CUDA/vLLM 权重。选择候选后会展开显存预算、TP 链路、主机内存、磁盘、启动并行度和逐项推荐理由。此时可确认、返回候选列表或重新搜索。

ModelScope 下载器固定安装在独立虚拟环境，实际命令为 `/opt/llm-cluster/hub-venv/bin/ms`。安装器会在下载大权重前验证 `ms download --help`；`.partial` 目录用于断点续传，下载错误时不要手工删除。

若所选模型已存在，只有 Hub、模型 ID、revision、配置架构、完整权重和体积全部匹配时才跳过下载，且不会复制权重。保留的 `/data/ornith/models` 可被自动识别；不同来源或模型 ID 即使名称相似也不会混用。

切换到不同模型：回到项目目录，重新运行安装器。

```bash
sudo bash install-llm-cluster.sh --force-reconfigure
```

这样会重新检查权重、架构、TP、上下文和能力解析器。直接 `llmctl download` 只允许重新核验/补齐当前模型，不允许绕过规划切换模型。

## 镜像维护、代理和离线包

升级 LLMCtl 控制面本身（不会重新安装或重启 Worker）：

```bash
sudo llmctl upgrade
```

命令会询问是否从 GitHub 获取 `main` 最新提交。它同时预检 GitHub API 和 ZIP 归档下载站点；预检后真实下载仍可能失败，此时会自动检查已保存维护代理，仍不可用就询问新的代理并重试。升级内容限于 `llmctl` 与 `/usr/local/lib/llm-cluster/` 下由升级清单声明的控制面程序；当前模型、Worker、网关运行数据、配置、密钥、数据库和 Nginx 均保留。账户门户正在运行时会短暂停止并验收，失败自动从 `/var/backups/llmctl/` 回滚。

升级不会自动重写 Nginx。需要应用新版公网安全配置时再执行 `sudo llmctl nginx apply`；它只更新 LLMCtl 自己的 `/etc/nginx/conf.d/llm-cluster.conf`，不会重启 GPU Worker。

```bash
sudo llmctl upgrade --proxy http://192.168.9.104:1082 --save-proxy
sudo llmctl upgrade --from-zip /root/LLMCtl-main.zip
sudo llmctl upgrade --check
```

`llmctl update` 与 `llmctl upgrade` 含义不同：前者显式更新容器镜像，后者只更新 LLMCtl 控制面程序。镜像维护命令如下：

```bash
sudo llmctl proxy set 10.1.0.6 7890 http
sudo llmctl update --vllm-image vllm/vllm-openai:v0.22.1
sudo llmctl update --gateway-image calciumion/new-api:v1.0.0-rc.22
sudo llmctl proxy clear
```

安装器默认使用已发布的 OmniRoute `diegosouzapw/omniroute:3.8.48`。若镜像标签不存在或仓库不可达，安装器会保留 Docker 的原始错误并给出对应的 `--*-image` 覆盖参数，不会只报告一个脚本行号。例如可显式指定：

```bash
sudo ./install-llm-cluster.sh --omniroute-image diegosouzapw/omniroute:3.8.48
```

导出/导入：

```bash
sudo llmctl offline export /data/offline/llm-bundle
sudo llmctl offline import /data/offline/llm-bundle
```

离线包包含当前选择的接入层类型与镜像；不能在 New API、LiteLLM、Bifrost、OmniRoute 规划之间混用。OmniRoute 包不需要 PostgreSQL 镜像。

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
- 接入层不健康但 Worker 健康：检查 `llm-router` 和 `llm-database` 日志。New API 自动配置失败时，Router 日志之后还会在 `llm-cluster.service` 日志中显示具体的 setup/login/channel/token 错误。
- OmniRoute 健康但门户不可用：运行 `llmctl account status`、`llmctl logs account`。邮件收不到时重点检查 SMTP 主机、TLS 模式、发件地址和垃圾邮件策略；门户不会绕过邮箱验证直接发 key。
- 同一图片或 PDF 请求直连 Worker 很快、经 OmniRoute 却稳定多出约 30 秒，而纯文本正常：这是 OmniRoute Vision Bridge 把自定义 Provider 的原生视觉 Combo 误判为未知能力后，等待默认 `openai/gpt-4o-mini` 视觉描述调用超时。升级 LLMCtl 后运行 `sudo llmctl router reconcile`；它会在确认本地模型原生支持图片时关闭这条冗余桥接，并同步受管连接/Combo 的并发设置。该操作在线生效，不重启 Router 或 GPU Worker。随后用原图片请求复测，首字节不应再固定在约 30 秒。

## 原地升级门户与控制面

```bash
sudo llmctl upgrade
```

确认后，LLMCtl 从 GitHub 获取最新项目内容；API 预检或实际 ZIP 下载任一步失败都会引导配置临时或持久代理并重试。升级会更新 `llmctl`、安装维护脚本、账户门户后端和 Vue 静态资源，并对门户 SQLite 执行向后兼容的原地迁移。现有 vLLM Worker、模型目录、路由组合、用户和账本不会被重装。

升级完成后建议执行：

```bash
sudo llmctl health
sudo llmctl info
```

模型编辑页面的“重新读取”用于从当前 AI 接入层刷新上下文和最大输出；只有管理员实际修改相应字段时才会写回底层目标。参数同步状态会显示为 `read`、`synced`、`partial` 或 `failed`。

## 卸载

默认保留模型、接入层 PostgreSQL/SQLite 本地状态和 root-only 恢复凭据：

```bash
sudo llmctl uninstall
```

卸载会并发停止 Router、数据库和全部 Worker，并每 5 秒显示仍活动的 systemd 单元、容器和 GPU 显存。正常停止总等待上限为 180 秒；超时后只对 `llm-*` 命名的本集群单元/容器执行一次有界强制停止，仍失败则保留配置并明确退出，不会边运行边删除。删除包含大量小文件的编译缓存、以及显式 `--purge-model` 的模型目录时也会持续输出心跳，不会再长时间静默。

可选永久删除：

```bash
sudo llmctl uninstall --purge-model --purge-images --purge-database
```

`--purge-model` 只有在模型目录存在安装器标记时才允许执行；`--purge-database` 会永久删除 Web UI 数据、虚拟 key、New API/Bifrost 状态，以及 OmniRoute 的两个独立 SQLite 文件和账户审计。只想保留现有权重时不要使用 `--purge-model`。

备份 OmniRoute 时应同时备份 `gateway/storage.sqlite` 和 `portal/account-portal.db`。最简单且一致的方式是短暂停止门户和网关，复制两个文件后再启动；只备份其中一个可能导致门户用户与 OmniRoute key/限额无法对应。
