"""Quality test: gray-composited start image + 30 steps + cfg 5.5,
cape-billowing action. Outputs output/cape_test.gif for eyeballing."""
import sys, time, requests
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import comic_art_creator as cac
from PIL import Image

ROOT = Path(__file__).parent.parent
RAW = ROOT / "output" / "_raw"
gen = cac.Generator.__new__(cac.Generator)

# composite the transparent character onto neutral gray (video models
# hate hard cutouts on black)
src = Image.open(RAW / "transparency_test.png").convert("RGBA")
bg = Image.new("RGBA", src.size, (200, 200, 205, 255))
bg.alpha_composite(src)
name = gen._upload_pil(bg.convert("RGB"), "cbac_anim_gray.png")

motion = list(cac.ANIM_MOTION.values())[0]
prompt = ("the superheroes' capes billow and wave dramatically in a "
          "strong wind, fabric rippling and flowing. "
          f"{motion}. The characters stay centered in frame against a "
          "flat plain solid background, full body always visible, locked "
          "camera, no camera movement, no scene change.")
g = cac.build_wan_flf_graph(dict(prompt=prompt, anim_image_name=name,
                                 width=704, height=704, length=33, seed=4))
g["8"]["inputs"]["steps"] = 30
g["8"]["inputs"]["cfg"] = 5.5
r = requests.post(f"{cac.ENGINE_URL}/prompt", json={"prompt": g}, timeout=30)
r.raise_for_status()
pid = r.json()["prompt_id"]
t0 = time.time()
metas = None
while time.time() - t0 < 2400:
    time.sleep(5)
    h = requests.get(f"{cac.ENGINE_URL}/history/{pid}", timeout=15).json()
    if pid in h:
        st = h[pid].get("status", {})
        if st.get("status_str") == "error":
            print("ENGINE ERROR"); sys.exit(1)
        if h[pid].get("outputs"):
            metas = [i for o in h[pid]["outputs"].values()
                     for i in o.get("images", [])]
            break
assert metas, "timeout"
frames = [gen._fetch_image(m) for m in metas][:-1]
cac.save_gif(frames, ROOT / "output" / "cape_test.gif", 16, False)
print(f"{len(frames)+1} frames in {time.time()-t0:.0f}s -> "
      f"output/cape_test.gif")
