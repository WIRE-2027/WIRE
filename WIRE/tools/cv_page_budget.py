"""Bound material pages while keeping complete records available for pagination."""

import json

PAGE_BYTES = 128 * 1024


def bounded_material_page(read_page, limit):
    page = read_page(limit)
    while len(json.dumps(page, ensure_ascii=False).encode("utf-8")) > PAGE_BYTES and limit > 1:
        limit = max(1, limit // 2)
        page = read_page(limit)
    return page
