"""Audit: every model file the app references must have a manifest entry
(so Setup installs it and the startup check keeps it current), and every
manifest entry must resolve on HuggingFace with a size."""
import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import comic_art_creator as cac

manifest = json.loads(
    (Path(__file__).parent / "models_manifest.json").read_text("utf-8"))
by_key = {(e["dir"], e["local"]) for e in manifest}

required = [
    ("checkpoints", cac.FLUX_CLIP_SRC),
    ("diffusion_models", cac.KONTEXT_FILE),
    ("diffusion_models", cac.QWEN_EDIT_FILE),
    ("text_encoders", cac.QWEN_TE_FILE),
    ("vae", cac.QWEN_VAE_FILE),
    ("diffusion_models", cac.WAN_FILE),
    ("text_encoders", cac.WAN_TE_FILE),
    ("vae", cac.WAN_VAE_FILE),
    ("diffusion_models", cac.WAN_FLF_FILE),
    ("clip_vision", cac.WAN_CLIPVIS_FILE),
    ("vae", cac.WAN21_VAE_FILE),
    ("ipadapter", "ip-adapter-plus_sdxl_vit-h.safetensors"),
    ("clip_vision", "CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors"),
]
missing = [k for k in required if k not in by_key]
if missing:
    print("MISSING FROM MANIFEST:", missing)
    sys.exit(1)
print(f"constants covered: all {len(required)} referenced files are in "
      f"the manifest ({len(manifest)} entries total)")

repo_cache, bad = {}, []
for e in manifest:
    repo = e["repo"]
    if repo not in repo_cache:
        r = requests.get(
            f"https://huggingface.co/api/models/{repo}?blobs=true",
            timeout=30)
        repo_cache[repo] = r.json().get("siblings", []) if r.ok else []
    sib = next((s for s in repo_cache[repo]
                if s.get("rfilename") == e["remote_file"]), None)
    if not sib or not sib.get("size"):
        bad.append(f"{repo}/{e['remote_file']}")
total = 0
for e in manifest:
    sib = next((s for s in repo_cache[e["repo"]]
                if s.get("rfilename") == e["remote_file"]), None)
    if sib:
        total += sib.get("size") or 0
if bad:
    print("UNRESOLVABLE ON HF:", bad)
    sys.exit(1)
print(f"HF resolution: all {len(manifest)} entries resolve; full pack = "
      f"{total / (1 << 30):.1f} GB")
print("AUDIT OK")
