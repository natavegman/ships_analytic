"""
DB package for Quotas_analytic.

This module intentionally keeps imports light to reduce side-effects at import time.
"""

from .models import Base  # noqa: F401
from .catch_allocator import CatchAllocator  # noqa: F401

