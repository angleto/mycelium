"""OpenAPI schema is complete and DB-free (guards `gen:api`, task ed720f5b).

``scripts/dump_openapi.py`` renders the FE/BE contract offline from
``create_app().openapi()`` -- no server, no DB. This pins that the canonical
app mounts the whole surface (a router that silently stops being included
would drop paths and quietly mutilate ``schema.d.ts``), and that the render
needs no database.
"""

from __future__ import annotations

from mycelium_api.app import create_app

# Well below today's 324 paths but far above the ~16 a degraded standalone
# start produced: a regression that dropped a whole router would trip this.
_MIN_PATHS = 300


def test_openapi_is_complete_offline() -> None:
    schema = create_app().openapi()
    paths = schema.get("paths", {})
    assert len(paths) >= _MIN_PATHS, f"OpenAPI has only {len(paths)} paths (<{_MIN_PATHS})"
    # Spot-check one path from each of a few distinct routers so a broad
    # drop can't hide under the count.
    for expected in ("/notes", "/tasks", "/invoices", "/garden/review/pending", "/healthz"):
        assert expected in paths, f"missing {expected} from the OpenAPI schema"
