# 零售网关文档索引

## 自有功能与替代进度

[自有组织、审计与账本报表](retail-management.md) 说明首批实际功能、入口、角色边界、报表口径、运行配置与限制

[业务标签与管理来源](business-labels.md) 说明四类对象的独立分类、当前权限、来源记录、生命周期和运行边界

[账号模型消费额度](model-budgets.md) 说明固定生效窗口、同事务预留与结算、个人只读范围、暂停与晚到事实处理

[显式组织归属与消费概览](organization-consumption.md) 说明接纳归属快照、晚到结算、管理员实时消费与个人钱包边界

[固定组织消费 CSV](organization-reports.md) 说明冻结投影、持久任务、合并容量、逐次认证下载与回滚

[显式委派组织财务](delegated-finance.md) 说明按组织 admin 成员批准的 View/export、历史范围、安全投影、授权代际失效及独立门户

[操作审计异步外投递](audit-delivery.md) 说明服务器预设目标、签名、队列上限、重试、丢弃、配置变化与脱敏范围

[原生事务审计桥](native-audit.md) 说明自有轮换、自助改密、logout 及 OIDC 配置/签入接线、原生 outbox、跨库幂等桥接、容量与失败恢复；其他原生 CRUD 仍未覆盖

[服务端托管模型密钥](managed-secrets.md) 说明 Vault KV v2 固定引用、后台刷新与硬 TTL、OpenAI Chat 适配、拒绝覆盖和默认关闭边界；本地 main 未配置真实 Vault

[账号模型自然月续窗](monthly-budgets.md) 说明完整自然月、未来金额版本、当前窗口冻结、明确停止及停机缺窗保护

[自有 OIDC 控制台登录](oidc-sign-in.md) 说明固定 issuer、显式已有账号绑定、同站多 worker、管理会话撤销及默认关闭的接入方式

[外部 API access JWT 映射](api-access-jwt.md) 说明固定签发方、显式普通 Key 绑定、短期 access token、配置代际撤销、原计费权限复用和默认关闭边界

[API JWT 验收记录](validation/r12-2026-11-05.md) 保存协议、原生权限、双 worker 账本、热公钥故障、脱敏及回归检查的本地证据

[自有 SCIM Users 与 Groups 同步](scim-provisioning.md) 说明目录字段、独立 customer 创建、组成员和可选访问条件、不可复用凭据证明及默认关闭边界

[SCIM Users 验收记录](validation/r13-2026-11-05.md) 保存标准协议、原生 writer、双 worker 撤销、并发发行、流式结算与诊断脱敏证据

[SCIM Groups 验收记录](validation/r14-2026-11-05.md) 保存组成员、访问限制、永久撤销、有界分页及双 worker 业务验证证据

本地通过 bind mount 更新源码或静态 UI 时，Compose 配置不变的 up 不会自动重启 Python worker。沿用实际的 env-file 和 compose 文件上下文，使用 `up -d --force-recreate --no-deps --no-build --pull never litellm`，再核对启动时间、两个 worker、健康检查和数据库结构。覆盖文件 hash 一致只证明磁盘内容相同，不能单独证明进程已经加载新版本

[OIDC 控制台验收记录](validation/r11-2026-11-04.md) 保存协议、原生事务、跨 worker、恢复不复活、日志脱敏及真实浏览器回调的本地证据

[自然月预算验收记录](validation/r10-2026-11-04.md) 保存完整月续窗、停机恢复、跨 Key/资源池共享额度、实际三角色页面和有限数据库对照的本地证据

[原生审计与托管密钥验收](validation/r9-2026-11-04.md) 保存三类原生事务桥、双 worker 会话失效、队列满回滚、独立 Vault 读取和只读账号界面修复的本地证据

[首批本地验收记录](validation/r1-2026-11-03.md) 保存已验证功能、662 项定向测试、真实双 worker 故障结果及尚未覆盖的边界

[业务标签批次验收记录](validation/r2-2026-11-04.md) 保存独立分类、组织选择器分页、609 个去重用例及真实接口结果，实际浏览器视觉验收仍待完成

[模型额度批次验收记录](validation/r3-2026-11-04.md) 保存固定窗口额度、1,735 个去重用例、真实双 worker 请求和事务性能对照

[额度提前续接验收记录](validation/r4-2026-11-04.md) 保存唯一下一窗口、301 个去重用例、双 worker 到期切换和实际浏览器管理及个人只读流程

[审计外投递验收记录](validation/r5-2026-11-04.md) 保存 511 个去重用例、固定接收器故障、Cookie 隔离、撤权和实际浏览器管理流程；本地未配置真实目标

[组织归属批次验收记录](validation/r6-2026-11-04.md) 保存 1,512 个定向用例、真实双 worker 归属、角色撤销、页面流程及配对事务样本

[组织固定导出验收记录](validation/r7-2026-11-04.md) 保存固定金额与未决状态、两类报表共享限额、重启恢复和逐次下载授权的本地证据

[委派财务验收记录](validation/r8-2026-11-04.md) 保存显式 View/export、脱敏投影、授权代际失效、三队列合计资源及客户实际页面的本地证据

[授权功能自主替代方案](enterprise-replacement-plan.md) 保留分批范围、权限设计、迁移、回滚与验收条件；首批实现状态以实施清单和验收记录为准

[授权功能清单](enterprise-feature-inventory.md) 保留当前源码基线的 46 个后端条目、23 个前端条目及商业模块依赖的处理归属

[实施任务清单](enterprise-replacement-backlog.json) 保存稳定任务 ID、依赖、实现、验收证据及部署状态。未实施的后续批次继续保留待办，不因首批上线而标为完成

## 接入与权限

- [访问控制与脱敏](access-control.md)
- [渠道连接管理](channel-connections.md)
- [官网与公共页面](public-homepage.md)

## 价格与缓存

- [动态表达式定价](expression-pricing.md)
- [目录价格来源](catalog-pricing-sources.md)
- [缓存价格与 TTL](cache-pricing.md)
- [缓存兼容性](cache-compatibility.md)

## 日志与容量

- [请求追踪](request-tracing.md)
- [请求容量与结算](concurrency-and-settlement.md)
- [请求体保护](request-body-limits.md)
- [协调故障保护](coordination-guard.md)
- [渠道分析](channel-analytics.md)
- [模型分析](model-analytics.md)

## 模型元数据与来源

- [模型目录来源](model-catalog-sources.md)
- [第三方声明](THIRD_PARTY_NOTICES.md)
