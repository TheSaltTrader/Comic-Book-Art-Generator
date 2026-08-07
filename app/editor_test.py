"""Editor smoke test: one Kontext edit + one Qwen text-removal edit."""
import sys, time, requests
from pathlib import Path
from io import BytesIO
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
import comic_art_creator as cac

RAW = Path(__file__).parent.parent / "output" / "_raw"
gen = cac.Generator.__new__(cac.Generator)

TESTS = [
    ("kontext", RAW / "char_test.png",
     "make it night time with heavy rain, keep the characters and their "
     "costumes exactly the same", "edit_kontext.png"),
    ("qwen", RAW / "border_marvel_masked.png",
     "remove all text, letters, words and logos from the image, keep "
     "everything else exactly the same", "edit_qwen.png"),
]

for editor, src, instr, out in TESTS:
    name = gen._upload_ref(str(src))
    p = dict(prompt=instr, edit_image_names=[name], editor=editor,
             seed=7, steps=None)
    g = cac.build_graph(p)
    r = requests.post(f"{cac.ENGINE_URL}/prompt", json={"prompt": g},
                      timeout=30)
    if r.status_code == 400:
        print(editor, "400:", r.text[:800])
        sys.exit(1)
    r.raise_for_status()
    pid = r.json()["prompt_id"]
    t0 = time.time()
    while time.time() - t0 < 900:
        time.sleep(3)
        h = requests.get(f"{cac.ENGINE_URL}/history/{pid}",
                         timeout=15).json()
        if pid in h:
            st = h[pid].get("status", {})
            if st.get("status_str") == "error":
                import json as j
                print(editor, "ENGINE ERROR:",
                      j.dumps(st.get("messages", []))[:1200])
                sys.exit(1)
            if h[pid].get("outputs"):
                m = [i for o in h[pid]["outputs"].values()
                     for i in o.get("images", [])][0]
                img = Image.open(BytesIO(requests.get(
                    f"{cac.ENGINE_URL}/view",
                    params={"filename": m["filename"],
                            "subfolder": m.get("subfolder", ""),
                            "type": "output"}, timeout=60).content))
                img.save(RAW / out)
                print(f"{editor} OK in {time.time()-t0:.0f}s -> {out}")
                break
    else:
        print(editor, "TIMEOUT")
        sys.exit(1)
print("ALL PASS")
