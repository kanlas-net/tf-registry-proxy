"""tf-registry-proxy: caching reverse proxy for the Terraform module registry protocol.

The package is dependency-free: it only uses the Python standard library so it can run
in a slim container without a build step.
"""

__version__ = "1.1.0"
__all__ = ["__version__"]
