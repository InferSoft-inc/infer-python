"""Async end-to-end: upload, extract, then download results concurrently.

Usage:
    python async_pipeline.py PROMPT_ID DEST_DIR file1.pdf [file2.pdf ...]
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from infersoft import AsyncClient


async def main() -> None:
    if len(sys.argv) < 4:
        raise SystemExit(__doc__)
    prompt_id, dest, files = int(sys.argv[1]), Path(sys.argv[2]), sys.argv[3:]
    dest.mkdir(parents=True, exist_ok=True)

    async with AsyncClient() as client:
        result = await client.extract(files, prompts=[prompt_id])
        print(f"job {result.job.id}: {result.job.status.value}")

        # Fan out the downloads — file I/O runs off the event loop internally.
        paths = await asyncio.gather(
            *(
                client.documents.download(doc_id, dest, overwrite=True)
                for doc_id in result.values
            )
        )
        for path in paths:
            print(f"saved {path}")


if __name__ == "__main__":
    asyncio.run(main())
