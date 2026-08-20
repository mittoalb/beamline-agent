# Condensers (32-ID TXM)

Three condensers are configured in `optics_config.json`. Selection happens
in the TXM Optics Calculator (`bl_gui` / `optics_calc`); each profile has
its own geometry, NA, and position offset.

| Name     | Source dist [mm] | Inner Ø [μm] | Outer Ø [μm] | Length [mm] | Focal [mm] | NA tip [mrad] | Position offset [mm] |
|----------|------------------|--------------|--------------|-------------|------------|---------------|----------------------|
| Sigray   | 35               | 450          | 750          | 80          | 43.2       | 5.208         | 100                  |
| Sigray2  | 35               | 475          | 749          | 145         | 96.9       | 2.451         | 150                  |
| Zeiss    | 35000.0          | 850          | 1000         | 74          | 287.0      | 1.48          | 100                  |

Notes:
- "Source dist" is in mm — the Zeiss value (35000) is the Storage Ring
  source-to-sample distance; Sigray/Sigray2 are inserted optics 35 mm
  upstream of the sample plane.
- The active condenser is read by the optics calculator from the dropdown
  in `bl_gui`. It is NOT broadcast as an EPICS PV today — to know which one
  is in the beam, ask the on-shift scientist or check `bl_gui`.
- Each profile drives a different downstream geometry (the optics
  calculator picks `focal_length` and `position_offset` from this table).

Source of truth: `~/Software/txm_calc/optics_config.json` →
`condensers` block.
