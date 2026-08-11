# Comic Book Art Creator — knowledge base

Everything learned building this app, written down so it does not have to
be rediscovered. `<project>` below means the folder holding
`ComicArtCreator.exe`.

Companion documents: `RAGMAP.md` (the RAG-map contract), `SECURITY.md`
(threat model), `TRAINING.md` (building a dataset), `CHANGELOG.md` (what
changed when).

---

## 1. Architecture

A Tkinter desktop app drives a **headless ComfyUI** engine over
REST + websocket on `127.0.0.1:8188`. The app spawns the engine itself;
users never see it.

```
<project>/
  ComicArtCreator.exe      the app (PyInstaller onefile)
  Setup.exe                first-run installer (runtime + models)
  app/                     source, presets.json, models_manifest.json, settings.json
  ComfyUI/                 the engine (+ custom_nodes/)
  python/ or venv/         engine runtime — python/ (embedded) wins if present
  models/                  checkpoints, loras, vae, ipadapter, clip_vision,
                           diffusion_models, text_encoders, upscale_models, rembg
  output/                  finished art; output/_raw is engine scratch
  extra_model_paths.yaml   rewritten at every engine start (gitignored)
```

Key invariants:

- **Frozen mode**: `PROJECT` = the exe's own folder; source mode: the
  parent of `app/`. Everything else is derived from `PROJECT`, so the
  folder is portable — move or unzip it anywhere.
- `extra_model_paths.yaml` is **rewritten on every engine start** with
  this machine's absolute paths. Never rely on a checked-in copy.
- `_contained_env()` keeps every download and cache inside the project
  (`U2NET_HOME`, `HF_HOME`, `PIP_NO_CACHE_DIR`). Nothing lands in the
  user profile.
- Zero prerequisites by design: Setup bootstraps an embedded Python. Any
  feature that would need git or a system Python does not belong in the
  app (an in-app LoRA trainer was built and then removed for exactly
  this reason).

---

## 2. Engine lifecycle — the most expensive lessons

**The engine outlives the app unless you kill it.** It keeps whatever
model it last loaded resident: ~7 GB after an SDXL job, up to ~17 GB
after a Flux one. `_on_close` must stop it (v1.13.1). Before that fix an
engine was found still running two days after its session ended.

**Matching the engine process.** It is launched with `cwd=ComfyUI` and a
bare `main.py` argument, so **its command line does not contain the word
"ComfyUI"**. A filter like `*ComfyUI*main.py*` matches nothing — that bug
sat unnoticed for months and silently broke every restart path. Match on
the project path instead, and use `.Contains()` rather than `-like` so a
bracket in the path cannot act as a wildcard:

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -and
                 $_.CommandLine.Contains('<project>') -and
                 $_.CommandLine.Contains('main.py') }
```

**ComfyUI runs as a parent + child python pair**; the *child* holds the
socket. A path-based kill gets both. Killing only the parent PID leaks
the child, which keeps the port and the VRAM.

**Ownership** (`engine_owner.json`, written by `_mark_engine_owned`):

- `engine_is_ours()` — true if the owner pid is us **or still alive**.
  Right for *trusting* an engine, wrong for *killing* one.
- `engine_ours_to_stop()` — true only if we started it, or the session
  that did has died (an orphan worth cleaning). This is what shutdown
  uses, so a second open window never loses its engine.

**Port 8188 squatting** is the classic failure. Symptoms: the node or
model you just installed is missing from `/object_info`, `engine.log`
shows `Port 8188 is already in use` and `Could not acquire lock on
database comfyui.db`, and the app appears to work but answers come from
a stale engine. Diagnose with `Get-NetTCPConnection -LocalPort 8188` and
read the owning process's `CommandLine` — never trust the API alone.

**VRAM readings**: trust `nvidia-smi`, not ComfyUI's `/system_stats`.
They disagree wildly (30.2 GB "free" while nvidia-smi showed 20.5/32.6 GB
used at 98% utilisation).

**Never run engine tests while another GPU job is running.** On Windows
an NVIDIA card does not OOM when it runs out — the driver silently spills
to system RAM and everything gets 10-20x slower while looking healthy.
Aim to stay under ~85% VRAM.

**Boot heal**: if the engine cannot see checkpoints that exist on disk,
it was started with the wrong paths — kill and restart it once
(`_engine_heal_tried` guards against a loop). In a healthy install the
disk and engine lists match exactly and this never fires.

**Custom nodes need an engine restart, and the manifest cannot install
them.** `models_manifest.json` delivers *files* only; anything requiring
a node in `custom_nodes/` needs an app-side installer (see
`_install_style_support` for the pattern: download zip → extract →
move → download models → `kill_engine()` → reboot engine).

---

## 3. Building and releasing

No spec file is checked in. These commands are the source of truth; both
paths **must be absolute** because `--version-file` and `--icon` resolve
relative to `--specpath` (getting this wrong fails at the EXE step, after
several minutes of work):

```powershell
# app -> 58 MB
venv\Scripts\python.exe -m PyInstaller --noconfirm --onefile --windowed `
  --name ComicArtCreator --icon "<abs>\app\icon.ico" `
  --version-file "<abs>\app\version_app.txt" --collect-all av `
  --distpath dist_app --workpath build_app --specpath build_app `
  "<abs>\app\comic_art_creator.py"

# setup -> 15 MB
venv\Scripts\python.exe -m PyInstaller --noconfirm --onefile --windowed `
  --name Setup --icon "<abs>\app\icon.ico" `
  --version-file "<abs>\app\version_setup.txt" `
  --distpath dist_setup --workpath build_setup --specpath build_setup `
  "<abs>\app\setup_installer.py"
```

- `--collect-all av` is required for video export (PyAV's avcodec/avformat
  DLLs must land in the frozen bundle).
- rembg is deliberately **not** bundled — it runs via the engine runtime
  as a subprocess, which keeps the exe small.

Release procedure:

1. Bump `APP_VERSION` in `app/comic_art_creator.py` **and** the two
   `app/version_*.txt` resources.
2. Add a `CHANGELOG.md` entry written for users, not for developers.
3. Build both exes, copy them to `<project>`, launch-test the exe (it
   must still be alive after ~10 s, and `VersionInfo.FileVersion` must
   read the new number).
4. Assemble `releases/vX.Y.Z/release/` = both exes + the docs +
   `app/{presets,models_manifest}.json`; `source/` = `app/*.py` + icon;
   zip the release folder (~72 MB).
5. `git commit -F <file>` — **never** `-m` with embedded quotes; Windows
   PowerShell 5.1 splits the argument and mangles the message.
6. `git push origin main` + push the tag, then
   `gh release create vX.Y.Z <zip> --title "..." --notes-file <file>`
   (again, `--notes-file`, not inline quotes).

Editing gotcha: **never** edit `.py` or version files with PowerShell
`-replace`/`Set-Content` — it mojibakes em-dashes and eats quotes. Use a
real editor or a Python script file.

---

## 4. Model families and graphs

`FAMILY_DEFAULTS` picks sampler settings from the checkpoint name:

| family | detection | settings |
|---|---|---|
| flux | "flux" in name | euler/simple, cfg 1, `FluxGuidance` 3.5, `EmptySD3LatentImage` |
| schnell | "schnell" | as flux, 4 steps (Apache-licensed, unrestricted output) |
| turbo | "turbo"/"lightning" | 8 steps, cfg 2 — ignores negatives |
| anime | animagine/illustrious/noob/pony | euler_ancestral |
| sdxl | everything else | standard |

`build_graph(p)` node numbering, in build order: `1` checkpoint → `20+`
LoRA chain → `2`/`3` text encode → `4` FluxGuidance → `31+/41+/50/51`
IP-Adapter → `5` latent (or `10`-`14` for img2img / masked border) → `6`
KSampler → `7` VAEDecode → `40/41` optional upscale → `8` SaveImage.
Editing prompts (`edit_image_names`) take a completely separate path
(`build_kontext_graph` / `build_qwen_edit_graph`).

Editors:

- **Flux Kontext** (`flux1-dev-kontext_fp8`, 11 GB) uses the ordinary
  `flux1-dev-fp8` checkpoint as a **CLIP and VAE donor**, which saves
  downloading T5 and the autoencoder separately. `ReferenceLatent` +
  `FluxGuidance` 2.5, cfg 1. Multi-image via `ImageStitch`.
- **Qwen Image Edit** needs `ImageScaleToTotalPixels` with
  `resolution_steps: 1` on ComfyUI 0.30+, and `CLIPLoader type
  qwen_image`.
- "Output at canvas size" swaps the reference latent for an
  `EmptySD3LatentImage` at the requested dimensions — this is what makes
  restaging into a different aspect ratio work.

**IP-Adapter is SDXL-only** and needs exact filenames
(`ip-adapter-plus_sdxl_vit-h.safetensors` +
`CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors`) for the
`IPAdapterUnifiedLoader` preset "PLUS (high strength)". Flux Redux is
gated and not used.

**Two ways references reach IP-Adapter (v1.16.0):**

- *Images* (default RAG map): retrieved image files are uploaded to the
  engine's `input/` via `/upload/image`, then `LoadImage` → `ImageBatch`
  → `IPAdapter` (node `51`), which runs clip_vision internally.
- *Precomputed embeds* (embeddings-only map): the map ships no viewable
  pictures — each entry is a `.ipadpt` file (a `torch.save`d tensor = the
  CLIP vision **penultimate hidden states**, `[1,257,1280]` fp16, the
  exact "PLUS" image embed IP-Adapter conditions on). The app copies the
  retrieved `.ipadpt` files into the engine's `input/` (a plain file copy
  — the main process has no torch to build them; they arrive ready-made
  from Laura-Trainer's builder, which does) and the graph wires the STOCK
  nodes `IPAdapterUnifiedLoader` (PLUS) → one `IPAdapterLoadEmbeds`
  (`torch.load`) per file → `IPAdapterCombineEmbeds` (`concat`, ≤5) →
  `IPAdapterEmbeds` (nodes `50`/`52…`/`58`/`59`). Same PLUS adapter as the
  image path, so the guidance is equivalent — the source pictures simply
  never existed as files here. `IPAdapterEmbeds` needs either a
  `clip_vision` or a `neg_embed`; the UnifiedLoader bundles clip_vision, so
  passing only `pos_embed` is valid. The two paths are mutually exclusive
  and selected by `ragmap["_embeds_only"]`.
  **GOTCHA (v1.16.1, caught only by a live engine run):** `IPAdapterEmbeds`
  is an ADVANCED node — its `weight_type` list is `WEIGHT_TYPES` (`linear`,
  `ease in/out`, `style transfer`, …) and does NOT contain `"standard"`, which
  is only valid on the basic `IPAdapter` node the image path uses. Passing
  `"standard"` makes the engine reject the whole graph with HTTP 400
  `value_not_in_list` and no picture is produced — use `"linear"` (the
  advanced-node equivalent of standard uniform weighting). Static/offline
  checks pass this; only a real POST to a running engine catches the enum
  mismatch, so validate any new node-input value against a live engine, not
  just the node's Python source.

---

## 5. Subsystems

**Borders** took five iterations; the final recipe is: trained LoRA
(`SDXL_BorderFrames_v1`, trigger `cbacframe`, on Juggernaut-XL) + full
frame generation (no latent noise mask — masking locks the model into a
flat edge band) + a **content-aware centre cut** that flood-fills from
the centre using the frame's own colour, giving an organic inner
silhouette + a **floating margin** (the frame is downscaled onto a
transparent canvas, ~6%) + an automatic **second Kontext pass** that
empties the centre when the model drew a character there. Reference
bezels measured 100% transparent outer margin and 0.52-0.77 "rectness",
which is what those two post-processing steps reproduce.

**Animator** — Wan 2.2 ti2v 5B (24 fps native, 49 frames in ~30 s on a
5090). Findings that cost real time:

- Black backgrounds and hard cutouts freeze or mush *any* video model.
  Always pre-composite onto a neutral grey (200,200,205) before
  animating; the app does this automatically.
- First-last-frame conditioning (Wan 2.1 FLF) cannot do locomotion — a
  prepped walk scored 0.95 motion versus 22 for in-place actions. It is
  excellent for capes, hair, idle loops.
- Seamless loops come from generating freely and then cutting the best
  cycle: pairwise frame diffs on 64px greyscale, requiring the segment's
  internal motion to be ≥60% of the clip average, otherwise the "best
  loop" is just the quietest stretch of a dead clip.
- Measure motion on **cut** frames; raw frames include background
  shimmer.
- GIF transparency needs palette index 255 plus disposal 2.

**Transparency (cutout)** — rembg `isnet-general-use`. Defringing must
measure the background colour from the **raw** frame corners *before*
cutting; sampling a cut frame reads rembg's zeroed-black transparent
pixels and makes the outline worse. Erode 1px, feather 0.5, alpha floor
30.

**RAG maps** — see `RAGMAP.md`. Retrieval is literal word overlap
against keywords + caption, so keywords must use the words a person
would actually type. Optional CLIP embeddings (`embeddings.safetensors`,
pooled + L2-normalised) are used only to drop near-duplicate references
(cosine > 0.97) — NOT for conditioning. Every field is optional except
`entries`, and each missing piece costs only its own feature.

`load_ragmap` reads two conditioning layouts, distinguished by
`_embeds_only`:

- *with-images*: entries carry `image`; `_path` resolves to a real file
  (a bare folder must NOT count — check `is_file()`, or retrieval tries to
  feed a directory).
- *embeddings-only* (`mode: "embeddings-only"`, or embeds present and no
  images): entries carry `embed` (a `.ipadpt` under `embeds_dir`), resolved
  to `_ipadpt`; `_path` stays empty. `ragmap_retrieve` treats an entry as a
  usable reference when it has EITHER `_path` OR `_ipadpt`, so retrieval,
  dedup, LoRA auto-apply and trigger injection are identical for both
  layouts. Generation then branches on `_embeds_only` (see §4). The dedup
  `embeddings.safetensors` still ships in embeddings-only maps, so
  near-duplicate skipping keeps working.

This is the privacy path: it lets a map guide generation from real
training references without ever shipping an openable copy of those
images. It pairs with Laura-Trainer's "Private references" build option.

**Prompt enhancer** — optional local Ollama. It must degrade to nothing
when Ollama is absent: `ollama_models()` returns `[]` on any failure and
the button explains itself once. Clean the reply: strip `<think>` blocks
(reasoning models), "Sure, here's…" lead-ins, quotes and bullets, and
reject anything shorter than half the original as a non-answer.

---

## 6. Tkinter and threading rules

- **`exportselection=False` on every Combobox, Listbox, Entry and
  Spinbox.** The default ties the displayed value to the X/primary
  selection, so clicking another widget blanks the first one. This was
  reported repeatedly as "my selection disappears".
- Style `TCombobox` for **readonly, focus and disabled** states, not just
  the default one — otherwise the selected value renders in a colour
  that vanishes against a dark field and the box looks empty until
  clicked.
- **Never read Tk variables from a worker thread.** It raises "main
  thread is not in main loop", the thread dies silently and the UI is
  left frozen in its previous state. Snapshot values on the main thread
  and pass plain data in; `root.after(...)` back is fine.
- `_poll_queue` wraps each message in its own try/except and reschedules
  in a `finally`. One exception in a handler used to kill the UI loop.
- Settings persist on a 700 ms debounce (`_schedule_persist`) with a
  baseline save at startup, so a force-kill still keeps recent edits.
- The left panel is a Canvas + inner frame; a global wheel router walks
  `winfo_containing` so scrolling works over child widgets.

---

## 7. Testing

- **Graph tests need no engine**: call `build_graph` and assert on node
  wiring. Fast, and they catch the majority of regressions.
- **Live validation** drives the real `Generator` against the engine from
  a headless script. Cover every path: plain generation, LoRA chain,
  upscale, RAG guidance, each model family, border, editor.
- **UI tests** must drive a real `mainloop()` with `after()`-scheduled
  steps. An `update()` polling loop makes cross-thread `after()` fail and
  produces false failures.
- **A UI test that exercises persistence overwrites the user's saved
  state.** Back up `app/settings.json` before, restore after. Learned the
  hard way.
- **Leak tests**: clear leftovers *first* (a previous run's app can still
  be alive and the single-instance mutex will silently make your new
  launch exit); aim `CloseMainWindow()` at the process whose
  `MainWindowHandle != 0`, because a PyInstaller onefile app is a
  windowless bootloader parent plus the real child; compare `nvidia-smi`
  against a baseline taken with nothing running.
- `models_audit_test.py` asserts every model constant referenced in code
  exists in the manifest and still resolves upstream. Run it whenever a
  model is added.

---

## 8. Dead ends — do not retry without new upstream

- **LayerDiffuse native transparency** (attempted v1.13.0, reverted).
  The `ComfyUI-layerdiffuse` node is stale against current ComfyUI: its
  `LayeredDiffusionDecodeRGBA` calls `JoinImageWithAlpha
  .join_image_with_alpha()`, a method that has since been renamed. That
  part is routable around with core nodes (`LayeredDiffusionDecode` →
  `InvertMask` → `JoinImageWithAlpha`; note ComfyUI's join **inverts the
  mask internally**, so the InvertMask is required, not a double
  negation). The injection does apply — the same seed differs across
  73.5% of pixels — but the transparent VAE decoder returns garbage: a
  uniform mask under `diffusers` 0.39, and a faint ghost rather than a
  silhouette under 0.31. Either polarity yields a fully opaque or almost
  fully erased image. Untried: Conv Injection (3.6 GB), which shares the
  same failing decoder.
- **In-app LoRA training** — worked, removed on purpose: it was the only
  feature needing git and a system Python. Build datasets in-app
  (`⭐ Add to training set`) and train externally.
- **Bezel composer** — superseded by the editor plus border references.
- **Masked (`SetLatentNoiseMask`) border generation** — produces a flat
  edge band with no inward complexity. Generate full-frame and cut.

---

## 9. Security posture

Summarised from `SECURITY.md`: safetensors only (`.ckpt`/`.pt` are
pickles and can execute code on load, so they are never offered or
accepted), HTTPS-only downloads, the CivitAI API key encrypted at rest
with DPAPI, downloaded filenames sanitised to a basename, and the engine
bound to loopback. The exe is unsigned, so SmartScreen shows
"More info → Run anyway" on other machines. Open items: SHA-256 pinning
for downloads, and code signing (needs a purchased certificate).

---

## 10. Open ideas

Native transparency by some other route than LayerDiffuse; a builder for
`.ragmap.json` from an existing captioned dataset; SHA-256 pinning;
code signing.
