package main

import (
	"database/sql"
	"fmt"
	"strings"
	"time"
	"unicode"

	waProto "go.mau.fi/whatsmeow/binary/proto"
	"go.mau.fi/whatsmeow/types/events"
)

// MediaInfo is what we store about a media attachment. The metadata (duration, size, pages) lets
// the MCP client decide whether a file is worth opening before downloading it.
type MediaInfo struct {
	Type          string // image, sticker, video, audio, document, or "" for no media
	Filename      string
	Mimetype      string
	URL           string
	DirectPath    string
	MediaKey      []byte
	FileSHA256    []byte
	FileEncSHA256 []byte
	FileLength    uint64
	Seconds       uint32 // audio and video duration
	Width         uint32
	Height        uint32
	Pages         uint32 // documents
	Voice         bool   // audio recorded as a voice note (push to talk)
}

// LinkPreview is the preview WhatsApp attaches to a message with a link. The sender's phone built it,
// so it is untrusted text, but reading it never requires fetching the link.
type LinkPreview struct {
	URL         string
	Title       string
	Description string
}

// Columns added after the original schema; added to existing databases on startup
var mediaColumns = []struct{ name, kind string }{
	{"mimetype", "TEXT"},
	{"direct_path", "TEXT"},
	{"media_seconds", "INTEGER"},
	{"media_width", "INTEGER"},
	{"media_height", "INTEGER"},
	{"media_pages", "INTEGER"},
	{"media_voice", "BOOLEAN"},
	{"link_url", "TEXT"},
	{"link_title", "TEXT"},
	{"link_description", "TEXT"},
}

// Add the media and link preview columns to a messages table created by an older version
func migrateMessageColumns(db *sql.DB) error {
	rows, err := db.Query("PRAGMA table_info(messages)")
	if err != nil {
		return err
	}
	existing := map[string]bool{}
	for rows.Next() {
		var cid, notNull, pk int
		var name, kind string
		var dflt sql.NullString
		if err := rows.Scan(&cid, &name, &kind, &notNull, &dflt, &pk); err != nil {
			rows.Close()
			return err
		}
		existing[name] = true
	}
	rows.Close()

	for _, col := range mediaColumns {
		if existing[col.name] {
			continue
		}
		if _, err := db.Exec(fmt.Sprintf("ALTER TABLE messages ADD COLUMN %s %s", col.name, col.kind)); err != nil {
			return fmt.Errorf("failed to add column %s: %v", col.name, err)
		}
	}
	return nil
}

// Remove wrappers (ephemeral, view once, document with caption, edits...) the same way whatsmeow
// does for live messages, so history sync messages are read the same way
func unwrapMessage(msg *waProto.Message) *waProto.Message {
	if msg == nil {
		return nil
	}
	return (&events.Message{RawMessage: msg}).UnwrapRaw().Message
}

// Extract the text of a message: the text itself, or the caption of a photo, video or document
func extractTextContent(msg *waProto.Message) string {
	if msg == nil {
		return ""
	}
	if text := msg.GetConversation(); text != "" {
		return text
	}
	if ext := msg.GetExtendedTextMessage(); ext != nil {
		return ext.GetText()
	}
	if img := msg.GetImageMessage(); img != nil {
		return img.GetCaption()
	}
	if vid := msg.GetVideoMessage(); vid != nil {
		return vid.GetCaption()
	}
	if doc := msg.GetDocumentMessage(); doc != nil {
		return doc.GetCaption()
	}
	return ""
}

// Extract the link preview of a text message, if it has one
func extractLinkPreview(msg *waProto.Message) LinkPreview {
	ext := msg.GetExtendedTextMessage()
	if ext == nil || (ext.GetMatchedText() == "" && ext.GetTitle() == "") {
		return LinkPreview{}
	}
	return LinkPreview{
		URL:         cleanText(ext.GetMatchedText(), 500),
		Title:       cleanText(ext.GetTitle(), 200),
		Description: cleanText(ext.GetDescription(), 300),
	}
}

// Extract media info from a message
func extractMediaInfo(msg *waProto.Message) MediaInfo {
	if msg == nil {
		return MediaInfo{}
	}
	stamp := time.Now().Format("20060102_150405")

	if img := msg.GetImageMessage(); img != nil {
		return MediaInfo{
			Type: "image", Filename: "image_" + stamp + extForMime(img.GetMimetype(), ".jpg"), Mimetype: img.GetMimetype(),
			URL: img.GetURL(), DirectPath: img.GetDirectPath(), MediaKey: img.GetMediaKey(), FileSHA256: img.GetFileSHA256(), FileEncSHA256: img.GetFileEncSHA256(),
			FileLength: img.GetFileLength(), Width: img.GetWidth(), Height: img.GetHeight(),
		}
	}
	if st := msg.GetStickerMessage(); st != nil {
		return MediaInfo{
			Type: "sticker", Filename: "sticker_" + stamp + ".webp", Mimetype: st.GetMimetype(),
			URL: st.GetURL(), DirectPath: st.GetDirectPath(), MediaKey: st.GetMediaKey(), FileSHA256: st.GetFileSHA256(), FileEncSHA256: st.GetFileEncSHA256(),
			FileLength: st.GetFileLength(), Width: st.GetWidth(), Height: st.GetHeight(),
		}
	}
	if vid := msg.GetVideoMessage(); vid != nil {
		return MediaInfo{
			Type: "video", Filename: "video_" + stamp + extForMime(vid.GetMimetype(), ".mp4"), Mimetype: vid.GetMimetype(),
			URL: vid.GetURL(), DirectPath: vid.GetDirectPath(), MediaKey: vid.GetMediaKey(), FileSHA256: vid.GetFileSHA256(), FileEncSHA256: vid.GetFileEncSHA256(),
			FileLength: vid.GetFileLength(), Seconds: vid.GetSeconds(), Width: vid.GetWidth(), Height: vid.GetHeight(),
		}
	}
	if aud := msg.GetAudioMessage(); aud != nil {
		return MediaInfo{
			Type: "audio", Filename: "audio_" + stamp + extForMime(aud.GetMimetype(), ".ogg"), Mimetype: aud.GetMimetype(),
			URL: aud.GetURL(), DirectPath: aud.GetDirectPath(), MediaKey: aud.GetMediaKey(), FileSHA256: aud.GetFileSHA256(), FileEncSHA256: aud.GetFileEncSHA256(),
			FileLength: aud.GetFileLength(), Seconds: aud.GetSeconds(), Voice: aud.GetPTT(),
		}
	}
	if doc := msg.GetDocumentMessage(); doc != nil {
		// The file name was chosen by the sender: it is cleaned here and sanitized again before use in a path
		filename := cleanText(doc.GetFileName(), 120)
		if filename == "" {
			filename = cleanText(doc.GetTitle(), 120)
		}
		if filename == "" {
			filename = "document_" + stamp + extForMime(doc.GetMimetype(), "")
		}
		return MediaInfo{
			Type: "document", Filename: filename, Mimetype: doc.GetMimetype(),
			URL: doc.GetURL(), DirectPath: doc.GetDirectPath(), MediaKey: doc.GetMediaKey(), FileSHA256: doc.GetFileSHA256(), FileEncSHA256: doc.GetFileEncSHA256(),
			FileLength: doc.GetFileLength(), Pages: doc.GetPageCount(),
		}
	}
	return MediaInfo{}
}

// File extension for the common WhatsApp media types, or fallback
func extForMime(mimetype, fallback string) string {
	base := strings.ToLower(strings.TrimSpace(strings.SplitN(mimetype, ";", 2)[0]))
	switch base {
	case "image/jpeg":
		return ".jpg"
	case "image/png":
		return ".png"
	case "image/webp":
		return ".webp"
	case "image/gif":
		return ".gif"
	case "video/mp4":
		return ".mp4"
	case "video/quicktime":
		return ".mov"
	case "audio/ogg":
		return ".ogg"
	case "audio/mpeg":
		return ".mp3"
	case "audio/mp4", "audio/aac", "audio/x-m4a":
		return ".m4a"
	case "application/pdf":
		return ".pdf"
	}
	return fallback
}

// Strip control characters and newlines and cap the length of a short untrusted string
func cleanText(s string, max int) string {
	s = strings.Map(func(r rune) rune {
		if unicode.IsSpace(r) {
			return ' '
		}
		if unicode.IsControl(r) || unicode.Is(unicode.Cf, r) && r != '‍' {
			return -1
		}
		return r
	}, s)
	s = strings.TrimSpace(strings.Join(strings.Fields(s), " "))
	if runes := []rune(s); len(runes) > max {
		s = string(runes[:max])
	}
	return s
}
