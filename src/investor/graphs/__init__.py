from langgraph.checkpoint.sqlite import SqliteSaver

# Shared checkpointer so every graph run is resumable / inspectable.
# Lives in data/investor.db alongside OLTP tables.
CHECKPOINTER = SqliteSaver.from_conn_string("data/investor.db")
