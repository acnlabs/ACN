"""Blob objects for A2A FilePart. Mailbox hold + consumer-paid extend."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Header, Path, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ..core.errors import ACN_DEFAULT_RESPONSES, ACNErrorResponse, ACNHTTPError, ErrorCode
from ..services.blob_service import BlobService, safe_blob_filename, safe_blob_mime_type
from .dependencies import AgentApiKeyDep, AgentServiceDep, limiter

router = APIRouter(
    prefix="/api/v1/blobs",
    tags=["blobs"],
    responses={
        **ACN_DEFAULT_RESPONSES,
        402: {
            "model": ACNErrorResponse,
            "description": "Payment required — insufficient balance or billing unavailable.",
        },
        413: {
            "model": ACNErrorResponse,
            "description": "Uploaded file exceeds the per-file size limit.",
        },
    },
)

_blob_service: BlobService | None = None

BlobIdPath = Annotated[str, Path(max_length=64, description="Blob object id")]


def init_blob_service(service: BlobService) -> None:
    global _blob_service
    _blob_service = service


def get_blob_service() -> BlobService:
    if _blob_service is None:
        raise RuntimeError("BlobService not initialized")
    return _blob_service


BlobServiceDep = Annotated[BlobService, Depends(get_blob_service)]


class ExtendBody(BaseModel):
    extra_days: int = Field(..., ge=1, le=90)
    sig: str | None = None
    exp: int | None = None  # ignored; HMAC is blob_id-only, expiry is meta.exp


def _safe_filename(name: object) -> str:
    return safe_blob_filename(name)


@router.post("")
@limiter.limit("30/minute")
async def upload_blob(
    request: Request,
    agent_info: AgentApiKeyDep,
    blobs: BlobServiceDep,
    file: UploadFile = File(...),
    ttl_seconds: int | None = Form(default=None),
) -> dict:
    data = await file.read()
    name = file.filename or "file"
    mime = file.content_type or "application/octet-stream"
    return await blobs.put(
        owner_id=agent_info["agent_id"],
        data=data,
        name=name,
        mime_type=mime,
        ttl_seconds=ttl_seconds,
    )


@router.get("/usage")
async def blob_usage(agent_info: AgentApiKeyDep, blobs: BlobServiceDep) -> dict:
    return await blobs.usage(agent_info["agent_id"])


@router.post("/{blob_id}/extend")
@limiter.limit("30/minute")
async def extend_blob(
    request: Request,
    blob_id: BlobIdPath,
    body: ExtendBody,
    agent_info: AgentApiKeyDep,
    blobs: BlobServiceDep,
) -> dict:
    return await blobs.extend(
        blob_id,
        agent_info["agent_id"],
        body.extra_days * 86400,
        sig=body.sig,
    )


@router.get("/{blob_id}")
@limiter.limit("60/minute")
async def download_blob(
    request: Request,
    blob_id: BlobIdPath,
    blobs: BlobServiceDep,
    agent_service: AgentServiceDep,
    authorization: str | None = Header(default=None),
    sig: str | None = Query(default=None),
    exp: int | None = Query(default=None),
) -> Response:
    caller_id: str | None = None
    if authorization and authorization.startswith("Bearer "):
        agent = await agent_service.get_agent_by_api_key(authorization[7:])
        if agent is not None:
            caller_id = agent.agent_id
        elif not sig:
            raise ACNHTTPError(
                ErrorCode.AUTHENTICATION_REQUIRED,
                401,
                details={"reason": "invalid_api_key"},
            )
    _ = exp  # accepted on old FilePart URIs; HMAC is blob_id-only
    data, meta = await blobs.get_bytes(blob_id, caller_id=caller_id, sig=sig)
    filename = _safe_filename(meta.get("name"))
    headers = {
        "content-disposition": f'attachment; filename="{filename}"',
        "x-acn-blob-sha256": str(meta.get("sha256") or ""),
    }
    return Response(
        content=data,
        media_type=safe_blob_mime_type(meta.get("mime_type")),
        headers=headers,
    )
