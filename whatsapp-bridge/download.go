package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waMmsRetry"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
)

// Error codes returned by /api/download, so the MCP server can tell the user what happened and what to do
const (
	errNotFound     = "not_found"     // no such message
	errNotMedia     = "not_media"     // the message has no attachment
	errIncomplete   = "incomplete"    // WhatsApp did not include the download keys
	errExpired      = "expired"       // gone from WhatsApp's servers and could not be requested again
	errNotOnPhone   = "not_on_phone"  // gone from the servers and the sender's phone no longer has it
	errRetryTimeout = "retry_timeout" // asked the phone to upload it again, no answer in time
	errRetryLimited = "retry_limited" // too many re-upload requests in the last hour
	errFailed       = "failed"        // anything else
)

// DownloadError carries one of the codes above and a technical description
type DownloadError struct {
	Code string
	Err  error
}

func (e *DownloadError) Error() string { return e.Err.Error() }

func downloadErr(code, format string, args ...any) *DownloadError {
	return &DownloadError{Code: code, Err: fmt.Errorf(format, args...)}
}

// How long to wait for the phone to upload expired media again, and how often we may ask. The phone app
// asks the same way when you open an old attachment; the limit keeps us from looking like a bot.
const (
	mediaRetryWait    = 30 * time.Second
	mediaRetryPerHour = 20
)

// Everything stored about one attachment that is needed to download it
type mediaRecord struct {
	MediaType, Filename, URL, DirectPath, Sender, SenderJID string
	IsFromMe                                                bool
	MediaKey, FileSHA256, FileEncSHA256                     []byte
	FileLength                                              uint64
}

func (store *MessageStore) mediaRecord(id, chatJID string) (mediaRecord, error) {
	var r mediaRecord
	var mediaType, filename, url, directPath, sender, senderJID sql.NullString
	var fileLength sql.NullInt64
	var isFromMe sql.NullBool
	err := store.db.QueryRow(`SELECT media_type, filename, url, direct_path, sender, sender_jid, is_from_me,
		media_key, file_sha256, file_enc_sha256, file_length FROM messages WHERE id = ? AND chat_jid = ?`, id, chatJID,
	).Scan(&mediaType, &filename, &url, &directPath, &sender, &senderJID, &isFromMe, &r.MediaKey, &r.FileSHA256, &r.FileEncSHA256, &fileLength)
	r.MediaType, r.Filename, r.URL, r.DirectPath = mediaType.String, filename.String, url.String, directPath.String
	r.Sender, r.SenderJID, r.IsFromMe = sender.String, senderJID.String, isFromMe.Bool
	if fileLength.Int64 > 0 {
		r.FileLength = uint64(fileLength.Int64)
	}
	return r, err
}

// MediaDownloader implements the whatsmeow.DownloadableMessage interface
type MediaDownloader struct {
	URL           string
	DirectPath    string
	MediaKey      []byte
	FileLength    uint64
	FileSHA256    []byte
	FileEncSHA256 []byte
	MediaType     whatsmeow.MediaType
}

func (d *MediaDownloader) GetDirectPath() string             { return d.DirectPath }
func (d *MediaDownloader) GetURL() string                    { return d.URL }
func (d *MediaDownloader) GetMediaKey() []byte               { return d.MediaKey }
func (d *MediaDownloader) GetFileLength() uint64             { return d.FileLength }
func (d *MediaDownloader) GetFileSHA256() []byte             { return d.FileSHA256 }
func (d *MediaDownloader) GetFileEncSHA256() []byte          { return d.FileEncSHA256 }
func (d *MediaDownloader) GetMediaType() whatsmeow.MediaType { return d.MediaType }

// MediaFetcher downloads attachments on request and, in the background, as they arrive
type MediaFetcher struct {
	client *whatsmeow.Client
	store  *MessageStore
	policy MediaPolicy
	queue  chan [2]string // message ID, chat JID

	mu       sync.Mutex
	waiting  map[types.MessageID]chan *events.MediaRetry
	retries  []time.Time
	inflight map[string]*sync.Mutex // one download at a time per file
}

func NewMediaFetcher(client *whatsmeow.Client, store *MessageStore, policy MediaPolicy) *MediaFetcher {
	return &MediaFetcher{
		client: client, store: store, policy: policy,
		queue:    make(chan [2]string, 500),
		waiting:  map[types.MessageID]chan *events.MediaRetry{},
		inflight: map[string]*sync.Mutex{},
	}
}

// Download the media of a message (or reuse the local copy) and return its absolute path. With allowRetry,
// media that WhatsApp already deleted from its servers is requested again from the phone that sent it.
func (f *MediaFetcher) Fetch(messageID, chatJID string, allowRetry bool) (string, *DownloadError) {
	rec, err := f.store.mediaRecord(messageID, chatJID)
	if errors.Is(err, sql.ErrNoRows) {
		return "", downloadErr(errNotFound, "message %s not found in chat %s", messageID, chatJID)
	} else if err != nil {
		return "", downloadErr(errFailed, "failed to read the message: %v", err)
	}
	if rec.MediaType == "" {
		return "", downloadErr(errNotMedia, "message %s has no media", messageID)
	}

	localPath := localMediaPath(chatJID, messageID, rec.Filename, rec.MediaType)
	absPath, err := filepath.Abs(localPath)
	if err != nil {
		return "", downloadErr(errFailed, "failed to get absolute path: %v", err)
	}

	// The HTTP handler and the auto-download worker may ask for the same file at once
	lock := f.lockFor(localPath)
	lock.Lock()
	defer lock.Unlock()

	if _, err := os.Stat(localPath); err == nil {
		touchMedia(localPath)
		return absPath, nil
	}
	if err := os.MkdirAll(filepath.Dir(localPath), 0700); err != nil {
		return "", downloadErr(errFailed, "failed to create chat directory: %v", err)
	}

	// The same file forwarded to several chats is stored once, as hard links
	if f.linkDuplicate(rec, messageID, chatJID, localPath) {
		touchMedia(localPath)
		return absPath, nil
	}

	if (rec.URL == "" && rec.DirectPath == "") || len(rec.MediaKey) == 0 || len(rec.FileSHA256) == 0 ||
		len(rec.FileEncSHA256) == 0 || rec.FileLength == 0 {
		return "", downloadErr(errIncomplete, "WhatsApp did not include the information needed to download this file")
	}

	// Messages stored by older versions only have the URL
	directPath := rec.DirectPath
	if directPath == "" {
		directPath = extractDirectPathFromURL(rec.URL)
	}
	// The path comes from the sender's message and is appended to a WhatsApp media host
	if !validDirectPath(directPath) {
		return "", downloadErr(errFailed, "invalid media path in message")
	}

	var waMediaType whatsmeow.MediaType
	switch rec.MediaType {
	case "image", "sticker":
		waMediaType = whatsmeow.MediaImage
	case "video":
		waMediaType = whatsmeow.MediaVideo
	case "audio":
		waMediaType = whatsmeow.MediaAudio
	case "document":
		waMediaType = whatsmeow.MediaDocument
	default:
		return "", downloadErr(errFailed, "unsupported media type: %s", rec.MediaType)
	}

	downloader := &MediaDownloader{
		URL: rec.URL, DirectPath: directPath, MediaKey: rec.MediaKey, FileLength: rec.FileLength,
		FileSHA256: rec.FileSHA256, FileEncSHA256: rec.FileEncSHA256, MediaType: waMediaType,
	}

	ctx := context.Background()
	data, err := f.client.Download(ctx, downloader)
	if mediaGone(err) {
		if !allowRetry {
			return "", downloadErr(errExpired, "the file is no longer on WhatsApp's servers (%v)", err)
		}
		fmt.Printf("Media for message %s expired on WhatsApp's servers, asking the phone to upload it again\n", messageID)
		newPath, derr := f.requestReupload(ctx, rec, messageID, chatJID)
		if derr != nil {
			return "", derr
		}
		// Keep the new path, so the next download works without asking again
		f.store.db.Exec("UPDATE messages SET direct_path = ? WHERE id = ? AND chat_jid = ?", newPath, messageID, chatJID)
		downloader.DirectPath = newPath
		data, err = f.client.Download(ctx, downloader)
	}
	if mediaGone(err) {
		return "", downloadErr(errExpired, "the file is no longer on WhatsApp's servers (%v)", err)
	} else if err != nil {
		return "", downloadErr(errFailed, "failed to download media: %v", err)
	}

	// Write to a temporary name first, so a crash never leaves a truncated file under the final name
	tmp := localPath + partialSuffix
	if err := os.WriteFile(tmp, data, 0600); err != nil {
		os.Remove(tmp)
		return "", downloadErr(errFailed, "failed to save media file: %v", err)
	}
	if err := os.Rename(tmp, localPath); err != nil {
		os.Remove(tmp)
		return "", downloadErr(errFailed, "failed to save media file: %v", err)
	}
	fmt.Printf("Downloaded %s media for message %s (%d bytes)\n", rec.MediaType, messageID, len(data))
	return absPath, nil
}

func (f *MediaFetcher) lockFor(path string) *sync.Mutex {
	f.mu.Lock()
	defer f.mu.Unlock()
	l, ok := f.inflight[path]
	if !ok {
		l = &sync.Mutex{}
		f.inflight[path] = l
	}
	return l
}

// WhatsApp answers 403, 404 or 410 once it has deleted the file from its media servers
func mediaGone(err error) bool {
	return err != nil && (errors.Is(err, whatsmeow.ErrMediaDownloadFailedWith403) ||
		errors.Is(err, whatsmeow.ErrMediaDownloadFailedWith404) || errors.Is(err, whatsmeow.ErrMediaDownloadFailedWith410))
}

// Hard-link an already downloaded copy of the same file (same SHA-256) from another message
func (f *MediaFetcher) linkDuplicate(rec mediaRecord, messageID, chatJID, localPath string) bool {
	if len(rec.FileSHA256) == 0 {
		return false
	}
	rows, err := f.store.db.Query(`SELECT id, chat_jid, filename, media_type FROM messages
		WHERE file_sha256 = ? AND NOT (id = ? AND chat_jid = ?) LIMIT 20`, rec.FileSHA256, messageID, chatJID)
	if err != nil {
		return false
	}
	defer rows.Close()
	for rows.Next() {
		var id, chat string
		var filename, mediaType sql.NullString
		if rows.Scan(&id, &chat, &filename, &mediaType) != nil {
			continue
		}
		other := localMediaPath(chat, id, filename.String, mediaType.String)
		if info, err := os.Lstat(other); err == nil && info.Mode().IsRegular() && os.Link(other, localPath) == nil {
			return true
		}
	}
	return false
}

// Ask the phone that sent the message to upload expired media again, and wait for the new path
func (f *MediaFetcher) requestReupload(ctx context.Context, rec mediaRecord, messageID, chatJID string) (string, *DownloadError) {
	chat, err := types.ParseJID(chatJID)
	if err != nil {
		return "", downloadErr(errExpired, "the file expired and the chat JID is invalid: %v", err)
	}
	info := &types.MessageInfo{ID: messageID, MessageSource: types.MessageSource{
		Chat: chat, IsFromMe: rec.IsFromMe, IsGroup: chat.Server == types.GroupServer,
	}}
	if info.IsGroup && !info.IsFromMe {
		if info.Sender = f.senderJID(ctx, rec); info.Sender.IsEmpty() {
			return "", downloadErr(errExpired, "the file expired and the sender of this group message is unknown, so it cannot be requested again")
		}
	}

	if !f.takeRetrySlot() {
		return "", downloadErr(errRetryLimited, "already asked for %d expired files in the last hour", mediaRetryPerHour)
	}

	ch := make(chan *events.MediaRetry, 1)
	f.mu.Lock()
	f.waiting[messageID] = ch
	f.mu.Unlock()
	defer func() {
		f.mu.Lock()
		delete(f.waiting, messageID)
		f.mu.Unlock()
	}()

	if err := f.client.SendMediaRetryReceipt(ctx, info, rec.MediaKey); err != nil {
		return "", downloadErr(errExpired, "the file expired and the request to upload it again failed: %v", err)
	}

	select {
	case evt := <-ch:
		notif, err := whatsmeow.DecryptMediaRetryNotification(evt, rec.MediaKey)
		if errors.Is(err, whatsmeow.ErrMediaNotAvailableOnPhone) {
			return "", downloadErr(errNotOnPhone, "the sender's phone no longer has this file")
		} else if err != nil {
			return "", downloadErr(errExpired, "the phone's answer could not be read: %v", err)
		}
		switch notif.GetResult() {
		case waMmsRetry.MediaRetryNotification_SUCCESS:
		case waMmsRetry.MediaRetryNotification_NOT_FOUND:
			return "", downloadErr(errNotOnPhone, "the sender's phone no longer has this file")
		default:
			return "", downloadErr(errExpired, "the phone could not upload the file again (%s)", notif.GetResult())
		}
		if p := notif.GetDirectPath(); validDirectPath(p) {
			return p, nil
		}
		return "", downloadErr(errFailed, "the phone answered with an invalid media path")
	case <-time.After(mediaRetryWait):
		return "", downloadErr(errRetryTimeout, "the phone did not answer within %s", mediaRetryWait)
	}
}

// Full JID of a group message's sender. Older rows only stored the user part: it is a LID if WhatsApp
// knows a phone number for it, otherwise a phone number.
func (f *MediaFetcher) senderJID(ctx context.Context, rec mediaRecord) types.JID {
	for _, s := range []string{rec.SenderJID, rec.Sender} {
		if strings.Contains(s, "@") {
			if jid, err := types.ParseJID(s); err == nil {
				return jid.ToNonAD()
			}
		}
	}
	if rec.Sender == "" {
		return types.EmptyJID
	}
	lid := types.NewJID(rec.Sender, types.HiddenUserServer)
	if pn, err := f.client.Store.LIDs.GetPNForLID(ctx, lid); err == nil && !pn.IsEmpty() {
		return lid
	}
	return types.NewJID(rec.Sender, types.DefaultUserServer)
}

func (f *MediaFetcher) takeRetrySlot() bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	cutoff := time.Now().Add(-time.Hour)
	kept := f.retries[:0]
	for _, t := range f.retries {
		if t.After(cutoff) {
			kept = append(kept, t)
		}
	}
	f.retries = kept
	if len(f.retries) >= mediaRetryPerHour {
		return false
	}
	f.retries = append(f.retries, time.Now())
	return true
}

// Hand a media retry answer to the download waiting for it
func (f *MediaFetcher) HandleRetry(evt *events.MediaRetry) {
	f.mu.Lock()
	ch := f.waiting[evt.MessageID]
	f.mu.Unlock()
	if ch != nil {
		select {
		case ch <- evt:
		default:
		}
	}
}

// Queue a new message's attachment for download if the policy wants it
func (f *MediaFetcher) MaybeQueue(messageID, chatJID string, media MediaInfo) {
	if f.policy.wantsAuto(media) {
		f.enqueue(messageID, chatJID)
	}
}

func (f *MediaFetcher) enqueue(messageID, chatJID string) {
	select {
	case f.queue <- [2]string{messageID, chatJID}:
	default:
		fmt.Printf("Auto-download queue is full, skipping message %s\n", messageID)
	}
}

// Download queued attachments one at a time. Never asks phones to upload again: that is only done when
// the user asks for a file.
func (f *MediaFetcher) Run() {
	for job := range f.queue {
		if _, err := f.Fetch(job[0], job[1], false); err != nil {
			fmt.Printf("Auto-download of message %s failed (%s): %v\n", job[0], err.Code, err)
		}
		time.Sleep(500 * time.Millisecond)
	}
}

// Catch up on attachments that arrived while the bridge was off and are still on WhatsApp's servers
func (f *MediaFetcher) QueueRecent(maxAge time.Duration) {
	rows, err := f.store.db.Query(`SELECT id, chat_jid, media_type, filename, media_voice, file_length FROM messages
		WHERE media_type IS NOT NULL AND media_type != '' AND timestamp > ? ORDER BY timestamp DESC LIMIT 300`,
		time.Now().Add(-maxAge))
	if err != nil {
		fmt.Printf("Failed to look for recent attachments: %v\n", err)
		return
	}
	var jobs [][2]string
	for rows.Next() {
		var id, chat string
		var mediaType, filename sql.NullString
		var voice sql.NullBool
		var size sql.NullInt64
		if rows.Scan(&id, &chat, &mediaType, &filename, &voice, &size) != nil {
			continue
		}
		media := MediaInfo{Type: mediaType.String, Voice: voice.Bool, FileLength: uint64(max(size.Int64, 0))}
		if !f.policy.wantsAuto(media) {
			continue
		}
		if _, err := os.Stat(localMediaPath(chat, id, filename.String, mediaType.String)); err == nil {
			continue
		}
		jobs = append(jobs, [2]string{id, chat})
	}
	rows.Close()
	for _, j := range jobs {
		f.enqueue(j[0], j[1])
	}
	if len(jobs) > 0 {
		fmt.Printf("Queued %d recent attachment(s) for download\n", len(jobs))
	}
}

// DownloadMediaRequest represents the request body for the download media API
type DownloadMediaRequest struct {
	MessageID string `json:"message_id"`
	ChatJID   string `json:"chat_jid"`
}

// DownloadMediaResponse represents the response for the download media API
type DownloadMediaResponse struct {
	Success   bool   `json:"success"`
	Message   string `json:"message"`
	ErrorCode string `json:"error_code,omitempty"`
	Path      string `json:"path,omitempty"`
}

// /api/download: download one attachment on request, asking the phone again if it expired
func downloadHandler(f *MediaFetcher) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var req DownloadMediaRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "Invalid request format", http.StatusBadRequest)
			return
		}
		if req.MessageID == "" || req.ChatJID == "" {
			http.Error(w, "Message ID and Chat JID are required", http.StatusBadRequest)
			return
		}

		w.Header().Set("Content-Type", "application/json")
		path, err := f.Fetch(req.MessageID, req.ChatJID, true)
		if err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(DownloadMediaResponse{Message: err.Error(), ErrorCode: err.Code})
			return
		}
		json.NewEncoder(w).Encode(DownloadMediaResponse{Success: true, Message: "Media downloaded", Path: path})
	}
}

// Extract the direct path from a WhatsApp media URL. The query string (ccb, oh, oe...) is part of the
// path: whatsmeow appends "&hash=..." to it, so dropping it makes the media server answer 403.
// Example URL: https://mmg.whatsapp.net/v/t62.7118-24/13812002_..._n.enc?ccb=11-4&oh=...
func extractDirectPathFromURL(url string) string {
	parts := strings.SplitN(url, ".net/", 2)
	if len(parts) < 2 {
		return url // Return original URL if parsing fails
	}
	return "/" + parts[1]
}

// A direct path must stay a path on the media host: no "//host", userinfo, backslashes or whitespace
func validDirectPath(p string) bool {
	return strings.HasPrefix(p, "/") && !strings.HasPrefix(p, "//") && !strings.ContainsAny(p, "@\\ \t\r\n")
}
