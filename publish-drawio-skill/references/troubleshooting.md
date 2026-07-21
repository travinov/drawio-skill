# Troubleshooting — Common Mistakes

Read this when something looks wrong in the output (rendering, export, layout, edges) or when a CLI invocation fails. Most rows have a one-line fix.

| Mistake | Fix |
|---------|-----|
| Missing `id="0"` and `id="1"` root cells | Always include both at the top of `<root>` |
| Shapes not connected | `source` and `target` on edge must match existing shape `id` values |
| Self-closing edge `mxCell` (`<mxCell ... edge="1" />`) | Use the expanded form with `<mxGeometry relative="1" as="geometry" />` child — self-closing edges won't render |
| `--` inside XML comments | Illegal per XML spec — use single hyphens or rephrase |
| Special characters in `value` | Use XML entities: `&amp;` `&lt;` `&gt;` `&quot;` |
| Literal `\n` in label text | Use `&#xa;` for line breaks in `value` attributes |
| Overlapping shapes | Scale spacing with complexity (200–350px); leave routing corridors |
| Edges crossing through shapes | Add waypoints, distribute entry/exit points, or increase spacing |
| Arrowhead overlaps bend | Final edge segment before target must be ≥20px — increase spacing or add waypoints |
| Iteration loop never ends | After 5 rounds, suggest user open .drawio in draw.io desktop for fine-tuning |
| Isolated role calls global Jira/Bitbucket MCP and then reaches exit 53 | Reinstall a release whose role command contains `--allowed-mcp-server-names ""`. Run the bundled verifier; it must confirm the flag before a role starts. Do not resume a run that failed before its first checkpoint; start a fresh `/drawio:review` or `/drawio:improve`. |
| `Supervisor decision must retain the supervisor role` after a model-proven exit 0 | The older host contradicted its prompt by requiring Supervisor to list itself among downstream siblings. Install `1.23.0-corporate.10` or newer and start a fresh run; a run stopped before its first checkpoint is not resumable. |
| `Supervisor decision omitted mandatory initial roles: semantic_analyst` after a model-proven exit 0 | The older host incorrectly delegated mandatory lifecycle topology to one model response. Install `1.23.0-corporate.11` or newer and start a fresh run; the new host records model-declared and host-mandatory roles separately. |
| `isolated reviewer output evidence binding mismatch: receipt_sha256` with verified DeepSeek model | The older contract made Reviewer manually copy host-known hashes. Install `1.23.0-corporate.12` or newer; the host now derives final bindings from validated input and records any model-declared mismatch in `binding_proof`. |
| Bare `/drawio:trace` shows an older improve immediately after review | Older review runs had no `workflow.json`, so trace ignored them. Install `1.23.0-corporate.12` or newer; review is now a first-class trace workflow and the published explicit run UUID resolves to the same run. |
| `command not found: draw.io` on macOS | The command is usually `drawio` (no dot). Use `drawio --version`, not `draw.io --version`. The dot-name exists inside the `.app` bundle (`/Applications/draw.io.app/Contents/MacOS/draw.io`) and on Windows (`draw.io.exe`). In corporate environments, install draw.io Desktop from SberUserSoft first. |
| Export command not found on macOS | Try full path `/Applications/draw.io.app/Contents/MacOS/draw.io` |
| Corporate / Windows install path is non-standard | Set `DRAWIO_BIN` to the exact executable path, or create `~/.drawio-skill/config.json` / `%USERPROFILE%\.drawio-skill\config.json` with `{"drawio_bin": "C:\\Program Files\\draw.io\\draw.io.exe"}`. Try `%LOCALAPPDATA%\Programs\draw.io\draw.io.exe` for per-user installs. |
| Vision returns "Unable to resize image — dimensions exceed the 2576x2576px limit" | The preview PNG is too large for Claude's vision API. Re-export with `--width 2000` instead of `-s 2` (the flag is `--width`; there is no short `-w` — passing `-w 2000` silently breaks input-file parsing and drawio errors with "input file/directory not found"). For very tall-narrow diagrams that still overshoot, use `--height 2000` instead. |
| Linux: blank/error output headlessly | Prefix command with `xvfb-run -a` |
| Linux: `--no-sandbox` placed before input file (parsed as filename) | Move `--no-sandbox` to the very end of the command (drawio-desktop#249, #1056) |
| Linux: `Failed to get 'appData' path` / `Home directory not accessible` | `export HOME=/tmp` before invoking drawio (drawio-desktop#127) |
| Linux server: segfault / EGL / MESA `failed to load driver` errors | Add `--disable-gpu` (suppresses Chromium GL init when no GPU available) |
| PDF export fails | Ensure Chromium is available (draw.io bundles it on desktop) |
| Background color wrong in CLI export | Known CLI bug; add `--transparent` flag or set background via style |
| Vision returns 400 "Could not process image" on draft PNG | Re-export the preview without `-e` (issue #8). Root cause is a truncated IEND chunk in `-e` PNGs, not the `zTXt` chunk itself — but skipping `-e` for the preview is the simplest fix. |
| Final `-e` PNG won't open in image viewers / vision APIs | Run `python3 <this-skill-dir>/scripts/repair_png.py <path>`. draw.io CLI emits `-e` PNGs with an 8-byte truncation at IEND. SVG/PDF unaffected. |
| WSL2: `drawio` / `draw.io` not found | The CLI lives on the Windows side. Use the Windows desktop exe via `/mnt/c`: `"/mnt/c/Program Files/draw.io/draw.io.exe"` (or per-user `"/mnt/c/Users/<you>/AppData/Local/Programs/draw.io/draw.io.exe"`). |
| WSL2: opening an exported file fails with a `/mnt/c/...`-style path | `cmd.exe` can't resolve WSL paths — convert first: `cmd.exe /c start "" "$(wslpath -w diagram.drawio.png)"`. The empty `""` after `start` is the (required) window title. |
| Browser URL opens to a blank/empty diagram (Windows/WSL2) | `cmd.exe`'s `start` treats `&` as a separator and drops everything after `#` — so the `#R…`/`#create=…` fragment (the whole diagram) is lost. Never pass the URL straight to `start`. Write a `.url` shortcut file and open *that* (see "WSL2 / Windows" below). |

## WSL2 / Windows specifics

**Locate the CLI.** Detect WSL2 with `grep -qi microsoft /proc/version`. On WSL2 the
export CLI is the Windows desktop exe, reached through `/mnt/c` (quote the path —
it contains a space):

```bash
"/mnt/c/Program Files/draw.io/draw.io.exe" --version
# per-user install fallback:
"/mnt/c/Users/$USER/AppData/Local/Programs/draw.io/draw.io.exe" --version
```

**Open a file.** Convert the WSL path to a Windows path first; `cmd.exe` cannot
follow `/mnt/c/...`:

```bash
cmd.exe /c start "" "$(wslpath -w diagram.drawio.png)"
```

**Open a browser-fallback URL.** `cmd.exe /c start` strips the URL fragment
(`&` ends the command, `#…` is dropped) — and the fragment carries the entire
diagram. Write a `.url` shortcut and open it instead, so the URL survives intact:

```bash
URL=$(python3 <this-skill-dir>/scripts/encode_drawio_url.py --edit diagram.drawio)
TMP=$(mktemp --suffix=.url)
printf '[InternetShortcut]\r\nURL=%s\r\n' "$URL" > "$TMP"
cmd.exe /c start "" "$(wslpath -w "$TMP")"
```

On native Windows the same `.url`-file trick applies (`start "" "%TEMP%\d.url"`).
On macOS/Linux just `open "$URL"` / `xdg-open "$URL"` — no workaround needed.
