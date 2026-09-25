#!/usr/bin/env python3
"""
hold_drain_lock.py - deliberately holds the write-queue's drain lock,
for testing contention by hand.

This exists purely to let you reproduce, on demand, the exact
situation gui.py's write queue is designed to handle: "something else
currently holds the drain lock." With this running, any GUI action
that writes (Add Site, Mark Stale, etc.) should durably queue instead
of applying, and show its "Queued - this will apply shortly" notice
instead of its normal result. That's what you're checking for.

Usage - run from the same directory as sad.db (the project root,
same place you'd run gui.py from):

    python hold_drain_lock.py           # holds until you press Ctrl+C
    python hold_drain_lock.py 30        # holds for 30 seconds, then releases itself

While it's running, go use a real gui.py and try a write action.
When it exits (Ctrl+C or the timer runs out), it releases the lock
AND immediately drains anything that piled up while it was held -
watch the printed summary to confirm your queued action(s) actually
applied, and check the activity log to confirm they're credited to
the right person, not to this script.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "utilities"))

import db  # noqa: E402
import write_queue  # noqa: E402


def main():
    hold_seconds = None
    if len(sys.argv) > 1:
        try:
            hold_seconds = float(sys.argv[1])
        except ValueError:
            sys.exit(f"Usage: {sys.argv[0]} [seconds-to-hold]")

    qdir = write_queue._queue_dir()
    print(f"Write-queue directory: {qdir}")

    if not write_queue._claim_drain_lock(qdir):
        print(
            "Could not claim the drain lock - something else already holds it "
            "right now (another GUI, or a previous run of this script that didn't "
            "exit cleanly). If you're sure nothing legitimate is draining, delete "
            f"{os.path.join(qdir, write_queue.LOCK_FILENAME)} by hand and try again."
        )
        sys.exit(1)

    print("Lock claimed. Any GUI write action right now should queue instead of")
    print("applying, and show its 'Queued - this will apply shortly' notice.")
    if hold_seconds:
        print(f"Holding for {hold_seconds:.0f} second(s)...")
    else:
        print("Holding until you press Ctrl+C...")

    try:
        if hold_seconds:
            time.sleep(hold_seconds)
        else:
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        print("\nCtrl+C received.")
    finally:
        write_queue._release_drain_lock(qdir)
        print("Lock released. Draining anything that piled up while it was held...")
        outcomes = write_queue.try_drain()
        if not outcomes:
            print("  Nothing was queued while the lock was held.")
        else:
            for request_id, outcome in outcomes.items():
                if outcome["ok"]:
                    print(f"  Applied {request_id}: {outcome['result']}")
                else:
                    print(f"  FAILED {request_id}: {outcome['error']}")
        print("Done. Check the GUI (and the activity log) to confirm it landed correctly.")


if __name__ == "__main__":
    main()
