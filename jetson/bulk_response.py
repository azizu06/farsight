"""Disconnect-safe bulk HTTP bodies, independent of Starlette ASGI versions."""

import asyncio
from contextlib import suppress

from starlette.requests import ClientDisconnect
from starlette.responses import Response, StreamingResponse


class PacedStreamingResponse(StreamingResponse):
    def __init__(self, source, pacer, **options):
        self.source = source
        super().__init__(pacer.chunks(source), **options)

    async def __call__(self, scope, receive, send):
        writer = asyncio.create_task(self.stream_response(send))
        disconnected = asyncio.create_task(self.listen_for_disconnect(receive))
        tasks = (writer, disconnected)
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if disconnected in done:
                disconnected.result()
            if writer in done:
                writer.result()
        except (OSError, ClientDisconnect):
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # A generator suspended at yield can still own its FIFO slot even
            # after an ASGI send is cancelled. Explicitly close it after the
            # writer exits, before handing the shared budget to other streams.
            for iterator in (self.body_iterator, self.source):
                close = getattr(iterator, "aclose", None)
                if close:
                    with suppress(asyncio.CancelledError):
                        await close()
        if self.background is not None:
            await self.background()


def bulk_response(body, pacer, *, media_type, headers=None):
    if not pacer.enabled:
        return Response(body, media_type=media_type, headers=headers)
    headers = dict(headers or {})
    if not any(key.lower() == "content-length" for key in headers):
        headers["Content-Length"] = str(len(body))
    return PacedStreamingResponse(body, pacer, media_type=media_type, headers=headers)
