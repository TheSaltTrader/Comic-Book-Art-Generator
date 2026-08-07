"""Headless smoke test: generate one image per model family via the app's
own graph builder, exactly as the GUI would."""
import sys, time, json, requests
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from comic_art_creator import build_graph, ENGINE_URL

TESTS = [
    dict(model="DreamShaperXL-Turbo-v2.1.safetensors",
         prompt="a caped superhero punching a robot on a city rooftop at night, "
                "vintage 1940s golden age comic book art, bold heavy inks, halftone dots",
         negative="photo, blurry", width=832, height=1216, seed=12345,
         steps=None, cfg=None, loras=[]),
    dict(model="flux1-dev-fp8.safetensors",
         prompt="an armored heroine landing in a crater on a rain-soaked street, "
                "modern comic book cover art, dynamic composition, cinematic lighting",
         negative="", width=832, height=1216, seed=12345,
         steps=None, cfg=None, loras=[]),
    dict(model="Juggernaut-XL-v9.safetensors",
         prompt="graphic novel illustration, a detective in a rain coat under "
                "a streetlamp, noir mood, detailed ink work",
         negative="photo, blurry", width=832, height=1216, seed=777,
         steps=None, cfg=None,
         loras=[("SDXL_GraphicNovel.safetensors", 0.9)]),
]

for t in TESTS:
    g = build_graph(t)
    r = requests.post(f"{ENGINE_URL}/prompt", json={"prompt": g}, timeout=30)
    r.raise_for_status()
    pid = r.json()["prompt_id"]
    print(f"[{t['model']}] queued {pid}", flush=True)
    t0 = time.time()
    while True:
        time.sleep(2)
        h = requests.get(f"{ENGINE_URL}/history/{pid}", timeout=15).json()
        if pid in h:
            st = h[pid].get("status", {})
            if st.get("status_str") == "error":
                msgs = [m for m in h[pid].get("status", {}).get("messages", [])]
                print("  ERROR:", json.dumps(msgs)[:2000])
                sys.exit(1)
            outs = h[pid].get("outputs", {})
            imgs = [i for o in outs.values() for i in o.get("images", [])]
            if imgs:
                print(f"  OK in {time.time()-t0:.1f}s -> {imgs[0]['filename']}")
                break
        if time.time() - t0 > 600:
            print("  TIMEOUT"); sys.exit(1)
print("ALL PASS")
