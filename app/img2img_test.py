"""img2img smoke test: upload a reference, redraw it in a new style."""
import sys, time, requests
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from comic_art_creator import build_graph, Generator, ENGINE_URL

ref = Path(__file__).parent.parent / "output" / "_raw" / "cbac_00001_.png"
gen = Generator.__new__(Generator)  # just for _upload_ref
name = Generator._upload_ref(gen, str(ref))
print("uploaded as:", name)

p = dict(model="Juggernaut-XL-v9.safetensors",
         prompt="two superheroes fighting on a rooftop, stark black and "
                "white noir comic art, heavy shadows, rain",
         negative="color, photo", width=832, height=1216, seed=99,
         steps=None, cfg=None, loras=[], ref_image_name=name, denoise=0.55)
g = build_graph(p)
assert g["6"]["inputs"]["denoise"] == 0.55 and "10" in g
r = requests.post(f"{ENGINE_URL}/prompt", json={"prompt": g}, timeout=30)
r.raise_for_status()
pid = r.json()["prompt_id"]
t0 = time.time()
while time.time() - t0 < 300:
    time.sleep(2)
    h = requests.get(f"{ENGINE_URL}/history/{pid}", timeout=15).json()
    if pid in h and h[pid].get("outputs"):
        imgs = [i for o in h[pid]["outputs"].values()
                for i in o.get("images", [])]
        print(f"OK in {time.time()-t0:.1f}s -> {imgs[0]['filename']}")
        sys.exit(0)
print("TIMEOUT/FAIL")
sys.exit(1)
