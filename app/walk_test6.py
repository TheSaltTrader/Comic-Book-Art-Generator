"""Full-pipeline walking test: prep -> 5B free motion (4 s) -> rembg ->
defringe -> auto-cut -> transparent GIF. Mirrors the v1.7.5 app exactly."""
import sys, time, requests
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import comic_art_creator as cac
from PIL import Image

ROOT = Path(__file__).parent.parent
RAW = ROOT / "output" / "_raw"
gen = cac.Generator.__new__(cac.Generator)

src = Image.open(
    ROOT / "output" / "20260807_131545_seed2029744857.png").convert("RGBA")
cut = cac.remove_background(src)
stage = Image.new("RGBA", cut.size, cac.STAGE_BG + (255,))
stage.alpha_composite(cut)
name = gen._upload_pil(stage.convert("RGB"), "cbac_anim_prep6.png")

motion = list(cac.ANIM_MOTION.values())[0]
prompt = ("the armored hero walks in place with a confident powerful "
          "stride, legs lifting and stepping, arms swinging fully with "
          "each step, jacket swaying. "
          f"{motion}. The character stays centered in frame against a "
          "flat plain solid background, full body always visible, locked "
          "camera, no camera movement, no scene change.")
g = cac.build_wan_graph(dict(prompt=prompt, anim_image_name=name,
                             width=704, height=1280, length=97, seed=31))
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
print(f"{len(metas)} frames generated in {time.time()-t0:.0f}s")
frames = [gen._fetch_image(m) for m in metas]
kept = frames[::2]                       # 12 fps sprite rate
kept = cac.best_loop_cut(kept)           # forward-loop cut
print(f"auto-cut cycle: {len(kept)} frames")
tmp_raw, tmp_out = RAW / "_walk6_raw", RAW / "_walk6_cut"
tmp_raw.mkdir(parents=True, exist_ok=True)
for i, f in enumerate(kept):
    f.save(tmp_raw / f"frame_{i:03d}.png")
cac.remove_background_dir(tmp_raw, tmp_out)
clean = [cac.defringe(Image.open(fp).convert("RGBA"))
         for fp in sorted(tmp_out.glob("*.png"))]
seam = cac.frame_diff(clean[0], clean[-1])
mid = cac.frame_diff(clean[0], clean[len(clean) // 2])
cac.save_gif(clean, ROOT / "output" / "walk_test6.gif", 12, True)
print(f"seam: {seam:.2f} vs mid-motion: {mid:.2f} -> output/walk_test6.gif")
