"""Track when a member has seen feedback on their weekly update

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("feedback", sa.Column("seen_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_feedback_owner_unseen", "feedback", ["update_owner_id"], postgresql_where=sa.text("seen_at is null"))


def downgrade():
    op.drop_index("ix_feedback_owner_unseen", table_name="feedback")
    op.drop_column("feedback", "seen_at")
