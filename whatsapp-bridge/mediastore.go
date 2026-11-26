package main

import (
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

// Downloaded media lives in store/media/<chat>/<message id>_<file name>. It is a cache: WhatsApp keeps media
// on its servers for only ~2-3 weeks, so documents and voice notes are fetched when they arrive, and files
// not used for a while are deleted again. Text extracted from documents and transcripts stay in the MCP
// server's cache, so a document that was read once remains readable after its file is gone.
//
// Files are not compressed: measured on real chats, PDFs shrink by ~15%, JPEG by ~6% and Opus, MP4 and ZIP
// not at all, while every reader (pdftotext, ffmpeg, the user) would need a decompressed copy. Space is
// saved instead by downloading only small, useful kinds, by hard-linking forwarded copies of the same file,
// and by the retention and total size limits below.
const mediaDir = "store/media"

// MediaPolicy holds the auto-download and cleanup settings, read from the environment
type MediaPolicy struct {
	AutoKinds    map[string]bool // document, voice, audio, image, video, sticker
	AutoMaxBytes int64           // larger files are never fetched automatically
	Retention    time.Duration   // files unused for this long are deleted (0: keep)
	MaxBytes     int64           // total size of store/media (0: no limit)
}

// Defaults: documents and voice notes up to 25 MB, kept 30 days after last use, 500 MB in total
func loadMediaPolicy() (MediaPolicy, error) {
	p := MediaPolicy{AutoKinds: map[string]bool{}}
	kinds := envOr("WHATSAPP_AUTO_DOWNLOAD", "document,voice")
	for _, k := range strings.Split(kinds, ",") {
		switch k = strings.TrimSpace(strings.ToLower(k)); k {
		case "", "none", "0", "off":
		case "document", "voice", "audio", "image", "video", "sticker":
			p.AutoKinds[k] = true
		default:
			return p, fmt.Errorf("invalid WHATSAPP_AUTO_DOWNLOAD %q: use a comma-separated list of document, voice, audio, image, video, sticker, or none", kinds)
		}
	}
	var err error
	if p.AutoMaxBytes, err = envMB("WHATSAPP_AUTO_DOWNLOAD_MAX_MB", 25); err != nil {
		return p, err
	}
	if p.MaxBytes, err = envMB("WHATSAPP_MEDIA_MAX_TOTAL_MB", 500); err != nil {
		return p, err
	}
	if p.Retention, err = parseDays(envOr("WHATSAPP_MEDIA_RETENTION", "30d")); err != nil {
		return p, fmt.Errorf("invalid WHATSAPP_MEDIA_RETENTION: %v (e.g. 30d, 12h, 0 to keep files)", err)
	}
	return p, nil
}

func envOr(name, fallback string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return fallback
}

func envMB(name string, fallback float64) (int64, error) {
	v := os.Getenv(name)
	if v == "" {
		return int64(fallback * (1 << 20)), nil
	}
	mb, err := strconv.ParseFloat(v, 64)
	if err != nil || mb < 0 {
		return 0, fmt.Errorf("invalid %s %q: use a number of megabytes", name, v)
	}
	return int64(mb * (1 << 20)), nil
}

// Like time.ParseDuration, plus whole days ("30d")
func parseDays(s string) (time.Duration, error) {
	if days, ok := strings.CutSuffix(s, "d"); ok {
		n, err := strconv.Atoi(days)
		if err != nil || n < 0 {
			return 0, fmt.Errorf("%q is not a number of days", s)
		}
		return time.Duration(n) * 24 * time.Hour, nil
	}
	d, err := time.ParseDuration(s)
	if err != nil || d < 0 {
		return 0, fmt.Errorf("%q is not a duration", s)
	}
	return d, nil
}

// Whether a newly received attachment should be downloaded right away
func (p MediaPolicy) wantsAuto(media MediaInfo) bool {
	if media.Type == "" || media.FileLength == 0 || int64(media.FileLength) > p.AutoMaxBytes {
		return false
	}
	kind := media.Type
	if kind == "audio" && media.Voice {
		kind = "voice"
	}
	return p.AutoKinds[kind]
}

// Local path of a message's media. Chat JID, message ID and file name come from other people: all sanitized.
func localMediaPath(chatJID, messageID, filename, mediaType string) string {
	chatDir := safeFilename(strings.ReplaceAll(chatJID, ":", "_"), "unknown_chat")
	return filepath.Join(mediaDir, chatDir, safeFilename(messageID, "message")+"_"+safeFilename(filename, mediaType))
}

// Mark a file as used now, so cleanup keeps it for another retention period
func touchMedia(path string) {
	now := time.Now()
	os.Chtimes(path, now, now)
}

// Downloads still being written are skipped by cleanup
const partialSuffix = ".part"

// Delete media files unused for longer than the retention, then the least recently used ones until the
// folder fits the size limit. Returns how many files were removed and how many bytes were freed.
func cleanupMedia(dir string, now time.Time, retention time.Duration, maxBytes int64) (int, int64, error) {
	type file struct {
		path  string
		size  int64
		mtime time.Time
	}
	var files []file
	var total int64
	err := filepath.WalkDir(dir, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			if os.IsNotExist(err) {
				return nil
			}
			return err
		}
		if !d.Type().IsRegular() {
			return nil
		}
		info, err := d.Info()
		if err != nil {
			return nil
		}
		// A leftover partial download older than an hour is garbage; a recent one is in progress
		if strings.HasSuffix(path, partialSuffix) && now.Sub(info.ModTime()) < time.Hour {
			return nil
		}
		files = append(files, file{path, info.Size(), info.ModTime()})
		total += info.Size()
		return nil
	})
	if err != nil {
		return 0, 0, err
	}

	sort.Slice(files, func(i, j int) bool { return files[i].mtime.Before(files[j].mtime) })
	removed, freed := 0, int64(0)
	for _, f := range files {
		expired := retention > 0 && now.Sub(f.mtime) > retention
		tooBig := maxBytes > 0 && total > maxBytes
		if !expired && !tooBig {
			continue
		}
		if err := os.Remove(f.path); err == nil {
			removed++
			freed += f.size
			total -= f.size
		}
	}
	removeEmptyDirs(dir)
	return removed, freed, nil
}

// Remove the per-chat folders left empty by cleanup (the media folder itself stays)
func removeEmptyDirs(dir string) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return
	}
	for _, e := range entries {
		if e.IsDir() {
			os.Remove(filepath.Join(dir, e.Name())) // fails, harmlessly, unless empty
		}
	}
}

// Run cleanup now and then every few hours for as long as the bridge runs
func startMediaCleanup(p MediaPolicy) {
	if p.Retention == 0 && p.MaxBytes == 0 {
		return
	}
	go func() {
		for {
			removed, freed, err := cleanupMedia(mediaDir, time.Now(), p.Retention, p.MaxBytes)
			if err != nil {
				fmt.Printf("Media cleanup failed: %v\n", err)
			} else if removed > 0 {
				fmt.Printf("Media cleanup: removed %d file(s), freed %.1f MB\n", removed, float64(freed)/(1<<20))
			}
			time.Sleep(6 * time.Hour)
		}
	}()
}
