# 零售网关文档索引

## 自有功能与替代进度

[自有组织、审计与账本报表](retail-management.md) 说明首批实际功能、入口、角色边界、报表口径、运行配置与限制

[业务标签与管理来源](business-labels.md) 说明四类对象的独立分类、当前权限、来源记录、生命周期和运行边界

[账号模型消费额度](model-budgets.md) 说明固定生效窗口、同事务预留与结算、个人只读范围、暂停与晚到事实处理

[操作审计异步外投递](audit-delivery.md) 说明服务器预设目标、签名、队列上限、重试、丢弃、配置变化与脱敏范围

[首批本地验收记录](validation/r1-2026-11-03.md) 保存已验证功能、662 项定向测试、真实双 worker 故障结果及尚未覆盖的边界

[业务标签批次验收记录](validation/r2-2026-11-04.md) 保存独立分类、组织选择器分页、609 个去重用例及真实接口结果，实际浏览器视觉验收仍待完成

[模型额度批次验收记录](validation/r3-2026-11-04.md) 保存固定窗口额度、1,735 个去重用例、真实双 worker 请求和事务性能对照

[额度提前续接验收记录](validation/r4-2026-11-04.md) 保存唯一下一窗口、301 个去重用例、双 worker 到期切换和实际浏览器管理及个人只读流程

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
