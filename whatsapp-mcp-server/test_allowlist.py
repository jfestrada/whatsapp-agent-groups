import os
import sqlite3
import tempfile
import unittest

import whatsapp


def insert_chat(conn, jid, name, last_message_time):
    conn.execute(
        "INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)",
        (jid, name, last_message_time),
    )


def insert_message(conn, message_id, chat_jid, sender, content, timestamp):
    conn.execute(
        """
        INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me, media_type)
        VALUES (?, ?, ?, ?, ?, 0, NULL)
        """,
        (message_id, chat_jid, sender, content, timestamp),
    )


class AllowlistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "messages.db")
        whatsapp.MESSAGES_DB_PATH = self.db_path
        whatsapp.ALLOWED_CHAT_JIDS = None
        for name in whatsapp.GROUP_ENV_VARS:
            os.environ.pop(name, None)
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TEXT)")
        conn.execute(
            """
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                chat_jid TEXT,
                sender TEXT,
                content TEXT,
                timestamp TEXT,
                is_from_me INTEGER,
                media_type TEXT
            )
            """
        )
        insert_chat(conn, "111@g.us", "Alpha", "2026-10-23T12:00:00")
        insert_chat(conn, "222@g.us", "Beta", "2026-10-23T12:00:00")
        insert_chat(conn, "333@g.us", "Gamma", "2026-10-23T12:00:00")
        insert_chat(conn, "444@g.us", "Delta", "2026-10-23T13:00:00")
        insert_chat(conn, "555@s.whatsapp.net", "Private Person", "2026-10-23T13:00:00")
        insert_chat(conn, "666@s.whatsapp.net", "Allowed Person", None)
        insert_chat(conn, "777@s.whatsapp.net", "Hidden Person", None)
        insert_message(conn, "alpha-1", "111@g.us", "666@s.whatsapp.net", "alpha-secret", "2026-10-23T12:00:00")
        insert_message(conn, "beta-1", "222@g.us", "666@s.whatsapp.net", "beta-secret", "2026-10-23T11:00:00")
        insert_message(conn, "gamma-1", "333@g.us", "666@s.whatsapp.net", "gamma-secret", "2026-10-23T10:00:00")
        insert_message(conn, "delta-1", "444@g.us", "666@s.whatsapp.net", "delta-secret", "2026-10-23T13:00:00")
        insert_message(conn, "dm-1", "555@s.whatsapp.net", "555@s.whatsapp.net", "dm-secret", "2026-10-23T13:00:00")
        insert_message(conn, "hidden-1", "444@g.us", "777@s.whatsapp.net", "hidden-secret", "2026-10-23T13:00:00")
        insert_chat(conn, "twin-a@g.us", "Twin", "2026-10-23T09:00:00")
        insert_chat(conn, "twin-b@g.us", "Twin", "2026-10-23T09:00:00")
        conn.commit()
        conn.close()

    def tearDown(self):
        whatsapp.ALLOWED_CHAT_JIDS = None
        self.tmp.cleanup()

    def allow_names(self):
        os.environ["ALLOWED_GROUP_1"] = "Alpha"
        os.environ["ALLOWED_GROUP_2"] = "Beta"
        os.environ["ALLOWED_GROUP_3"] = "Gamma"
        whatsapp.load_allowlist()

    def test_refuses_everything_until_configured(self):
        listed = whatsapp.list_messages(include_context=False, limit=50)
        self.assertNotIn("alpha-secret", listed)
        self.assertNotIn("delta-secret", listed)
        self.assertEqual(whatsapp.list_chats(include_last_message=False), [])
        self.assertIsNone(whatsapp.get_chat("111@g.us", include_last_message=False))
        success, message = whatsapp.send_message("111@g.us", "hello")
        self.assertFalse(success)
        self.assertEqual(message, "Recipient is not an allowed group")

    def test_names_resolve_and_hide_other_chats(self):
        self.allow_names()
        self.assertEqual(whatsapp.ALLOWED_CHAT_JIDS, ("111@g.us", "222@g.us", "333@g.us"))

        listed = whatsapp.list_messages(include_context=False, limit=50)
        self.assertIn("alpha-secret", listed)
        self.assertIn("beta-secret", listed)
        self.assertIn("gamma-secret", listed)
        self.assertNotIn("delta-secret", listed)
        self.assertNotIn("dm-secret", listed)

        blocked = whatsapp.list_messages(chat_jid="444@g.us", include_context=False, limit=50)
        self.assertNotIn("delta-secret", blocked)

        chats = whatsapp.list_chats(include_last_message=True, limit=50)
        self.assertEqual({chat.jid for chat in chats}, {"111@g.us", "222@g.us", "333@g.us"})
        self.assertTrue(all(chat.last_message != "delta-secret" for chat in chats))

        self.assertIsNone(whatsapp.get_chat("444@g.us"))
        allowed_chat = whatsapp.get_chat("111@g.us")
        self.assertIsNotNone(allowed_chat)
        self.assertEqual(allowed_chat.last_message, "alpha-secret")

        with self.assertRaises(ValueError) as caught:
            whatsapp.get_message_context("delta-1")
        self.assertNotIn("delta-secret", str(caught.exception))
        context = whatsapp.get_message_context("alpha-1", before=0, after=0)
        self.assertEqual(context.message.content, "alpha-secret")

        last = whatsapp.get_last_interaction("666@s.whatsapp.net")
        self.assertIn("alpha-secret", last)
        self.assertNotIn("delta-secret", last)
        self.assertIsNone(whatsapp.get_last_interaction("555@s.whatsapp.net"))

        contact_chats = whatsapp.get_contact_chats("666@s.whatsapp.net", limit=20)
        self.assertEqual({chat.jid for chat in contact_chats}, {"111@g.us", "222@g.us", "333@g.us"})
        self.assertIsNone(whatsapp.get_direct_chat_by_contact("555"))

        allowed_contacts = whatsapp.search_contacts("Allowed")
        self.assertEqual([contact.jid for contact in allowed_contacts], ["666@s.whatsapp.net"])
        self.assertEqual(whatsapp.search_contacts("Hidden"), [])
        self.assertEqual(whatsapp.search_contacts("Private"), [])

        for recipient in ("444@g.us", "34123456789"):
            success, message = whatsapp.send_message(recipient, "hello")
            self.assertFalse(success)
            self.assertEqual(message, "Recipient is not an allowed group")
            success, message = whatsapp.send_file(recipient, "/tmp/does-not-matter")
            self.assertFalse(success)
            self.assertEqual(message, "Recipient is not an allowed group")
            success, message = whatsapp.send_audio_message(recipient, "/tmp/does-not-matter")
            self.assertFalse(success)
            self.assertEqual(message, "Recipient is not an allowed group")

        self.assertIsNone(whatsapp.download_media("delta-1", "444@g.us"))
        self.assertIsNone(whatsapp.download_media("delta-1", "111@g.us"))

        counted = sqlite3.connect(self.db_path)
        counted.execute(
            "UPDATE messages SET timestamp = ? WHERE id = ?",
            ("2026-10-23 12:00:00", "alpha-1"),
        )
        counted.commit()
        counted.close()
        self.assertEqual(whatsapp.count_messages("111@g.us", "2026-10-23T11:00:00"), 1)
        self.assertEqual(whatsapp.count_messages("111@g.us", "2026-10-23T12:00:00"), 0)
        self.assertEqual(whatsapp.count_messages("444@g.us", "2026-10-23T11:00:00"), 0)

    def test_jid_values_are_accepted(self):
        os.environ["ALLOWED_GROUP_1"] = "111@g.us"
        os.environ["ALLOWED_GROUP_2"] = "222@g.us"
        os.environ["ALLOWED_GROUP_3"] = "333@g.us"
        self.assertEqual(whatsapp.load_allowlist(), ("111@g.us", "222@g.us", "333@g.us"))

    def test_one_or_two_groups_are_enough(self):
        os.environ["ALLOWED_GROUP_1"] = "Alpha"
        self.assertEqual(whatsapp.load_allowlist(), ("111@g.us",))
        listed = whatsapp.list_messages(include_context=False, limit=50)
        self.assertIn("alpha-secret", listed)
        self.assertNotIn("beta-secret", listed)
        self.assertNotIn("gamma-secret", listed)
        self.assertNotIn("delta-secret", listed)
        chats = whatsapp.list_chats(include_last_message=False, limit=50)
        self.assertEqual([chat.jid for chat in chats], ["111@g.us"])

        whatsapp.ALLOWED_CHAT_JIDS = None
        os.environ["ALLOWED_GROUP_3"] = "Gamma"
        self.assertEqual(whatsapp.load_allowlist(), ("111@g.us", "333@g.us"))
        chats = whatsapp.list_chats(include_last_message=False, limit=50)
        self.assertEqual({chat.jid for chat in chats}, {"111@g.us", "333@g.us"})

    def test_missing_unknown_ambiguous_and_duplicate_names(self):
        with self.assertRaises(whatsapp.AllowlistError) as missing:
            whatsapp.load_allowlist()
        self.assertIn("At least one of ALLOWED_GROUP_1", str(missing.exception))

        os.environ["ALLOWED_GROUP_1"] = "Alpha"
        os.environ["ALLOWED_GROUP_2"] = "Nope"
        with self.assertRaises(whatsapp.AllowlistError) as unknown:
            whatsapp.load_allowlist()
        self.assertIn("ALLOWED_GROUP_2 does not match a group", str(unknown.exception))
        self.assertNotIn("444@g.us", str(unknown.exception))

        os.environ["ALLOWED_GROUP_2"] = "Twin"
        with self.assertRaises(whatsapp.AllowlistError) as ambiguous:
            whatsapp.load_allowlist()
        message = str(ambiguous.exception)
        self.assertIn("twin-a@g.us", message)
        self.assertIn("twin-b@g.us", message)
        self.assertNotIn("444@g.us", message)

        os.environ["ALLOWED_GROUP_1"] = "Alpha"
        os.environ["ALLOWED_GROUP_2"] = "Alpha"
        os.environ["ALLOWED_GROUP_3"] = "Alpha"
        with self.assertRaises(whatsapp.AllowlistError) as duplicate:
            whatsapp.load_allowlist()
        self.assertIn("must be different groups", str(duplicate.exception))


if __name__ == "__main__":
    unittest.main()
