# 服务端托管模型密钥

ID-SECRET 首版使用独立的 Vault KV v2 只读适配器。服务端固定配置引用，后台预热和刷新有界内存缓存，推理请求只读本地快照。本地 main 未配置真实 Vault，开关默认关闭；不替代原生商业 Secret Manager、团队配置页、KMS、自动登录或凭据分发能力

## 首版支持范围

当前仅支持走 Router 的 OpenAI Chat Completions 部署，模型参数必须使用 `openai/` 前缀及服务端固定 `api_base`，包括同步 `completion`、异步 `acompletion` 和显式选择部署的对应调用。密钥在每次选中部署、重试或 fallback 的最后参数副本中注入，共享部署配置保留 `retail-secret/<ref>`，不被真实值改写

托管部署绕过预先缓存的 SDK client，避免轮换后继续复用旧密钥；客户端不能自行传入 SDK client。Responses、embedding、图片、音频、rerank 等非 Chat 调用以及非 OpenAI provider 在首版直接拒绝，不尝试把托管引用发送给上游

## 服务端配置示例

环境项如下，JSON 文件必须在每个 worker 容器内可读。示例域名、路径、token 和模型均为不可用占位符，不能直接用于业务

```yaml
environment:
  RETAIL_MANAGED_SECRETS_ENABLED: "true"
  RETAIL_MANAGED_SECRETS_CONFIG: /run/gateway/managed-secrets.example.json
  RETAIL_VAULT_TOKEN: REPLACE_WITH_SERVER_SIDE_VAULT_TOKEN
volumes:
  - /etc/gateway/managed-secrets.example.json:/run/gateway/managed-secrets.example.json:ro
```

`RETAIL_MANAGED_SECRETS_CONFIG` 是 JSON 文件路径，不是 JSON 正文或 token。真实 token 仅通过服务器环境或部署秘密注入，不写入配置仓库、知识库、浏览器和模型参数。可用 `token_env` 改为另一个合法环境变量名；没有该值时拒绝启动适配器，不降级使用模型环境密钥

启用后，JSON 或认证环境缺失、非法会使启动校验失败，应先核对配置再重启全部 worker。Vault 暂时不可达则由后台有界重试，冷缓存期间对应模型返回不可用

JSON 文件内容：

```json
{
  "endpoint": "https://vault.example.invalid",
  "token_env": "RETAIL_VAULT_TOKEN",
  "mount": "secret",
  "refs": [
    {"ref": "example_chat", "path": "models/example-chat", "field": "api_key"}
  ],
  "ttl_seconds": 60,
  "refresh_seconds": 15,
  "timeout_seconds": 2,
  "retries": 1,
  "concurrency": 4,
  "max_response_bytes": 65536,
  "allow_loopback_http": false
}
```

模型配置仅保存引用及固定目标：

```yaml
model_list:
  - model_name: managed-chat-example
    litellm_params:
      model: openai/REPLACE_WITH_SUPPORTED_CHAT_MODEL
      api_base: https://models.example.invalid/v1
      api_key: retail-secret/example_chat
```

`refs` 是服务器白名单，`ref` 必须唯一，客户端不能提交 Vault 路径。引用名及字段名采用字母开头的受控名称，最多 96 个字符；路径只能由允许的非空片段组成，不接受 URL、查询参数或目录穿越。可选 `namespace` 同样由服务器配置。`endpoint` 只接受根 HTTPS 地址，不允许 URL 用户信息、查询参数和片段，不跟随重定向、不读取系统代理环境，也没有关闭证书校验的选项

适配器读取 [Vault KV v2 官方 HTTP 协议](https://developer.hashicorp.com/vault/api-docs/secret/kv/kv-v2#read-secret-version) 的 `GET /v1/{mount}/data/{path}`，通过服务器的 `X-Vault-Token` 和可选 `X-Vault-Namespace` 认证，读取 `data.data[field]` 及版本、删除和销毁元数据。没有版本查询参数时读取当前版本；本实现不执行 Vault 写入、token 登录、续租或动态秘密签发

隔离测试可显式设置 `allow_loopback_http=true`，仅对 localhost、127.0.0.1 或 ::1 放行 HTTP，不把该选项用于普通内网或公网地址

## 缓存、刷新与故障

每个 worker 启动后台预热并周期刷新，不在启动时等待全部引用成功，也不让请求临时访问 Vault。冷缓存、未配置引用或尚未完成首次刷新时，请求返回通用不可用，之后由后台继续尝试

| 边界 | 默认与限制 |
| --- | --- |
| 配置文件 | 最多 128 KiB，未知字段拒绝 |
| 引用 | 1 至 128 项固定白名单 |
| 硬 TTL | 默认 60 秒，允许 1 至 3600 秒 |
| 刷新间隔 | 默认 15 秒，且不超过 TTL 的一半 |
| 单次读取 | 默认 2 秒，允许 0.1 至 10 秒 |
| 重试 | 默认额外一次，最多额外两次，有界短退避 |
| 并发 | 默认四条、最多八条 provider 连接 |
| 响应体 | 默认 64 KiB，最大 1 MiB |

配置校验还要求整批引用的最坏读取与刷新预算能放进 TTL，不能无限增加引用或排队。引用值需要是非空、长度不超过 8192 的可打印 ASCII 字符串，版本必须有效；缓存不接受低于该 worker 已见版本的倒退值

超时、连接失败、429 或 5xx 可暂时保留尚未到期的旧快照，但不会延长旧值 TTL。TTL 到期后请求不可继续读取旧值。观察到 401、403、404、410、已删除或已销毁版本时立即删除对应缓存值；无效响应也不作为可用值。Vault 恢复并重新读到有效版本后才能恢复服务，关闭适配器会清空缓存并关闭 provider 客户端

撤销可见时间取决于后台下一次观察或硬 TTL，不承诺即时推送到所有 worker。已选取凭据的在途尝试使用读取时快照，后续路由检查或上下文压缩可能延迟实际发送；快照不会因后台撤销而追溯取消，已经发到上游的请求也不会撤回。变更 Vault 认证 token、JSON 白名单或目标后须重启全部相关 worker，避免进程继续使用旧的服务器配置

## 请求边界与隐私

托管部署的密钥和目标由服务器确定。请求不能更换 `api_key`、`api_base`、`base_url`、provider、命名凭据、API 版本，也不能提交 `client`、`headers`、`extra_headers` 或 `default_headers` 来覆盖路由和授权。拒绝可能发生在原生认证或托管路由检查，不能依赖所有拒绝都返回同一个 4xx；拒绝请求不会因此读取 Vault 或转发到指定的新目标

实际密钥仅注入本次调用的参数副本，provider 的 Vault 请求与响应正文不进入适配器诊断；Vault I/O 的 httpx/httpcore 日志在该上下文中抑制。普通客户错误使用通用消息，不显示 Vault endpoint、路径、namespace、版本、token、响应正文或堆栈。客户请求响应继续使用既有脱敏与响应头投影，不以开放自定义授权 header 的方式支持托管密钥

不得把真实密钥复制到请求 metadata、业务标签或网页表单。Secret Manager 的服务端 token 与模型上游密钥是两种凭据，不能把 Vault token 当成模型 API Key

## 关闭、恢复与后续范围

`RETAIL_MANAGED_SECRETS_ENABLED` 默认 false。关闭或配置缺失时，仍带 `retail-secret/` 前缀的部署不可调用，不能把引用当成普通密钥发给上游，也不回退到 `OPENAI_API_KEY`、同名环境变量或其他凭据来源。`retail-secret/` 不是原生 `os.environ/` 解析语法

回滚时先停用相应托管部署或明确恢复经过审核的普通凭据配置，再关闭开关并重启全部 worker。恢复托管服务需重新完成配置校验与后台预热；没有用旧缓存跨重启兜底的行为。服务器 JSON 和环境应保留安全访问权限，不因为开关关闭而将 token 写入公共文档

团队级秘密配置 UI、其他 Vault 引擎、AppRole/OIDC/自动续租、其他云 Secret Manager、KMS 和非 Chat provider 适配仍属后续任务。ID-SECRET 仅按本页范围跟踪，不把原生许可检查改成通过，也不宣称已接通真实企业 Vault
