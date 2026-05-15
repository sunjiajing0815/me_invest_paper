from langgraph.checkpoint.memory import MemorySaver


def make_checkpointer(db_path: str = "data/investor.db") -> MemorySaver:
    """In-memory checkpointer for within-invocation graph state.

    Previously used SqliteSaver, which caused 'database is locked' errors:
    the OLTP SQLAlchemy session holds a write tx during node execution (for
    llm_call_log flushes) and SqliteSaver's separate sqlite3 connection
    cannot acquire the write lock to checkpoint. Graph state is ephemeral —
    one graph.invoke() per ticker per run — so MemorySaver is sufficient.
    db_path is kept for call-site compatibility but is unused.
    """
    return MemorySaver()
