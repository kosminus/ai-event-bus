"""WebSocket hub for real-time event streaming."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()

_ws_hub = None


def init(ws_hub):
    global _ws_hub
    _ws_hub = ws_hub


logger = logging.getLogger(__name__)


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    sub_id = _ws_hub.register(ws)
    logger.info("WebSocket client connected (sub_id=%d)", sub_id)

    try:
        while True:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
                action = msg.get("action")
                if action == "subscribe":
                    channels = set(msg.get("channels", []))
                    _ws_hub.update_channels(sub_id, channels)
                elif action == "unsubscribe":
                    channels = set(msg.get("channels", []))
                    # Remove these channels from subscription
                    current_ws, current_channels = _ws_hub._subscribers.get(sub_id, (None, set()))
                    if current_ws:
                        _ws_hub.update_channels(sub_id, current_channels - channels)
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        _ws_hub.unregister(sub_id)
        logger.info("WebSocket client disconnected (sub_id=%d)", sub_id)
