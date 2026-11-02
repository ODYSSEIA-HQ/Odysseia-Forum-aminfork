# 统一作品 Banner

V2 推荐申请沿用 banner_application、banner_waitlist 和 banner_carousel。原生作品使用 target_type=3、真实 work_id 和空 thread_id，不在 Discord 编号字段放置作品编号。频道赛事维持 target_type=2 和原审核入口。

作者在作品详情申请，个人页查看状态；管理在个人页审核原生作品申请。审批直接进入真实轮播或 FIFO 候补，不创建无消费者的异步任务。容量为全站 3 条、每频道 8 条，展示 3 天。既有候补优先占据空位；同一范围由 PostgreSQL 事务锁串行化审批、晋升和清理。

接口：
- GET /v2/works/{id}/banner-scopes 返回允许的范围。
- POST /v2/works/{id}/banner-applications 接受 target_scope（默认 global）和可选 cover_image_url。
- GET /v2/me/banner-applications 与 GET /v2/admin/banner-applications 接受 offset、limit，返回 Page。
- PATCH /v2/admin/banner-applications/{id} 接受 approve、可选 reason。
- GET /v2/banner 返回可见作品和频道赛事；作品项 image_url 优先使用申请自定义封面，默认作品首图。

fluxer_owner_id 是可合并的业务归属；applicant_id、reviewer_id、fluxer_applicant_id、fluxer_reviewer_id 保留实际历史审计。身份合并仅迁移归属，不改申请/审核历史。

先执行 work_banner_extension（下接 work_layer_prepare），再启动引用新列的进程。迁移回填旧 Discord 帖子的 work_id 和已知统一业务归属，不修改历史申请或审核身份。V1 无法表达原生作品定位，明确排除 target_type=3，不虚构编号，也不从候补补位掩盖真实容量。

验证覆盖真实隔离 PostgreSQL 的申请、HTTP 权限、审批、全站/频道容量、FIFO 晋升、三天时长与实际审计；另有 V1 排除和 Discord 状态展示测试。无需连接默认 Redis 或默认数据库。网页包含作者申请、个人状态、管理审核及大整数 ID 回归。
