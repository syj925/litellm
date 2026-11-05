# 自有 SCIM Users 同步

R13 接收企业目录的用户创建、查询、修改、停用、恢复与删除，将其同步为全新的普通 customer 账号。目录不能按邮箱接管既有客户，不能创建管理员、Key、钱包、充值、模型授权或组织成员关系。现有原生商业 SCIM 路由和许可判断保持原样

这是固定来源的 Users 协议子集，主本地站点默认关闭，尚未接入真实企业目录。Groups、企业扩展、角色授权、密码同步、多来源、多租户及目录产品互通认证仍待后续验收。协议参照 [RFC 7643](https://www.rfc-editor.org/rfc/rfc7643.html) 和 [RFC 7644](https://www.rfc-editor.org/rfc/rfc7644.html)；[RFC 9865 游标分页](https://www.rfc-editor.org/rfc/rfc9865.html) 与 [RFC 9967 异步事件](https://www.rfc-editor.org/rfc/rfc9967.html) 属于本批未启用的扩展

## 配置与接入

服务端 JSON 示例不包含可用凭据：

```json
{
  "namespace": "company-directory",
  "generation": 1,
  "public_origin": "https://gateway.example.invalid",
  "max_users": 1000
}
```

环境项为 `RETAIL_SCIM_ENABLED=true`、`RETAIL_SCIM_CONFIG=/server/private/scim.json` 和 `RETAIL_SCIM_TOKEN`。完整令牌由 `rt-scim.` 前缀和 32–256 个 URL-safe 随机字符组成，只放在受控服务端环境和目录客户端的凭据配置，不写进 JSON、网页、日志、知识库或 URL

`RETAIL_SCIM_CONFIG` 是网关进程可读的路径。Docker 部署需另行将 JSON 文件只读挂载到容器内，再填写容器路径；本仓 compose override 只转发环境值，不自动挂载宿主机上的配置文件

客户端 Base URL 为 `public_origin + /retail/scim/v2`，使用 `Authorization: Bearer <完整目录令牌>`。它是独立的 opaque bearer，不能拿本站管理员 Key、控制台会话、API JWT 或其他原生凭据代替。这个令牌也不能访问钱包、模型或其他管理接口

生产要求固定 HTTPS origin，以及 operation audit、native audit、零售计费开关、实际 RetailBilling 回调、非空主密钥、标准原生 Key 认证、writer 和健康的必需协调 Redis。首版不支持 native custom auth/JWT/OAuth、read replica、自定义凭据 header、非根 `SERVER_ROOT_PATH(S)` 部署。反向代理应正确传递受信的 Host/scheme，不能通过接受任意转发头解决 origin 错误

没有主密钥的匿名开发模式本来就允许绕过整个原生 Key 身份解析；它不在受管身份保证范围内。关闭 SCIM 接收开关或移除其配置路径，仍要求保留上述安全的原生认证模式，不能把改成匿名/custom-auth 的环境称为受支持的回滚

配置在 worker 启动时读取。变更提高 generation 并重启所有 worker；原生设置保存配置与令牌摘要，旧代际、同代际不同摘要和 namespace 改绑均拒绝。旧 worker 的目录操作会被当前数据库权威拒绝，不能沿用旧允许结果。当前没有 SCIM 管理表单，配置由受控运维维护

## 支持的接口和字段

| 接口 | 本批行为 |
| --- | --- |
| `GET /ServiceProviderConfig` | 声明 PATCH、限定 filter、ETag；不宣称 Bulk、排序和改密 |
| `GET /ResourceTypes`、`GET /ResourceTypes/User` | 仅 User 资源 |
| `GET /Schemas`、`GET /Schemas/{User schema URN}` | 本批 User 属性定义 |
| `POST /Users` | 创建新的目录资源与原生 customer，同事务审计 |
| `GET /Users`、`POST /Users/.search` | 有界索引分页与单个等值条件 |
| `GET /Users/{id}` | 当前命名空间中的非删除资源 |
| `PATCH /Users/{id}` | 至多 16 项、按序计算、整体原子提交 |
| `PUT /Users/{id}` | 替换可写 profile；要求明确 active，省略 displayName 则清除 |
| `DELETE /Users/{id}` | 软删除并永久废弃旧身份凭据，原生账号和财务历史保留 |

表内路径均相对于 Base URL。仅接收 `application/scim+json` 或 `application/json`，单次请求至多 64 KiB。支持 `schemas` 为 core User、必需 externalId/userName、可选 displayName 和 active；POST 的 active 默认 true。externalId 为本来源中的精确标识，userName 首版只接收无空白 ASCII，并按大小写不敏感比较；displayName 支持有界 Unicode。目录名称和显示字段不会成为本站密码登录标识

例如创建请求只需这些目录字段：

```json
{
  "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
  "externalId": "directory-assigned-id",
  "userName": "account@example.invalid",
  "displayName": "Example User",
  "active": true
}
```

POST 重复 externalId 或 userName 返回 409，不重复创建原生用户；客户端可重新按 filter 查找既有资源。它不承诺重复 POST 返回 200。列表 startIndex 从 1 开始，小于 1 按 1；count 小于 0 按 0，最多返回 100 项，count=0 只查询总数。超大起点返回空页，不把大整数直接送入 PostgreSQL offset

filter 仅支持 `id`、`externalId`、`userName` 的单个 `eq` JSON 字符串或 `active eq true/false`；不支持的表达式返回 `invalidFilter`。分页按创建时间和 ID 稳定排序，不承诺多次 HTTP 请求之间冻结整个目录快照。未知参数、重复字段、重复大小写变体或不支持的属性被拒绝

PATCH 使用标准 PatchOp schema，支持单值 add/replace，以及移除 displayName；pathless add/replace 对象也受同样字段范围限制。externalId 不可改绑。失败的后续操作会使整个 PATCH 无写入。即使同一次 PATCH 先停用再恢复，最终 active 与原值相同，仍会废弃旧凭据。POST/PUT 忽略只读 id/meta，不允许客户端决定原生身份；PATCH 显式修改只读身份字段被拒绝

响应提供 `meta.version` 和 ETag。If-Match 可选；提供时必须匹配当前版本或 `*`，过期版本返回 412，不覆盖别人的更新。无 If-Match 采用串行原子更新，不能宣称具有客户端乐观锁保护

## 新账号如何使用网关

新建账号只有 `customer` 角色，原生 ID 使用保留前缀 `retail-scim-user:` 和随机 UUID，密码、邮箱和 SSO 字段为空。不自动创建 Key、钱包、充值或资源权限，目录用户名也不能直接用于本站密码登录。受管账号即使后来被人工设置密码，本批仍拒绝密码登录，避免形成绕过目录生命周期的入口

需要控制台登录时，运维另行配置 [自有 OIDC](oidc-sign-in.md) 的显式 subject 与本站 user_id 绑定。首次成功登录后访问自己的 Wallet，使用现有流程创建空钱包，再由管理员按现有规则充值、配置模型与资源池并发放普通 Key。模型调用也可以由管理员按已有流程发 Key；资金与授权必须分别确认，目录同步不代做

原生 ID 不放入 SCIM 响应。受控运维可在原生 writer 中按 SCIM resource ID 只读查询映射，不能把这张权威表开放给普通客户：

```sql
SELECT id, user_id, active, deleted, version
FROM retail_scim_users
WHERE id = '<SCIM-resource-UUID>'::uuid;
```

## 停用、恢复和凭据墓碑

原生库新增 `retail_scim_settings`、`retail_scim_users`、`retail_scim_credentials`。目录 resource 的凭据 revision 在每次 active 状态改变及删除时更新；原生 Key 创建或有效轮换时，数据库触发器在同一事务记录不可变 hash、归属和 revision 证明。发行与生命周期操作在同一资源行上排序，不依赖尽力缓存广播或时间戳精度

受管身份的每次接纳从 writer 读取当前账号、确切呈现的 Key、目录状态及凭据证明。仅当前 customer、未重置、未封禁、未过期、资源 active 且未删除、证明 revision 匹配时通过，再执行现有模型、预算、限流、资源池和钱包校验。缓存中的管理员角色不能让受管用户免计费。普通非受管账号只增加本地前缀判断，不新增本批数据库读取或外部身份 RPC

停用后，普通 Key、控制台会话和映射到该 Key 的 API JWT 都拒绝。恢复只恢复账号发行新凭据的资格，不恢复任何旧 Key/session/JWT；应新建 Key 或重新登录，并按需要更新 API JWT 绑定 revision。人工 blocked 标志不被清除。已经接纳并转发的请求继续完成和结算，不追溯取消

有效 Key 可以按既有入口轮换，生成新 hash 证明并保留旧 hash 墓碑。呈现旧明文时不跟随 deprecated/grace 映射；旧 hash 也不能被重新发行给其他用户。涉及受管账号的 Key owner 迁移和原生 user_id 改名被阻止。原生删除 Key 后墓碑仍保留，不以删除再创建恢复旧凭据

DELETE 后原 SCIM ID 的所有操作返回 404，列表不再包含它；后续相同目录 externalId/userName 可以创建全新 UUID 与原生账号。旧账号、钱包、余额、Key 行和历史账本保留，不能借重新建档接管旧资金。受管原生账号不通过原生 user-delete 清除，生命周期应走目录入口

重新建档也不会自动把旧 OIDC subject 或 API JWT 绑定迁到新账号；这些绑定继续服从各自的不可变身份及 revision 规则，需要另行制定显式迁移，不能直接沿用旧目录名称接管

## 审计、资源与回滚

四类元数据事件为 `native_scim.configured`、`native_scim.user_created`、`native_scim.user_updated`、`native_scim.user_deleted`。变更与原生 outbox 同事务，失败或满队列整体回滚；事件只有服务器操作者、受影响数量和随机操作 ID，不复制目录姓名、externalId、userName、原生凭据或令牌。旧三类、五类、六类动作约束兼容升级为十类，旧事件不改写

专用入口不经过推理追踪、模型回调和上游处理。凭据 header 在进入目录应用前移除，访问日志把目录路径及 query 变成固定资源标记，校验错误不反射 body。目录数据库与凭据证明属于受控权威数据；不能把应用诊断脱敏等同于能防止数据库管理员自行开启参数日志或读取原表

每 worker 最多八个目录 HTTP 请求，入队两秒，上传正文五秒；store 原生 IO 最多四槽，读含排队两秒、写含排队八秒，锁等待 500 ms、单语句 1.5 秒。受管认证另有四槽/两秒 fresh 预算。复用原生 PG pool，不增加 Redis/HTTP pool，不逐请求访问外部目录。可选初始化失败在后台重试，不阻塞普通账号启动和结算

配置 max_users 默认 1000、上限 10000，包含删除墓碑；全局凭据证明最多 1000000、单账号最多 10000，包含历史轮换与删除。达到上限时拒绝新建/发行，旧证明不自动清理，也不以清空历史绕过撤销语义。本批不是无限生命周期或 20000 RPM 的容量认证

先升级全部 API、发行 Key 与审计 worker，再启用目录。关闭 SCIM 并重启停止新同步，但保留受管身份 guard、数据库触发器、权威表和墓碑。在同一受支持原生认证模式下，停用账号仍拒绝。不能删除这些结构或回滚到不认识受管前缀的旧二进制后继续接受已发行凭据。生产环境与真实目录互通需单独验收
