"""Walk test round 2: auto-prep (rembg -> gray composite) before FLF."""
import sys, time, requests, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import comic_art_creator as cac
from PIL import Image, ImageChops

ROOT = Path(__file__).parent.parent
gen = cac.Generator.__new__(cac.Generator)

src = Image.open(
    ROOT / "output" / "20260807_131545_seed2029744857.png").convert("RGBA")
cut = cac.remove_background(src)          # isolate the character
bg = Image.new("RGBA", cut.size, (200, 200, 205, 255))
bg.alpha_composite(cut)
name = gen._upload_pil(bg.convert("RGB"), "cbac_anim_prep.png")

motion = list(cac.ANIM_MOTION.values())[0]
prompt = ("the armored hero walks in place with a confident powerful "
          "stride, arms and legs swinging fully with each step, jacket "
          "swaying. "
          f"{motion}. The character stays centered in frame against a "
          "flat plain solid background, full body always visible, locked "
          "camera, no camera movement, no scene change.")
g = cac.build_wan_flf_graph(dict(prompt=prompt, anim_image_name=name,
                                 width=704, height=1280, length=33,
                                 seed=17))
g["8"]["inputs"]["steps"] = 30
g["8"]["inputs"]["cfg"] = 5.5
r = requests.post(f"{cac.ENGINE_URL}/prompt", json={"prompt": g}, timeout=30)
r.raise_for_status()
pid = r.json()["prompt_id"]
t0 = time.time()
metas = None
while time.time() - t0 < 3600:
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


def mean_diff(a, b):
    hst = ImageChops.difference(a.convert("RGB"),
                                b.convert("RGB")).histogram()
    return sum(hst[i % 256] * (i % 256)
               for i in range(len(hst))) / (a.width * a.height * 3)


d = mean_diff(frames[len(frames) // 4], frames[len(frames) // 2])
cac.save_gif(frames, ROOT / "output" / "walk_test2.gif", 16, False)
frames[len(frames) // 2].convert("RGB").save(
    ROOT / "output" / "_raw" / "walk2_mid.png")
print(f"{len(frames)+1} frames in {time.time()-t0:.0f}s, "
      f"q1-vs-mid motion: {d:.2f} -> output/walk_test2.gif")
