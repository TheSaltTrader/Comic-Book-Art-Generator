"""Motion-strength test: seamless FLF with the new Strong-motion template
must produce visibly more motion than the old subtle result (~2.4)."""
import sys, time, requests
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import comic_art_creator as cac
from PIL import Image, ImageChops

RAW = Path(__file__).parent.parent / "output" / "_raw"
gen = cac.Generator.__new__(cac.Generator)

name = gen._upload_ref(str(RAW / "transparency_test.png"))
motion = list(cac.ANIM_MOTION.values())[0]   # Strong
prompt = ("the two superheroes laugh heartily while walking in place, "
          "arms and legs swinging with each step, heads thrown back. "
          f"{motion}. The character stays centered in frame against a "
          "flat plain solid background, full body always visible, locked "
          "camera, no camera movement, no scene change.")
g = cac.build_wan_flf_graph(dict(prompt=prompt, anim_image_name=name,
                                 width=704, height=704, length=33, seed=9))
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
frames = [gen._fetch_image(m) for m in metas]


def mean_diff(a, b):
    hst = ImageChops.difference(a.convert("RGB"), b.convert("RGB")).histogram()
    total = sum(hst[i % 256] * (i % 256) for i in range(len(hst)))
    return total / (a.width * a.height * 3)


first = frames[0]
mid = frames[len(frames) // 2]
q1 = frames[len(frames) // 4]
d_mid = mean_diff(first, mid)
d_q1 = mean_diff(first, q1)
d_loop = mean_diff(first, frames[-1])
mid.convert("RGB").save(RAW / "motion_mid.png")
print(f"{len(frames)} frames in {time.time()-t0:.0f}s")
print(f"motion first-vs-mid: {d_mid:.2f}  first-vs-quarter: {d_q1:.2f}  "
      f"loop closure: {d_loop:.2f}")
print("OK" if d_mid > 4.0 else "STILL SUBTLE")
