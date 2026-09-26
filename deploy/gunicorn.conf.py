"""Shared gunicorn config for locatron-api and locatron-bulk.

Only the hook lives here. Everything that differs between the two services —
worker count, bind address, timeouts — stays on each unit's ExecStart, where
you can read the whole shape of a service in one place. Command-line flags
override this file, so a unit can always have the last word.

Why post_worker_init and not --preload
--------------------------------------
--preload would load the gazetteers once before fork and let the children
inherit them. It does not hold up: CPython refcounting writes to the object
header on every access, so copy-on-write pages get dirtied and the shared
memory degrades toward one private copy per worker anyway. CLAUDE.md records
this, and it is also why the street gazetteer lives in SQLite rather than
Python memory.

post_worker_init runs inside each worker, after gunicorn has imported the app
and so after logging is configured. Each worker pays its own load during its
own startup, before it accepts a connection. That is the point: gunicorn does
not route traffic to a worker until it enters its run loop, which is after this
hook returns, so no user ever waits on a cold gazetteer.
"""

from typing import Any


def post_worker_init(worker: Any) -> None:
    """Load this worker's gazetteers before it starts serving."""
    # Imported inside the hook, not at module scope: gunicorn reads this config
    # in the arbiter before forking, and importing the app package there would
    # put a copy of it in the parent for no benefit.
    from locatron.gazetteer.warm import warm

    warm()
