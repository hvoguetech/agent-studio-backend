"""Default-deny authorization seam (B/E4, #14).

A single `authorize(subject, permission)` chokepoint plus a static permission registry.
Routing is default-deny: every route must declare either a required permission
(`require_permission("resource:action")`) or that it is intentionally public
(`public_endpoint`). A route that declares NEITHER fails closed at request time
(`default_deny_guard`) and is reported by `audit_route_coverage()` (startup log + a
CI coverage test). Authorization is therefore STRUCTURAL, not "did the dev remember a
`require_role` on this route".

Engine-agnostic by design: today `authorize()` maps a permission to the coarse role
tiers in `ros.services.auth` (owner > admin > editor > viewer > connector). B/E12
(#22, OpenFGA fine-grained/ReBAC) plugs in BEHIND this same chokepoint later — callers
never change. Postgres RLS remains the mandatory tenant-isolation floor underneath.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from fastapi.routing import APIRoute

from ros.deps import CurrentUser, effective_role, get_current_user
from ros.services.auth import role_at_least

# Role tiers mirror ros.services.auth.ROLES. `ANY` is a sentinel meaning "any
# authenticated principal" — including the least-privileged `connector` — used for
# self-service actions (manage your own account / connections / MCP tokens).
OWNER, ADMIN, EDITOR, VIEWER = "owner", "admin", "editor", "viewer"
ANY = "__any_authenticated__"

# The permission registry: `resource:action` -> minimum role. This is the source of
# truth and the ONLY set of permissions `authorize()` will ever grant — an unknown
# permission is denied (default-deny). Roles preserve each route's pre-existing gate;
# the only tightenings vs. before are the routes that used to be authenticated-but-not
# -role-gated (notably run execution, which a viewer/connector could previously hit).
PERMISSIONS: dict[str, str] = {
    "account:self": ANY,
    "agent:read": VIEWER,
    "agent:write": EDITOR,
    "apikey:read": ADMIN,
    "apikey:write": ADMIN,
    "assistant:write": EDITOR,
    "audit:read": ADMIN,
    "auth_provider:read": VIEWER,
    "auth_provider:write": EDITOR,
    "channel:read": VIEWER,
    "channel:write": EDITOR,
    "component:read": VIEWER,
    "component:write": EDITOR,
    "connection:read": VIEWER,
    "connection:self": ANY,
    "connection:write": EDITOR,
    "conversation:read": VIEWER,
    "conversation:write": ADMIN,
    "embed:read": EDITOR,
    "embed:write": EDITOR,
    "eval:read": VIEWER,
    "eval:write": EDITOR,
    "handoff:read": VIEWER,
    "handoff:write": EDITOR,
    "knowledge:read": VIEWER,
    "knowledge:write": EDITOR,
    "mcp_client:read": VIEWER,
    "mcp_client:write": EDITOR,
    "mcp_token:self": ANY,
    "model:read": VIEWER,
    "node:read": VIEWER,
    "pricing:read": ADMIN,
    "pricing:write": ADMIN,
    "project:members": ADMIN,
    "project:read": VIEWER,
    "project:write": ADMIN,
    "run:execute": EDITOR,
    "run:read": VIEWER,
    "secret:read": VIEWER,
    "secret:write": ADMIN,
    "stat:read": VIEWER,
    "team:read": ADMIN,
    "team:write": ADMIN,
    "tool:read": VIEWER,
    "tool:write": EDITOR,
    "tool_set:read": VIEWER,
    "tool_set:write": EDITOR,
    "trace:read": VIEWER,
    "trigger:read": VIEWER,
    "version:read": VIEWER,
    "version:write": EDITOR,
    "workflow:read": VIEWER,
    "workflow:write": EDITOR,
    "workspace:read": ADMIN,
    "workspace:write": OWNER,
}


@dataclass(frozen=True)
class Subject:
    """The principal an authorization decision is made about. `role` is the caller's
    EFFECTIVE role on the request (tenant role, possibly elevated by a per-project
    ProjectMember grant — see ros.deps.effective_role)."""

    id: str
    tenant_id: str
    role: str


class AuthzDenied(HTTPException):
    def __init__(self, detail: str) -> None:
        super().__init__(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def authorize(subject: Subject, permission: str) -> None:
    """The single authorization chokepoint. Default-deny: raise 403 unless the permission
    is in the registry AND the subject's role satisfies it. `ANY` grants any authenticated
    principal. Returns None on success."""
    required = PERMISSIONS.get(permission)
    if required is None:
        # Unknown/unregistered permission is never granted (default-deny).
        raise AuthzDenied(f"authorization denied: unknown permission {permission!r}")
    if required == ANY:
        return
    if not role_at_least(subject.role, required):
        raise AuthzDenied(f"requires permission {permission!r} (role {required!r} or higher)")


# --- route declaration markers (statically discoverable on route.dependant) ---
_PERM_ATTR = "__forge_permission__"
_PUBLIC_ATTR = "__forge_public__"


def require_permission(permission: str):
    """Dependency factory declaring + enforcing a route's required permission. Resolves the
    caller, computes their effective role, calls `authorize()`, and binds the tenant for
    Postgres RLS (parity with `current_tenant_id`). Returns the CurrentUser (effective role
    reflected) so it can be used as a route parameter if desired."""
    if permission not in PERMISSIONS:
        # Fail loudly at import/route-build time, not at request time.
        raise RuntimeError(f"require_permission: {permission!r} is not in the PERMISSIONS registry")

    async def _dep(request: Request, user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        role = await effective_role(user, request)
        authorize(Subject(id=user.id, tenant_id=user.tenant_id, role=role), permission)
        # Keep the request tenant-scoped for RLS regardless of which other deps the route uses.
        from ros.db.scoping import set_current_tenant

        set_current_tenant(user.tenant_id)
        if role != user.role:
            return CurrentUser(
                id=user.id,
                tenant_id=user.tenant_id,
                role=role,
                email=user.email,
                is_fallback=user.is_fallback,
            )
        return user

    setattr(_dep, _PERM_ATTR, permission)
    return _dep


async def public_endpoint() -> None:
    """Marker dependency: this route is intentionally unauthenticated (login/refresh,
    health, inbound webhooks, the public embed surface, OAuth endpoints, and the MCP
    transport which self-authenticates via MCP tokens/OAuth). Makes "public" a positive,
    auditable declaration rather than the mere absence of an auth dependency."""
    return None


setattr(public_endpoint, _PUBLIC_ATTR, True)


# --- coverage / default-deny enforcement ---
def _iter_api_routes(routes):
    """Yield every leaf APIRoute, descending through FastAPI 0.135 `_IncludedRouter`
    wrappers (which expose the real routes via `.original_router`)."""
    for r in routes:
        if isinstance(r, APIRoute):
            yield r
        orig = getattr(r, "original_router", None)
        if orig is not None and getattr(orig, "routes", None):
            yield from _iter_api_routes(orig.routes)
        elif orig is None and getattr(r, "routes", None):
            yield from _iter_api_routes(r.routes)


def _markers(dependant) -> list[tuple[str, str | None]]:
    out: list[tuple[str, str | None]] = []

    def walk(d) -> None:
        call = getattr(d, "call", None)
        if call is not None:
            perm = getattr(call, _PERM_ATTR, None)
            if perm is not None:
                out.append(("permission", perm))
            if getattr(call, _PUBLIC_ATTR, False):
                out.append(("public", None))
        for sub in getattr(d, "dependencies", []):
            walk(sub)

    walk(dependant)
    return out


def route_declaration(route: APIRoute) -> tuple[str, str | None] | None:
    """('permission', name) | ('public', None) | None (undeclared)."""
    markers = _markers(route.dependant)
    for kind, val in markers:
        if kind == "permission":
            return ("permission", val)
    for kind, _val in markers:
        if kind == "public":
            return ("public", None)
    return None


async def default_deny_guard(request: Request) -> None:
    """App-level default-deny backstop. A route that declares neither a permission nor
    `public_endpoint` fails closed with 403 — so forgetting to annotate a new route denies
    access rather than silently exposing it. Declared routes are enforced by their own
    `require_permission` dependency; this only catches the undeclared."""
    route = request.scope.get("route")
    if not isinstance(route, APIRoute):
        return  # mounts / static / non-API — nothing to gate here
    if route_declaration(route) is None:
        raise AuthzDenied("authorization not declared for this route (default-deny)")


def audit_route_coverage(app) -> list[tuple[list[str], str, str]]:
    """Return (methods, path, endpoint_name) for every API route missing an authz
    declaration. Empty list == full coverage. Used at startup (log) and by the coverage
    test (hard failure)."""
    undeclared: list[tuple[list[str], str, str]] = []
    for r in _iter_api_routes(app.routes):
        if route_declaration(r) is None:
            methods = sorted((r.methods or set()) - {"HEAD", "OPTIONS"})
            undeclared.append((methods, r.path, r.endpoint.__name__))
    return undeclared
