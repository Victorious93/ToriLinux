# Ask your documents (offline)

The **Documents** tab in the Control Center (and `victor-ctl docs` on the Debian edition) answers questions from files you choose, using only this PC.

## What it reads

- Only folders or files you add. `/`, system folders (`/etc`, `/usr`, `/proc`, ...), VictorLinux secrets, settings and TLS keys, the Control Center session token, `~/.ssh` and `~/.gnupg` are refused. If you add a parent folder, those private folders inside it are skipped.
- Hidden folders and symlinks are skipped. Files over 20 MiB are skipped.
- Supported: text, Markdown, code, CSV/JSON/YAML, HTML. PDF only when `pdftotext` (poppler) is installed.
- Removing a folder deletes it from the index. Your files are never changed.

## How it answers

1. **Search.** BM25 keyword ranking always works. When local embeddings are available, results also use meaning (cosine) similarity: 60 % meaning, 40 % keywords.
2. **Answer.** If a model is running, it writes an answer from the top passages and cites them as `[1]`, `[2]`. If no model is running, you get the matching passages only.

Embeddings come from llama-server on `127.0.0.1`:

- **Arch edition:** choose an embedding GGUF under *Meaning search* and VictorLinux starts a second llama-server with `--embeddings` on `127.0.0.1:18082`. If you don't choose one, the chat server is used when it serves `/v1/embeddings`.
- **Debian edition:** uses the running chat server's embeddings if it serves them, otherwise keyword search only. The separate embedding server is Arch-only.

Model and embedding URLs must be loopback. The code refuses any other host. There is no cloud fallback, no telemetry and nothing is downloaded.

## Where the index lives

One SQLite file with 0600 permissions on your storage drive: `docs/index.sqlite` on Arch, `docindex/index.sqlite` on Debian. Indexing is incremental: unchanged files are skipped, changed files are re-read and deleted files are dropped. Switching the embedding model recomputes the vectors.

## CLI (Debian)

```
victor-ctl docs add ~/Documents
victor-ctl docs index
victor-ctl docs ask "What does my lease say about pets?"
victor-ctl docs search "invoice 2026" -k 3
victor-ctl docs list
victor-ctl docs remove ~/Documents
```
