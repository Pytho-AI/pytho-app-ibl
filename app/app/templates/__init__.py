"""Built-in structured templates for doctrinal intel products.

Each module under `products/` exports `SPEC = {...}` — a schema dict the
agent runner and exporter consume to build, render, and audit one artifact.
This is the schema counterpart to user-uploaded HTML templates living in
`uploads/<file_key>.html`. The two coexist; `handlers/product.py` is the
unified entry point that resolves either form into the prompt and rendering
pipeline.

Authoring is data-only (enum imports + a SPEC dict). All accessor logic and
prompt rendering lives in `handlers/product.py`.
"""
from .common import *  # noqa: F401,F403  — surfaces enums + HEADER_* bedrock
from .products import PRODUCT_SCHEMAS, PRODUCTS

__all__ = ["PRODUCT_SCHEMAS", "PRODUCTS"]
