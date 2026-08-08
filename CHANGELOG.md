# Changelog — Comic Book Art Creator

## v1.7.6 — 2026-08-07
- Loop menu finalized: Seamless auto-cut is the permanent default (fresh
  at every launch), with Ping-pong and Crossfade as per-run options. The
  in-place generated mode is removed from the menu — it froze
  walking-type actions. All remaining options are guarded against short
  clips and cannot break the pipeline.
- Validated: 4-second walks give the auto-cutter several stride cycles;
  loop seam measurably below general motion level (17.7 vs 22.7).
- Defringe made adaptive and more aggressive: the background color is
  sampled from each frame (the model repaints the stage, so it drifts),
  silhouettes erode 2 px, and faint halo pixels are dropped — removes
  the residual outline on transparent frames.

## v1.7.5 — 2026-08-07
- Clean edges on transparent animation frames: new defringe pass
  un-blends the known staging color out of semi-transparent edge pixels
  (removes the white/gray outline), then tightens and smooths the
  silhouette. Applied to every transparent animation frame and its GIF.

## v1.7.4 — 2026-08-07
- New default loop mode "Seamless motion (auto-cut)": the clip is
  generated with free, fluid motion, then automatically cut at its two
  most similar frames so it loops playing forward — walks and runs loop
  without ping-pong reversal. Longer durations (3–5 s) give tighter
  seams. "Seamless in-place (generated)" remains best for capes/idles.
- Auto-prep on every animation: the character is extracted from its
  background and staged on neutral gray before animating — fixes frozen
  or mushy motion from black backgrounds and hard cutouts.
- Seamless in-place quality: 30 steps / cfg 5.5.

## v1.7.3 — 2026-08-07
- Fixes near-static animations ("only the eyes blinked"): seamless mode
  anchors first and last frames to the same pose, and the model would
  take the laziest path. New **Motion** control (Subtle / Normal /
  Strong, default Strong) plus a rewritten motion-demanding prompt
  template. Measured ~10x more motion with the loop still closing
  cleanly.

## v1.7.2 — 2026-08-07
- Each section has its own progress bar + percent: the main bar serves
  only the main generation; the Animator and Border maker got dedicated
  bars under their generate buttons.
- Sections reordered: Animator above, Border maker at the very bottom.
- Bezel composer removed (superseded by the Image editor + border
  references); its code is deleted.

## v1.7.1 — 2026-08-07
- Progress everywhere, with percentages: Setup shows one overall bar +
  percent across the entire installation (runtime, packages, engine,
  every model byte); the app's generate bar shows percent and now also
  covers animations (which previously ran silently) and in-app model
  downloads.
- Clicking any Generate while a job runs now offers to cancel it —
  long animation jobs can no longer lock the app with no way out.
- "Make GIF" is now a checkbox in the Animator; generated GIFs land in
  the history, PLAY animated in the preview when selected, and Save As
  saves the .gif.
- New "Use selected" button in the Image editor: one click points the
  editor at the currently selected history image (label shows
  "selection"); greyed out while the history is empty.

## v1.7.0 — 2026-08-07
- True seamless loops: new "Seamless (generated loop)" mode — the
  animation is generated to start AND end on the character's exact pose
  (Wan 2.1 first-last-frame, ~18 GB, self-installs), so it loops playing
  forward. No ping-pong moonwalking, no crossfade. Now the default loop
  mode; ping-pong/crossfade remain as fast alternatives.
- Double-click a history thumbnail to load that image straight into the
  Animator's character slot.
- Model governance: a permanent audit test verifies every model file the
  app references is in the update manifest and resolves on HuggingFace —
  Setup always installs the latest of everything, and the startup check
  validates the full set (24 files, ~129 GB fully loaded).

## v1.6.0 — 2026-08-07
- ANIMATOR: animate a character image into sprite frames and a GIF
  (Wan 2.2, Apache 2.0, ~18 GB, self-installs). Pick a character (any
  image, or the current gallery selection), describe the action, choose
  duration (1–5 s), frames to keep (24/12/8/6 fps), loop mode
  (Ping-pong = perfect loop, Crossfade, or none), and canvas. Output:
  transparent PNG frames folder + looping GIF + optional zip — made for
  old-school animated sprites.
- Mouse wheel now scrolls the left panel from anywhere inside it.

## v1.5.3 — 2026-08-07
- Border maker defaults to widescreen 16:9 HD (1920x1080). Borders keep
  the exact-canvas-size logic from v1.5.2 (references condition the art,
  the selected aspect sets the output dimensions).

## v1.5.2 — 2026-08-07
- New "Output at canvas size" checkbox in the Image editor: the loaded
  image is used purely as reference while the output is generated at the
  canvas size selected in the settings — e.g. a portrait character can
  be re-staged into a widescreen scene. Off by default so in-place edits
  (like text removal) stay pixel-faithful to the original size.
- Borders from references now always output at the exact selected
  aspect/dimensions (no more resolution snapping).

## v1.5.1 — 2026-08-07
- Border maker simplified: the Style selector and the section's own
  Model/Variations controls are removed — borders now mimic the main
  generation settings (model, LoRAs, Variations, Steps, Seed, Editor).
  The section only sets border prompt, aspect, thickness and references.
- Full validation pass: main generation (3 model families), prompt-only
  masked borders, and border-from-reference via the editor all
  regression-tested.

## v1.5.0 — 2026-08-07
- Border references now use the image editor (same engines and logic as
  the Image Editor section): the reference is shaped to the border canvas
  and redrawn as the frame — style, characters and composition carry
  over. The old img2img seeding for border refs is removed, along with
  its influence slider. Prompt-only borders keep masked generation.

## v1.4.1 — 2026-08-07
- Setup now recalculates its numbers live from the model manifest: full
  model-pack size, how many GB are still to download on this machine, and
  the estimated total disk use when installation completes.
- Running the app before Setup now states plainly that it cannot create
  art until Setup completes the installation.

## v1.4.0 — 2026-08-07
- The reference feature is replaced by a true **image editor**
  (Gemini-style): load image(s) and the prompt is the instruction —
  change settings, remove words/titles, move characters to new scenes,
  increase details, combine images. Two engines: **Flux Kontext** (best
  overall) and **Qwen Image Edit** (best text removal, Apache 2.0),
  selectable in the editor row; models self-install on first use.
- Border-reference influence slider moved into the Border maker section.

## v1.3.4 — 2026-08-07
- "🗑 Delete image" button: permanently deletes the selected image from
  disk and the history strip (with confirm) — generate variations, keep
  only the one you want.

## v1.3.3 — 2026-08-07
- GPU compatibility check: the app detects your NVIDIA card's VRAM at
  startup. Models that fit show green in the model dropdowns; models
  beyond the card's memory are greyed out, labeled "exceeds GPU memory",
  and can't be selected or generated with (a clear warning explains why).
  Machines without an NVIDIA GPU get an explicit incompatibility notice.

## v1.3.2 — 2026-08-07
- Reference images simplified to a single override behavior (the three
  modes are gone): loading references makes them the source of art style,
  composition and characters — the prompt picks what to use from them,
  and the art-style preset + LoRA selections are automatically ignored
  while references are active. "Change amount" is now "Reference
  influence".
- Fixes "Prompt outputs failed validation (CheckpointLoaderSimple)": a
  stale engine left over from an old version could be running with wrong
  model paths. The app now detects an engine that can't see the model
  folder and restarts it with correct settings — automatically at
  startup, and with a prompt at generate time.

## v1.3.1 — 2026-08-07
- Fixes "HTTPError 400 … /prompt" for users who updated from older
  versions: the update popup could deliver the IP-Adapter models but not
  the engine node. The app now detects the missing add-on when a
  style/character reference is used and installs it itself (node +
  models + engine restart) after a yes/no prompt.
- Engine 400 responses now surface the real reason (invalid nodes,
  missing models) instead of a bare HTTP error.

## v1.3.0 — 2026-08-07
- Reference images now have three modes (fixes "the model just repeats
  the image"): **Redraw composition** (img2img, keeps layout — all
  models), **Copy the style** (paints new scenes in the reference's look),
  and **Use the character** (puts the reference's subject in whatever
  scene the prompt describes). Style/character modes use IP-Adapter and
  work with SDXL-family models.
- IP-Adapter node + models added to Setup and the update manifest;
  engine updates now preserve custom nodes.

## v1.2.3 — 2026-08-07
- First-run experience fix (blank model list on fresh PCs): the app now
  detects a missing engine or empty model folder, explains it plainly,
  and offers to launch Setup.exe directly. The status bar also says what
  to do when no models are found.

## v1.2.2 — 2026-08-07
- Portability fix for public release: the model-paths config is now
  written by the app with the machine's own absolute path at every engine
  start — the folder works wherever it is unzipped.
- First GitHub release.

## v1.2.1 — 2026-08-06
- Border maker takes a full multiline prompt (not just a theme), with a
  checkbox to either wrap it in the frame-structure wording (recommended)
  or use it 100% verbatim for maximum precision.

## v1.2.0 — 2026-08-06
- Bezel composer (🧩 button): composite character/art PNGs onto any
  border — position presets (side panels, corners, top/bottom), size,
  flip, nudge, optional screen-area clipping, preview, one-click save.
  In-app directions explain which borders and images to use.
- New model: Flux.1-schnell fp8 (Apache 2.0 — outputs fully unrestricted,
  including commercial use). 4-step sampling wired in automatically.
- Startup update check now also covers tools: the ComfyUI engine is
  checked against GitHub and offered in the same update popup as models;
  updating preserves user data, refreshes packages, restarts the engine.
- Multiple reference images for the main generation (several = collage),
  and the Border maker now takes reference images too — they guide the
  border art (influence via the Change amount slider) while the center
  stays empty.
- "⭐ Add to training set" button banks any result (image + prompt as
  caption) into training\dataset — the raw material for training your own
  style LoRA. See the new TRAINING.md for the full path from dataset to
  LoRA and how styles carry across model generations.

## v1.1.12 — 2026-08-06
- ROOT-CAUSE FIX for values vanishing from dropdowns (aspect ratio, model,
  LoRAs…): Tk's selection-export made a box's displayed value disappear
  whenever another box was clicked. All 12 input widgets now disable
  selection export — selections always stay visible, including after
  restart.
- Full code review pass; border variations no longer force random seeds
  (fixed seed + variations is reproducible again).

## v1.1.11 — 2026-08-06
- No selection is ever cleared by another choice: switching models only
  adapts the preset dropdown's option list; your current preset, border
  style, border model and aspect all stay exactly as you set them.
- Border model selector is fully independent of the main model selector.

## v1.1.10 — 2026-08-06
- Model is now selected first (moved above Art Style), and the preset
  dropdown only lists styles suited to the selected model's family.
- Selecting a preset no longer changes the model (the old auto-switch
  that reset the model selection is removed).

## v1.1.9 — 2026-08-06
- Border maker theming: style selector (Franchise for games/movies/comics,
  Arcade cabinet bezel, Material/concept), its own model selector, and
  its own Variations 1–10 to generate multiple borders per theme and pick
  the best.
- Borders now use masked generation — art can only be painted in the
  border zone, so the center stays empty no matter how busy the theme.
- Anti-text hardening baked into the border prompts (works even on Turbo
  models where negative prompts are weak).

## v1.1.8 — 2026-08-06
- Border maker moved from a popup into the bottom of the main control
  panel; its settings (theme, aspect, thickness) are remembered like
  everything else.
- The left control panel scrolls vertically (scrollbar + mouse wheel)
  when the window is too small to show all options.

## v1.1.7 — 2026-08-06
- Border maker: generate themed 4:3 / 16:9 (720p or 1080p) border frames
  with a fully transparent center — thickness slider 6–30%, hard-wired
  no-text negative prompt, uses the main window's model/LoRAs/variations.
  Saved as `border_*.png` with alpha.

## v1.1.6 — 2026-08-06
- Reference image (img2img): load any picture and the AI modifies /
  redraws / reinterprets it per your prompt and style preset. "Change
  amount" slider (10–95%) controls how far it strays from the original.
  Works with all models, variations, LoRAs and transparency; the
  reference's name and change amount are recorded in the PNG metadata.

## v1.1.5 — 2026-08-06
- "Batch" renamed to "Variations" and extended to 1–10: each variation
  runs the same prompt with a different seed.
- Clarified: the pipeline has no content filter or safety checker of any
  kind — generation limits come only from the chosen model's training.

## v1.1.4 — 2026-08-06
- "❌ Delete art files…" button under Clear history: permanently deletes
  all generated PNGs (output folder + engine scratch) after a red-text
  confirmation dialog showing file count and size.

## v1.1.3 — 2026-08-06
- Session gallery: horizontal scrollbar + mouse-wheel scrolling through
  thumbnails; newest result auto-scrolls into view.
- "Clear history" button at the bottom right empties the gallery strip
  (saved PNGs in `output\` are not touched).

## v1.1.2 — 2026-08-06
- LoRA selection redesigned: the three dropdowns are gone — one list where
  you tick any number of LoRAs (plus a single strength slider). This also
  eliminates the selection-eats-selection bug for good.
- Settings now auto-save on every change (debounced), not just on
  generate/close — verified to survive even a force-killed process.
- UI message loop hardened: an error while handling a result can no longer
  freeze the interface mid-generation.

## v1.1.1 — 2026-08-06
- Everything stays inside the app folder: pip cache disabled, Setup temp
  files moved in-folder, background-removal model and HuggingFace caches
  relocated to `models\` — nothing is written to the user profile or
  AppData.
- Setup always verifies models against their **latest releases** on
  HuggingFace (missing *and* outdated files are fetched), using the same
  check as the app — Setup and the app can no longer disagree, so no more
  surprise multi-GB prompts right after a completed setup.
- Setup grays out to "Installed" when everything is present and current,
  both on launch and after a successful run.
- `ComicArtCreator.exe` is now a single file in the main folder next to
  `Setup.exe` (no more `ComicArtCreator\` subfolder).
- LoRA fixes: the same LoRA can no longer be selected in two slots
  (it silently stacked twice); added a third LoRA slot.

## v1.1.0 — 2026-08-06
- Zero-prerequisite installs: Setup.exe now bootstraps a private embedded
  Python 3.12 runtime inside the app folder (`python\`) — target machines
  need nothing but 64-bit Windows 10/11 and an NVIDIA driver. Nothing is
  installed system-wide.
- App prefers the bundled runtime and falls back to a dev venv.
- `Setup.exe --cli [--skip-models]` headless mode (logs to setup.log).

## v1.0.2 — 2026-08-06
- Batch-file-free release: `Setup.exe` (graphical installer with progress
  log) replaces `setup.bat`/`setup.ps1`; the app launches from
  `ComicArtCreator.exe` only.
- Setup.exe no longer requires git — the engine is fetched as a zip.
- Both exes carry proper Windows version metadata; built for 64-bit
  Windows 10 and 11.

## v1.0.1 — 2026-08-06
- Version bump / packaging release (no functional changes from v1.0.0).

## v1.0.0 — 2026-08-06
Initial release.
- Tkinter studio app driving a headless ComfyUI engine (loopback only,
  auto-started, auto-spawned if not running).
- 4 starter checkpoints: Flux.1-dev fp8, Juggernaut XL v9,
  DreamShaper XL Turbo v2.1, Animagine XL 4.0 — sampler/steps/CFG
  auto-tuned per model family; model sizes + VRAM guidance shown in-app.
- 21 art style presets with editable style text, examples, and per-preset
  model hints.
- 7-LoRA starter pack (Flux_/SDXL_ prefixed) + 2 LoRA slots with strength
  sliders + CivitAI downloader (DPAPI-encrypted API key).
- Transparent-background generation (rembg isnet-general-use).
- Auto-save with full recipe embedded in PNG metadata; session gallery;
  Save As; seed control + reuse; batch up to 8.
- UI-state persistence across restarts; Clear button; user-typed negative
  prompts never overwritten by preset switching.
- Startup model-update check against HuggingFace with yes/no popup.
- Packaged exe (PyInstaller onedir), setup.ps1 for fresh machines.
- OWASP-mapped security review (SECURITY.md): safetensors-only policy,
  path-traversal-safe downloads, HTTPS-only, loopback engine.
