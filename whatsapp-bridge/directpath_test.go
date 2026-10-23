package main

import "testing"

func TestExtractDirectPathKeepsCDNSignature(t *testing.T) {
	raw := "https://mmg.whatsapp.net/v/t62.7118-24/file.enc?ccb=11-4&oh=abc&oe=def"
	got := extractDirectPathFromURL(raw)
	want := "/v/t62.7118-24/file.enc?ccb=11-4&oh=abc&oe=def"
	if got != want {
		t.Fatalf("got %q, want %q", got, want)
	}
}

func TestExtractDirectPathAddsQueryWhenMissing(t *testing.T) {
	got := extractDirectPathFromURL("https://mmg.whatsapp.net/v/file.enc")
	want := "/v/file.enc?"
	if got != want {
		t.Fatalf("got %q, want %q", got, want)
	}
}
