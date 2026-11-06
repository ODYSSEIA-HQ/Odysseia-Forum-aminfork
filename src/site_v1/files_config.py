"""上传、存储、分级的配置（环境变量），和 config.py 分开，免得几个领域同时改一个文件。

存储：
- SITE_STORAGE_BACKEND=local（默认，没配 S3 桶时）/ s3（Cloudflare R2 等 S3 兼容存储）。
- local：文件落在 SITE_STORAGE_LOCAL_DIR，直传地址和读取地址是本服务的签名端点 /api/storage/...。
- s3：预签名 PUT / GET，凭证见 SITE_S3_*。

分级：
- SITE_CLASSIFIER=stub（FLUXER_FAKE 或测试钩子打开时默认）/ http（外部分级服务）/ manual（生产没配分级服务时默认：
  图片一律判「拿不准」，按 R18 处理并进人工审核）。
"""

import logging
import os
import secrets
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError as exc:
        raise RuntimeError(f"{name} 必须是整数，当前值 {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        return float(raw) if raw not in (None, "") else default
    except ValueError as exc:
        raise RuntimeError(f"{name} 必须是数字，当前值 {raw!r}") from exc


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class FilesConfig:
    storage_backend: str
    """local / s3。"""

    local_dir: str
    """本地存储根目录。"""

    public_base: str
    """本服务对外的根地址（如 https://forum.example.com），本地存储拼签名地址用；
    留空时上传地址按请求的 Host 拼，读取地址写相对路径 /api/storage/...。"""

    signing_key: bytes
    """本地存储签名密钥；多进程部署必须显式配置，否则每个进程各自随机。"""

    ticket_ttl: int
    """直传地址有效期（秒）。"""

    read_ttl: int
    """R18 图片和下载的签名地址有效期（秒，协议：10 分钟）。"""

    public_read_ttl: int
    """全年龄图片读取地址有效期（没有公开桶地址时用签名地址）。"""

    s3_endpoint: str
    s3_region: str
    s3_bucket: str
    s3_access_key: str
    s3_secret_key: str
    s3_public_base: str
    """全年龄图片的公开地址前缀（R2 自定义域名）；留空时也用签名地址。"""

    classifier: str
    """stub / http / manual。"""

    classifier_url: str
    classifier_token: str
    classifier_timeout: float
    classifier_r18_threshold: float
    classifier_r18g_threshold: float
    classifier_confidence: float
    """最高分低于它就算「拿不准」（suspect），进人工审核。"""

    classify_inline_wait: float
    """POST /uploads/{id}/complete 最多等分级多少秒，超时返回 processing。"""


@lru_cache(maxsize=1)
def get_files_config() -> FilesConfig:
    bucket = os.environ.get("SITE_S3_BUCKET", "").strip()
    backend = os.environ.get("SITE_STORAGE_BACKEND", "").strip() or (
        "s3" if bucket else "local"
    )
    if backend not in {"local", "s3"}:
        raise RuntimeError("SITE_STORAGE_BACKEND 只能是 local 或 s3")
    if backend == "s3" and not bucket:
        raise RuntimeError("SITE_STORAGE_BACKEND=s3 需要 SITE_S3_BUCKET")

    raw_key = os.environ.get("SITE_STORAGE_SIGNING_KEY", "")
    if raw_key:
        signing_key = raw_key.encode()
    else:
        signing_key = secrets.token_bytes(32)
        if backend == "local":
            logger.warning(
                "SITE_STORAGE_SIGNING_KEY 没配置：本地存储签名密钥随机生成，只在本进程有效"
            )

    test_mode = _flag("FLUXER_FAKE") or _flag("SITE_TEST_HOOKS")
    classifier = os.environ.get("SITE_CLASSIFIER", "").strip() or (
        "http"
        if os.environ.get("SITE_CLASSIFIER_URL")
        else ("stub" if test_mode else "manual")
    )
    if classifier not in {"stub", "http", "manual"}:
        raise RuntimeError("SITE_CLASSIFIER 只能是 stub、http 或 manual")
    if classifier == "http" and not os.environ.get("SITE_CLASSIFIER_URL"):
        raise RuntimeError("SITE_CLASSIFIER=http 需要 SITE_CLASSIFIER_URL")

    return FilesConfig(
        storage_backend=backend,
        local_dir=os.environ.get("SITE_STORAGE_LOCAL_DIR", "")
        or str(REPO_ROOT / "data" / "site_uploads"),
        public_base=os.environ.get("SITE_PUBLIC_BASE", "").rstrip("/"),
        signing_key=signing_key,
        ticket_ttl=_int("SITE_UPLOAD_TICKET_TTL", 3600),
        read_ttl=_int("SITE_SIGNED_URL_TTL", 600),
        public_read_ttl=_int("SITE_PUBLIC_URL_TTL", 86400),
        s3_endpoint=os.environ.get("SITE_S3_ENDPOINT", ""),
        s3_region=os.environ.get("SITE_S3_REGION", "auto"),
        s3_bucket=bucket,
        s3_access_key=os.environ.get("SITE_S3_ACCESS_KEY_ID", ""),
        s3_secret_key=os.environ.get("SITE_S3_SECRET_ACCESS_KEY", ""),
        s3_public_base=os.environ.get("SITE_S3_PUBLIC_BASE", "").rstrip("/"),
        classifier=classifier,
        classifier_url=os.environ.get("SITE_CLASSIFIER_URL", ""),
        classifier_token=os.environ.get("SITE_CLASSIFIER_TOKEN", ""),
        classifier_timeout=_float("SITE_CLASSIFIER_TIMEOUT", 15.0),
        classifier_r18_threshold=_float("SITE_CLASSIFIER_R18_THRESHOLD", 0.5),
        classifier_r18g_threshold=_float("SITE_CLASSIFIER_R18G_THRESHOLD", 0.5),
        classifier_confidence=_float("SITE_CLASSIFIER_CONFIDENCE", 0.7),
        classify_inline_wait=_float("SITE_CLASSIFY_INLINE_WAIT", 3.0),
    )


def reload_files_config() -> FilesConfig:
    """测试改环境变量后调用；同时丢掉已建的存储和分级器实例。"""
    get_files_config.cache_clear()
    from site_v1.classifier import set_classifier
    from site_v1.storage import set_storage

    set_storage(None)
    set_classifier(None)
    return get_files_config()
