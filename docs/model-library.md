# Model library

The **Models** tab lists every GGUF model in your model directories and storage destinations, including split sets.

| Column | Meaning |
|---|---|
| Quant | Read from the GGUF header (`general.file_type`), or from the file name if the header doesn't say |
| Integrity | *Not hashed yet*, *Hashed*, *Matches your checksum*, *File changed since it was hashed*, *Checksum changed: file is corrupted or was replaced* (same size and date, different bytes), or *Split set is missing parts* |
| On this PC | Last successful run on this PC (backend, device, context, GPU layers). Recorded only after llama-server actually produced a token |
| Remembered | Runtime settings saved for this model. **Use with saved settings** selects the model and applies them |

**Hash all in background** reads each file once with SHA-256, shows progress and can be cancelled. Hashing never
modifies, moves or downloads anything. **Accept current file** clears the reference checksum after you replaced a
file on purpose.

"This PC" is a short hash of the CPU model, GPU models and RAM size. It contains no serial numbers or network
addresses. Everything is stored in `settings/library.json` (mode 0600) on your storage drive.

Debian edition: the same module is used by `victor-ctl model library|hash|remember|accept`. The Tk Models tab has
*Check integrity (hash)* and *Remember settings* buttons. Run history ("worked on this PC") is recorded by the Arch
edition only.
