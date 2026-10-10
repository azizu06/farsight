"""Stdlib helpers shared by tools.ready and tools.measure, which run on the Jetson or the Mac."""

import json
import math
import ssl
import urllib.request


def tls_context(ca_file=None, insecure=False):
    """None for plain HTTP or the system trust store; a custom context for a private CA or self-signed check."""
    if insecure:
        return ssl._create_unverified_context()
    if ca_file:
        return ssl.create_default_context(cafile=ca_file)
    return None


def fetch_json(url, path, context=None, timeout=2):
    with urllib.request.urlopen(url.rstrip("/") + path, timeout=timeout, context=context) as response:
        return json.load(response)


def percentile(values, fraction):
    """Nearest-rank percentile; None for an empty list."""
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None
