# 外部 API access JWT 映射

R12 为固定身份提供方的短期 access JWT 增加独立入口，把经过验签的身份映射到一个已经存在的普通模型 Key。模型权限、钱包、价格、模型额度、RPM/TPM 和资源池继续使用这个 Key 的现有规则。它不创建账号或 Key，不从外部 email、角色或组推导权限，也不修改原生商业授权判断。

主本地站点已接入代码，功能默认关闭；真实身份提供方尚未接入。隔离验收使用两个 worker、合成身份、本地 RSA 签名服务和固定模型上游，不代表生产或 20,000 RPM 容量认证。

## 接入范围

客户端使用明确的网关凭据前缀：`Authorization: Bearer rt-jwt.<access JWT>`。支持现有原生凭据 header；多个凭据、token Cookie、query 凭据和禁用回调 header 会被拒绝。SDK 可设置 `api_key="rt-jwt." + access_token`。这是一层网关适配，不宣称普通无前缀 OAuth Bearer 已兼容。原生裸 JWT、原生 SSO 和其他商业认证保持原有路径。

仅支持 POST `/chat/completions`、`/v1/chat/completions`、`/messages`、`/v1/messages`、`/responses`、`/v1/responses`。管理、钱包、Key 列表、模型目录、MCP、BYOK、blob、异步 batch、realtime 和 WebSocket 不在本批范围。保留前缀在功能关闭、配置错误或依赖故障时仍拒绝，不回落到其他认证。

映射目标必须是当前 customer 或 internal_user 的普通模型 Key。管理员、只读角色、控制台会话、服务账号及带 team/org/project/object-permission/agent 关联的目标被拒绝。历史 `key_type=null` 仅在没有额外管理路由权限时兼容；普通 `llm_api` 同样不能夹带管理授权。还会排除当前自定义管理员和本站主密钥。首版最多 128 条显式绑定。

## 身份提供方合同

固定 issuer、audience、client_id 与 JWKS URI；RS256；`typ` 必须是 `at+jwt` 或 `application/at+jwt`。必需声明为 `iss`、`sub`、`aud`、`exp`、`iat`、`client_id`、`jti`、`scope`，以及本模块要求的自定义 `gateway_revision` UUID。scope 默认需要 `models:invoke`。最大有效跨度默认 300 秒，可配置 60–900 秒；拒绝未来 iat，检查 nbf。重复 JSON 字段、错误类型和不支持的 JOSE 扩展被拒绝。

access token 与 ID token 分开验证，参照 [RFC 9068 access JWT 要求](https://www.rfc-editor.org/rfc/rfc9068.html) 和 [RFC 8725 token 类型隔离](https://www.rfc-editor.org/rfc/rfc8725.html)。`gateway_revision` 和 `rt-jwt.` 是本模块额外合同，需要签发方明确支持，不属于这两个标准的通用字段或传输要求。不能直接把登录用的 ID token 当模型凭据。

`jti` 只用于校验，不作为每次模型请求的幂等键。同一个 token 可以重复调用；多个 token 映射到同一 Key 时仍共享该 Key 的计费与限制身份。外部 role、email、groups 和额外声明不会写入 `UserAPIKeyAuth.jwt_claims`。

## 服务端配置

先创建普通账号、钱包与模型 Key，配置好模型权限、资源池和额度；再由受控运维写入服务端 JSON。以下全部是占位符，不包含可用凭据：

```json
{
  "namespace": "company-api",
  "generation": 1,
  "issuer": "https://identity.example.invalid/tenant",
  "audience": "gateway-model-api",
  "client_id": "registered-api-client",
  "jwks_uri": "https://identity.example.invalid/tenant/jwks",
  "required_scope": "models:invoke",
  "max_token_seconds": 300,
  "bindings": [{
    "subject": "explicit-external-subject",
    "user_id": "existing-local-user",
    "token_hash": "<64-hex-existing-key-hash>",
    "binding_revision": "<new-unique-UUID>",
    "enabled": true
  }]
}
```

启用项为 `RETAIL_API_JWT_ENABLED=true` 与 `RETAIL_API_JWT_CONFIG=/server/private/api-jwt.json`。同时需要开启 operation audit、native audit、`RETAIL_BILLING_ENABLED`、`RETAIL_REQUIRE_COORDINATION_REDIS`，以及健康的协调 Redis、原生 writer、非空主密钥和实际安装的 RetailBilling 回调。只设置环境开关不等于已安装计费回调。

首版拒绝与原生 custom auth、原生 JWT/OAuth、read replica 或自定义凭据 header 配置混用。需要这些部署方式时单独扩展并验收，不能通过改开关绕过兼容性检查。OIDC 控制台登录与此入口独立；启用其中一个不会隐含启用另一个。

配置文件上限 128 KiB，只挂载到服务端，不放入静态网站、普通账号 API 或团队知识库。文件含明确的外部 subject 与内部引用；数据库绑定保存 subject 的 SHA256，原始 subject 不传入数据库 driver。不能因此把权威表当作普通诊断或客户可读数据。`allow_loopback_http=true` 只允许 issuer/JWKS 都在 loopback 的隔离测试，生产固定 HTTPS，禁止重定向。

## 撤销、滚动更新与回滚

原生 writer 保存 `retail_api_jwt_settings`、`retail_api_jwt_bindings`、`retail_api_jwt_revisions`。每次 JWT 调用用一个新的关联读取核对当前配置、绑定、Key 和账号，不信任 Key 或用户缓存，也不跟随 deprecated Key 的轮换宽限映射。删除、过期或封禁目标立即使下一次接纳失败；角色变成管理员/只读或要求重置时同样失败。已经接纳的请求不会被追溯取消。

**临时 Key block/unblock 是暂停和恢复**：解封后仍未过期且绑定未变化的 JWT 可继续使用。它沿用普通 Key 语义，不能当作永久 token 撤销。需要永久撤销旧外部 token 时，提高 generation 并为变更绑定生成全新的 revision；签发方随后签发带新 revision 的 token。停用后重新启用绑定也必须使用新 revision。当前角色/重置状态恢复不等于永久撤销标志。

配置只在启动读取一次；变更提高 generation 并滚动重启全部 worker。同代际不同摘要、旧代际不能覆盖当前权威；切换 namespace 同样必须提高 generation 且不能复用 revision。旧 worker 在数据库代际更新后拒绝旧配置，不会继续放行。每个 revision 在全局不可变台账只使用一次，旧 UUID 不能通过后续配置复活；台账最多 10,000，达到上限后配置失败关闭，不自行删除历史台账绕过上限。

全量升级认识保留前缀的 API 与桥接 worker 后才启用。关闭功能并重启会拒绝新 JWT 调用，普通 Key 保留。回滚保留权威表、revision 台账与审计；不要删除表或回滚到不认识此前缀的旧认证代码后继续接收同类凭据。

配置与 `native_api_identity.configured` 审计意图在同一个原生 writer 事务中提交；outbox 满或审计失败则配置回滚。事件只包含受影响数量、操作者和随机操作 ID，不含 subject、JWT、Key hash 或声明。旧三动作和五动作约束都迁移为六动作，不改写既有事件。相关运维边界见 [原生事务审计](native-audit.md)。

## 性能与故障边界

普通 Key 不增加 R12 数据库查询或身份提供方 RPC；JWT 热验签只使用本地公钥，但每次请求有新的原生权限读取。跨 worker JWKS 快照复用独立 Redis，硬 TTL 300 秒、240 秒后刷新，固定地址响应最多 64 KiB、最多 16 个 RSA 2048–4096 公钥。未知 kid 受本机合并、共享租约和 30 秒冷却约束，请求最多等待 0.5 秒恢复；没有新鲜公钥则拒绝。相同 kid 换公钥等待正常刷新，不因任意坏签名无限强刷。

每 worker 独立 Redis 最多四连接、HTTP 最多两连接，不新建原生 PG 池。Redis 连接/读取各一秒；提供方总期限五秒。原生 fresh 读取最多四个并行槽，含排队两秒；配置含排队八秒。可选初始化在后台进行，配置或 IdP 不可用不阻塞普通 Key 启动和结算。公钥尚在硬 TTL 内时 IdP 离线不影响热验签；Redis 协调或当前权威不可确认时 JWT 拒绝，不能沿用旧允许结果。

凭据中间件位于追踪和协调检查之前，移除所有凭据 header 后才进入内部调用；不把 JWT、其摘要、subject、jti 或原始声明放入上游、请求追踪或诊断表。错误只返回通用认证失败/不可用，管理诊断与客户投影仍分别执行服务端权限。完整容量、真实 IdP 互通、多 issuer、OAuth 签发/refresh/introspection、SCIM 和配置 UI 均未由本批完成。
