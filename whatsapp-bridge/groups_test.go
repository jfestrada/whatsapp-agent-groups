package main

import (
	"database/sql"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestChooseGroupsKeepsJoinedTitlesAndRejectsTheRest(t *testing.T) {
	joined := map[string]string{
		"222@g.us": "Beta",
		"111@g.us": "Alpha",
	}
	chosen, err := chooseGroups(joined, []string{" 222@g.us ", "111@g.us", "111@g.us"})
	if err != nil {
		t.Fatal(err)
	}
	if len(chosen) != 2 || chosen[0].JID != "111@g.us" || chosen[0].Name != "Alpha" || chosen[1].Name != "Beta" {
		t.Fatalf("chosen = %#v", chosen)
	}
	if _, err := chooseGroups(joined, []string{"999@g.us"}); err != errNotJoined {
		t.Fatalf("unknown group error = %v", err)
	}
	if _, err := chooseGroups(joined, []string{"555@s.whatsapp.net"}); err != errNotAGroup {
		t.Fatalf("direct chat error = %v", err)
	}
	if _, err := chooseGroups(joined, nil); err != errSelectionRequired {
		t.Fatalf("missing list error = %v", err)
	}
	empty, err := chooseGroups(joined, []string{})
	if err != nil || len(empty) != 0 {
		t.Fatalf("empty selection = %#v, %v", empty, err)
	}
}

func TestSelectionFileSurvivesARewriteAndStaysPrivate(t *testing.T) {
	path := filepath.Join(t.TempDir(), allowedGroupsFile)
	first := []groupChoice{{JID: "111@g.us", Name: "Alpha"}}
	if err := saveSelection(path, first); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0600 {
		t.Fatalf("mode = %o", info.Mode().Perm())
	}
	loaded, err := loadSelection(path)
	if err != nil || len(loaded) != 1 || loaded[0].JID != "111@g.us" || loaded[0].Name != "Alpha" {
		t.Fatalf("loaded = %#v, %v", loaded, err)
	}
	if err := saveSelection(path, []groupChoice{}); err != nil {
		t.Fatal(err)
	}
	loaded, err = loadSelection(path)
	if err != nil || len(loaded) != 0 {
		t.Fatalf("cleared = %#v, %v", loaded, err)
	}
	if _, err := os.Stat(path + ".tmp"); !os.IsNotExist(err) {
		t.Fatal("temporary selection file was left behind")
	}
}

func TestRememberGroupsUpdatesTheTitleWithoutClearingHistory(t *testing.T) {
	store, err := NewMessageStore(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	when := time.Date(2026, 10, 24, 12, 0, 0, 0, time.UTC)
	if err := store.StoreChat("111@g.us", "Old", when); err != nil {
		t.Fatal(err)
	}
	err = store.RememberGroups([]groupChoice{
		{JID: "111@g.us", Name: "Alpha"},
		{JID: "222@g.us", Name: "Beta"},
	})
	if err != nil {
		t.Fatal(err)
	}
	var name string
	var stamp sql.NullString
	if err := store.db.QueryRow("SELECT name, last_message_time FROM chats WHERE jid = ?", "111@g.us").Scan(&name, &stamp); err != nil {
		t.Fatal(err)
	}
	if name != "Alpha" || !stamp.Valid || stamp.String == "" {
		t.Fatalf("existing chat = %q %v", name, stamp)
	}
	if err := store.db.QueryRow("SELECT name, last_message_time FROM chats WHERE jid = ?", "222@g.us").Scan(&name, &stamp); err != nil {
		t.Fatal(err)
	}
	if name != "Beta" || stamp.Valid {
		t.Fatalf("new chat = %q %v", name, stamp)
	}
}
