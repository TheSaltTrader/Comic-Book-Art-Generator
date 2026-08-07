"""Seamless-loop test: Wan FLF with start=end=character. Verifies the
generated clip closes its own loop (first vs last frame similarity)."""
import sys, time, requests
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import comic_art_creator as cac
from PIL import Image, ImageChops

RAW = Path(__file__).parent.parent / "output" / "_raw"
gen = cac.Generator.__new__(cac.Generator)

name = gen._upload_ref(str(RAW / "transparency_test.png"))
prompt = ("the two superheroes flex their muscles and their capes wave in "
          "the wind, then return to their exact starting pose. The "
          "character performs the action smoothly in place, full body "
          "visible, flat plain solid background, locked camera, no camera "
          "movement.")
g = cac.build_wan_flf_graph(dict(prompt=prompt, anim_image_name=name,
                                 width=704, height=704, length=33, seed=5))
r = requests.post(f"{cac.ENGINE_URL}/prompt", json={"prompt": g}, timeout=30)
if r.status_code == 400:
    print("400:", r.text[:800]); sys.exit(1)
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
first, last = frames[0].convert("RGB"), frames[-1].convert("RGB")
mid = frames[len(frames) // 2].convert("RGB")


def mean_diff(a, b):
    h2 = ImageChops.difference(a, b).histogram()
    total = sum(h2[i % 256] * (i % 256) for i in range(len(h2)))
    return total / (a.width * a.height * 3)


d_loop = mean_diff(first, last)
d_mid = mean_diff(first, mid)
mid.save(RAW / "seamless_mid.png")
frames[-1].save(RAW / "seamless_last.png")
print(f"first-vs-last mean diff: {d_loop:.2f} (loop closure)")
print(f"first-vs-mid  mean diff: {d_mid:.2f} (motion happened)")
assert d_loop < d_mid, "loop did not close better than mid-motion"
print("OK — loop closes")
