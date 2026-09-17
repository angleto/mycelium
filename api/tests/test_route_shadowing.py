"""A literal route declared after a parameterised one is dead.

Starlette matches in declaration order and the first full match wins, so
``GET /tasks/{task_id}`` declared above ``GET /tasks/leases`` swallows
the second: the request never reaches its handler, and the caller gets a
422 complaining that "leases" is not a uuid. Nothing else in the gate
sees it. The service layer is fine, its unit tests pass, the route is in
the OpenAPI document, the generated client types carry it, and the SPA
compiles against a path that answers 422 at runtime.

That is what happened to the possession listings (ADR-0063): both
``/tasks/leases`` and ``/tasks/workers`` shipped unreachable, and the
board that reads them rendered every card as free. Their POST siblings
worked, which made it look like a client problem -- there is no
``POST /tasks/{task_id}``, so nothing shadowed those.

The check is over every router, not the one that broke: the failure is a
property of declaration order, and the next literal route appended to
the bottom of any file with a ``/{id}`` route above it dies the same way.
"""

from __future__ import annotations

from fastapi.routing import APIRoute

from mycelium_api.main import app


def _api_routes() -> list[APIRoute]:
    return [r for r in app.routes if isinstance(r, APIRoute)]


def test_no_literal_route_is_shadowed_by_an_earlier_parameterised_one() -> None:
    routes = _api_routes()
    shadowed: list[str] = []
    for i, route in enumerate(routes):
        if "{" in route.path:
            continue
        for earlier in routes[:i]:
            if "{" not in earlier.path:
                continue
            # A method mismatch is not a shadow: Starlette remembers a
            # path-only match as partial and keeps looking, so the later
            # route still gets the request.
            if not (route.methods or set()) & (earlier.methods or set()):
                continue
            if earlier.path_regex.match(route.path):
                shadowed.append(
                    f"{sorted(route.methods or [])} {route.path} is unreachable: "
                    f"{sorted(earlier.methods or [])} {earlier.path} is declared first "
                    f"and matches it"
                )
    assert not shadowed, "\n".join(shadowed)
