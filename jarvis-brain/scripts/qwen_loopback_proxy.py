#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os

LISTEN_HOST = os.environ.get("QWEN_PROXY_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("QWEN_PROXY_LISTEN_PORT", "8000"))
TARGET_HOST = os.environ.get("QWEN_PROXY_TARGET_HOST", "qwen-analyzer")
TARGET_PORT = int(os.environ.get("QWEN_PROXY_TARGET_PORT", "8000"))


async def pump(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        while True:
            data = await reader.read(256 * 1024)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.write_eof()
        except (AttributeError, ConnectionError, OSError):
            pass


async def handle(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
) -> None:
    upstream_writer: asyncio.StreamWriter | None = None
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(
            TARGET_HOST,
            TARGET_PORT,
        )
        await asyncio.gather(
            pump(client_reader, upstream_writer),
            pump(upstream_reader, client_writer),
        )
    except (ConnectionError, OSError):
        pass
    finally:
        for writer in (upstream_writer, client_writer):
            if writer is None:
                continue
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass


async def main() -> None:
    server = await asyncio.start_server(
        handle,
        LISTEN_HOST,
        LISTEN_PORT,
        reuse_address=True,
    )
    sockets = ", ".join(str(sock.getsockname()) for sock in (server.sockets or []))
    print(
        "qwen-loopback-proxy "
        f"listening={sockets} target={TARGET_HOST}:{TARGET_PORT}",
        flush=True,
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
