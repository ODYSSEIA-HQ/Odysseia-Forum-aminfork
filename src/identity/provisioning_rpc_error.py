"""离线预建工具只暴露不含凭证的错误代号。"""

import re


class ProvisioningRpcError(Exception):
    """服务错误分类与是否可重试；不保留远端任意错误文本。"""

    def __init__(self, code, retryable=False):
        """只保存安全错误代号和明确的可重试标记。"""
        self.code = (
            code
            if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code)
            else "INVALID_RESPONSE"
        )
        self.retryable = retryable is True
        super().__init__(self.code)
