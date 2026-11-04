# 显式委派组织财务

本模块在自有组织内提供只读消费查询和可选的固定 CSV 导出。全站可写 Proxy Admin 必须对具体成员显式授权；组织成员、组织管理员、原生组织或团队身份都不会自动获得财务权限。管理员原有的 [组织消费](organization-consumption.md) 和 [组织 CSV](organization-reports.md) 保留独立入口、接口及任务作用域

## 授权与使用

全站管理员进入 Organizations → Members，在目标成员的 Finance access 中选择 None、View only 或 View and export。目标必须是同一自有组织内当前启用的 admin 成员，组织也必须启用。View and export 同时包含 View；只给 export 而不给 view 的请求会被拒绝

授权覆盖该组织已有的、具有接纳归属证据的历史请求集合，不以授权创建时间截断，也不只限于被授权成员自己的请求。授权前应确认组织范围；弹窗会明确提示已有历史包含在内。无归属证据的旧请求仍保持未归属，不按今天的成员关系回填

门户接口每次核验原生 writer 中的账号、凭据及可写状态，允许的非只读原生角色为 `proxy_admin`、`internal_user` 和 `customer`。原生 viewer，以及仅有原生 `org_admin`、`team` 等非门户角色的会话不能生效；团队身份不提供财务资格。原生 `customer` 在界面中使用已有最小普通 App User 会话，不获得 Internal User 或管理员管理权限。即使原生角色允许进入门户，仍须同时满足有效自有组织 admin 成员关系和显式财务授权

被授权账号从 Organizations 当前组织行的 Finance 进入 `/ui/organization-finance/?organization=…`。选择器仅列出当前有效授权的组织；普通成员和未授权账号没有财务链接。目录按当前页最多 25 个精确组织 ID 一次核验，路径和查询 ID 本身不赋予权限

View only 可以读取实时汇总和安全请求明细，不能创建、列出、查询或下载财务导出。View and export 可以为已成功应用的预览窗口建立固定任务，日期输入中尚未 Apply 的草稿不参与创建。任务只属于当前创建者、URL 组织和当前授权代际，不与个人账本或全站管理员组织任务合并显示

None 清除现有授权。停用或降级成员仍可由全站管理员检查并撤销历史授权，不能据此授予新的 View 或 export。成员或组织资格变化后出现 approval must be renewed 时，须由全站管理员明确重新批准；恢复启用状态不会自动续批

## 查询、快照与安全投影

实时查询使用已有不可变请求组织归属，按接纳时间选取 `[start, end)` 集合，再取得当前可核验的实际客户扣款。时间使用明确时区，底层规范化为 UTC，界面以北京时间展示；币种为 USD。窗口最多 31 天、10,000 个归属请求，更严格的报表配置仍生效，超限要求缩小窗口而不静默截断

授权后的历史查询包含该组织既有 admission cohort。换组不移动旧请求，晚到结算更新原接纳窗口的实时金额。pending、unknown、conflict 分列，未确定金额留空；已确认的合法零金额保留为零。金额使用整数纳美元和精确十进制字符串，不经浮点数重新计价

委派实时明细只投影 `sequence`、公开 `model`、`captured_at`、`state` 和 `settled_usd`。CSV 列固定为 `sequence`、`model`、`captured_at_utc`、`captured_at_beijing`、`state`、`settled_nanos`、`settled_usd`，文本单元格处理公式注入

两种投影均不返回原始 user/request ID、归属版本、grant 内部标识或版本、Key、个人余额、供应商身份、成本利润、价格表达式、原始请求或内部错误。公开任务元数据保留必要的任务及组织标识、时间窗、状态、汇总和不透明 `access_revision`；不返回内部 `error_code`、文件哈希、幂等摘要或创建者原始 ID。界面中的失败信息使用受控恢复文案

固定任务在创建的一致性事务中物化安全投影、金额和未决状态，后台只读取这份投影。后续结算、分页、改名、生成重试和 worker 重启不会改写旧文件；需要较新的结果时创建新任务。历史窗口也可能含未决事实，固定快照并不表示全部请求已经完成结算

## 授权代际、撤销与恢复

每个委派任务绑定创建时的组织、创建者、grant 版本与不透明 revision、成员版本和组织启停 generation。所有列表、详情、完整下载和每次 Range 下载都重新检查当前原生账号及这组持久权限，不凭旧会话或浏览器缓存读取文件

以下变化使原授权不再匹配：

- 全站管理员清除或修改 grant；重新批准会产生新的授权 revision
- 成员启停、降级或其他导致成员版本变化的操作；恢复成 admin 后仍须明确续批
- 组织启停引起 generation 变化；重新启用后仍须明确续批，单纯改名不改变启停 generation

旧授权代际的任务从委派列表隐藏，详情与下载不再有权读取。再次授予相同 View/export 也不能恢复旧任务；应在新授权下建立新快照。旧幂等操作键跨授权代际重放返回 409，不能利用重新批准复活旧结果。同一有效代际中结果不确定的相同创建操作应复用原键；变更窗口、作用域或授权后使用新的操作键

当前原生角色、账号、凭据、密码重置或其他实时资格不满足时，同样拒绝访问。原生资格恢复不替代对 retail grant、成员版本和组织 generation 的核验。客户端失去 View 或无法验证权限时隐藏全部财务历史及详情；失去 export 时隐藏固定任务，合法的 View 可继续使用。恢复先重新取得能力，再在当前 revision 下获取新数据

认证响应使用 `private, no-store`，下载没有公共链接或带 token 的 URL。未知、跨组织、跨创建者或旧授权任务不泄露对象存在性；过期下载返回 410，未就绪返回 409。已经合法下载的本地字节无法通过后续撤权远程收回

## 运行资源与开关

委派授权与消费查询只在控制面进行，不增加推理请求中的远程权限读取。grant 使用诊断事务；固定快照和生成使用现有有界报表资源，不借用钱包预扣或终态事实连接

`retail_delegated_report_jobs` 与 `retail_delegated_report_rows` 是独立队列和安全明细投影。明细不复制源请求或账号 ID；任务内部仍保存所有权和授权核验必需的创建者引用及 epoch，公开响应不返回这些内部字段。个人账本、管理员组织 CSV 和委派 CSV 三类任务共用报表连接池、线程池、快照槽及全局生成租约，并合计执行同一创建者滚动 24 小时配额和全局未完成任务上限。新模块没有另开一套无限并发额度

容量与过期策略沿用 [固定组织消费 CSV](organization-reports.md) 和 [自有管理功能](retail-management.md) 的有界报表配置。生成任务不持有原始登录凭据，私有产物完成不代表调用者重新获得访问权；对外读取仍逐次核验当前权限

`RETAIL_ORG_FINANCE_DELEGATION_ENABLED` 模块默认 false。生效还要求组织财务、自有组织、有限组织委派和审计前置开关开启：`RETAIL_ORG_FINANCE_ENABLED`、`RETAIL_ORGANIZATIONS_ENABLED`、`RETAIL_ORGANIZATION_DELEGATION_ENABLED`、`RETAIL_OPERATION_AUDIT_ENABLED`。固定导出另要求 `RETAIL_REPORT_EXPORT_ENABLED`；关闭导出不关闭已允许的实时 View

公开配置 `org_finance_delegation_enabled` 只表示模块可用，不代表当前用户已获 grant。关闭它隐藏新门户、授权及委派导出入口，不影响原有全站管理员组织财务、个人账本扣款或已保存有效归属的接纳采集

升级顺序是保留该开关关闭、完成新增结构初始化、更新全部 API 与报表 worker，再开启入口。共享资源的全局统计必须由所有 worker 识别三类队列；不能让旧 worker 和新 worker 在不同配额口径下同时生成任务

回滚先关闭委派财务开关，不删除 grant、组织 generation、新队列、固定投影或审计表。旧版本不认识委派队列，应保持入口关闭；恢复支持该模块的版本后，只有仍符合当前授权代际且未过期的任务可继续处理或读取。功能开关切换本身不代替 grant 撤销，也不改写已冻结金额

## 当前范围

本模块是显式委派的财务只读与导出，不建立共享钱包、不转移余额、不代发其他成员 Key，也不授予充值、改价、成员或全站管理能力。原生付费功能及 license gate 保持原样

REP-03 继续记为 `validated_scoped_locally`，共享钱包仍未实现。本地功能、接口、浏览器验收和生产部署分别记录；本文不将本地验证扩大为生产上线或容量承诺

## 接口

| 接口 | 范围 |
| --- | --- |
| `GET/PATCH /retail/admin/organizations/{org}/finance-grants/{user}` | 全站可写 Proxy Admin 读取、批准或清除具体成员授权；PATCH 使用权限布尔值、expected_version 和幂等键 |
| `GET /retail/organization-finance/organizations` | 当前有效 scopes；支持 q、limit、offset 与最多 25 个精确 ids 的有界批量筛选 |
| `GET /retail/organizations/{org}/finance/access` | 当前账号对目标组织的 View/export 能力及不透明 revision |
| `GET /retail/organizations/{org}/finance/consumption` | 通过 View 授权的实时安全投影；start、end、limit、offset |
| `POST/GET /retail/organizations/{org}/finance/reports` | 通过 View+export 授权的固定任务创建或当前代际列表 |
| `GET /retail/organizations/{org}/finance/reports/{id}` | 当前创建者和有效授权代际下的安全任务状态 |
| `GET /retail/organizations/{org}/finance/reports/{id}/download` | 逐次重新核验权限的私有 CSV，含 Range 检查 |
