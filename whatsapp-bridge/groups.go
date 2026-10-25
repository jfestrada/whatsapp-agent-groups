package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/types"
)

const allowedGroupsFile = "allowed-groups.json"

var (
	errSelectionRequired = errors.New("jids is required")
	errTooManyGroups     = errors.New("too many groups")
	errNotAGroup         = errors.New("not a group")
	errNotJoined         = errors.New("group is not on this account")
)

type groupChoice struct {
	JID  string `json:"jid"`
	Name string `json:"name"`
}

type selectionFile struct {
	Version int           `json:"version"`
	SavedAt string        `json:"saved_at"`
	Groups  []groupChoice `json:"groups"`
}

type groupView struct {
	JID      string `json:"jid"`
	Name     string `json:"name"`
	Selected bool   `json:"selected"`
}

type groupDirectory struct {
	mu    sync.Mutex
	at    time.Time
	names map[string]string
}

func normalizeGroupJID(raw string) (string, bool) {
	jid := strings.TrimSpace(raw)
	if jid == "" || strings.ContainsAny(jid, " \t\r\n") {
		return "", false
	}
	user, server, ok := strings.Cut(jid, "@")
	if !ok || server != "g.us" || user == "" || strings.Contains(user, "@") {
		return "", false
	}
	for _, r := range user {
		switch {
		case r >= '0' && r <= '9':
		case r >= 'a' && r <= 'z':
		case r >= 'A' && r <= 'Z':
		case r == '.' || r == '_' || r == '-':
		default:
			return "", false
		}
	}
	return jid, true
}

func displayGroupName(jid, name string) string {
	name = strings.TrimSpace(name)
	if name == "" {
		return jid
	}
	return name
}

func chooseGroups(joined map[string]string, requested []string) ([]groupChoice, error) {
	if requested == nil {
		return nil, errSelectionRequired
	}
	if len(requested) > 500 {
		return nil, errTooManyGroups
	}
	seen := make(map[string]struct{}, len(requested))
	out := make([]groupChoice, 0, len(requested))
	for _, raw := range requested {
		jid, ok := normalizeGroupJID(raw)
		if !ok {
			return nil, errNotAGroup
		}
		name, found := joined[jid]
		if !found {
			return nil, errNotJoined
		}
		if _, dup := seen[jid]; dup {
			continue
		}
		seen[jid] = struct{}{}
		out = append(out, groupChoice{JID: jid, Name: displayGroupName(jid, name)})
	}
	sort.Slice(out, func(i, j int) bool {
		left := strings.ToLower(out[i].Name)
		right := strings.ToLower(out[j].Name)
		if left == right {
			return out[i].JID < out[j].JID
		}
		return left < right
	})
	return out, nil
}

func saveSelection(path string, groups []groupChoice) error {
	if groups == nil {
		groups = []groupChoice{}
	}
	raw, err := json.MarshalIndent(selectionFile{
		Version: 1,
		SavedAt: time.Now().UTC().Format(time.RFC3339Nano),
		Groups:  groups,
	}, "", "  ")
	if err != nil {
		return err
	}
	raw = append(raw, '\n')
	tmp := path + ".tmp"
	file, err := os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0600)
	if err != nil {
		return err
	}
	if _, err := file.Write(raw); err != nil {
		file.Close()
		os.Remove(tmp)
		return err
	}
	if err := file.Sync(); err != nil {
		file.Close()
		os.Remove(tmp)
		return err
	}
	if err := file.Close(); err != nil {
		os.Remove(tmp)
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		os.Remove(tmp)
		return err
	}
	return os.Chmod(path, 0600)
}

func loadSelection(path string) ([]groupChoice, error) {
	raw, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var payload selectionFile
	if err := json.Unmarshal(raw, &payload); err != nil || payload.Version != 1 || payload.Groups == nil {
		return nil, fmt.Errorf("invalid selection file")
	}
	for _, group := range payload.Groups {
		if _, ok := normalizeGroupJID(group.JID); !ok {
			return nil, fmt.Errorf("invalid selection file")
		}
	}
	return payload.Groups, nil
}

func (store *MessageStore) RememberGroups(groups []groupChoice) error {
	tx, err := store.db.Begin()
	if err != nil {
		return err
	}
	for _, group := range groups {
		if _, err := tx.Exec(`
			INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, NULL)
			ON CONFLICT(jid) DO UPDATE SET name = excluded.name
		`, group.JID, group.Name); err != nil {
			tx.Rollback()
			return err
		}
	}
	return tx.Commit()
}

func (d *groupDirectory) joinedNames(client *whatsmeow.Client, fresh bool) (map[string]string, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if !fresh && time.Since(d.at) < 15*time.Second && d.names != nil {
		copied := make(map[string]string, len(d.names))
		for jid, name := range d.names {
			copied[jid] = name
		}
		return copied, nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	infos, err := client.GetJoinedGroups(ctx)
	if err != nil {
		return nil, err
	}
	names := make(map[string]string, len(infos))
	for _, info := range infos {
		if info == nil || info.JID.Server != types.GroupServer {
			continue
		}
		names[info.JID.String()] = strings.TrimSpace(info.Name)
	}
	d.names = names
	d.at = time.Now()
	copied := make(map[string]string, len(names))
	for jid, name := range names {
		copied[jid] = name
	}
	return copied, nil
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(payload)
}

func registerGroupRoutes(client *whatsmeow.Client, messageStore *MessageStore, storeDir string, status *bridgeStatus) {
	selectionPath := filepath.Join(storeDir, allowedGroupsFile)
	directory := &groupDirectory{}
	var selectionMu sync.Mutex

	http.HandleFunc("/api/groups", func(w http.ResponseWriter, r *http.Request) {
		if status.get() != "connected" {
			writeJSON(w, http.StatusConflict, map[string]string{"error": "WhatsApp is not connected"})
			return
		}
		switch r.Method {
		case http.MethodGet:
			joined, err := directory.joinedNames(client, r.URL.Query().Get("fresh") == "1")
			if err != nil {
				writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Could not load groups"})
				return
			}
			selectionMu.Lock()
			saved, err := loadSelection(selectionPath)
			selectionMu.Unlock()
			if err != nil {
				writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "Saved group selection could not be read"})
				return
			}
			selected := make(map[string]bool, len(saved))
			for _, group := range saved {
				selected[group.JID] = true
			}
			views := make([]groupView, 0, len(joined))
			for jid, name := range joined {
				views = append(views, groupView{
					JID:      jid,
					Name:     displayGroupName(jid, name),
					Selected: selected[jid],
				})
			}
			sort.Slice(views, func(i, j int) bool {
				left := strings.ToLower(views[i].Name)
				right := strings.ToLower(views[j].Name)
				if left == right {
					return views[i].JID < views[j].JID
				}
				return left < right
			})
			writeJSON(w, http.StatusOK, map[string]any{"groups": views})
		case http.MethodPut:
			r.Body = http.MaxBytesReader(w, r.Body, 64*1024)
			var req struct {
				JIDs []string `json:"jids"`
			}
			if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
				writeJSON(w, http.StatusBadRequest, map[string]string{"error": "Request body must be JSON"})
				return
			}
			joined, err := directory.joinedNames(client, true)
			if err != nil {
				writeJSON(w, http.StatusBadGateway, map[string]string{"error": "Could not load groups"})
				return
			}
			chosen, err := chooseGroups(joined, req.JIDs)
			if err != nil {
				writeJSON(w, http.StatusBadRequest, map[string]string{"error": "Select groups from the list on this page"})
				return
			}
			selectionMu.Lock()
			defer selectionMu.Unlock()
			if err := messageStore.RememberGroups(chosen); err != nil {
				writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "Could not store the selected groups"})
				return
			}
			if err := saveSelection(selectionPath, chosen); err != nil {
				writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "Could not store the selected groups"})
				return
			}
			fmt.Printf("Saved %d allowed groups\n", len(chosen))
			views := make([]groupView, 0, len(chosen))
			for _, group := range chosen {
				views = append(views, groupView{JID: group.JID, Name: group.Name, Selected: true})
			}
			writeJSON(w, http.StatusOK, map[string]any{"groups": views})
		default:
			w.Header().Set("Allow", "GET, PUT")
			writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "Method not allowed"})
		}
	})
}
