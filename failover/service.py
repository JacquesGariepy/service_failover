from abc import ABC, abstractmethod
import aioping
import socket
import asyncio
from typing import Optional, Tuple, Dict, Any, Protocol
from urllib.parse import urlparse
from datetime import datetime
import logging
import configparser

# Load configuration from config file
config = configparser.ConfigParser()
config.read('config.ini')

logger = logging.getLogger(__name__)

class MetricsCollectorProtocol(Protocol):
    def record_health_check(self, is_healthy: bool) -> None: ...
    def record_error(self, error_type: str, message: str) -> None: ...

class CacheProtocol(Protocol):
    def get(self, key: str) -> Any: ...
    def set(self, key: str, value: Any) -> None: ...

class ConnectionPoolProtocol(Protocol):
    async def __aenter__(self) -> None: ...
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None: ...

class HealthStatus:
    def __init__(self):
        self.timestamp = datetime.now()
        self.dns_check = {"status": False, "message": "", "duration": 0}
        self.ping_check = {"status": False, "message": "", "duration": 0}
        self.overall_status = False
        self.error_message = ""

    def to_dict(self) -> Dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "dns_check": self.dns_check,
            "ping_check": self.ping_check,
            "overall_status": self.overall_status,
            "error_message": self.error_message
        }

    def __str__(self) -> str:
        status_symbol = "✅" if self.overall_status else "❌"
        return f"""
╔════════════════════ Health Check Report ════════════════════╗
║ Timestamp: {self.timestamp.strftime('%Y-%m-%d %H:%M:%S')}
║ Overall Status: {status_symbol} {'Healthy' if self.overall_status else 'Unhealthy'}
║
║ DNS Check:
║   Status: {'✅' if self.dns_check['status'] else '❌'}
║   Duration: {self.dns_check['duration']:.3f}s
║   {self.dns_check['message'] if not self.dns_check['status'] else 'OK'}
║
║ Ping Check:
║   Status: {'✅' if self.ping_check['status'] else '❌'}
║   Duration: {self.ping_check['duration']:.3f}s
║   {self.ping_check['message'] if not self.ping_check['status'] else 'OK'}
║
║ {f'Error: {self.error_message}' if self.error_message else ''}
╚═══════════════════════════════════════════════════════════╝"""

class Service(ABC):
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.metrics: MetricsCollectorProtocol = self._create_metrics_collector()
        self.cache: CacheProtocol = self._create_cache()
        self.connection_pool: ConnectionPoolProtocol = self._create_connection_pool()
        self._last_health_status: Optional[HealthStatus] = None
        self.delay_threshold = config.getfloat('DEFAULT', 'DELAY_THRESHOLD', fallback=1.0)
        self.timeout = config.getfloat('DEFAULT', 'TIMEOUT', fallback=5.0)
        logger.info(f"Service initialized with base_url={base_url}")

    def _create_metrics_collector(self) -> MetricsCollectorProtocol:
        from .metrics import MetricsCollector
        return MetricsCollector()

    def _create_cache(self) -> CacheProtocol:
        from .cache import Cache
        return Cache()

    def _create_connection_pool(self) -> ConnectionPoolProtocol:
        from .connection_pool import ConnectionPool
        return ConnectionPool()

    @abstractmethod
    async def request(self, endpoint: str, method: str = 'GET', params: Dict = None, data: Dict = None) -> str:
        pass

    async def _check_dns(self, hostname: str, timeout: float = None) -> Tuple[bool, Optional[str], float]:
        """
        Check if DNS resolution works for the given hostname.
        Returns (success, error_message, duration)
        """
        timeout = timeout or self.timeout
        logger.debug(f"Checking DNS for hostname={hostname}")
        start_time = asyncio.get_event_loop().time()
        try:
            await asyncio.get_event_loop().getaddrinfo(hostname, None)
            duration = asyncio.get_event_loop().time() - start_time
            return True, None, duration
        except socket.gaierror as e:
            duration = asyncio.get_event_loop().time() - start_time
            return False, f"DNS resolution failed: {str(e)}", duration
        except asyncio.TimeoutError:
            duration = asyncio.get_event_loop().time() - start_time
            return False, "DNS resolution timed out", duration
        except Exception as e:
            duration = asyncio.get_event_loop().time() - start_time
            return False, f"Unexpected error during DNS resolution: {str(e)}", duration

    async def _check_ping(self, hostname: str, timeout: float = None) -> Tuple[bool, Optional[str], float]:
        """
        Check if host responds to ping within acceptable delay.
        Returns (success, error_message, duration)
        """
        timeout = timeout or self.timeout
        logger.debug(f"Checking ping for hostname={hostname}")
        start_time = asyncio.get_event_loop().time()
        try:
            delay = await asyncio.wait_for(aioping.ping(hostname), timeout=timeout)
            duration = asyncio.get_event_loop().time() - start_time
            if delay >= self.delay_threshold:
                return False, f"High latency detected: {delay:.2f}s", duration
            return True, None, duration
        except asyncio.TimeoutError:
            duration = asyncio.get_event_loop().time() - start_time
            return False, "Ping timed out", duration
        except OSError as e:
            duration = asyncio.get_event_loop().time() - start_time
            return False, f"Network error during ping: {str(e)}", duration
        except Exception as e:
            duration = asyncio.get_event_loop().time() - start_time
            return False, f"Unexpected error during ping: {str(e)}", duration

    async def health_check(self, timeout: float = None) -> HealthStatus:
        """
        Comprehensive health check that verifies DNS resolution and network connectivity.
        Returns HealthStatus object containing detailed check results.
        
        Args:
            timeout: Maximum time in seconds to wait for health check
        """
        timeout = timeout or self.timeout
        logger.info(f"Performing health check for service with base_url={self.base_url}")
        health_status = HealthStatus()
        
        try:
            parsed_url = urlparse(self.base_url)
            hostname = parsed_url.hostname
            
            if not hostname:
                health_status.error_message = "Invalid URL format: missing hostname"
                return health_status

            # Check DNS
            dns_ok, dns_error, dns_duration = await self._check_dns(hostname, timeout)
            health_status.dns_check = {
                "status": dns_ok,
                "message": dns_error if not dns_ok else "",
                "duration": dns_duration
            }

            # Check ping if DNS is successful
            if dns_ok:
                ping_ok, ping_error, ping_duration = await self._check_ping(hostname, timeout)
                health_status.ping_check = {
                    "status": ping_ok,
                    "message": ping_error if not ping_ok else "",
                    "duration": ping_duration
                }
            
            health_status.overall_status = dns_ok and (not dns_ok or health_status.ping_check["status"])

        except Exception as e:
            health_status.error_message = f"Health check failed: {str(e)}"

        self._last_health_status = health_status
        return health_status
