# Wanted desktop board regression fixtures

The three `desktop_*.png` files are native 1280x720 user captures from
2026-11-04: real-world gold, ordinary stamina, and ordinary dog-food cooperation.
The top-left 410x55 identity/status strip is masked. The low-resolution partial
clipboard reference was excluded from templates and recognition evidence.

`real_cooperation.png`, `jade_with_mystery.png`, and `double_cooperation.png`
are earlier native captures used to check the retained layout and simulate
real-world stamina/jade and ordinary cat-food rewards at desktop size. These
simulations are not new real-game captures. Per-file sources, SHA256 values,
and masking regions are recorded in `provenance.json`.

Desktop markers sit to the left of character artwork; card pitch is 268 pixels.
Fixed title, close, footer, marker and invite templates are crops from the two
original desktop captures. Reward templates derive from existing reward assets
scaled to 95% with OpenCV linear interpolation. Threshold remains 0.8, with gold
checked after food. Derivations are in
`tasks/DailyAltAcc/cooperation_v2/desktop_provenance.json`.

Run from the repository root with an environment containing OpenCV and NumPy:

```
python -B -m unittest discover -s dev_tools -p test_wanted_desktop_board.py -q
```

The standalone test compiles production classes/methods without device/config
imports. It checks native screenshots, four-slot simulations, absent invitations,
unknown markers, resolution, old-layout compatibility and production OCR call
regions. OCR results are mocked; no device, real OCR or invitation action runs.
