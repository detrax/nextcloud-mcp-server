"""Add nonce column to oauth_sessions for OIDC ID-token binding.

PR #758 finding 2: the browser OAuth flow generated PKCE + state but no
``nonce``. Without a nonce, an attacker who obtains a valid ID token for
another user (e.g. from a parallel auth request) could replay it inside
this flow because the token isn't cryptographically tied to the
authorization request. The nonce is generated in ``oauth_login``,
forwarded to the IdP in the auth URL, and verified on the way back.

Revision ID: 006
Revises: 005
Create Date: 2026-05-02 16:00:00.000000
"""

from alembic import op

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE oauth_sessions ADD COLUMN nonce TEXT")


def downgrade() -> None:
    # SQLite < 3.35 cannot DROP COLUMN; leave the column on downgrade.
    pass
