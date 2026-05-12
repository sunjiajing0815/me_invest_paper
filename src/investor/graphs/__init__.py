import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver


def make_checkpointer(db_path: str = "data/investor.db") -> SqliteSaver:
    """Create a SqliteSaver backed by a dedicated sqlite3 connection.

    SqliteSaver.from_conn_string is a @contextmanager that cannot be used at
    module level. We call the constructor directly with an explicit connection.
    check_same_thread=False is required: APScheduler may invoke the graph from
    a different thread than the one that opened the connection.
    """
    conn = sqlite3.connect(db_path, check_same_thread=False)
    return SqliteSaver(conn)
