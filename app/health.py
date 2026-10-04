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

    try:
        server = await asyncio.start_server(handle, "0.0.0.0", port)
    except OSError as e:  # e.g. something else (python -m http.server) already uses the port
        log.warning("could not open the health endpoint on :%s (%s) - continuing without it", port, e)
        return None
    log.info("health endpoint listening on :%s", port)
    return server
