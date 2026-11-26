package main

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestMediaPolicy(t *testing.T) {
	t.Setenv("WHATSAPP_AUTO_DOWNLOAD", "")
	t.Setenv("WHATSAPP_MEDIA_RETENTION", "")
	p, err := loadMediaPolicy()
	if err != nil {
		t.Fatal(err)
	}
	if p.Retention != 30*24*time.Hour || p.MaxBytes != 500<<20 || p.AutoMaxBytes != 25<<20 {
		t.Errorf("defaults: %+v", p)
	}
	cases := []struct {
		media MediaInfo
		want  bool
	}{
		{MediaInfo{Type: "document", FileLength: 1 << 20}, true},
		{MediaInfo{Type: "audio", Voice: true, FileLength: 50 << 10}, true},
		{MediaInfo{Type: "audio", FileLength: 50 << 10}, false}, // music, not a voice note
		{MediaInfo{Type: "video", FileLength: 1 << 20}, false},
		{MediaInfo{Type: "document", FileLength: 26 << 20}, false}, // over the size limit
		{MediaInfo{Type: "document"}, false},                       // unknown size
		{MediaInfo{}, false},
	}
	for _, c := range cases {
		if got := p.wantsAuto(c.media); got != c.want {
			t.Errorf("wantsAuto(%+v) = %v", c.media, got)
		}
	}

	t.Setenv("WHATSAPP_AUTO_DOWNLOAD", "none")
	if p, _ := loadMediaPolicy(); p.wantsAuto(MediaInfo{Type: "document", FileLength: 1}) {
		t.Error("none must disable auto-download")
	}
	for name, bad := range map[string]string{"WHATSAPP_AUTO_DOWNLOAD": "documents", "WHATSAPP_MEDIA_RETENTION": "-1d",
		"WHATSAPP_MEDIA_MAX_TOTAL_MB": "lots"} {
		t.Setenv(name, bad)
		if _, err := loadMediaPolicy(); err == nil {
			t.Errorf("%s=%q should be rejected", name, bad)
		}
		t.Setenv(name, "")
	}
}

func TestLocalMediaPathStaysInside(t *testing.T) {
	p := localMediaPath("../../etc", "../x", "../../passwd", "document")
	rel, err := filepath.Rel(mediaDir, p)
	if err != nil || filepath.Dir(filepath.Dir(rel)) != "." || filepath.IsAbs(rel) || rel[:2] == ".." {
		t.Errorf("path escapes the media folder: %s", p)
	}
}

func TestCleanupMedia(t *testing.T) {
	dir := t.TempDir()
	now := time.Now()
	write := func(name string, size int, age time.Duration) string {
		p := filepath.Join(dir, name)
		os.MkdirAll(filepath.Dir(p), 0700)
		if err := os.WriteFile(p, make([]byte, size), 0600); err != nil {
			t.Fatal(err)
		}
		os.Chtimes(p, now.Add(-age), now.Add(-age))
		return p
	}
	old := write("chatA/old.pdf", 100, 40*24*time.Hour)
	lru := write("chatB/lru.ogg", 300, 10*24*time.Hour)
	fresh := write("chatB/fresh.ogg", 300, time.Hour)
	partial := write("chatB/now.pdf"+partialSuffix, 50, time.Minute)

	removed, freed, err := cleanupMedia(dir, now, 30*24*time.Hour, 400)
	if err != nil {
		t.Fatal(err)
	}
	// old.pdf is past retention; then lru.ogg goes to fit 400 bytes; the download in progress is untouched
	if removed != 2 || freed != 400 {
		t.Errorf("removed %d files, %d bytes", removed, freed)
	}
	for p, want := range map[string]bool{old: false, lru: false, fresh: true, partial: true} {
		if _, err := os.Stat(p); (err == nil) != want {
			t.Errorf("%s exists=%v, want %v", p, err == nil, want)
		}
	}
	if _, err := os.Stat(filepath.Join(dir, "chatA")); !os.IsNotExist(err) {
		t.Error("empty chat folder should be removed")
	}
	if _, _, err := cleanupMedia(filepath.Join(dir, "missing"), now, time.Hour, 0); err != nil {
		t.Errorf("missing folder: %v", err)
	}
}

func TestParseDays(t *testing.T) {
	for in, want := range map[string]time.Duration{"30d": 720 * time.Hour, "12h": 12 * time.Hour, "0": 0, "0d": 0} {
		if got, err := parseDays(in); err != nil || got != want {
			t.Errorf("parseDays(%q) = %v, %v", in, got, err)
		}
	}
	for _, bad := range []string{"d", "x", "-2h", "1.5d"} {
		if _, err := parseDays(bad); err == nil {
			t.Errorf("parseDays(%q) should fail", bad)
		}
	}
}
