from __future__ import annotations

import time
import uuid

from fastapi import Request


async def request_logging_middleware(request: Request, call_next):
    correlation_id = request.headers.get('X-Correlation-ID') or str(uuid.uuid4())
    request.state.correlation_id = correlation_id
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
    response.headers['X-Correlation-ID'] = correlation_id
    response.headers['X-Response-Time-Ms'] = str(elapsed_ms)
    return response
