"""已完成协议边界校验的统一用户编号。"""


class CanonicalUserId(str):
    """内部调用显式携带身份空间，不再以原始 str/int 猜测来源。"""

    def __new__(cls, value, source_discord_id=None):
        """保留本次真实 Discord 操作者，避免历史审计值改成当前绑定。"""
        instance = super().__new__(cls, value)
        instance.source_discord_id = source_discord_id
        return instance
