# V2 完整搜索偏好

## 协议与身份

`GET /v2/me/preferences` 和 `PATCH /v2/me/preferences` 只操作当前已认证成员自身。沿用 `current_member` 的 Fluxer Bearer、当前 Discord 绑定与正式成员核验，不接受 body 中的 user_id、guild_id 或其他目标身份。其他用户不能通过路径参数读取或写入。

完整响应包含 V1 搜索偏好的全部字段，并增加：

- `user_id`：Fluxer 用户 ID，字符串。
- `guild_id`：旧搜索偏好所属的 Discord 主服务器 ID，字符串，来自服务端配置。
- `is_first_time`：此账户在该搜索作用域尚无偏好记录。GET 只返回默认值，不创建记录，也不冒充已经设置。
- `show_nsfw`：Fluxer 中保存的原始开关。
- `nsfw_eligible`：独立的原生年龄资格。内容可见性使用两者同时为真，不能把开关当成年龄证明。
- `nsfw_preference_version`：Fluxer 返回的 CAS 版本。
- `nsfw_guild_id`：NSFW 权威所属的原生 Fluxer guild，不是上面的 Discord guild。

旧偏好继续使用现有 `user_search_preferences` 表及配置主 guild，不复制到另一套偏好表。作者列表读出的是 Fluxer ID；若旧 Discord 作者列表还没有 canonical 投影，会显式映射并构造 DTO，不在 GET 时修改 ORM。缺少映射返回 503，不将未知作者伪装为空列表。频道列表仍是旧 Discord 来源频道 ID，不重新解释为 Fluxer 频道。继承这些频道的搜索强制匹配 Discord 来源，不会因相同数字的原生频道 ID 命中。显式搜索 channel_ids 默认同样使用 Discord 来源；只有显式指定 platform=fluxer 时才按原生频道筛选，旧虚拟频道映射也不用于原生频道。

## 普通偏好部分更新

可写字段为：

- `preferred_channels`、`include_authors`、`exclude_authors`。
- `include_tags`、`exclude_tags`、`include_keywords`、`exclude_keywords`、`exclude_keyword_exemption_markers`。
- `preview_image_mode`、`results_per_page`、`ui_page_size`。
- `sort_method`、`custom_base_sort`。
- `created_after`、`created_before`、`active_after`、`active_before`。

省略字段保留原值，未知字段和只读字段返回 422，空 PATCH 返回 400。前端应仅提交实际修改字段，不能把旧完整快照当作新的部分修改，否则同字段仍会采用最后写入值。

筛选数组可用空列表或 null 清除；canonical 作者清除后保存为空列表，防止旧 Discord 列表再次被当作待迁移值而复活。关键词 null 清为空字符串；时间 null 或空字符串清除；豁免标记 null 清为空列表。显示模式、分页、排序和 NSFW 字段不接受 null。数组 ID 在 JSON 中应传完整数字字符串，拒绝布尔值、浮点及不完整数字。

BOT 分页范围 1–9，网页分页范围 1–100。默认 BOT 每页 5，网页每页 48，预览模式 thumbnail，排序 comprehensive。排序支持 comprehensive、created_at、last_active、last_active_at、reaction_count、reply_count、collection_count、view_count、collected_at、reddit_hot、custom；custom_base_sort 不允许 custom 自身。日期沿用 V1 的绝对或相对时间语法，如 2026-11-05、-7d。更新后与现有值合并验证前后范围，不允许倒置。

保存针对同一 canonical 用户与搜索 guild 使用 PostgreSQL 事务级 advisory lock，首次尚无行时同样串行化。每次仅修改明确传入列，避免多个已升级 worker 的首次写入产生重复记录或互相清空其他字段。滚动发布期间，未升级旧进程不具有此锁；应先统一升级写入者，或在维护窗口切换，不能据此声称跨旧进程提供版本冲突检测。既有唯一索引和 identity runtime 迁移仍为 V2 前置，不新增数据库迁移。

## 成人内容偏好独立提交

`PATCH {"show_nsfw": true, "expected_version": 7}` 调用 Fluxer 的 `rpc.preferences.v1.update_service`，不再修改 Forum 的 User.show_nsfw。本地字段不是读取失败时的替代权威。

保留旧客户端只提交 show_nsfw 的兼容性：使用本次认证水合的权威版本进行 CAS，版本过时仍返回 409，不盲目覆盖。新前端应提交刚读到的 expected_version。RPC 成功必须返回相同开关及恰好加一的版本；不确定结果和不可用返回 503，不自动重试写入。原生同步使用持久意图与围栏；错误不能证明已经回滚，稍后恢复仍可能完成写入。网页在失败后重新读取权威状态，再允许下一次基于可见版本保存。

NSFW 与普通字段混合提交返回 422，两套存储不宣称跨库原子事务。仅保存 NSFW 不会伪造普通偏好已完成首次设置。开关为真但年龄资格为假时，设置页保留真实开关并展示限制，服务端内容过滤保持关闭。

## 搜索与网页

`POST /v2/search` 在 apply_preferences=true 时合并旧搜索偏好。明确传入的字段始终优先，包括空字符串和空列表；省略筛选、排序才使用保存值。未传 limit 时使用 ui_page_size，首次无记录使用 48。显式 limit 不变；旧库中越界的分页只在查询时限制到 1–100，不擅自重写旧记录。offset 是记录偏移，响应 limit/offset 是网页翻页依据。

网页的共享偏好读取、个人页和首次设置改用真实 V2 self 协议；V2 首次设置不依赖尚未迁移的 V1 频道 metadata。V2 页面手工频道输入明确标注 Discord 来源 ID，作者输入明确标注 Fluxer ID。前端保存失败保留草稿，后台刷新不覆盖编辑中字段，重复提交在保存期间被阻止。旧部署仍使用原 V1 协议。

## 上线顺序与验收

1. 在目标分支检查补丁基线，执行 git apply --check。离线重建快照不是目标仓库真实历史，应用和提交应在真实 dev-new 上重新核验；不自动 push。
2. 先部署支持偏好字段、CAS RPC、NATS subject ACL 的 Fluxer API/schema/gateway 版本，再部署 Forum 后端。Forum 当前成员水合依赖该 RPC，未先部署会明确返回 503，不能关闭检查作为降级。
3. 配置 work_forums.guild_id 为目标 Fluxer guild，discord_guild_id 为旧搜索主 guild。不得互换。沿用原身份 runtime 迁移和唯一索引。
4. 最后部署网页。使用已有偏好账户验证旧值保留、新账户验证首次保存及返回地址、两设备验证不同字段部分修改及 NSFW 版本冲突、网络失败验证草稿保留、搜索显式清除/排序/分页、换绑/撤销/年龄限制验证拒绝访问。
5. 不将本地通过等同于真实 NATS、Cassandra、Redis、成员资格服务或已登录浏览器验收。回退需要同时考虑权威字段和 RPC，不能在旧 Forum 版本里悄悄恢复本地 NSFW 权威。
