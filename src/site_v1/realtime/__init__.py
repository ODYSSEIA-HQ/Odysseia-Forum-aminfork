"""实时推送（Centrifugo）：前端 src/shared/realtime/centrifugo.ts 的后端一侧。

- config.py     环境变量（CENTRIFUGO_*）
- jwt.py        HS256 连接令牌、订阅令牌
- channels.py   频道命名、订阅权限（复用 work_access / chat_messages.resolve）
- publisher.py  Centrifugo HTTP server API（publish / disconnect / unsubscribe），后台任务
- events.py     外层事件信封、收件人上下文、踢出看不到作品的订阅者
- hooks.py      ORM 钩子：事务提交后发布提醒、公告、作品、资格、禁言、上传事件
聊天消息的面板事件在 services/chat_messages.py 里 commit 之后发布。说明见 docs/site_v1/README.md「实时推送」。
"""
