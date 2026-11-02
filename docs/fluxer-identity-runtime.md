# Fluxer 身份运行规则

`FORUM_AUTH_MODE` 默认是 `legacy`；只有完成统一用户导入、Discord 绑定投影、历史回填和迁移审计后才可显式设置为 `fluxer`。身份权威在 Fluxer，Forum 的 `user` 和 `discord_identity` 是本地只读投影，不创建或猜测 Fluxer ID。运行迁移允许原生 Fluxer 用户的旧 Discord 字段为空；历史 Discord ID 保留作来源和审计。

Fluxer 用户可以浏览公开内容、管理自己的收藏和书单，以及关注作者和作品。作者页面、作者筛选、通知和作品展示使用统一用户 ID，Discord 内容的帖子、频道及公开链接仍使用原有 Discord ID。API 中的用户 ID 以字符串输出。旧模式继续使用 Discord 身份及旧字段。

首期涉及 Discord 成员身份组或 Discord 操作的功能（例如向 Discord 发布书单、Banner 申请、标签治理）要求账户绑定 Discord。原生 Fluxer 用户没有绑定时不能执行这些操作，但可以使用普通浏览、收藏和书单。Discord 角色和机器人管理员权限只按已验证的 Discord ID 查询，不能把 Fluxer ID 发送给 Discord。

机器人写入以 Discord 数值 ID 作为来源，先查询本地绑定投影；映射不存在时写入失败或延迟重试，不能把 Discord ID 充当 Fluxer ID。系统事件的零或空作者不创建用户。账号绑定发生变化后应及时同步本地投影；权威主体与投影不一致时，依赖 Discord 权限的操作必须等待投影收敛。
