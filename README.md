# Comic Book Art Creator

**v1.1.0** — a fully local, unrestricted comic-book art studio for your GPU.
Nothing leaves your machine — no accounts, no filters, no cloud.
Compatible with 64-bit **Windows 10 and 11** — **no Python, git, or any
other software required**; everything the app needs lives in its own
folder.

## Quick start

Launch **`ComicArtCreator.exe`** (right next to `Setup.exe`).

The app starts the engine (headless ComfyUI) automatically — the first
launch takes a minute or two while a model loads into VRAM. When the status
line says **"Engine ready."** you're good to go.

1. Pick an **Art Style Preset** (e.g. *Noir / Sin City*).
2. Click **Try example** for a ready-made prompt, or write your own —
   your prompt describes the *scene*, the preset supplies the *style*.
3. Hit **⚡ GENERATE**.

Your prompt, negative prompt, style and all settings are **remembered** —
across generations and app restarts — until you change them or hit
**Clear**. Every image is auto-saved to `output\` with its full recipe
(prompt, model, seed, LoRAs) embedded in the PNG metadata, so any result
can be reproduced. **Save As…** copies the current image anywhere.

On start the app also **checks for newer releases of the installed models
(HuggingFace) and of the ComfyUI engine (GitHub)** and asks (yes/no popup)
before updating anything.

## Border maker & Bezel composer

The **Border maker** (bottom of the left panel) generates themed 4:3/16:9
frames with a transparent screen hole — pick a style (Franchise, Arcade
bezel, Material), a model, thickness, and 1–10 variations. Masked
generation keeps the center empty; prompts are text-free by design.

The **🧩 Bezel composer** (button under the preview) finishes the job the
way real arcade bezels are made: load a border (a `border_*.png` you
generated, or any bezel PNG with a transparent hole), add
transparent-background character/art PNGs (game or movie renders), place
them on the side panels or corners with size/flip/nudge controls, preview,
and save. Note: images of franchise characters carry their own copyright —
fine for a personal cab, not redistributable.

## The models

Sizes are shown next to each model in the dropdown — **your video card RAM
must be above that number** (keep ~2 GB headroom; the app shows your
detected VRAM under the model box).

| Model | Size | Best for | Speed on a 5090 |
|---|---|---|---|
| **flux1-dev-fp8** | 16.1 GB | Best overall quality + prompt understanding; painted styles, covers | ~16 s |
| **Flux1-Schnell-fp8** | 16.1 GB | Near-dev quality in 4 steps; **Apache 2.0 — outputs fully unrestricted incl. commercial** | ~4 s |
| **Juggernaut-XL-v9** | 6.6 GB | Versatile, great with LoRAs, semi-real styles | ~5 s |
| **DreamShaperXL-Turbo-v2.1** | 6.5 GB | Fast iteration — 8 steps; stylized art | ~2 s |
| **Animagine-XL-4.0** | 6.5 GB | Manga / anime lineage | ~5 s |

Steps, CFG and sampler are chosen automatically per model family — leave
**Steps** on `auto` unless you want to override.

## Style presets (21)

From Golden Age to Moebius to Marvel House Style to Photorealistic. The
preset's style text is shown in an editable box — tweak it freely; it's
appended to your prompt. Each preset auto-picks a suitable model (you can
change it). A negative prompt you typed yourself is never overwritten by
preset switching. Add your own presets in `app\presets.json`.

## LoRAs — starter pack included

LoRAs are small style add-ons that push a base model hard toward a specific
look. Seven come pre-installed (`models\loras`) — the name prefix tells you
which model family they need:

| LoRA | Pair with | Look |
|---|---|---|
| Flux_RetroComic_v2 | Flux | vintage comic print |
| Flux_ArtStyle_XLabs | Flux | painterly art style |
| Flux_Realism_XLabs | Flux | photoreal boost |
| SDXL_GraphicNovel | Juggernaut / DreamShaper | graphic-novel ink & color |
| SDXL_BW_Manga | any SDXL | bold B&W manga |
| SDXL_LineArt_Manga | any SDXL | clean line art |
| SDXL_StyleEnhancer_Anime | Animagine | anime detail boost |

Get more with the **⬇ Get LoRAs (CivitAI)** button — paste a CivitAI model
page URL (free API key from civitai.com → Account Settings → API Keys;
stored encrypted). Or drop `.safetensors` files into `models\loras` and
hit **↻**.

## Transparency

Tick **Transparent BG** for a clean transparent PNG of the subject —
perfect for compositing panels, stickers, or layering in an editor. The
first use downloads a small background-removal model (~180 MB).

## Fresh install (new machine / from GitHub)

The models and engine are too big for a Git repo, so a release ships the
app plus **`Setup.exe`** — a graphical installer that makes the folder
fully self-contained: it downloads a private Python runtime (into
`python\`, nothing installed system-wide), the engine, and the starter
model pack (~36 GB), all with a progress log. Requirements on the new
machine: **just 64-bit Windows 10/11, an NVIDIA GPU with a current
driver, and ~60 GB free disk** — no Python, no git, no anything else.
Tick *"Engine only"* in Setup to skip the model downloads.
(`Setup.exe --cli --skip-models` works for scripted installs.)

Windows SmartScreen will warn about unsigned exes on a machine that
downloaded them: **More info → Run anyway** (or right-click → Properties →
Unblock).

## Security

Reviewed against the OWASP Top 10 — see `SECURITY.md`. Highlights: engine
is loopback-only, CivitAI key is DPAPI-encrypted, safetensors-only policy
(no pickle model formats), sanitized download paths, HTTPS-only.

## Troubleshooting

- **Engine did not come up** → read `engine.log`; the last lines say why.
- **Model missing from dropdown** → wrong folder or still downloading;
  checkpoints go in `models\checkpoints`, then hit **↻**.
- **Out of VRAM** → model size + canvas must fit your card; close other
  GPU-hungry apps.
- The engine stays running after you close the app (faster next launch).
  To stop it: Task Manager → end the `python.exe` using the most GPU
  memory.

## Layout

```
Comic_book__art_creator\
├── ComicArtCreator.exe                   ← run this
├── Setup.exe                             first-time install / model updates
├── app\comic_art_creator.py              the studio app (source)
├── app\setup_installer.py                Setup.exe (source)
├── app\presets.json                      art style presets (editable!)
├── app\models_manifest.json              update-checker model list
├── ComfyUI\                              engine (headless, auto-started)
├── venv\                                 python environment
├── models\checkpoints|loras\             your model library
├── output\                               finished art (auto-saved)
├── releases\vX.Y.Z\                      versioned releases for GitHub
└── SECURITY.md                           OWASP security review
```
