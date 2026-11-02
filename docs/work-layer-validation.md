# 作品层兼容运行验证记录

本轮只在Windows本地实施，未推送、未合并dev/test、未运行生产迁移、未执行真实用户预建。Forum两条交付分支使用完整兼容runtime，旧迁移分支原提交另有备份分支保留。

## 实现与事实边界

- Work BIGINT与平台主子来源、逐表情主帖统计、默认书单收藏、标签治理、作者/作品关注、公告/旧新通知共用事实。
- V1继续Discord wire ID/JWT，V2使用Fluxer统一主体；历史绑定只唯一当前，显式管理合并保持实际操作者审计。
- Fluxer原生创建/正文/附件/分类移动/处罚通过可靠operation_id；子帖初始化锁等待全部处罚依赖；Discord主帖不被原生处罚操作。
- PG触发器捕获旧协议持续写入；缺映射有持久列桶及有界追增；来源永久墓碑阻止乱序复活。
- 真正gateway删除与管理索引移除分开；临时404/403/timeout只计异常；旧SQL物理删除关联Thread会整事务拒绝回滚。
- 关键词、排除豁免、rjieba FTS、虚拟频道标签、筛选与排序沿用既有搜索语义。
- HTTP适配按领域拆分，入口Qo与ActorDTO不传session/原始token；新单表偏好/标签/统计操作在repo。

## 验证环境与结果

使用自建Windows PostgreSQL 127.0.0.1:55439，独立forum_work_release3数据库从空库执行alembic upgrade head。数据库未引用用户既有实例，全部测试完成后停止该自建实例。

最终专项覆盖：真实PG迁移/历史身份/旧新收藏立即可见/偏好往返/批量书单保留隐藏原生条目/默认书单真实并发/主帖统计/持续回填/七天审核/永久墓碑/分类隐私/原生标签/独立锁来源/旧Discord删除/Banner真实队列/公告通知原子事务；实际worker序列化请求由相邻Fluxer生产IntegrationSchemas校验；认证连续性与角色缓存用可控Redis模拟。

独立原生验证由Fluxer代理执行：集成56通过、2项live可选项跳过；后续真实来源created_at专测7通过；附件早期四套73通过。真实Windows NATS权限与持久拉取/ACK已由主代理验证，临时broker已停止。网页全量422通过，后续pending状态6项定向通过，TypeScript/lint/V2构建通过。

Forum不是无条件全库测试声明：旧Redis集成fixture未连接未知默认服务；SQLite批量测试使用临时aiosqlite依赖。专项命令中TEST_WORK_DB_URL与FLUXER_MIGRATION_TEST_URL均显式指向隔离PG；所有随机schema在测试后删除。

## 部署仍需完成的配置

生产OAuth需单独登记/v2/auth/callback并设置FORUM_V2_DISCORD_REDIRECT_URI；正确配置自部署API/Web origin、四个Fluxer论坛、正式/管理/深渊角色及受限NATS凭据；管理员预建AUTH/CONTENT stream与durable。参见fluxer-online-migration.md。

本轮未部署真实全栈生产环境，原生live可选依赖并未被模拟成已验证。处罚初始化最多100个目标，超过上限明确拒绝而不丢处罚。分类移动和跨服权限失效有明确异步窗口，失败保留持久可诊断任务；不承诺跨服零延迟。
