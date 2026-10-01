"""Optional tiny HTTP endpoint for hosts that insist on an open port (set PORT)."""
import asyncio
import logging

log = logging.getLogger(__name__)


async def start_health_server(port: int):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await asyncio.wait_for(reader.read(1024), timeout=5)
        except Exception:
            pass
        try:
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "0.0.0.0", port)
    log.info("health endpoint listening on :%s", port)
    return server
