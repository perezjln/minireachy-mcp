"""Open N successive WebRTC sessions with the real daemon and report time-to-first-frame and ICE pair.
Read-only (camera/microphone subscription only).

    docker compose --profile e2e run --rm -v ./src:/app/src:ro -v ./scripts:/app/scripts:ro e2e \
        python scripts/media_link_probe.py 8
"""
import asyncio
import logging
import sys
import time

from minireachy_mcp.media.webrtc import MediaLink, MediaUnavailableError

logging.basicConfig(level=logging.WARNING)


async def main(n: int) -> None:
    for i in range(n):
        link = MediaLink("ws://host.docker.internal:8443", connect_timeout_s=25)
        t = time.monotonic()
        try:
            await link.latest_frame()
            st = link.status()
            print(f"#{i+1} OK  {time.monotonic()-t:5.2f}s sessions={st['sessions']} pair={st['selected_ice_pair']} "
                  f"errors={link.stats.last_error}")
        except MediaUnavailableError as exc:
            print(f"#{i+1} FAIL {time.monotonic()-t:5.2f}s {exc}")
        await link.close()
        await asyncio.sleep(1.0)


asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 5))
