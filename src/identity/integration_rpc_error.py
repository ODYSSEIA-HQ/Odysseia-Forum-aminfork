"""受信跨服务操作的结构化失败。"""


class IntegrationRpcError(Exception):
    """保留非敏感错误码与是否可重试，不记录原始会话。"""

    def __init__(self, code, retryable):
        """仅携带平台定义的错误类别。"""
        self.code = code
        self.retryable = bool(retryable)
        super().__init__(code)
