"""Webhook API — receives incoming HTTP POSTs and publishes them as bus events."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/v1/webhook", tags=["webhook"])

_producer_manager = None


def init(producer_manager):
    global _producer_manager
    _producer_manager = producer_manager


def _get_webhook_producer():
    from aiventbus.producers.webhook import WebhookProducer

    if not _producer_manager:
        raise HTTPException(status_code=503, detail="Webhook producer not initialized")
    producer = _producer_manager.get("webhook")
    if not producer or not producer.is_running:
        raise HTTPException(status_code=503, detail="Webhook producer is disabled")
    return producer


@router.post("/{topic_path:path}")
async def receive_webhook(topic_path: str, request: Request):
    """Receive a webhook POST and publish it as a bus event.

    The URL path after ``/api/v1/webhook/`` becomes the topic suffix.
    For example, ``POST /api/v1/webhook/github/push`` → topic ``webhook.github.push``.

    Body can be JSON (preferred) or form data.  Optional headers:

    - ``Authorization: Bearer <secret>`` — validated if ``webhook_secret`` is set
    - ``X-Hub-Signature-256`` — GitHub HMAC; validated if ``webhook_secret`` is set
    - ``X-Priority`` — event priority (low/medium/high/critical)
    - ``X-Dedupe-Key`` — deduplication key
    - ``X-Source`` — custom source identifier
    """
    if not topic_path:
        raise HTTPException(status_code=400, detail="Topic path required (e.g. /api/v1/webhook/github/push)")

    producer = _get_webhook_producer()

    # Auth check: Bearer token or HMAC signature
    body = await request.body()
    auth_header = request.headers.get("authorization")
    sig_header = request.headers.get("x-hub-signature-256")

    if producer.secret:
        if sig_header:
            if not producer.verify_signature(body, sig_header):
                raise HTTPException(status_code=401, detail="Invalid signature")
        elif not producer.verify_bearer(auth_header):
            raise HTTPException(status_code=401, detail="Invalid or missing authorization")

    # Parse body
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")
    elif "form" in content_type:
        form = await request.form()
        payload = dict(form)
    else:
        # Try JSON, fall back to raw text
        try:
            payload = await request.json()
        except Exception:
            payload = {"raw": body.decode("utf-8", errors="replace")}

    if not isinstance(payload, dict):
        payload = {"data": payload}

    event_id = await producer.receive(
        topic_path=topic_path,
        payload=payload,
        source=request.headers.get("x-source"),
        priority=request.headers.get("x-priority"),
        dedupe_key=request.headers.get("x-dedupe-key"),
    )

    return {"event_id": event_id, "topic": f"webhook.{topic_path.replace('/', '.')}"}
