#!/usr/bin/env python3
"""
scripts/host_proxy.py -- tiny host-side TCP proxy so the Docker containers
can reach TrueForge.

Why this exists (found live, running the actual stack, not guessed):
`npx @truefoundry/trueforge` binds only to loopback (127.0.0.1) -- its own
`--help` has no flag to change that. A container reaching your machine via
Docker's bridge gateway is a different network path than "localhost" from
TrueForge's point of view, so those connections get refused even though the
route itself works fine.

`host.docker.internal` isn't a safe alternative either -- on Docker Desktop
for Mac, verified live, it resolved to an unroutable IPv6 address in this
environment.

This script sidesteps both problems: it listens on ALL interfaces on a
different port and blindly forwards raw bytes to TrueForge on loopback
(works for both normal requests and TrueForge's SSE streams, since it never
interprets the traffic, just relays it). The `backend` container then
reaches it via the Docker bridge gateway IP, auto-discovered in
backend/trueforge_client.py -- not any hostname.

Run this in its own terminal, alongside `npx @truefoundry/trueforge`, before
`docker compose up`:
    python3 scripts/host_proxy.py
"""

import asyncio
import os

LISTEN_PORT = int(os.environ.get("TRUEFORGE_PROXY_PORT", "18790"))
# "localhost" (not a hardcoded "127.0.0.1") on purpose: found live via
# `lsof -iTCP:8790 -sTCP:LISTEN` that `npx trueforge` binds ONLY the IPv6
# loopback (::1), not IPv4, in this environment -- asyncio.open_connection
# resolves "localhost" and tries both families, so this works whichever way
# a given TrueForge version/OS actually binds, instead of assuming one.
TARGET_HOST = os.environ.get("TRUEFORGE_HOST", "localhost")
TARGET_PORT = int(os.environ.get("TRUEFORGE_PORT", "8790"))


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        writer.close()


async def _handle(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
    try:
        target_reader, target_writer = await asyncio.open_connection(TARGET_HOST, TARGET_PORT)
    except OSError as exc:
        print(f"could not reach TrueForge at {TARGET_HOST}:{TARGET_PORT}: {exc}")
        client_writer.close()
        return
    await asyncio.gather(
        _pipe(client_reader, target_writer),
        _pipe(target_reader, client_writer),
    )


async def main() -> None:
    server = await asyncio.start_server(_handle, "0.0.0.0", LISTEN_PORT)
    print(f"host_proxy: listening on 0.0.0.0:{LISTEN_PORT} -> {TARGET_HOST}:{TARGET_PORT}")
    print("Leave this running alongside `npx @truefoundry/trueforge` -- it lets the")
    print("Docker containers reach TrueForge, which only binds to loopback itself.")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
