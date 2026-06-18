"""Production-shaped error handling: budget gates, timeouts, and resumption.

Usage:
    python robust_pipeline.py PROMPT_ID DOCUMENT_ID [DOCUMENT_ID ...]
"""

from __future__ import annotations

import sys

from infersoft import (
    Client,
    CreditsLimitExceededError,
    InfersoftError,
    WaitTimeoutError,
)

MAX_CREDITS = 500


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    prompt_id, doc_ids = int(sys.argv[1]), [int(d) for d in sys.argv[2:]]

    with Client() as client:
        try:
            job = client.jobs.run(
                step="extractor",
                document_ids=doc_ids,
                prompts=[prompt_id],
                max_credits=MAX_CREDITS,  # abort BEFORE starting if too costly
                wait=True,
            )
        except CreditsLimitExceededError as e:
            raise SystemExit(
                f"would cost {e.estimate.total_credits} credits "
                f"(budget {e.max_credits}) — job not started"
            ) from e
        except WaitTimeoutError:
            # The job keeps running server-side; find it again and keep waiting
            # (or persist its id and resume from another process).
            running = client.jobs.search(statuses=["running"]).items
            print(f"still running after the wait budget: {[j.id for j in running]}")
            job = client.jobs.wait(running[0].id, max_wait_seconds=None)
        except InfersoftError as e:
            raise SystemExit(f"infersoft call failed: {e}") from e

        print(f"job {job.id}: {job.status.value} ({job.error_count} errors)")
        for doc in client.jobs.results(job, prompts=[prompt_id]):
            print(doc.id, doc.name, doc.extractions.get(prompt_id))


if __name__ == "__main__":
    main()
