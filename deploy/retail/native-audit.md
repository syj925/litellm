# 原生管理操作的事务审计桥

本模块把明确接线的原生数据库变更，通过同事务 outbox 汇入自有 Operation Audit。当前只覆盖自有 Key 轮换、用户自助改密和退出当前 UI 会话，AUD-01 保持有范围的实现状态，不代表全部原生管理审计完成

## 覆盖范围与入口

| 原生变更 | 入口 | 事件 |
| --- | --- | --- |
| 自有 API Key 轮换 | `POST /retail/keys/rotate` | `native_key.rotated` |
| 密码登录用户修改自己的密码 | `POST /user/password/change` | `native_password.changed`；实际撤销其他 UI 会话时另记 `native_session.revoked` |
| 退出本人当前 UI 会话 | `POST /session/logout` | 实际删除会话时记 `native_session.revoked`，已经撤销的会话不重复记事件 |

轮换继续核验当前原生角色、凭据和目标 Key 的可修改权限，不允许只读账号轮换。改密只接受本人密码登录会话，允许 `password_reset_required=true` 的用户自救；不能把控制面的强制重置限制直接套在自助改密上。logout 不接受目标账号或目标 Key，只能撤销请求认证所使用的 UI 会话

改密先在事务外验证旧密码、密码策略、泄漏密码检查和生成新 hash。事务内锁定用户及当前凭据，再验证当前归属、UI 会话类型、有效期、停用状态及密码登录标记，核对锁定的密码 hash 与已验证快照一致。并发改密导致快照变化时返回 409，不覆盖另一请求的新密码。更新密码、清除强制重置状态、删除其他 UI 会话及原生删除归档均在同一事务中完成

上述路径的 outbox 写入与原生变更使用同一个原生 writer 事务。审计意图无法保存时整个受控变更回滚并返回通用不可用；改密和 logout 提交后再进行既有的本机缓存失效和跨 worker 广播，轮换在原生缓存处理之外也在提交后再次失效。Redis 故障边界仍以 [协调保护](coordination-guard.md) 为准，不承诺已经开始的请求被追溯中断

## 跨库提交与时间

原生业务库与 retail 库可以分开，二者不使用跨库分布式事务。业务成功表示原生变更和持久 outbox 已一起提交，不需要等待 retail 库或外部接收端

后台先在原生库取得短租约，再把不可变事件写入 retail 库，最后回到原生库确认并删除已经桥接的 outbox 行。retail 提交成功但原生确认丢失时，恢复后重放同一 `source_id`、事件 ID 和内容摘要，只确认已有事件，不重新执行轮换、改密或 logout。相同幂等身份对应不同内容时进入完整性冲突失败，不能静默覆盖

Operation Audit 的发生时间保留原生事务记录的 `occurred_at`，不会改成桥接成功时间。页面中的资源使用服务端随机操作 ID，`organization_id` 为 null；普通组织管理员的组织审计不能读取这三类全站事件

## 启用与回滚

| 环境项 | 默认 | 用途 |
| --- | --- | --- |
| `RETAIL_OPERATION_AUDIT_ENABLED` | false | 自有审计基础及后台初始化 |
| `RETAIL_NATIVE_AUDIT_ENABLED` | false | 原生事务意图与后台桥，依赖前一开关 |

以下只是开关示例，不包含连接凭据：

```yaml
environment:
  RETAIL_OPERATION_AUDIT_ENABLED: "true"
  RETAIL_NATIVE_AUDIT_ENABLED: "true"
```

先升级全部 API 与后台 worker，确认原生 writer 和 retail 数据库权限、兼容 schema 以及后台初始化，再开启新开关并重启对应 worker。原生新增结构仅为 `retail_native_audit_settings`、`retail_native_audit_outbox` 及相关约束和索引；初始化失败会重试，不把失败伪装为可正常审计

默认关闭，不因文档或新代码存在而自动启用。关闭新开关并重启后停止新增原生审计意图及桥接，密码和 logout 回到原有兼容路径。回滚保留原生 outbox、retail 事件、来源身份和不可变约束；再次启用后继续恢复已有意图。不要删除 schema 或清空 outbox 来回滚，也不要以重放业务 API 代替恢复审计

## 容量、失败与恢复

桥接使用每 worker 独立、惰性打开、最多两条连接的 retail 审计池，不借用钱包、终态或报表池；访问原生库沿用原生 writer，桥接并发另外限制。全部 worker 合计最多两个有效租约，每个租约 30 秒；单次桥接处理有八秒期限，数据库事务另有限制锁等待和语句时长

原生 outbox 最多保留 10,000 项，包含 pending、processing 和 failed。达到上限时拒绝新的受控原生变更，已有业务和审计意图仍保留。这个队列没有外投递那种允许丢失并累计缺口的策略，两者不能混用

临时故障有界退避，自动尝试最多五次、原始自动处理期限七天。完整性冲突、尝试耗尽或期限到达会转为 failed，failed 不自动删除且继续占用容量。七天是自动尝试期限，不是无条件清除时间

当前没有单独的原生 outbox 管理页面或公开修复 HTTP 接口。受控运维可先在原生库只读检查队列：

```sql
SELECT state, error_code, count(*) AS total
FROM retail_native_audit_outbox
GROUP BY state, error_code
ORDER BY state, error_code;
```

修复存储、连接或兼容性问题后，由受控运维任务调用 `NativeAuditStore.requeue_failed(event_id)` 恢复明确的 failed 事件。它保留原 ID、来源、发生时间和内容，重新开始有限尝试并设置新的七天处理期限。完整性冲突须先人工核对原生意图和 retail 回执，不直接绕过摘要检查。成功桥接并确认后释放容量，失败恢复不重放领域写入

## 数据边界与未覆盖项

outbox 只保存服务端验证的操作者、受控动作、受影响数量、随机操作标识、原发生时间以及队列租约元数据，不保存 Key、token、密码、hash、客户请求正文或供应商配置。原生会话删除归档继续遵循原有原生表的访问边界，不会复制到审计 outbox 或普通客户投影

原生充值、改价、渠道 CRUD、账号 CRUD、旧 `/key/regenerate` 等其他入口尚未因此获得事务桥接，全部拒绝访问尝试也未纳入。自有操作审计与原生商业 Audit Logs 的许可判断分开，不更改许可门禁

本页描述实现合同和运维边界，真实多 worker 验收、性能及部署状态由相应批次记录补充，不把三类接线扩大为全部原生功能验收

R11 的 [自有 OIDC](oidc-sign-in.md) 在同一 outbox 另外接入 `native_identity.configured`、`native_identity.signed_in`。原有三类动作和恢复语义保留；兼容迁移只扩展动作 CHECK，不改写旧事件。全部 API 与桥接 worker 升级后才能启用 OIDC，不能让只认识三类动作的旧 worker 接到新事件。

R12 的 [API access JWT](api-access-jwt.md) 再接入 `native_api_identity.configured`，仅记录配置变更的元数据；token 验签或每次推理不是本动作。旧三动作、五动作 schema 兼容扩展为六动作，旧行及设置保持原样；只有全部 API 与桥接 worker 已支持新动作后才启用该功能。普通组织成员不能查询这类全站事件。
