"""End-to-end animator test: character image -> Wan frames -> transparent
frames -> ping-pong GIF."""
import sys, time, requests
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import comic_art_creator as cac
from PIL import Image

RAW = Path(__file__).parent.parent / "output" / "_raw"
OUT = Path(__file__).parent.parent / "output" / "animations" / "test_run"
gen = cac.Generator.__new__(cac.Generator)

name = gen._upload_ref(str(RAW / "transparency_test.png"))
prompt = ("the two superheroes raise their fists triumphantly and their "
          "capes sway in the wind. The character performs the action "
          "smoothly in place, full body visible, flat plain solid "
          "background, locked camera, no camera movement.")
g = cac.build_wan_graph(dict(prompt=prompt, anim_image_name=name,
                             width=704, height=704, length=49, seed=21))
r = requests.post(f"{cac.ENGINE_URL}/prompt", json={"prompt": g}, timeout=30)
if r.status_code == 400:
    print("400:", r.text[:600]); sys.exit(1)
r.raise_for_status()
pid = r.json()["prompt_id"]
t0 = time.time()
metas = None
while time.time() - t0 < 1800:
    time.sleep(5)
    h = requests.get(f"{cac.ENGINE_URL}/history/{pid}", timeout=15).json()
    if pid in h:
        st = h[pid].get("status", {})
        if st.get("status_str") == "error":
            import json as j
            print("ENGINE ERROR:", j.dumps(st.get("messages", []))[:1000])
            sys.exit(1)
        if h[pid].get("outputs"):
            metas = [i for o in h[pid]["outputs"].values()
                     for i in o.get("images", [])]
            break
assert metas, "timeout"
print(f"generated {len(metas)} frames in {time.time()-t0:.0f}s")
frames = [gen._fetch_image(m) for m in metas]
kept = frames[::2]
raw_dir = OUT / "_raw_frames"; frames_dir = OUT / "frames"
raw_dir.mkdir(parents=True, exist_ok=True)
for i, f in enumerate(kept):
    f.save(raw_dir / f"frame_{i:03d}.png")
cac.remove_background_dir(raw_dir, frames_dir)
kept = [Image.open(fp).convert("RGBA")
        for fp in sorted(frames_dir.glob("*.png"))]
looped = cac.apply_loop(kept, "Ping-pong (perfect loop)")
cac.save_gif(looped, OUT / "animation.gif", 12, True)
mid = kept[len(kept) // 2]
a = mid.getchannel("A")
print(f"frames kept: {len(kept)}, gif frames: {len(looped)}, "
      f"mid-frame corner alpha: {a.getpixel((5, 5))}")
print("OK")
