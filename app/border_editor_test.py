"""Integration test: border generated from a reference via the editor,
through the real Generator.run plumbing (collage -> edit -> cut)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import comic_art_creator as cac

RAW = Path(__file__).parent.parent / "output" / "_raw"
BORDER = Path(__file__).parent.parent / "border"


class Q:
    def __init__(self):
        self.items = []

    def put(self, x):
        self.items.append(x)


q = Q()
gen = cac.Generator(q)
template = cac.BORDER_TEMPLATES["Franchise (game / movie / comic)"]
params = dict(
    prompt="redraw this image as " + template.format(
        theme="anime spirit detectives with glowing energy"),
    user_prompt="test", style="border frame", negative=cac.BORDER_NEGATIVE,
    model="editor:kontext", loras=[], width=1280, height=720, seed=99,
    steps=None, cfg=None, batch=1, random_seed=False, transparent=False,
    preset="border maker", border_cut=14,
    ref_images=[str(BORDER / "YuYuHakusho.png")], editor="kontext",
    ref_collage_size=(1280, 720))
gen.run(params)
imgs = [m for m in q.items if m[0] == "image"]
errs = [m for m in q.items if m[0] == "error"]
if errs:
    print("ERROR:", errs)
    sys.exit(1)
assert imgs, f"no image produced; messages: {[m[0] for m in q.items]}"
out = cac.cut_center(imgs[0][1], 14)
out.save(RAW / "border_editor_test.png")
a = out.getchannel("A")
print("OK size:", out.size, "center alpha:",
      a.getpixel((out.width // 2, out.height // 2)))
