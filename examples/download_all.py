"""Download every document in a folder into a local directory.

Usage:
    python download_all.py FOLDER_ID DEST_DIR
"""

from __future__ import annotations

import sys
from pathlib import Path

from infersoft import Client, build_folder_selector, build_selectors


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    folder_id, dest = int(sys.argv[1]), Path(sys.argv[2])
    dest.mkdir(parents=True, exist_ok=True)

    selectors = build_selectors(include=[build_folder_selector(folder_id)])
    with Client() as client:
        for doc in client.documents.iterate(selectors):
            try:
                path = client.documents.download(doc.id, dest)
            except FileExistsError:
                print(f"already have {doc.name}, skipping")
                continue
            print(f"saved {path}")


if __name__ == "__main__":
    main()
