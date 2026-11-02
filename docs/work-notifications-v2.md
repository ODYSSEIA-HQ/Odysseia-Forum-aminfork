# 作品公告与通知兼容

`GET /v2/works/{id}/announcements?offset=0&limit=20` 返回 `{items,total,offset,limit}`，limit 为 1–100。历史包含旧正式更新投影；`legacy_update_id` 唯一关联旧 ThreadUpdate，不补发历史通知。

手动发布公告在同一 PostgreSQL 事务中创建公告并用单条 INSERT SELECT 快照活跃关注者，排除发布者和发布后才关注的人。不会创建空通知任务。受众写入失败则公告一并回滚；每次发布独立通知，编辑不再通知，撤回保留公告和原通知的 read_at。旧正式更新已映射公告时，旧撤回操作也保留通知并显示已撤回。

`GET /v2/me/notifications` 返回共享 Notification 行，兼容 thread_update、author_new_thread、tag_review、work_announcement，保留 Page 字段并提供 unread_count。旧更新通过 legacy_update_id 映射公告，已有旧通知时不重复展示镜像通知。work_id/announcement_id 对不关联作品的书单审核可为空，target_type/target_id/proposal_id 用于审核目标。作品删除、来源隐藏、NSFW 和深渊过滤同步作用于列表、total 和 unread_count。标记已读更新原通知行，V1/V2 不复制已读事实。

V1 只展示真实 Discord 帖子或书单可表达的通知。作品公告适配成原 thread_update wire，不将 Fluxer thread_id 伪装成 Discord ID；过滤条件同样用于 total/unread-count。event_source_platform 将同雪花的 Discord/Fluxer 来源隔离，旧平台删除或按线程已读不会误伤原生记录。

`NotificationFanoutService.fanout_work_created(work_id, thread_id, author_id, platform, created_at)` 复用调用方 session，不提交事务。事件 snapshot.created_at 来自原生 ThreadState 创建时间（缺状态时取 Snowflake 时间），不是首次索引时间。只给 followed_at <= 实际创建时间的作者关注者写入通知，排作者并按平台来源幂等；重新索引历史作品不会给后来关注者补发通知。
