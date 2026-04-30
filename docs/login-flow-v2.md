# Login Flow v2 (Multi-User Mode)

This is the recommended multi-user deployment mode for the Nextcloud MCP Server. It works with **stock Nextcloud 16+** (no upstream patches) and is the mode used by hosted offerings like [Astrolabe Cloud](https://astrolabecloud.com).

For the design rationale, see [ADR-022](ADR-022-deployment-mode-consolidation.md). For other deployment modes, see [Authentication](authentication.md).

## How It Works

Two authentication legs, each with a different mechanism:

```
┌─────────────────┐    OAuth/OIDC    ┌──────────────────┐   App password    ┌─────────────────┐
│   MCP Client    │ ───────────────> │   MCP Server     │ ────────────────> │   Nextcloud     │
│ (Claude, etc.)  │  (per-app scopes)│  (OIDC RP of IdP,│   (Basic Auth)    │   (NC 16+)      │
│                 │                  │  OAuth facade,   │                   │                 │
│                 │                  │  app-pwd holder) │                   │                 │
└─────────────────┘                  └──────────────────┘                   └─────────────────┘
                                              ▲
                                              │ OIDC discovery + token validation
                                              ▼
                                     ┌─────────────────┐
                                     │ OIDC Provider   │
                                     │ (Nextcloud OIDC,│
                                     │  Keycloak,      │
                                     │  AWS Cognito,…) │
                                     └─────────────────┘
```

- **MCP client → MCP server**: OAuth 2.1 with PKCE. The MCP server is **not** a standalone OAuth issuer — it acts as an OIDC relying party of a configurable identity provider and exposes an OAuth facade in front of it. The IdP is selected by `OIDC_DISCOVERY_URL` (defaults to Nextcloud's built-in OIDC); Keycloak, AWS Cognito, and other OIDC-compliant IdPs are also supported. Tokens are signed by that IdP, validated by the MCP server against the IdP's JWKS, and carry per-app scopes (`notes.read`, `talk.read`, `files.write`, …) that gate which tools the user can call.
- **MCP server → IdP (auth leg)**: The MCP server registers itself with the IdP via static `NEXTCLOUD_OIDC_CLIENT_ID`/`SECRET` (preferred — these are generic OIDC client credentials despite the Nextcloud-flavored naming, and work with any OIDC provider) or RFC 7591 DCR (fallback). This relationship is used for OIDC discovery, JWKS retrieval, and token validation.
- **MCP server → Nextcloud (data leg)**: Per-user **app password** obtained via Nextcloud's native [Login Flow v2](https://docs.nextcloud.com/server/latest/developer_manual/client_apis/LoginFlow/index.html#login-flow-v2). Sent as HTTP Basic Auth. Login Flow v2 is always Nextcloud's protocol regardless of which IdP authenticated the MCP client.

App passwords appear in **Settings → Security → Devices & Sessions** in Nextcloud and can be revoked by the user at any time.

### Why not forward OAuth bearer tokens to Nextcloud?

Earlier deployment modes forwarded the client's OAuth bearer token directly to Nextcloud APIs. That required upstream patches to `user_oidc` (Bearer-token validation on non-OCS endpoints) which were never merged. Nextcloud also doesn't enforce OAuth scopes on its app APIs even when Bearer tokens are accepted, so the security guarantees were weaker than they appeared. App passwords are the simplest mechanism that works on every supported Nextcloud version and surfaces user-revocable credentials in the standard UI.

Scope enforcement happens at the MCP server layer (defense-in-depth). See [Scope Enforcement](#scope-enforcement) below.

## Setup

### Required Environment Variables

```bash
# Nextcloud connection (data leg — always Nextcloud, regardless of which IdP authenticates clients)
NEXTCLOUD_HOST=https://your.nextcloud.example.com

# IdP selection (auth leg). Defaults to NEXTCLOUD_HOST/.well-known/openid-configuration
# (i.e. Nextcloud's built-in OIDC). Override to point at Keycloak, AWS Cognito, etc.
# OIDC_DISCOVERY_URL=https://keycloak.example.com/realms/myrealm/.well-known/openid-configuration

# OIDC client credentials for the MCP server's relying-party relationship with the IdP.
# These are generic OIDC client credentials — they work with any OIDC provider, despite
# the Nextcloud-flavored env-var names. Preferred path: register a client in your IdP
# (Nextcloud admin → OIDC, Keycloak realm → Clients, etc.) and set these. If both are
# unset and the IdP advertises a `registration_endpoint`, the server falls back to RFC 7591 DCR.
NEXTCLOUD_OIDC_CLIENT_ID=<your-client-id>
NEXTCLOUD_OIDC_CLIENT_SECRET=<your-client-secret>

# Enable Login Flow v2 (per-user Nextcloud app-password provisioning for the data leg)
ENABLE_LOGIN_FLOW=true

# App-password storage (required for persistence across restarts)
TOKEN_STORAGE_DB=/app/data/tokens.db
TOKEN_ENCRYPTION_KEY=<fernet-key>          # see "Generating an encryption key" below

# Public URLs (for browser redirects)
NEXTCLOUD_MCP_SERVER_URL=https://mcp.example.com
NEXTCLOUD_PUBLIC_ISSUER_URL=https://your.nextcloud.example.com  # Public URL of Nextcloud
```

When using an external IdP (Keycloak, Cognito, etc.), see [Keycloak Multi-Client Token Validation](keycloak-multi-client-validation.md) for how Nextcloud's `user_oidc` app handles realm-level token validation if you also federate Nextcloud's own login through the same IdP.

### Generating an Encryption Key

App passwords are stored encrypted with Fernet. Generate a key once and reuse it:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Lose the key and stored app passwords become unrecoverable — users will need to re-provision.

### Docker Compose

The repo ships with a working reference under the `login-flow` profile:

```bash
docker compose --profile login-flow up -d
# Server listens on http://localhost:8004
```

Excerpt from `docker-compose.yml`:

```yaml
mcp-login-flow:
  build: .
  command: ["--transport", "streamable-http", "--oauth", "--port", "8004"]
  ports:
    - 127.0.0.1:8004:8004
  environment:
    - NEXTCLOUD_HOST=http://app:80
    - NEXTCLOUD_MCP_SERVER_URL=http://localhost:8004
    - NEXTCLOUD_PUBLIC_ISSUER_URL=http://localhost:8080
    - ENABLE_LOGIN_FLOW=true
    # Dev-only inline value. In production, mount via Docker secret and read
    # from a *_FILE env var or a secrets-management init step.
    - TOKEN_ENCRYPTION_KEY=<your-fernet-key>
    - TOKEN_STORAGE_DB=/app/data/tokens.db
  volumes:
    - login-flow-data:/app/data
    - login-flow-oauth-storage:/app/.oauth
```

> **Production note:** `TOKEN_ENCRYPTION_KEY` is a credential — losing it makes every stored app password unrecoverable. Inline-environment values are fine for local development but should be passed via Docker secrets (or your platform's equivalent) in production. See [Configuration → Best Practices for Docker](configuration.md#for-docker).

The `--oauth` flag enables the OAuth/OIDC identity layer that Login Flow v2 builds on (user identity via OAuth session, Nextcloud access via app passwords).

## Per-User Provisioning Flow

Each user goes through provisioning **once**, the first time they connect. Subsequent requests reuse the stored app password.

```
┌─────────────┐                  ┌──────────────────┐                  ┌─────────────────┐
│ MCP Client  │                  │   MCP Server     │                  │    Nextcloud    │
└──────┬──────┘                  └────────┬─────────┘                  └────────┬────────┘
       │  1. OAuth PKCE                   │                                     │
       ├─────────────────────────────────>│                                     │
       │  ← access token (per-app scopes) │                                     │
       │                                  │                                     │
       │  2. MCP request                  │                                     │
       ├─────────────────────────────────>│                                     │
       │                                  │                                     │
       │  3. No stored app password →     │                                     │
       │     elicit URL or 401            │                                     │
       │<─────────────────────────────────┤                                     │
       │  "Visit <login-url> to grant     │                                     │
       │   access"                        │                                     │
       │                                  │  4. POST /index.php/login/v2        │
       │                                  ├────────────────────────────────────>│
       │                                  │  ← {login_url, poll_endpoint, token}│
       │                                  │                                     │
       │  5. User opens login_url in browser, authenticates, clicks "Grant"     │
       │  ────────────────────────────────────────────────────────────────────> │
       │                                  │                                     │
       │                                  │  6. Poll endpoint (background)      │
       │                                  ├────────────────────────────────────>│
       │                                  │  ← {loginName, appPassword}         │
       │                                  │                                     │
       │                                  │  7. Encrypt + store in SQLite       │
       │                                  │                                     │
       │  8. Retry MCP request            │                                     │
       ├─────────────────────────────────>│                                     │
       │                                  │  9. GET /apps/notes/...             │
       │                                  ├────────────────────────────────────>│
       │                                  │  Authorization: Basic <app-pwd>     │
       │                                  │  ← response                         │
       │  10. ← result                    │                                     │
```

### Provisioning Endpoints

The server exposes browser endpoints for management UIs (Astrolabe, custom dashboards):

| Endpoint | Purpose |
|----------|---------|
| `GET /app/provision?redirect_uri=…` | Start Login Flow v2 and redirect to Nextcloud's grant page |
| `GET /app/provision/status?id=…` | Check whether the background poll has completed |

Both require a valid OAuth bearer token in the `Authorization` header (the user's identity is taken from the token, not from a query parameter).

Implementation: [`nextcloud_mcp_server/auth/provision_routes.py`](../nextcloud_mcp_server/auth/provision_routes.py).

### Provisioning via MCP Tools (Elicitation)

For MCP clients, the same flow is exposed as tools (`nc_auth_provision_access`, `nc_auth_check_status`). Clients that support **URL elicitation** (MCP spec 2025-11-25) get a clickable link automatically; clients without that capability fall back to a copy-paste URL in an error message. See [ADR-022 §"MCP Elicitation for Login Flow v2"](ADR-022-deployment-mode-consolidation.md) for the full capability matrix.

## Scope Enforcement

Nextcloud's app passwords have **no native scope support** — they grant the user's full API access. The MCP server enforces scopes at the application layer.

### Scope Reference

Scopes are **per-app** and follow an `<app>.<read|write>` pattern. There is no `mcp:` prefix.

| Scope | Covers |
|-------|--------|
| `notes.read` / `notes.write` | Notes app |
| `talk.read` / `talk.write` | Talk (spreed) |
| `files.read` / `files.write` | Files / WebDAV |
| `calendar.read` / `calendar.write` | Calendar (events + tasks/VTODO) |
| `contacts.read` / `contacts.write` | Contacts (CardDAV) |
| `deck.read` / `deck.write` | Deck |
| `tables.read` / `tables.write` | Tables |
| `cookbook.read` / `cookbook.write` | Cookbook |
| `todo.read` / `todo.write` | Tasks (VTODO outside Calendar) |
| `collectives.read` / `collectives.write` | Collectives |
| `news.read` | News (read-only) |
| `sharing.write` | Share-link / share-permission management |
| `semantic.read` | Semantic search + RAG (when enabled) |

The authoritative list is enumerated at runtime by [`scope_authorization.discover_all_scopes()`](../nextcloud_mcp_server/auth/scope_authorization.py) from each tool's `@require_scopes(...)` decorator and exposed via the PRM endpoint (`/.well-known/oauth-protected-resource/mcp`).

Standard OIDC scopes (`openid`, `profile`, `email`) are also accepted and have no effect on tool access.

### How Scopes Are Enforced

Each MCP tool is decorated with `@require_scopes(...)`:

```python
@mcp.tool()
@require_scopes("notes.read")
async def nc_notes_get_note(note_id: int, ctx: Context):
    ...
```

When a client calls `list_tools`, the server returns only tools the user has granted scopes for (dynamic tool filtering). When a client calls a tool whose scope is missing, the server returns:

```http
HTTP/1.1 403 Forbidden
WWW-Authenticate: Bearer error="insufficient_scope",
                  scope="notes.write",
                  resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource/mcp"
```

Clients can use this header to trigger **step-up authorization** — re-running the OAuth flow with additional scopes.

Implementation: [`nextcloud_mcp_server/auth/scope_authorization.py`](../nextcloud_mcp_server/auth/scope_authorization.py).

## OAuth Endpoints

When `--oauth` is enabled, the MCP server exposes OAuth 2.1 endpoints. **These endpoints front the configured IdP**: discovery metadata is sourced from the IdP, and tokens served via the MCP server's `/token` endpoint are signed by the IdP's key and validated against its JWKS — the MCP server has no signing keys of its own. The IdP is selected by `OIDC_DISCOVERY_URL` (Nextcloud OIDC by default, or Keycloak / Cognito / etc.).

| Endpoint | RFC | Purpose |
|----------|-----|---------|
| `GET /.well-known/oauth-authorization-server` | RFC 8414 | Server metadata (advertises the configured IdP as the upstream issuer) |
| `GET /.well-known/oauth-protected-resource/mcp` | RFC 9728 | PRM — advertises supported scopes (dynamically discovered from `@require_scopes`) |
| `POST /register` | RFC 7591 | Dynamic Client Registration (for MCP clients; see also `NEXTCLOUD_OIDC_CLIENT_ID/SECRET` for the MCP server's own RP credentials with the IdP) |
| `PUT/DELETE /register/{client_id}` | RFC 7592 | Client management with registration token |
| `GET /authorize` | RFC 6749 | Authorization endpoint (PKCE required, S256) |
| `POST /token` | RFC 6749 | Token endpoint |

Implementation: [`nextcloud_mcp_server/auth/oauth_routes.py`](../nextcloud_mcp_server/auth/oauth_routes.py), [`nextcloud_mcp_server/auth/client_registration.py`](../nextcloud_mcp_server/auth/client_registration.py).

PKCE with S256 is **mandatory** — required by the MCP specification and enforced at the authorization endpoint.

## Token Format

The MCP server can issue or accept either JWT or opaque access tokens depending on configuration.

| | JWT (recommended) | Opaque |
|---|---|---|
| Validation | Signature check via JWKS (local, fast) | Introspection HTTP call |
| Scope claim | Embedded in `scope` claim | Returned by introspection endpoint |
| Size | ~800-1200 chars | ~72 chars |
| Standard | RFC 9068 | RFC 7662 |

JWTs are preferred for production because validation is local and stateless. Opaque tokens are useful when you need server-side revocation without JWT blocklist infrastructure.

## Troubleshooting

### "Provisioning loop" — user keeps being asked to authorize

Check that `TOKEN_STORAGE_DB` is on a persistent volume. The default (`/tmp` or per-process tempfile) is wiped on container restart, so each restart loses every stored app password.

### "Failed to start login flow" / 502 from `/app/provision`

The MCP server cannot reach Nextcloud at `NEXTCLOUD_HOST`. Verify network connectivity and that `NEXTCLOUD_HOST` uses an address reachable from the server (not the user's browser). For Docker Compose deployments, this is typically the internal service hostname (e.g. `http://app:80`).

### "Login URL points to localhost in browser"

`NEXTCLOUD_PUBLIC_ISSUER_URL` is missing or wrong. Set it to the public URL of Nextcloud as the user's browser sees it. The server rewrites the login URL's origin from the internal `NEXTCLOUD_HOST` to `NEXTCLOUD_PUBLIC_ISSUER_URL` before redirecting the browser.

### Stored app password rejected by Nextcloud (401)

The user revoked it from **Settings → Security → Devices & Sessions**. Delete the row from the storage DB (or call `nc_auth_provision_access` again) to trigger a fresh Login Flow.

### `cryptography.fernet.InvalidToken` on startup

`TOKEN_ENCRYPTION_KEY` changed since the DB was created — stored app passwords cannot be decrypted with a different key. Either restore the original key or wipe the DB and have users re-provision.

### Multiple worker processes

The provisioning session store is in-memory; `ENABLE_LOGIN_FLOW=true` assumes a single worker. Running with `uvicorn --workers N` will cause provisioning sessions to randomly fail. For higher concurrency, scale horizontally (multiple containers behind a sticky-session load balancer) rather than within a single process.

> **Sticky-session keying:** route on the **user identity** (e.g. the `sub` claim from the OAuth Bearer token) — **not** the raw token value, and **not** source IP. Bearer tokens rotate on refresh, which would silently break token-value affinity if a refresh lands between the request that initiates provisioning and the polling request that completes it. MCP clients may also not maintain stable IPs across those requests. A stable per-user identifier extracted from the `Authorization` header (e.g. `sub`) is the right key.

## See Also

- [ADR-022: Deployment Mode Consolidation](ADR-022-deployment-mode-consolidation.md) — design rationale
- [Authentication](authentication.md) — overview of all deployment modes
- [Authentication Flows](auth-flows.md) — sequence diagrams per mode
- [Configuration](configuration.md) — full environment variable reference
- [Nextcloud Login Flow v2 spec](https://docs.nextcloud.com/server/latest/developer_manual/client_apis/LoginFlow/index.html#login-flow-v2)
