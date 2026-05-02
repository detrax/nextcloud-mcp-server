"""Add browser_sessions table for random-id browser cookie auth.

Replaces the prior `mcp_session=<user_id>` cookie pattern (issue #626
finding 2) with a server-side mapping from a cryptographically random
session id to the authenticated user_id. The cookie value is now opaque
and revocable.

Revision ID: 005
Revises: 004
Create Date: 2026-05-02 15:00:00.000000
"""

from alembic import op

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS browser_sessions (
            session_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_browser_sessions_user
        ON browser_sessions(user_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_browser_sessions_expires
        ON browser_sessions(expires_at)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_browser_sessions_expires")
    op.execute("DROP INDEX IF EXISTS idx_browser_sessions_user")
    op.execute("DROP TABLE IF EXISTS browser_sessions")
