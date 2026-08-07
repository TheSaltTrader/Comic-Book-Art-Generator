# Training your own style — how "enriching the model" really works

You can't practically fine-tune a whole 16 GB model, and you don't need
to. The way personal art styles are added to image models is a **LoRA** —
a small add-on (100–400 MB) trained on your images that pushes any base
model toward your style. Your RTX 5090 trains one in well under an hour.

## The key insight: the dataset is the portable asset

A LoRA only works with the model family it was trained for (an SDXL LoRA
does nothing for Flux). What carries your style to *any* current or
future model is the **dataset** — your curated images + captions.
Retraining a LoRA for a new model from an existing dataset is quick and
mechanical; rebuilding a lost dataset is not. So the app focuses on
making dataset-building effortless:

- Hit **⭐ Add to training set** on any generation you love — the image
  and its full prompt (as the caption) are copied to
  `training\dataset\`.
- Drop **any external art** (your border collection, favorite comic
  pages) into `training\dataset\` too, and create a same-named `.txt`
  file describing it (e.g. `vine frame border, medieval carved stone,
  transparent center`). Every image should have its `.txt` caption.
- 15–30 well-captioned images make a good style LoRA; 50+ is excellent.
  Consistency matters more than volume — one style per dataset.

## Training the LoRA (pick one tool)

| Tool | Best for | Notes |
|---|---|---|
| **OneTrainer** | Easiest GUI on Windows | SDXL + Flux presets, point it at `training\dataset` |
| **kohya_ss GUI** | The community standard for SDXL | most tutorials use it |
| **ai-toolkit (ostris)** | Best for Flux LoRAs | config-file driven, 5090-friendly |

Typical settings for a style LoRA: rank 16–32, learning rate ~1e-4
(SDXL) / 1e-4..4e-4 (Flux), 1500–3000 steps, batch 1–2. On a 5090:
SDXL ≈ 20–40 min, Flux ≈ 45–90 min.

## Using and preserving what you trained

1. Drop the trained `.safetensors` into `models\loras` (prefix the name
   `SDXL_` or `Flux_` so you know its family) and hit ↻ in the app.
2. Keep `training\dataset` backed up — that's your style's source of
   truth. When a better model appears (new Flux, SD4, whatever), retrain
   against it from the same dataset and your "skill" transfers.
3. Version your LoRAs like the app versions itself: `Flux_MyBorders_v2`
   beats overwriting v1 — you can always mix old and new at different
   strengths.

## What doesn't work

- "Continuous learning" during generation — diffusion models don't learn
  at inference; training is a separate offline pass. The ⭐ button is how
  you bank material for the next pass.
- Full fine-tunes of Flux/SDXL at home — technically possible for SDXL,
  but LoRAs deliver ~95% of the benefit at 5% of the cost, and they stack.
