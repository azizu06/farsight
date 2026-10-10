"""Phone enrollment routes; no motor, navigation or drill scoring actions."""

import asyncio
import base64
import binascii
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from .identity_engine import IdentityError

MAX_BODY = 6 * 1024 * 1024
MAX_IMAGE = 4 * 1024 * 1024


def identity_router(service):
    router = APIRouter()

    @router.get('/phone/identity/enroll', response_class=HTMLResponse)
    async def page():
        return HTMLResponse(Path(__file__).with_name('identity_enroll.html').read_text(),
                            headers={'Cache-Control': 'no-store'})

    @router.get('/phone/identity/status')
    async def status():
        return service.status()

    @router.get('/phone/identity/people')
    async def people():
        return dict(service.status(), people=service.people())

    @router.post('/phone/identity/people')
    async def enroll(request: Request):
        # Streaming limit applies before parsing JSON or allocating decoded images.
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_BODY:
                raise HTTPException(413, 'Enrollment upload is too large')
            body.extend(chunk)
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict) or payload.get('consent') is not True:
                raise ValueError('Confirm that this is your own face and you agree to enrollment')
            name, image = payload.get('name'), payload.get('image')
            person_id = payload.get('person_id')
            if not isinstance(name, str) or not isinstance(image, str):
                raise ValueError('Provide a name and a camera photo')
            if person_id is not None and not isinstance(person_id, str):
                raise ValueError('Invalid enrollment ID')
            raw = base64.b64decode(image, validate=True)
            if not raw or len(raw) > MAX_IMAGE:
                raise ValueError('Photo must be between 1 byte and 4 MiB')
        except (ValueError, binascii.Error, UnicodeError) as exc:
            raise HTTPException(422, str(exc)) from exc
        if not service.status()['ready']:
            raise HTTPException(503, service.error or 'Face recognition models are not configured')
        try:
            return await service.enroll(name, raw, person_id)
        except IdentityError as exc:
            raise HTTPException(429 if 'busy' in str(exc) else 422, str(exc)) from exc

    @router.delete('/phone/identity/people/{person_id}')
    async def remove(person_id: str):
        try:
            removed = await asyncio.to_thread(service.remove, person_id)
        except IdentityError as exc:
            raise HTTPException(503, str(exc)) from exc
        if not removed:
            raise HTTPException(404, 'Enrollment not found')
        return dict(removed=True)

    @router.websocket('/ws/identity')
    async def identities(ws: WebSocket):
        await ws.accept()
        try:
            while True:
                await asyncio.wait_for(ws.send_json(service.snapshot()), .5)
                await asyncio.sleep(.2)
        except (WebSocketDisconnect, asyncio.TimeoutError, RuntimeError):
            return

    return router
