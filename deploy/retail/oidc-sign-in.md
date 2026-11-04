# 自有 OIDC 控制台登录

首版把一个固定身份提供方接入已有本站账号，使用授权码、nonce、S256 PKCE 和 RS256 签名验证。它是独立实现，不修改原生许可判断，也不通过外部 email、组或角色自动创建账号、绑定账号或授予权限。真实身份提供方尚未接入，主本地站点默认关闭；验收只使用隔离数据库、本地签名 IdP 和合成身份。

## 账号、入口与配置

先在本站建立账号并决定其角色，再由受控运维在服务端配置明确的 issuer + subject → user_id 绑定。支持本站现有 proxy_admin、proxy_admin_viewer、internal_user、internal_user_viewer、customer；不产生 Team Admin、Org Admin、钱包权限或模型授权。首版一个 issuer，最多 128 条绑定，会话 300–3600 秒，不宣称已经具备无限规模身份管理。

新入口为登录页的 Organization sign in。未启用时不显示；配置暂不可用时显示通用提示，密码登录继续保留。原生已经配置的 SSO、其许可、自动跳转和控制平面跨站流程仍保留。新入口支持共享协调 Redis 的同站多 worker，不支持跨网关站点或跨站控制平面跳转。

配置文件只在服务端挂载，包含外部身份映射，不放入前端静态目录或团队知识库。客户端密钥从环境读取，不写进配置文件、URL、日志或审计事件。

```json
{
  "namespace": "company-login",
  "generation": 1,
  "issuer": "https://identity.example.invalid/tenant",
  "client_id": "registered-client-id",
  "client_auth_method": "client_secret_basic",
  "client_secret_env": "RETAIL_OIDC_CLIENT_SECRET",
  "public_origin": "https://gateway.example.invalid",
  "endpoint_origins": [],
  "label": "Organization sign in",
  "session_seconds": 1800,
  "bindings": [{"subject": "provider-subject", "user_id": "existing-local-user", "enabled": true}]
}
```

开关 `RETAIL_OIDC_SSO_ENABLED=true`，路径 `RETAIL_OIDC_SSO_CONFIG`，默认密钥环境变量 `RETAIL_OIDC_CLIENT_SECRET`。同时需要 operation audit、native audit、`RETAIL_REQUIRE_COORDINATION_REDIS=true`、可用的协调 Redis、原生 writer 和非空本站签名主密钥。Compose 默认全部 OIDC 配置为空且关闭；自定义密钥环境变量需要自行显式传入容器。

IdP 注册的回调必须精确为 `public_origin + /retail/auth/oidc/callback`，响应模式 query，scope openid，code flow、S256，token endpoint 使用显式 basic 或 post 客户端认证。HTTPS 必需；`allow_loopback_http=true` 仅用于所有端点都在 loopback 的隔离验收。

反向代理应把正确的公开 Host 和 scheme 传给应用，并只信任受控代理来源的转发头；不要通过信任任意客户端 X-Forwarded-* 修复配置。请求 origin 与固定公开 origin 不符时拒绝签发，标准显式/隐式 443 或 80 按同一 origin 处理，非默认端口仍严格匹配。

Discovery 的 issuer 必须精确匹配；授权、token、JWKS 地址只允许配置中的固定 HTTPS origin，总计最多四个，不追随重定向。若服务商把这些端点拆在不同域名，显式加入 `endpoint_origins`，不要通配。PKCE 元数据省略时仍强制发送 S256；显式声明不支持 S256 则拒绝。2026-11-04 核对的 [Google Discovery](https://accounts.google.com/.well-known/openid-configuration) 展示分离的端点 origin，[Microsoft Discovery](https://login.microsoftonline.com/common/v2.0/.well-known/openid-configuration) 含租户 issuer 模板；接入后者须使用具体租户的固定 issuer。核对公开元数据不等于已完成真实企业租户互通验收。

## 协议与会话边界

`GET /retail/auth/oidc/status` 仅公开 enabled、available、label，不输出 issuer、client_id 或绑定。start 保存 300 秒的有界 Redis 流程，callback 必须同时匹配 state 和浏览器 Cookie；不匹配 Cookie 不消耗有效流程，匹配后原子消费一次。重复参数、未知参数、超限字段和异常回调统一失败，不反射提供方错误说明。成功后 303 跳转到事先保存并重新校验的同站 `/ui` 路径，URL 不含本站 JWT、原生 Key 或 IdP token。

生产 HTTPS 流程 Cookie 使用受保护的 `__Host-` 名称、Secure、HttpOnly、SameSite=Lax、Path=/，不设 Domain；隔离 loopback HTTP 使用普通测试名称。重复同名 Cookie 被拒绝。失败回调不清除已有本站登录 Cookie，也不凭随机 state 取消另一条有效流程。响应设置 private/no-store、no-cache 和 no-referrer；OIDC access log 去掉整个 query，httpx/httpcore 的本流程敏感日志单独屏蔽。

原生 Key、会话证明和签入审计意图在同一个原生 writer 事务内提交，JWT 签名在提交前完成。原生控制台需要 JavaScript 读取 `token` Cookie 来设置 Authorization，因此这个兼容 Cookie **不是 HttpOnly**；有效期绑定原生会话到期，Secure 取固定配置的公开 origin。这个事实不等于能防止站点自身的 XSS，站点内容安全边界仍需维持。

凭据前缀 `sk-retail-oidc-ui-` 保留给本站签入会话。它只用于管理与个人控制台，拒绝 LLM、MCP、BYOK、实验性 blob 与 realtime。客户进行模型调用仍使用普通 Virtual Key。普通 Key 只增加本地识别分支，不增加 OIDC 数据库查询或外部 RPC；每个 OIDC 控制台请求需要新的原生权限读取。

## 撤销、配置代际与回滚

原生 `retail_oidc_settings`、`retail_oidc_bindings`、`retail_oidc_sessions` 保存权威代际、绑定 revision 和会话证明。会话有效性逐次关联当前用户、Key、绑定与设置；外部角色声明不会覆盖本站角色。原生用户表没有 blocked 字段，不宣称读取不存在的账号停用状态。

绑定停用再启用、Key block 再 unblock、缩短到期后延长、删除 Key、修改密码、强制重置或角色变化，均不能恢复旧会话证明。已开始执行的请求和已下载的数据不能收回。会话的身份、元数据和管理路由范围受数据库及 API 保护，不能用普通 Key 更新或 regenerate 把它变成推理凭据。

配置变更提高 generation 并重启全部 worker。同代际不同摘要、旧代际或其他 namespace 不能覆盖当前权威；同 subject 不自动改绑到另一个 user_id。若需迁移账号，先制定明确的人工迁移流程，不改文件绕过冲突。Redis 状态丢失后后台可恢复相同权威，但原生数据库仍决定最终可签发身份。

关闭功能标志会停止新登录，已签发会话仍执行当前权威检查直到撤销或到期；关闭开关本身不是全体注销。紧急撤销需要在仍具备新 guard 的全部 worker 上更新权威代际或受控停用绑定，并验证旧会话失效。先全量升级 worker，再启用；不能回滚到不认识保留会话前缀和证明校验的旧二进制后继续接受这些会话。保留权威表、触发器和审计表，不以删表清数据作回滚。

## 连接与故障预算

每 worker 为 OIDC 分配独立、最多四条连接的 Redis 池及最多四条连接的 HTTP 池；首版要求独立普通 Redis，未验证 Sentinel、Cluster 或云 IAM 连接方式。沿用 REDIS_URL 或 REDIS_HOST/PORT/USERNAME/PASSWORD/DB/SSL；URL query 只允许 db 和受限 TLS 字段，拒绝重复字段及扩大连接数、超时、重试等参数。Redis 连接和读写分别限一秒，提供方请求总期限五秒。不新建原生 PG 池。原生身份 IO 最多两个并行槽，事务与锁等待有界；这些是额外控制面容量，不能称零连接成本。

Discovery/JWKS 跨 worker 共享，快照硬 TTL 300 秒，240 秒后尝试刷新，刷新租约与 30 秒冷却防止未知 kid 放大请求。相同 kid 换公钥不会因无效签名无条件强刷，待正常刷新周期更新。流程分配按 namespace 每分钟最多 120，协议兑换段并行四个、含排队总期限 12 秒；成功后原生签发另有八秒事务与排队上限，HTTP 回调可接近二十秒再加响应开销。首版并非无限登录吞吐。可选登录初始化在后台运行，异常不阻塞普通认证、推理和结算。IdP/Redis/数据库故障使新登录失败关闭；控制台状态可能短暂滞后，实际签发事务仍检查权威。

共享 Redis 正常运行时 compare-and-consume 保证同流程只有一个回调进行兑换。这里不承诺 Redis 灾难恢复、异步副本故障切换或人工恢复旧数据后的永久消费记忆；IdP 必须独立执行授权码单次使用。Redis 不是原生会话权限的最终权威。

审计新增 `native_identity.configured` 和 `native_identity.signed_in`，只记录操作者、动作、受影响数量及随机操作 ID。原生 outbox 仍遵循 [原生审计](native-audit.md) 的容量和恢复规则，队列满则事务回滚，不能出现无审计的成功签入。并不因此覆盖所有拒绝尝试、全部原生管理操作、API JWT、SCIM、SAML、多 issuer 或自动账号生命周期。

会话证明总量最多 10,000、单账号有效会话最多 128；每次签入清理最多 128 条过期证明。删除证明不会自动删除其指向的原生 Key。生产启用 OIDC 时同时启用现有 `LITELLM_EXPIRED_UI_SESSION_KEY_CLEANUP_ENABLED=true`，按流量设置 interval 和 batch（Compose 提供 60 秒、1000 条配置默认值，但清理开关仍默认关闭）。原生任务按 dashboard team 和到期时间清理，也覆盖本模块签发的 Key；不开任务则过期原生 Key 行会保留，会话到期拒绝访问和数据保留是两件事。不新增第二套清理线程或 PG 池。
