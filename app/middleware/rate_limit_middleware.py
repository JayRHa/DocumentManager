"""
Rate limiting middleware for FastAPI to prevent brute force attacks and API abuse.
"""
import time
from typing import Dict, Iterable, Optional, Tuple
from collections import defaultdict
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.status import HTTP_429_TOO_MANY_REQUESTS
import asyncio
from threading import Lock

class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Rate limiting middleware that tracks requests per IP address.
    
    Features:
    - Per-IP rate limiting
    - Different limits for different endpoints
    - Sliding window rate limiting
    - Automatic cleanup of old entries
    """
    
    def __init__(
        self,
        app,
        default_limit: int = 100,  # requests per window
        window_seconds: int = 60,  # 1 minute window
        login_limit: int = 5,  # stricter limit for login attempts
        login_window_seconds: int = 300,  # 5 minute window for login
        cleanup_interval: int = 300,  # cleanup every 5 minutes
        trusted_proxy_ips: Optional[Iterable[str]] = None,
    ):
        super().__init__(app)
        self.default_limit = default_limit
        self.window_seconds = window_seconds
        self.login_limit = login_limit
        self.login_window_seconds = login_window_seconds
        self.cleanup_interval = cleanup_interval
        self.trusted_proxy_ips = set(trusted_proxy_ips or ())
        
        # Store request counts: {ip: {endpoint: [(timestamp, count)]}}
        self.request_counts: Dict[str, Dict[str, list]] = defaultdict(lambda: defaultdict(list))
        self.lock = Lock()
        
        # Special rate limits for specific endpoints
        self.endpoint_limits = {
            "/api/auth/login": (self.login_limit, self.login_window_seconds),
            "/api/auth/setup/initial-user": (self.login_limit, self.login_window_seconds),
            "/api/documents/upload": (20, 60),  # 20 uploads per minute
            "/api/ai/chat": (30, 60),  # 30 AI requests per minute
            "/api/ai/extract": (20, 60),  # 20 extraction requests per minute
        }
        
        # Start cleanup task
        asyncio.create_task(self._cleanup_old_entries())
    
    async def _cleanup_old_entries(self):
        """Periodically clean up old request entries to prevent memory bloat."""
        while True:
            await asyncio.sleep(self.cleanup_interval)
            current_time = time.time()
            
            with self.lock:
                # Clean up old entries
                for ip in list(self.request_counts.keys()):
                    for endpoint in list(self.request_counts[ip].keys()):
                        # Remove entries older than the largest window
                        max_window = max(self.window_seconds, self.login_window_seconds)
                        self.request_counts[ip][endpoint] = [
                            (ts, count) for ts, count in self.request_counts[ip][endpoint]
                            if current_time - ts < max_window * 2
                        ]
                        
                        # Remove empty endpoints
                        if not self.request_counts[ip][endpoint]:
                            del self.request_counts[ip][endpoint]
                    
                    # Remove empty IPs
                    if not self.request_counts[ip]:
                        del self.request_counts[ip]
    
    def get_client_ip(self, request: Request) -> str:
        """Extract client IP address from request."""
        direct_ip = request.client.host if request.client else "unknown"

        # Forwarding headers are attacker-controlled unless the direct peer is
        # a configured reverse proxy. Never let an arbitrary client choose the
        # rate-limit bucket used for its request.
        if direct_ip in self.trusted_proxy_ips:
            forwarded_for = request.headers.get("X-Forwarded-For")
            if forwarded_for:
                return forwarded_for.split(",")[0].strip()

            real_ip = request.headers.get("X-Real-IP")
            if real_ip:
                return real_ip.strip()

        return direct_ip
    
    def get_rate_limit(self, path: str) -> Tuple[int, int]:
        """Get rate limit for a specific path."""
        # Check if path has a specific limit
        for endpoint_path, limits in self.endpoint_limits.items():
            if path.startswith(endpoint_path):
                return limits
        
        # Return default limits
        return self.default_limit, self.window_seconds
    
    def is_rate_limited(self, ip: str, endpoint: str, limit: int, window: int) -> Tuple[bool, Optional[int]]:
        """Check if the IP has exceeded the rate limit for this endpoint."""
        current_time = time.time()
        window_start = current_time - window
        
        with self.lock:
            # Get request history for this IP and endpoint
            request_history = self.request_counts[ip][endpoint]
            
            # Count requests in the current window
            recent_requests = sum(
                count for ts, count in request_history
                if ts >= window_start
            )
            
            if recent_requests >= limit:
                # Calculate when the oldest request in the window will expire
                oldest_in_window = min(
                    ts for ts, count in request_history
                    if ts >= window_start
                )
                retry_after = int(oldest_in_window + window - current_time) + 1
                return True, retry_after
            
            # Add current request
            # Consolidate requests within the same second
            if request_history and current_time - request_history[-1][0] < 1:
                # Increment the count for the current second
                request_history[-1] = (request_history[-1][0], request_history[-1][1] + 1)
            else:
                # Add new entry
                request_history.append((current_time, 1))
            
            return False, None
    
    async def dispatch(self, request: Request, call_next):
        """Process the request and apply rate limiting."""
        # Skip rate limiting for static files and health checks
        if request.url.path.startswith("/static") or request.url.path == "/api/health":
            return await call_next(request)
        
        # Get client IP
        client_ip = self.get_client_ip(request)
        
        # Get rate limit for this endpoint
        limit, window = self.get_rate_limit(request.url.path)
        
        # Check if rate limited
        is_limited, retry_after = self.is_rate_limited(
            client_ip, request.url.path, limit, window
        )
        
        if is_limited:
            # Log rate limit event
            print(f"Rate limit exceeded for IP {client_ip} on {request.url.path}")
            
            # Return 429 response
            return JSONResponse(
                status_code=HTTP_429_TOO_MANY_REQUESTS,
                content={
                    "detail": "Rate limit exceeded",
                    "error": "rate_limit_exceeded",
                    "retry_after": retry_after
                },
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Window": str(window),
                    "X-RateLimit-Remaining": "0"
                }
            )
        
        # Process request
        response = await call_next(request)
        
        # Add rate limit headers
        with self.lock:
            request_history = self.request_counts[client_ip][request.url.path]
            window_start = time.time() - window
            recent_requests = sum(
                count for ts, count in request_history
                if ts >= window_start
            )
            remaining = max(0, limit - recent_requests)
        
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Window"] = str(window)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        
        return response


class RateLimitProtect:
    """
    Helper class for rate limiting in FastAPI applications.
    """
    
    def __init__(self, app=None, **kwargs):
        self.app = app
        self.config = kwargs
        
        if app is not None:
            self.init_app(app)
    
    def init_app(self, app):
        """Initialize rate limiting for the FastAPI app."""
        # Add rate limit middleware
        app.add_middleware(RateLimitMiddleware, **self.config)
