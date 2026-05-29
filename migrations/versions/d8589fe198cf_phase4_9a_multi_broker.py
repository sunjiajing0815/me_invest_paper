"""phase4_9a multi broker

Adds the multi-broker partition key (broker_account_id) to every per-account
table, promotes broker_account to a dual-purpose identity+state table via a
stable account_ref column, and replaces the single meta.auto_trade_mode key
with a per-broker auto_trade_state table. Backfills all existing rows to Jane's
existing (Alpaca) account so single-broker history carries through intact.

Assumes the create_all-managed tables (broker_account, target_allocation,
positions_snapshot) already exist — init_db() always runs create_all before
alembic, so this holds for the app and for any DB that has ever started the app.
A pure `alembic upgrade head` on a never-create_all'd empty DB will fail here,
which is fine: those tables are create_all-only, so such a DB was never complete.

Revision ID: d8589fe198cf
Revises: 62b0733b198f
Create Date: 2026-05-29 18:42:23.820678

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd8589fe198cf'
down_revision: str | Sequence[str] | None = '7d25844a8a9a'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PER_ACCOUNT_TABLES = (
    "target_allocation",
    "positions_snapshot",
    "order_suggestion",
    "order_execution",
)

_ALPACA_CONN_CONFIG = (
    '{"api_key_env": "ALPACA_API_KEY", "secret_env": "ALPACA_SECRET_KEY", "paper": true}'
)


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()

    # 1. broker_account: stable partition key (account_ref) + identity columns.
    with op.batch_alter_table("broker_account", schema=None) as b:
        b.add_column(sa.Column("account_ref", sa.Integer(), nullable=True))
        b.add_column(sa.Column("nickname", sa.String(), nullable=True))
        b.add_column(
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default="1")
        )
        b.add_column(sa.Column("connection_config", sa.Text(), nullable=True))

    # 2. broker_account_id partition key on per-account tables (nullable for now;
    #    tightened to NOT NULL in a follow-up migration once all writers set it).
    for tbl in _PER_ACCOUNT_TABLES:
        with op.batch_alter_table(tbl, schema=None) as b:
            b.add_column(sa.Column("broker_account_id", sa.Integer(), nullable=True))

    # 3. per-broker auto_trade_state table (replaces meta.auto_trade_mode).
    #    Guarded: create_all(checkfirst=True) in init_db may have already created
    #    this new model table before alembic runs (depending on deploy order), so
    #    only create it here if it's absent — see CLAUDE.md create_all+alembic flow.
    if "auto_trade_state" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "auto_trade_state",
            sa.Column("broker_account_id", sa.Integer(), nullable=False),
            sa.Column("mode", sa.String(), server_default="OFF", nullable=False),
            sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("promotion_soak_complete_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_kill_switch_event", sa.DateTime(timezone=True), nullable=True),
            sa.Column("per_order_cap_usd", sa.Double(), nullable=True),
            sa.Column("per_day_cap_usd", sa.Double(), nullable=True),
            sa.Column("per_week_per_ticker_cap_usd", sa.Double(), nullable=True),
            sa.Column("per_day_order_count_cap", sa.Integer(), nullable=True),
            sa.PrimaryKeyConstraint("broker_account_id"),
        )

    # 4. Backfill. Identify Jane's canonical account = latest open broker_account
    #    row; fall back to the highest id if none is open. All existing per-account
    #    rows belong to that one account (single-broker history).
    jane_ref = bind.execute(
        sa.text(
            "SELECT id FROM broker_account WHERE effective_to IS NULL "
            "ORDER BY last_sync DESC LIMIT 1"
        )
    ).scalar()
    if jane_ref is None:
        jane_ref = bind.execute(
            sa.text("SELECT id FROM broker_account ORDER BY id DESC LIMIT 1")
        ).scalar()

    if jane_ref is not None:
        # account_ref constant across this account's state-history rows.
        bind.execute(
            sa.text("UPDATE broker_account SET account_ref = :ref"), {"ref": jane_ref}
        )
        # Identity columns on the latest open row (source of truth for identity).
        bind.execute(
            sa.text(
                "UPDATE broker_account "
                "SET nickname = COALESCE(nickname, 'Alpaca paper'), "
                "    connection_config = COALESCE(connection_config, :cfg) "
                "WHERE effective_to IS NULL"
            ),
            {"cfg": _ALPACA_CONN_CONFIG},
        )
        for tbl in _PER_ACCOUNT_TABLES:
            bind.execute(
                sa.text(f"UPDATE {tbl} SET broker_account_id = :ref"),  # noqa: S608
                {"ref": jane_ref},
            )
        # Seed auto_trade_state from the old global meta.auto_trade_mode key.
        mode = (
            bind.execute(
                sa.text("SELECT value FROM meta WHERE key = 'auto_trade_mode'")
            ).scalar()
            or "OFF"
        )
        bind.execute(
            sa.text(
                "INSERT OR IGNORE INTO auto_trade_state (broker_account_id, mode) "
                "VALUES (:ref, :mode)"
            ),
            {"ref": jane_ref, "mode": mode},
        )

    # The auto-trade mode is now per-broker in auto_trade_state.
    bind.execute(sa.text("DELETE FROM meta WHERE key = 'auto_trade_mode'"))

    # 5. Swap order_suggestion unique constraint to include broker_account_id.
    #    (Existing rows are backfilled above, so no NULL-uniqueness gap on real data.)
    with op.batch_alter_table("order_suggestion", schema=None) as b:
        b.drop_constraint("uq_one_per_ticker_per_week", type_="unique")
        b.create_unique_constraint(
            "uq_suggestion_account_week",
            ["broker_account_id", "week_of", "ticker", "side"],
        )

    # 6. Indexes.
    op.create_index("ix_broker_account_ref", "broker_account", ["account_ref"])
    op.create_index(
        "ix_target_alloc_account_ticker", "target_allocation",
        ["broker_account_id", "ticker"],
    )
    op.create_index(
        "ix_positions_account_ticker_ts", "positions_snapshot",
        ["broker_account_id", "ticker", "ts"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_positions_account_ticker_ts", table_name="positions_snapshot")
    op.drop_index("ix_target_alloc_account_ticker", table_name="target_allocation")
    op.drop_index("ix_broker_account_ref", table_name="broker_account")

    with op.batch_alter_table("order_suggestion", schema=None) as b:
        b.drop_constraint("uq_suggestion_account_week", type_="unique")
        b.create_unique_constraint(
            "uq_one_per_ticker_per_week", ["week_of", "ticker", "side"]
        )

    # Restore the global meta.auto_trade_mode from the canonical broker's row.
    bind = op.get_bind()
    mode = bind.execute(
        sa.text(
            "SELECT mode FROM auto_trade_state ORDER BY broker_account_id LIMIT 1"
        )
    ).scalar()
    if mode is not None:
        bind.execute(
            sa.text(
                "INSERT OR REPLACE INTO meta (key, value) "
                "VALUES ('auto_trade_mode', :mode)"
            ),
            {"mode": mode},
        )

    op.drop_table("auto_trade_state")

    for tbl in _PER_ACCOUNT_TABLES:
        with op.batch_alter_table(tbl, schema=None) as b:
            b.drop_column("broker_account_id")

    with op.batch_alter_table("broker_account", schema=None) as b:
        b.drop_column("connection_config")
        b.drop_column("is_active")
        b.drop_column("nickname")
        b.drop_column("account_ref")
