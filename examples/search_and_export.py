"""Export extraction values for a folder's documents to CSV.

Usage:
    python search_and_export.py FOLDER_ID PROMPT_ID [PROMPT_ID ...] > out.csv
"""

from __future__ import annotations

import csv
import sys

from infersoft import Client, build_folder_selector, build_selectors


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    folder_id, prompt_ids = int(sys.argv[1]), [int(p) for p in sys.argv[2:]]

    with Client() as client:
        selectors = build_selectors(include=[build_folder_selector(folder_id)])
        # {document_id: {field_name: parsed_value}} for every match.
        values = client.documents.get_values(selectors, prompts=prompt_ids)

    fields = sorted({name for row in values.values() for name in row})
    writer = csv.writer(sys.stdout)
    writer.writerow(["document_id", *fields])
    for doc_id, row in values.items():
        writer.writerow([doc_id, *(row.get(f, "") for f in fields)])


if __name__ == "__main__":
    main()
