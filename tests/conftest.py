# (c) Adam Davis - adamdavis.co.uk
"""
Shared test setup.

server.py refuses to import without PIXELMESH_ADMIN_TOKEN - a deliberate
guard so nobody exposes /admin/* by running uvicorn directly instead of
through run.sh. That guard is a bare sys.exit(1) at module scope, so every
test that imported server died with SystemExit until this file existed.

Set it once for the whole session. The value is irrelevant: no test asserts
on the token itself, only that admin routes are gated by it.
"""

import os

os.environ.setdefault("PIXELMESH_ADMIN_TOKEN", "test-token-not-a-real-secret")

# controller.py refuses to import unless run.sh set this - a guard against
# starting the controller by hand and getting a half-configured show. The
# ISO trim tests import controller for its pure helpers, which start no
# threads and open no camera, so satisfying the guard is enough.
os.environ.setdefault("PIXELMESH_LAUNCHED", "1")
