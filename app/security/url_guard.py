"""
SEC-002 / SEC-002b / SEC-NEW-5: SSRF protection.
Validates URLs before fetching to prevent access to private IP ranges.
"""
import logging
import socket
from urllib.parse import urlparse
from ipaddress import ip_address, ip_network

logger = logging.getLogger(__name__)

# Private/reserved IP ranges to block
BLOCKED_NETWORKS = [
    ip_network("127.0.0.0/8"),        # Loopback (127.0.0.0 - 127.255.255.255)
    ip_network("10.0.0.0/8"),         # RFC1918 private
    ip_network("172.16.0.0/12"),      # RFC1918 private
    ip_network("192.168.0.0/16"),     # RFC1918 private
    ip_network("169.254.0.0/16"),     # Link-local
    ip_network("::1/128"),            # IPv6 loopback
    ip_network("fc00::/7"),           # IPv6 private
]

BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain"}


def validate_url(url: str) -> str:
    """
    Validate a URL for SSRF safety before fetching.

    Checks:
    - URL is not None/empty
    - Scheme is http or https only
    - Hostname resolves to an allowed IP (not private/loopback)
    - Hostname is not a blocked literal (localhost)

    Args:
        url: URL string to validate

    Returns:
        The validated URL (unchanged if valid)

    Raises:
        ValueError: If URL is invalid or points to a blocked IP/hostname
    """
    if not url or not isinstance(url, str):
        raise ValueError("URL must be a non-empty string")

    url = url.strip()
    if not url:
        raise ValueError("URL must not be empty")

    parsed = urlparse(url)

    # Check scheme
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"Only http/https schemes allowed; got '{parsed.scheme}' in {url}"
        )

    # Check hostname exists
    hostname = parsed.hostname
    if not hostname:
        raise ValueError(f"URL has no valid hostname: {url}")

    # Check for blocked literal hostnames
    if hostname.lower() in BLOCKED_HOSTNAMES:
        raise ValueError(f"Blocked hostname '{hostname}' in {url}")

    # Resolve hostname and check IP
    try:
        # Use socket.getaddrinfo to resolve (handles both IPv4 and IPv6)
        addr_info = socket.getaddrinfo(hostname, None)
        if not addr_info:
            raise ValueError(f"Could not resolve hostname '{hostname}' in {url}")

        # Check each resolved IP
        for family, socktype, proto, canonname, sockaddr in addr_info:
            resolved_ip = sockaddr[0]
            try:
                ip = ip_address(resolved_ip)
                # Check if IP is in any blocked network
                for blocked_net in BLOCKED_NETWORKS:
                    if ip in blocked_net:
                        raise ValueError(
                            f"URL resolves to blocked IP {resolved_ip} (private/reserved). "
                            f"Hostname: {hostname}. URL: {url}"
                        )
            except ValueError as e:
                # Re-raise our SSRF errors, log others
                if "URL resolves to blocked IP" in str(e):
                    raise
                logger.error(f"Could not parse IP {resolved_ip}: {e}")
                raise ValueError(f"Invalid IP resolved for {hostname}: {resolved_ip}")

    except socket.gaierror as e:
        # DNS resolution failed — treat as invalid (prevents DNS rebind attacks)
        raise ValueError(f"Failed to resolve hostname '{hostname}': {e}")
    except ValueError:
        # Re-raise our validation errors
        raise
    except Exception as e:
        # Catch any other resolution errors
        logger.error(f"Unexpected error resolving {hostname}: {e}")
        raise ValueError(f"Error validating URL {url}: {e}")

    return url
