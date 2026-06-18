"""The whole pipeline in one call: upload -> extract -> values.

Usage:
    python quickstart_extract.py PROMPT_ID file1.pdf [file2.pdf ...]

Credentials come from INFERSOFT_CLIENT_ID / INFERSOFT_CLIENT_SECRET.
"""

from __future__ import annotations

import sys

from infersoft import Client


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    prompt_id, files = int(sys.argv[1]), sys.argv[2:]

    with Client() as client:
        result = client.extract(files, prompts=[prompt_id])

        # Files the server rejected (e.g. name conflicts) are skipped, not
        # raised — always check before trusting the values as complete.
        if result.upload and result.upload.failed:
            for f in result.upload.failed:
                code = f.error.code if f.error else "upload failed"
                print(f"skipped {f.file_name}: {code}", file=sys.stderr)

        print(f"job {result.job.id} finished: {result.job.status.value}")
        for doc_id, values in result.values.items():
            print(doc_id, values)


if __name__ == "__main__":
    main()
