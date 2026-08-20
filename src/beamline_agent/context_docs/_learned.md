# Agent-learned notes

Auto-appended by the pystream AI agent via `save_learned_note`. Review, promote to a curated tool doc if useful, then delete the entry from here.


## [tomogui] tomogui-cli writes recon output to &lt;parent&gt;_rec/, not the input dir   (2026-08-19T16:32:42)

When tomogui-cli runs a batch on `/some/path/DATA/`, the reconstructed `*_rec.h5` files are written to a sibling directory `/some/path/DATA_rec/`, NOT into the input directory. There is also a `try_center/` subfolder with the COR sweep outputs. Confirmed on tomo2 for `/data3/32ID/TMP/` → outputs in `/data3/32ID/TMP_rec/`.

Also: on tomo2 the login shell for usertxm is **tcsh** — any ssh command that needs bashisms (conda activate, &&, source) must be wrapped with `bash -lc "..."`. Quoting through ssh gets ugly fast; single-quote the outer ssh arg, then double-quote inside `bash -lc`.

The `rot_cen.json` and `recon_ai.log` stay in the INPUT directory though.

---

## [bl_gui] Motor moves on 32-ID go through `bl-cli`, not raw `caput`   (2026-08-20)

On bl32ID, EVERY motor move must go through the bl_gui / `bl-cli`
path — `bash("bl-cli motor set <PV> <VAL>")` — never raw
`caput <PV>.VAL`. Reason (from a live incident on 2026-08-20): a
raw caput to `32idbTXM:mcs2:c1:m4` accepted the value and updated
RBV, but the motor did not physically move — bl_gui handles motor
setup (regime / calibration / enable state) that raw caput skips.
The user's instruction was verbatim: "motors needs to be moved via
the bl_gui".

How to apply:
- Any "move motor X" / "position Y at Z" request on 32-ID →
  `bash: bl-cli motor set <PV> <VAL>` then `bl-cli motor wait <PV>`
  then `bl-cli motor rbv <PV>`.
- Any "set energy to N keV" → `bl-cli energy set <keV>` (coordinated
  ZP + QG via calibration), never raw caput to `EnergySet`.
- If a motor PV isn't in memory, look it up with
  `bl-cli layout motors --name bl32id --json`.
- Related unit fact: 32-ID motor RBVs are in **millimetres**, not
  µm. An RBV reading ~1e-6 is real (motor near zero), not a raw-
  units scaling artefact — do NOT rescale it.

---
