"""Walk test round 3: Wan 2.2 5B (non-seamless) + prepped image +
TRANSPARENT gif output — the full real pipeline."""
import sys, time, requests
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import comic_art_creator as cac
from PIL import Image, ImageChops

ROOT = Path(__file__).parent.parent
RAW = ROOT / "output" / "_raw"
gen = cac.Generator.__new__(cac.Generator)

src = Image.open(
    ROOT / "output" / "20260807_131545_seed2029744857.png").convert("RGBA")
cut = cac.remove_background(src)
bg = Image.new("RGBA", cut.size, (200, 200, 205, 255))
bg.alpha_composite(cut)
name = gen._upload_pil(bg.convert("RGB"), "cbac_anim_prep5b.png")

motion = list(cac.ANIM_MOTION.values())[0]
prompt = ("the armored hero walks in place with a confident powerful "
          "stride, legs lifting and stepping, arms swinging fully with "
          "each step, jacket swaying. "
          f"{motion}. The character stays centered in frame against a "
          "flat plain solid background, full body always visible, locked "
          "camera, no camera movement, no scene change.")
g = cac.build_wan_graph(dict(prompt=prompt, anim_image_name=name,
                             width=704, height=1280, length=49, seed=23))
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
frames = [gen._fetch_image(m) for m in metas]


def mean_diff(a, b):
    hst = ImageChops.difference(a.convert("RGB"),
                                b.convert("RGB")).histogram()
    return sum(hst[i % 256] * (i % 256)
               for i in range(len(hst))) / (a.width * a.height * 3)


d = mean_diff(frames[len(frames) // 4], frames[len(frames) // 2])
print(f"{len(frames)} frames in {time.time()-t0:.0f}s, "
      f"q1-vs-mid motion: {d:.2f}")

# real pipeline: keep every 2nd frame, strip backgrounds, transparent gif
kept = frames[::2]
tmp_raw = RAW / "_walk3_raw"
tmp_out = RAW / "_walk3_cut"
tmp_raw.mkdir(parents=True, exist_ok=True)
for i, f in enumerate(kept):
    f.save(tmp_raw / f"frame_{i:03d}.png")
cac.remove_background_dir(tmp_raw, tmp_out)
kept = [Image.open(fp).convert("RGBA")
        for fp in sorted(tmp_out.glob("*.png"))]
looped = cac.apply_loop(kept, "Ping-pong (perfect loop)")
cac.save_gif(looped, ROOT / "output" / "walk_test3.gif", 12, True)
print("transparent gif -> output/walk_test3.gif")
