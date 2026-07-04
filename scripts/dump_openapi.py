#!/usr/bin/env python3
"""Dump the API's OpenAPI schema OFFLINE, from ``create_app().openapi()``.

The web ``gen:api`` target used to fetch ``http://localhost:8000/openapi.json``
from a live uvicorn. That path is fragile: it needs a reachable DB and a
successful lifespan, and a bare/degraded standalone start yields a TRUNCATED
schema (the 16-vs-324-paths symptom of task ed720f5b) -- so ``schema.d.ts``
could silently regenerate against a mutilated contract.

``app.openapi()`` is pure: it walks the mounted routers, touches no database
and runs no lifespan, so it renders the FULL schema deterministically with
zero setup. Secrets are irrelevant to the schema SHAPE, so placeholders keep
``Settings`` constructible without a dev ``.env``.

Usage::

    python scripts/dump_openapi.py            # schema to stdout
    python scripts/dump_openapi.py out.json   # schema to a file
"""

from __future__ import annotations

import json
import os
import sys

# Settings requires these three; their VALUES never reach the schema, so
# placeholders make the dump run with no environment at all.
for _key, _val in {
    "MYCELIUM_JWT_SECRET": "openapi-dump-placeholder-secret-not-a-real-key-000",
    "MYCELIUM_SECRET_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "MYCELIUM_ISSUER_KEY_PEPPER": "openapi-dump-placeholder-issuer-pepper-000",
}.items():
    os.environ.setdefault(_key, _val)

from mycelium_api.app import create_app  # noqa: E402  -- after the env defaults


def main() -> None:
    # Insertion order (FastAPI's natural OpenAPI ordering) -- deterministic
    # because routers are included in a fixed order, and it keeps the diff of
    # the generated ``schema.d.ts`` minimal vs the historical live-server dump.
    schema = create_app().openapi()
    text = json.dumps(schema, indent=2) + "\n"
    if len(sys.argv) > 1 and sys.argv[1] != "-":
        with open(sys.argv[1], "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
