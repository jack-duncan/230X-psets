"""Render ps1/report/ps1_report.md to a self-contained HTML with every figure embedded."""
import base64, re
from pathlib import Path
import mistune

ROOT = Path(__file__).resolve().parents[1]
md = (ROOT / "report" / "ps1_report.md").read_text()
body = mistune.create_markdown(plugins=["table", "strikethrough"])(md)
names = []
for n in re.findall(r"((?:fx|eq)_[a-z0-9_]+\.png)", md):
    if n not in names:
        names.append(n)
for extra in sorted((ROOT / "figures").glob("*.png")):
    if extra.name not in names:
        names.append(extra.name)
figs = []
for n in names:
    b64 = base64.b64encode((ROOT / "figures" / n).read_bytes()).decode()
    figs.append(f'<figure><img src="data:image/png;base64,{b64}" alt="{n}"><figcaption>{n}</figcaption></figure>')
html = f"""<!doctype html><html><head><meta charset="utf-8"><title>MFE 230X Homework 1</title>
<style>
body{{max-width:860px;margin:2.5rem auto;padding:0 1.5rem;font:15px/1.5 Georgia,serif;color:#111}}
h1{{font-size:1.6rem}} h2{{font-size:1.25rem;margin-top:2rem}} h3{{font-size:1.05rem}}
table{{border-collapse:collapse;font-size:12.5px;margin:1rem 0;width:100%}} th,td{{border:1px solid #ccc;padding:3px 7px;text-align:right}}
th:first-child,td:first-child{{text-align:left}} code{{font-size:0.9em;background:#f3f3f3;padding:0 3px}}
figure{{margin:1.5rem 0;page-break-inside:avoid}} img{{max-width:100%}} figcaption{{font-size:12px;color:#555;font-family:monospace}}
@media print{{body{{max-width:none;margin:0;font-size:11.5pt}} h2{{page-break-after:avoid}}}}
</style></head><body>{body}
<h2>Appendix — all figures</h2>{"".join(figs)}</body></html>"""
(ROOT / "submission" / "ps1_report.html").write_text(html)
print(f"wrote ps1/submission/ps1_report.html ({len(html) // 1024} KB, {len(figs)} figures)")
