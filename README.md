# WhatsApp MCP (hardened)

**Read and search your WhatsApp messages from Claude or Codex, locally, with sending off by default.**

A [Model Context Protocol](https://modelcontextprotocol.io) server that links your personal WhatsApp account as a companion device (like WhatsApp Web) and exposes your chats to an MCP client such as Claude Code, Claude Desktop, OpenAI Codex or Cursor. Messages are stored in a local SQLite database and only reach the model when a tool call reads them.

This is a security-focused fork of [lharries/whatsapp-mcp](https://github.com/lharries/whatsapp-mcp), which is no longer maintained. It keeps the original design (Go bridge + Python MCP server) and fixes its main weaknesses. See [What changed from upstream](#what-changed-from-upstream).

![WhatsApp MCP in use](./example-use.png)

## Read this first

> [!WARNING]
> **This is an unofficial client.** It talks to WhatsApp through the multi-device protocol using [whatsmeow](https://github.com/tulir/whatsmeow). It is not affiliated with or endorsed by WhatsApp or Meta, and using unofficial clients can break WhatsApp's Terms of Service. **Your account can be restricted or banned.** Use it at your own risk, and preferably not on an account you cannot afford to lose.

> [!WARNING]
> **Sending is disabled by default, and you should think twice before enabling it.**
> Read access cannot change anything on your account. Write access lets an AI agent send messages *as you*, to real people. Anyone who can put text in front of the model (a message in a chat, a group, a forwarded file) can attempt a prompt injection that makes it send something you never intended. That is the [lethal trifecta](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/): private data, untrusted content, and an outbound channel. Sending is off unless you set `WHATSAPP_ALLOW_SEND=1`, and even then it is rate limited and restricted (see [Sending messages](#sending-messages-opt-in)).

> [!NOTE]
> Everything in your chats is **untrusted input** to the model. Even in read-only mode, a malicious message can try to steer the model, so review what an agent does with the data you give it.

## Features

- Find chats by name (accents and case ignored), phone number or JID; see what's new and which chats wait for your reply
- Read a conversation as a compact transcript, with time ranges like `today`, `3d` or `2026-11-20`
- Fast full-text search over messages, file names, and the text of documents and voice notes already opened
- Let the model see photos, read documents and listen to voice notes, one attachment at a time and only when it needs to (see [Media](#media-photos-voice-notes-video-documents))
- Documents and voice notes are downloaded as they arrive, before WhatsApp deletes them; older files are requested again from the sender's phone
- Save any attachment to `~/Downloads/WhatsApp`
- Everything stored locally; no third-party server in the path
- Optional, rate-limited sending of text, files and voice messages
- The bridge starts on demand and stops when your session ends

## Quick start

You need [Go](https://go.dev/dl/) (the version in `whatsapp-bridge/go.mod`), [uv](https://docs.astral.sh/uv/) and Python 3.11+. Optional: [FFmpeg](https://ffmpeg.org) (images, video, voice notes), [poppler](https://poppler.freedesktop.org) (PDFs) and [whisper.cpp](https://github.com/ggml-org/whisper.cpp) (voice note transcripts), see [Media](#media-photos-voice-notes-video-documents). Tested on macOS; Linux should work; Windows is untested.

> [!IMPORTANT]
> **Without whisper.cpp and a model, voice messages cannot be transcribed.** The model will only see that a voice note exists (sender, length), never what it says. Images and documents work without it. To enable transcripts, install whisper.cpp and then run `uv run python setup_media.py` in `whatsapp-mcp-server/`: it picks the right model for your computer and downloads it (`brew install whisper-cpp` alone is not enough). See [Media → Setup](#media-photos-voice-notes-video-documents). If something is missing, the server says so in its log at startup and the model tells you when you ask about a voice note.

**1. Clone and link your phone (once)**

```bash
git clone https://github.com/namecoder1/whatsapp-mcp-hardened.git
cd whatsapp-mcp-hardened/whatsapp-bridge
go run .
```

Scan the QR code from WhatsApp on your phone: *Settings → Linked devices → Link a device*. Wait until the initial history sync finishes, then stop the bridge with `Ctrl+C`. This is the only step that needs a terminal.

Optionally build the bridge binary now, so the first tool call does not have to compile it (the MCP server rebuilds it by itself when the sources change):

```bash
go build -o bin/whatsapp-bridge .
```

**2. Add the MCP server**

Run these from `whatsapp-bridge/`, or replace `$PWD/..` with the absolute path of the repository.

Claude Code:

```bash
claude mcp add whatsapp --scope user -- "$(which uv)" --directory "$PWD/../whatsapp-mcp-server" run main.py
```

OpenAI Codex (CLI, IDE extension and app share `~/.codex/config.toml`):

```bash
codex mcp add whatsapp -- "$(which uv)" --directory "$PWD/../whatsapp-mcp-server" run main.py
```

Or edit `~/.codex/config.toml` directly. The longer timeouts leave room for the first start, when `uv` installs the dependencies and the bridge may need to be built and connected:

```toml
[mcp_servers.whatsapp]
command = "/path/to/uv"
args = ["--directory", "/path/to/whatsapp-mcp-hardened/whatsapp-mcp-server", "run", "main.py"]
startup_timeout_sec = 60
tool_timeout_sec = 120
```

Check it with `codex mcp list`, or `/mcp` inside a Codex session.

Claude Desktop or Cursor (`claude_desktop_config.json` / `~/.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "whatsapp": {
      "command": "/path/to/uv",
      "args": ["--directory", "/path/to/whatsapp-mcp-hardened/whatsapp-mcp-server", "run", "main.py"]
    }
  }
}
```

Use absolute paths everywhere (`which uv` prints the one for `uv`): MCP clients do not start servers from your shell, so `~` and your shell `PATH` are not available.

**3. Restart your client.** The first WhatsApp tool call starts the bridge automatically. It stops again after 15 minutes without requests, or when the session ends.

## Tools

Read-only tools (always available):

| Tool | What it does |
| --- | --- |
| `recent_activity` | What's new: chats with messages since a time (default 24h), and which ones wait for your reply |
| `list_chats` | List chats by last activity, or find one by name, phone number or JID |
| `read_chat` | One conversation as a compact transcript, with an optional time range |
| `search_messages` | Full-text search (case and accents ignored, prefix match, `"exact phrases"`), also in documents and voice notes already opened |
| `list_attachments` | Attachments by chat, kind, file name or date, and whether each can be opened right away |
| `get_message_context` | Messages around a specific message |
| `inspect_media` | See an image, read a document, or get a transcript of a voice note or video, for one message |
| `download_media` | Save an attachment to `~/Downloads/WhatsApp` (`WHATSAPP_DOWNLOAD_DIR`) |

Wherever a tool takes a `chat`, you can pass a contact or group name (or part of it), a phone number or a JID. If several chats match, the tool lists them and asks for the JID. Times accept `today`, `yesterday`, `3d`, `12h`, `2w` or a date such as `2026-11-20`. Listings print one line per message, grouped by day:

```
Chat: Marco Rossi (393331234567@s.whatsapp.net, direct) — messages 1-3 of 3 counting back from the newest
── Wed 25 Nov 2026 ──
18:01 Marco Rossi: Ti mando la fattura di novembre
18:02 Marco Rossi: [document "Fattura_11.pdf" 3 pages application/pdf 117.2 KB · id 3EB0...]
18:05 Me: grazie!
```

Sending tools (only registered when `WHATSAPP_ALLOW_SEND=1`): `send_message`, `send_file`, `send_audio_message`.

## Media: photos, voice notes, video, documents

Message listings never include media content. Each attachment is shown as a one-line summary, followed by its caption:

```
[voice message 0:42 18.2 KB · id 3A2E...]
[document "contract.pdf" 14 pages application/pdf 1.2 MB · id ...] Here is the draft
[image 1280x960 168.7 KB · id ...] look at this
see this https://example.com [link preview: https://example.com — "Example Domain": ...]
```

The model reads the summary, the caption and the messages around it, and calls `inspect_media` only for the attachment it actually needs. Because it is a separate tool call, your MCP client asks for your permission first (unless you allowed the tool permanently), so in a chat full of photos nothing is opened that is not relevant to your question.

What `inspect_media` returns:

| Attachment | Result | `detail="high"` |
| --- | --- | --- |
| Image, sticker | The picture, shrunk to 768px | 1568px, to read small text |
| Voice message, audio | A transcript, made locally with whisper.cpp | (same) |
| Video | One contact sheet of 4 frames, plus a transcript of the audio | 9 larger frames |
| Document | The text of PDF, Word (`.docx`, `.doc`), PowerPoint (`.pptx`), Excel (`.xlsx`), OpenDocument (`.odt`, `.ods`, `.odp`), `.rtf` and plain text files, a few pages, slides or sheets per call (`pages="6-10"` to continue). Scanned PDFs are shown as page images. `.doc` and `.rtf` need macOS (`textutil`) | More text per call |

Everything runs on your computer: no file is sent to a transcription or OCR service. Transcripts and document text are cached in `store/media_cache.db`, so a second question about the same voice note costs nothing. Link previews are the ones WhatsApp attached to the message; links are never fetched by the server.

**Setup** (macOS with Homebrew; use your package manager elsewhere):

```bash
brew install ffmpeg poppler whisper-cpp

# Check what is installed, get the whisper model that fits this computer, download it after confirmation
cd whatsapp-mcp-server
uv run python setup_media.py
```

`setup_media.py` looks at your computer and recommends the most accurate model that still transcribes a voice note in a few seconds:

| Computer | Model | Size |
| --- | --- | --- |
| Apple Silicon Mac, 8 GB RAM or more | `turbo` (large-v3-turbo, q5_0) | 547 MB |
| Apple Silicon Mac, less than 8 GB | `small` (q5_1) | 181 MB |
| NVIDIA GPU, 16 GB RAM or more | `turbo-q8` (large-v3-turbo, q8_0) | 834 MB |
| NVIDIA GPU, less RAM | `turbo` | 547 MB |
| No GPU, 8+ cores and 16 GB RAM (Intel Mac, most Linux PCs) | `small` | 181 MB |
| Anything smaller | `base` (q5_1) | 57 MB |

Models are downloaded from the [whisper.cpp repository](https://huggingface.co/ggerganov/whisper.cpp) into `~/.cache/whisper-cpp/` and checked against pinned SHA-256 hashes. To choose yourself: `uv run python setup_media.py --model small` (`base`, `small`, `turbo`, `turbo-q8`); `--check` only reports. The server uses the recommended model if it is installed, otherwise the most accurate one it finds, or the file in `WHATSAPP_WHISPER_MODEL`. NVIDIA GPUs are only used if whisper.cpp was built with CUDA.

Each part is optional: without whisper.cpp, images and documents still work, and the server says what is missing, with the right command for your computer.

Captions, durations, sizes and link previews are recorded by the bridge for messages received from this version on. Older messages show only the media type and size.

### Expired media, automatic downloads and cleanup

WhatsApp keeps attachments on its servers for only about 2-3 weeks. To keep them readable:

- **Automatic download.** While the bridge runs, documents and voice notes up to 25 MB are downloaded as they arrive (and, at startup, those from the last 14 days). Nothing is opened or parsed until the model asks. Set `WHATSAPP_AUTO_DOWNLOAD` to choose the kinds (`document,voice,audio,image,video,sticker` or `none`). This works best with the bridge running 24/7 (`./service.sh install`).
- **Asking the phone again.** When you open an attachment that already expired, the bridge asks the sender's phone to upload it again, as the WhatsApp app does. That works if their phone is online and still has the file. Otherwise the tool says why it failed and suggests asking for the file again. At most 20 such requests per hour.
- **Cleanup.** `store/media/` is a cache. Files unused for 30 days are deleted (`WHATSAPP_MEDIA_RETENTION`), and the oldest ones go first when the folder grows past 500 MB (`WHATSAPP_MEDIA_MAX_TOTAL_MB`). Document text and transcripts stay in the cache, so a document you read once can still be read after its file is gone. The same file forwarded to several chats is stored once (hard links).
- **No compression.** Measured on real chats, PDFs shrink by about 15%, JPEG by 6%, and voice notes, videos and ZIPs not at all, while every reader would need a decompressed copy. Space is saved with the limits above instead.

## Sending messages (opt-in)

Sending is a separate, deliberate step. Enable it by adding the variable to your MCP config:

```bash
# Claude Code
claude mcp add whatsapp --scope user -e WHATSAPP_ALLOW_SEND=1 -- "$(which uv)" --directory /path/to/whatsapp-mcp-server run main.py

# Codex
codex mcp add whatsapp --env WHATSAPP_ALLOW_SEND=1 -- "$(which uv)" --directory /path/to/whatsapp-mcp-server run main.py
```

In `~/.codex/config.toml` the same setting is `env = { WHATSAPP_ALLOW_SEND = "1" }` under `[mcp_servers.whatsapp]`.

Even when enabled, the bridge enforces these limits regardless of what the model asks for:

- **At most 10 messages per hour, at least 30 seconds apart.** Counted in the database, so restarting does not reset them.
- **Only to chats where the other person has already written to you**, or to yourself. No first contact with strangers.
- **No groups**, unless you also set `WHATSAPP_ALLOW_GROUPS=1`.
- **Files only from an outbox folder** (`~/WhatsApp-outbox`), after resolving symlinks. The model cannot attach arbitrary files from your disk.
- **Automatic stop on restriction.** If WhatsApp reports a temporary ban or messaging restriction, the bridge writes `whatsapp-bridge/store/ACCOUNT_RESTRICTED`, exits, and refuses to start until you delete that file by hand.

These limits reduce the risk of a ban and of an injected message going out. They do not remove it.

## Configuration

All settings are environment variables. Set them in the `env` of your MCP config, with `-e` in `claude mcp add` or with `--env` in `codex mcp add`.

| Variable | Default | Effect |
| --- | --- | --- |
| `WHATSAPP_ALLOW_SEND` | off | Set to `1` to register the sending tools |
| `WHATSAPP_ALLOW_GROUPS` | off | Set to `1` to allow sending to groups |
| `WHATSAPP_SEND_PER_HOUR` | `10` | Maximum sends per hour |
| `WHATSAPP_SEND_MIN_INTERVAL` | `30s` | Minimum time between sends (`30s`, `2m`) |
| `WHATSAPP_OUTBOX_DIR` | `~/WhatsApp-outbox` | The only folder files can be sent from |
| `WHATSAPP_AUTOSTART` | on | Set to `0` to start and stop the bridge yourself |
| `WHATSAPP_IDLE_TIMEOUT` | `15m` | Stop the bridge after this long without requests (`0` disables) |
| `WHATSAPP_MEDIA_MAX_MB` | `100` | `inspect_media` refuses larger attachments |
| `WHATSAPP_AUTO_DOWNLOAD` | `document,voice` | Attachment kinds the bridge downloads as they arrive (`document`, `voice`, `audio`, `image`, `video`, `sticker`, or `none`) |
| `WHATSAPP_AUTO_DOWNLOAD_MAX_MB` | `25` | Larger attachments are never downloaded automatically |
| `WHATSAPP_MEDIA_RETENTION` | `30d` | Downloaded files unused for this long are deleted (`12h`, `30d`, `0` keeps them) |
| `WHATSAPP_MEDIA_MAX_TOTAL_MB` | `500` | Least recently used files are deleted when `store/media/` grows past this (`0`: no limit) |
| `WHATSAPP_DOWNLOAD_DIR` | `~/Downloads/WhatsApp` | Where `download_media` saves files |

The auto-download and cleanup settings belong to the bridge. When the MCP server starts the bridge, it passes them on. The always-on LaunchAgent (`service.sh`) does not see your MCP config: put them in `whatsapp-bridge/store/bridge.env`, one `NAME=value` per line, then run `./service.sh restart`.
| `WHATSAPP_WHISPER_MODEL` | auto | Path of the whisper.cpp model (`ggml-*.bin`) used for transcripts |
| `WHATSAPP_WHISPER_LANGUAGE` | `auto` | Language of voice notes (`it`, `en`...), if auto-detection gets it wrong |
| `WHATSAPP_TRANSCRIBE_MAX_MINUTES` | `10` | Only the first part of longer recordings is transcribed |

## What changed from upstream

The original project exposed a local HTTP API with no authentication and read any file path it was given. This fork fixes that:

- **Unix socket instead of a TCP port.** The bridge API listens on `store/bridge.sock` (mode `0600`). A TCP port can be taken over by another local program while the bridge is off.
- **Token authentication on every request**, with a constant-time compare, JSON-only bodies, and rejection of browser requests (`Origin` / `Sec-Fetch-Site`), so a web page cannot drive the bridge.
- **Sending is opt-in** and limited as described above, instead of always available.
- **Outbox-only file access**, with symlink resolution, on both the bridge and the MCP server.
- **Untrusted names are sanitized.** File names, chat IDs and contact names from WhatsApp are cleaned before use in paths or display. Names chosen by the other person are marked with a `~` so they are not mistaken for saved contacts. SQL is parameterized.
- **Private storage.** Everything in `store/` is created with user-only permissions and is git-ignored.
- **On-demand bridge** with a lock that stops two bridges from sharing one session.
- **Media is processed locally and treated as hostile.** Parsers (ffmpeg, poppler, whisper.cpp) run as separate processes with timeouts and no network access, sizes are capped, and every result is labelled as untrusted content for the model.
- **Media downloads work again.** Upstream stripped the signed query string from media paths, so downloads failed with HTTP 403.
- **Logs go to stderr** (stdout is the MCP channel) and `whatsmeow` is updated to a current version.
- **No message text in logs.** `bridge.log` records events, never message content or contact names.

The invariants these rely on are written down in [AGENTS.md](./AGENTS.md).

## Privacy and data

- Messages, the session, downloaded media, the media cache (transcripts and document text), the search index (`search.db`) and the API token live in `whatsapp-bridge/store/`. Treat that folder like a password: whoever can read it can read your chats and use your linked session.
- Nothing is uploaded anywhere by this project. Transcription, document reading and image resizing run on your computer. Data reaches a model only when a tool returns it, and then it is subject to your MCP client's and model provider's policies.
- To remove everything: unlink the device in WhatsApp (*Settings → Linked devices*) and delete `whatsapp-bridge/store/`.

## Troubleshooting

- **A tool returns nothing.** Check `whatsapp-bridge/store/bridge.log`. If the bridge says it is not paired, run `go run .` in `whatsapp-bridge/` and scan the QR code again.
- **Sessions expire.** Linked devices are logged out after roughly two weeks if your phone stays offline. Pair again.
- **"Account restricted" / the bridge will not start.** WhatsApp restricted the account. Read the reason in `bridge.log`, wait it out, then delete `store/ACCOUNT_RESTRICTED` yourself.
- **Codex: the server times out at startup or on the first call.** Raise `startup_timeout_sec` and `tool_timeout_sec` in `~/.codex/config.toml` (see [Quick start](#quick-start)), or build the bridge ahead of time with `go build -o bin/whatsapp-bridge .` in `whatsapp-bridge/`.
- **An old attachment cannot be opened.** WhatsApp deleted it from its servers and the sender's phone did not upload it again (offline, or the file is gone from their phone). The error says which; try later, or ask for the file again. See [Expired media](#expired-media-automatic-downloads-and-cleanup).
- **Wrong language in transcripts.** Set `WHATSAPP_WHISPER_LANGUAGE` (for example `it`), or use a larger model.
- **No messages after pairing.** The first history sync can take several minutes on large accounts.
- **Out of sync.** Stop the bridge, delete `store/messages.db` and `store/whatsapp.db`, and pair again.
- **Device limit reached.** Remove a linked device on your phone first.

## Development

```bash
# Bridge
cd whatsapp-bridge && go vet ./... && go test ./...

# MCP server: list the tools it exposes
cd whatsapp-mcp-server && uv run python -c "import main, asyncio; print([t.name for t in asyncio.run(main.mcp.list_tools())])"
```

Contributions are welcome. Please read [CONTRIBUTING.md](./CONTRIBUTING.md) first, and report vulnerabilities privately as described in [SECURITY.md](./SECURITY.md).

## License and credits

MIT. Original work © Luke Harries; changes in this fork © their authors. See [LICENSE](./LICENSE). Built on [whatsmeow](https://github.com/tulir/whatsmeow) and the [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk).

WhatsApp is a trademark of Meta Platforms, Inc. This project is not affiliated with it.
