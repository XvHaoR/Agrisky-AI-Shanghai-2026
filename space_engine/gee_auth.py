"""
Agrisky AI — GEE 鉴权模块
负责 Google Earth Engine 的初始化与认证。
"""

import ee
import os
import logging
import time
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_gee_initialized = False
_gee_unavailable_logged = False
# 失败负缓存：凭证存在但项目无权限/网络不通时，ee.Initialize() 每次会阻塞数十秒。
# 记录失败时刻，在 TTL 内的后续调用直接快速返回，避免每个请求都干等。
_gee_failed_at = 0.0
_GEE_FAILURE_TTL_SEC = 300.0  # 5 分钟后允许重试（便于修好项目/代理后自愈）


def _bounded_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return min(maximum, max(minimum, value))


def _forced_proxy_url() -> str:
    return os.getenv("GEE_PROXY_URL", "").strip()


def _force_proxy_environment() -> str:
    proxy_url = _forced_proxy_url()
    if not proxy_url:
        return ""
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ[name] = proxy_url
    os.environ["NO_PROXY"] = "localhost,127.0.0.1"
    os.environ["no_proxy"] = "localhost,127.0.0.1"
    return proxy_url


def _proxy_http_transport(deadline_ms: int):
    proxy_url = _force_proxy_environment()
    import httplib2

    if not proxy_url:
        return httplib2.Http(timeout=deadline_ms / 1000.0)
    parsed = urlparse(proxy_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.port:
        raise ValueError("GEE_PROXY_URL 必须是包含主机和端口的 http(s) URL")

    proxy_info = httplib2.ProxyInfo(
        httplib2.socks.PROXY_TYPE_HTTP,
        parsed.hostname,
        parsed.port,
        proxy_user=parsed.username,
        proxy_pass=parsed.password,
    )
    return httplib2.Http(proxy_info=proxy_info, timeout=deadline_ms / 1000.0)


def _configure_request_limits() -> None:
    """Keep transient GEE failures from stalling the local demo server."""
    deadline_ms = _bounded_int_env("GEE_REQUEST_DEADLINE_MS", 45000, 5000, 180000)
    max_retries = _bounded_int_env("GEE_API_MAX_RETRIES", 1, 0, 5)
    ee.data.setMaxRetries(max_retries)
    try:
        ee.data.setDeadline(deadline_ms)
    except Exception as exc:  # pragma: no cover - depends on EE client state/version
        logger.debug("GEE 尚未初始化，请求时限将在初始化后设置: %s", exc)


def _has_gee_credentials() -> bool:
    """检测本地是否存在可用的 GEE 凭证。

    无凭证时 ``ee.Initialize()`` 会阻塞在网络握手上，每次失败约 10 秒，
    叠加重试可达数十秒。提前判断可让未认证的本地环境快速回退到 mock。
    """
    explicit_paths = [
        os.getenv("GEE_CREDENTIALS"),
        os.getenv("GOOGLE_APPLICATION_CREDENTIALS"),
    ]
    if any(path and Path(path).is_file() for path in explicit_paths):
        return True
    if os.getenv("EARTHENGINE_TOKEN"):
        return True
    home = Path.home()
    candidates = [
        home / ".config" / "earthengine" / "credentials",  # earthengine authenticate
        home / ".config" / "gcloud" / "application_default_credentials.json",  # gcloud ADC
    ]
    return any(p.exists() for p in candidates)


def init_gee(project_id: str | None = None, max_retries: int = 2) -> bool:
    """初始化 GEE，带重试机制应对间歇性 SSL 中断。

    无本地凭证时立即返回 ``False``，不做注定失败、会阻塞数十秒的网络重试，
    以便在未认证的本地环境下快速回退到 mock 流程。
    """
    global _gee_initialized, _gee_unavailable_logged, _gee_failed_at
    if _gee_initialized:
        return True

    if not _has_gee_credentials():
        if not _gee_unavailable_logged:
            logger.warning(
                "未检测到 GEE 凭证（GEE_CREDENTIALS / GOOGLE_APPLICATION_CREDENTIALS / "
                "EARTHENGINE_TOKEN / "
                "~/.config/earthengine/credentials），跳过 GEE 初始化并回退到 mock"
            )
            _gee_unavailable_logged = True
        return False

    # 失败负缓存：近期已失败过且仍在 TTL 内，直接快速返回，避免每个请求都阻塞数十秒
    if _gee_failed_at and (time.monotonic() - _gee_failed_at) < _GEE_FAILURE_TTL_SEC:
        logger.info("GEE 近期初始化失败（负缓存生效），本次直接回退 mock")
        return False

    for attempt in range(max_retries):
        try:
            _configure_request_limits()
            deadline_ms = _bounded_int_env("GEE_REQUEST_DEADLINE_MS", 45000, 5000, 180000)
            http_transport = _proxy_http_transport(deadline_ms)
            credentials = None
            service_account_file = os.getenv("GEE_CREDENTIALS")
            if service_account_file and Path(service_account_file).is_file():
                credentials = ee.ServiceAccountCredentials(None, service_account_file)
            elif (
                os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
                or (Path.home() / ".config" / "gcloud" / "application_default_credentials.json").is_file()
            ):
                import google.auth

                credentials, detected_project = google.auth.default(scopes=ee.oauth.SCOPES)
                project_id = project_id or detected_project

            if credentials is not None:
                ee.Initialize(
                    credentials=credentials,
                    project=project_id,
                    http_transport=http_transport,
                )
            elif project_id:
                ee.Initialize(project=project_id, http_transport=http_transport)
            else:
                ee.Initialize(http_transport=http_transport)
            _configure_request_limits()
            _gee_initialized = True
            _gee_failed_at = 0.0
            logger.info("GEE 初始化成功")
            return True
        except Exception as e:
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.warning(f"GEE 初始化失败 (尝试 {attempt+1}/{max_retries})，{wait}秒后重试: {e}")
                time.sleep(wait)
            else:
                logger.error(f"GEE 初始化失败 (已重试{max_retries}次): {e}")
                _gee_failed_at = time.monotonic()
                return False
    return False


def get_roi_area_mu(roi_geojson: dict) -> float:
    """
    计算 ROI 区域的面积（亩）。

    Args:
        roi_geojson: GeoJSON 格式的 Polygon

    Returns:
        float: 面积（亩）
    """
    roi = ee.Geometry.Polygon(roi_geojson["coordinates"])
    area_sqm = roi.area().getInfo()
    return round(area_sqm * 0.0015, 2)


if __name__ == "__main__":
    init_gee()
