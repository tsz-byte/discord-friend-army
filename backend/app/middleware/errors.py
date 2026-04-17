from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from app.core.exceptions import AppError


async def app_error_handler(request: Request, exc: AppError):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            'error': {
                'code': exc.code,
                'message': exc.message,
                'correlation_id': getattr(request.state, 'correlation_id', None),
            }
        },
    )


async def generic_error_handler(request: Request, exc: Exception):
    if isinstance(exc, HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                'error': {
                    'code': 'http_error',
                    'message': str(exc.detail),
                    'correlation_id': getattr(request.state, 'correlation_id', None),
                }
            },
        )
    return JSONResponse(
        status_code=500,
        content={
            'error': {
                'code': 'internal_error',
                'message': 'An unexpected error occurred',
                'correlation_id': getattr(request.state, 'correlation_id', None),
            }
        },
    )
