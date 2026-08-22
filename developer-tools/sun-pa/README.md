# SUN_PA FITS Header Tool

`add_sun_pa_header.py` appends non-standard solar geometry keywords to an existing
moving-target FITS stack without rewriting its image data:

- `SUN_PA`: position angle of the Sun, measured from celestial north through east.
- `ASUN_PA`: anti-solar position angle (`SUN_PA + 180 deg`).
- `SUNRA`, `SUNDEC`, `SUNCENTR`, `SUNSRC`: reproducibility metadata.

The target coordinate is read from the Metcalf stack's `MTREFRA` / `MTREFDEC`, and
the timestamp is its `DATE-OBS`. The tool queries JPL Horizons for the Sun at that
same time. `fits-site` sends the supplied observatory coordinates to JPL.

```powershell
cd developer-tools\sun-pa
.\run-add-sun-pa-header.cmd "D:\path\moving_target_metcalf_stack.fit" `
  --center fits-site --site-longitude 139.535 --site-latitude 35.7147
```

Use `--dry-run` to inspect values before modifying a FITS file. `geocenter` is the
default and does not require site coordinates.
