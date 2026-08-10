# RAG map format (`.ragmap.json`)

A **RAG map** pairs a trained LoRA with a set of example images. When you
load one in the Comic Book Art Creator's first section, the app retrieves
the example images most relevant to your prompt and feeds them as
**visual guidance (IP-Adapter)** while it generates — so the output stays
faithful to the LoRA's subject/style. It also auto-applies the paired
LoRA and its trigger word.

This is the exact format the app reads. Have your training tool emit one
of these next to the LoRA it trains.

## Schema — `cbac-ragmap/1`

```json
{
  "schema": "cbac-ragmap/1",
  "name": "My Character",
  "lora": "SDXL_MyCharacter_v1.safetensors",
  "trigger": "mychar",
  "image_dir": "images",
  "weight": 0.8,
  "top_k": 4,
  "entries": [
    {
      "image": "0001.png",
      "keywords": ["red cape", "flying", "hero", "sky"],
      "caption": "the hero flying through the sky in a red cape"
    },
    {
      "image": "0002.png",
      "keywords": ["portrait", "smiling", "close up"],
      "caption": "a close-up portrait of the hero smiling"
    }
  ]
}
```

### Fields

| Field | Required | Meaning |
|---|---|---|
| `schema` | recommended | `"cbac-ragmap/1"`. |
| `name` | recommended | Display name shown in the app. |
| `lora` | optional | Filename of the paired LoRA (in `models\loras`). Auto-applied when the map is used. |
| `trigger` | optional | Trigger word; prepended to the prompt if not already present. |
| `image_dir` | optional | Folder holding the images, **relative to the map file**. Default: same folder as the map. |
| `weight` | optional | IP-Adapter strength 0.0-1.5 (default 0.8). |
| `top_k` | optional | How many images to retrieve per generation (default 4). |
| `entries` | **required** | The example images. |

### Entry fields

- `image` — filename (relative to `image_dir`) or an absolute path.
- `keywords` — short tags used to match the user's prompt.
- `caption` — a full description. Used both for matching and, when
  IP-Adapter isn't installed, appended to the prompt as text guidance.

## How retrieval works

The app scores each entry by how many of its `keywords`/`caption` words
appear in your prompt, then takes the top `top_k`. If nothing matches, it
uses the first `top_k` entries so the LoRA still gets representative
guidance.

## Packaging

Ship the map and its images together, e.g.:

```
MyCharacter/
  MyCharacter.ragmap.json
  images/
    0001.png
    0002.png
    ...
```

Point the app's **🧭 RAG map…** button at the `.ragmap.json`.

## Notes

- Image guidance uses **IP-Adapter**, which is **SDXL-only** — use an SDXL
  model (Juggernaut-XL, DreamShaper) as the base. With a Flux model the
  app skips image guidance.
- If IP-Adapter isn't installed yet, loading a map offers a one-time
  ~1 GB download; without it the map still helps by adding its captions
  to your prompt as text.
