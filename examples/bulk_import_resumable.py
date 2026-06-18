"""Resumable bulk import: crash anywhere, rerun, nothing duplicates.

The idempotency key is persisted BEFORE uploading; per-batch keys derive from
it, so a rerun with the same key, the same files in the same order, and the
same batch_size dedupes batch-by-batch.

Usage:
    python bulk_import_resumable.py DIRECTORY [PROJECT_NAME]
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

from infersoft import Client, UploadManyError

STATE = Path("import-state.json")


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    directory = sys.argv[1]
    project_name = sys.argv[2] if len(sys.argv) > 2 else "Bulk import"

    saved = json.loads(STATE.read_text()) if STATE.exists() else {}
    key = saved.get("key") or uuid.uuid4().hex
    STATE.write_text(json.dumps({"key": key}))  # persist BEFORE uploading

    with Client() as client:
        try:
            result = client.documents.upload_many(
                directory,
                flatten=False,  # replicate the local folder layout in the app
                project_name=project_name,
                idempotency_key=key,
                on_batch=lambda r: print(f"  batch done: {len(r.succeeded)} files"),
            )
        except UploadManyError as e:
            print(
                f"failed after {e.batches_completed} batches "
                f"({len(e.partial.document_ids)} documents uploaded so far); "
                "rerun this script to resume.",
                file=sys.stderr,
            )
            raise SystemExit(1) from e

    print(f"imported {len(result.succeeded)} files into {project_name!r}")
    STATE.unlink()  # the import is complete; next run is a NEW import


if __name__ == "__main__":
    main()
