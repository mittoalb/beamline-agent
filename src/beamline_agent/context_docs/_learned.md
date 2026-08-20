# Agent-learned notes

Auto-appended by the pystream AI agent via `save_learned_note`. Review, promote to a curated tool doc if useful, then delete the entry from here.


## [tomogui] tomogui-cli writes recon output to &lt;parent&gt;_rec/, not the input dir   (2026-08-19T16:32:42)

When tomogui-cli runs a batch on `/some/path/DATA/`, the reconstructed `*_rec.h5` files are written to a sibling directory `/some/path/DATA_rec/`, NOT into the input directory. There is also a `try_center/` subfolder with the COR sweep outputs. Confirmed on tomo2 for `/data3/32ID/TMP/` → outputs in `/data3/32ID/TMP_rec/`.

Also: on tomo2 the login shell for usertxm is **tcsh** — any ssh command that needs bashisms (conda activate, &&, source) must be wrapped with `bash -lc "..."`. Quoting through ssh gets ugly fast; single-quote the outer ssh arg, then double-quote inside `bash -lc`.

The `rot_cen.json` and `recon_ai.log` stay in the INPUT directory though.

---
