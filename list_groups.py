#!/usr/bin/env python3
"""Print the name and JID of every WhatsApp group in the bridge database."""

import sqlite3
import sys

DATABASE = "/data/messages.db"


def main() -> None:
    connection = sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True, timeout=5)
    connection.execute("PRAGMA busy_timeout = 5000")
    rows = connection.execute(
        """
        SELECT name, jid
        FROM chats
        WHERE jid LIKE '%@g.us'
        ORDER BY name COLLATE NOCASE, jid
        """
    ).fetchall()
    connection.close()

    if not rows:
        print("No groups found.", file=sys.stderr)
        sys.exit(1)

    for name, jid in rows:
        print(f"{name or '(no name)'}\t{jid}")


if __name__ == "__main__":
    main()