These fixtures retain original pixels inside the search ROIs declared in tasks/SixRealms/validated/image.json. Boss fixtures also retain the existing I_BATTLE_SKILL101 search ROI (5,83,235,51) to test confirmed Honglei ownership. Other pixels are zero. Cross-layout and second-capture tests use these fixtures; synthetic missing-button/marker cases are explicitly created by tests.

`boss_unmarked.png` is REAL_NEGATIVE_GREEN_MARK_FIXTURE. Source: `D:/Documents/MuMu共享文件夹/Screenshots/MuMu-20261004-021007-297.png`, original size 1280x720, SHA256 `1426f0f9a3831221fefe5312e131dd667dab822d251d320c24bce1f6f42882cf`. It is a genuine unmarked final-boss frame, not a marker-erased positive. The original is unchanged. Masking follows the existing ROI-only convention; expected battle=True, target=True, target mark=False.

`three_attribute_power_level_3_real.png` is the genuine stable attribute failure final frame, source `D:/Entertainment/Tool/OAS/log/screenshots/1791081818509.png`, timestamp 2026-10-04 10:43:38.509 +08:00, SHA256 `e1cceb73c28402cee8e4341c291880f1c6d751c41a55abc624e4e6d9df9a8e31`. The original is unchanged. Preserve all declared search ROIs plus legacy attribute-button search ROIs and the offered strength-card level badge (240,230,42,42). Expected THREE, strength/technique/charm all available. The card's displayed level is fixture provenance, not an inferred run-state counter.

### Original matcher replay (before the structural fix)

All scores use the actual RuleImage RGB TM_CCOEFF_NORMED and declared search ROIs; threshold is 0.9. The power-10 column is three_a. Three_b also passes all three titles/buttons. The new page variant is an exact crop from the real power-3 frame, not a transformed/generated template.

| Matcher | Power-10 three | Real power-3 three | Real two |
|---|---:|---:|---:|
| Three strength heading | 1.000000 | 0.870669 | -0.017131 |
| Three technique heading | 1.000000 | 0.889101 | 0.094545 |
| Three charm heading | 1.000000 | 0.843620 | 0.065688 |
| Three left button | 1.000000 | 0.901461 | 0.291278 |
| Three center button | 1.000000 | 0.932704 | -0.066191 |
| Three right button | 1.000000 | 0.903675 | 0.178607 |
| Two technique heading | 0.067244 | 0.114560 | 1.000000 |
| Two charm heading | 0.083693 | 0.076491 | 1.000000 |
| Two left button | 0.020182 | 0.033848 | 1.000000 |
| Two right button | 0.162030 | 0.135257 | 1.000000 |

The old three-layout hard gate required ALL three headings >0.9: True on three_a/b, False on the real power-3 frame. All three button slots pass on that same frame. Saved-frame minimum heading score is 0.8436 (charm), distinct from the log's 0.8568: the log is the maximum of per-frame minimum heading scores across ten probes; the nine other probe frames were not saved.

The heading ROIs cover y=334..368, excluding level badges near y=230..271 and description text beginning around y=382. Offered strength level/body changed, but those pixels do not enter heading matching. Best heading match positions shift by -2/-1/-1 px horizontally on the new frame; text edges/blue outline and in-ROI background pixels differ. The static captures cannot establish the precise rendering/capture cause. Button positions differ only -1/0/+1 px within the original search windows.

Synthetic missing-slot, slot-only, ordinary four-choice and conflicting-pattern negatives are explicitly assembled in tests; they are not represented as real screenshots. These scores document the original rendering investigation; the current simplified runtime has no exact power state, layout reconciliation or post-click transaction.

## Real TWO false-negative replay

`two_attribute_render_variant_real.png` is the masked real failure final frame from `D:/Entertainment/Tool/OAS/log/screenshots/1791089468816.png`, native 1280x720, captured 2026-10-04 12:51:08.816 +08:00, SHA256 `79456a46db0f8ee824f74c43f55df7eef645229c0aea9e19d581b554f79e1059`. All declared search ROIs and the two legacy peacock search ROIs preserve source pixels; other pixels are zero. The source is unchanged. Expected TWO with left technique/right charm; current decisions read the actual technique badge, not a stored counter. A single heading variant at the existing technique-title ROI supplies page identity; fixed button geometry and two-frame stability remain required.

| Existing matcher | Original validated TWO | Real failed TWO | Failed best position |
|---|---:|---:|---|
| Technique heading | 1.000000 | 0.886314 | (414,334) |
| Charm heading | 1.000000 | 0.896401 | (768,334) |
| Left choose button | 1.000000 | 0.936647 | (424,505) |
| Right choose button | 1.000000 | 0.931671 | (779,505) |
| Strength page variant | -0.003641 | 0.008316 | (232,336) |
| Legacy peacock1 hint, threshold0.8 | 0.281164 | 0.246934 | (248,515) |
| Legacy peacock2 hint, threshold0.8 | 0.935453 | 0.999016 | (779,501) |

Two heading best positions move -1px in x, within existing search windows. NCC already searches that shift, so translation alone does not explain the remaining scores below 0.9. Fixed-ROI RGB mean absolute differences are 26.651/26.993 intensity units; text edges, outline and local blue background differ. The ROIs cover y334..368, excluding level badges and body text. Static captures cannot isolate anti-aliasing, capture sampling or animated glow as the precise cause. This is the same labelled heading rendered differently, not evidence that the left/right choices changed.

`highest_two_score=0.9195` in the original timeout log is the maximum per-frame minimum heading score over ten probes, not a full two-slot/two-frame acceptance signal. Only the final probe was saved, and its minimum heading score is 0.886314. New tests replay repeated fixture frames offline; they do not claim an observed live temporal sequence. Heading-erased, missing-slot, ordinary-button and single-frame negatives are explicitly synthetic.

`attribute_reward_popup_real.png` is the real attribute-selection reward popup. Source: `D:/Documents/MuMu共享文件夹/Screenshots/MuMu-20261004-113148-281.png`, native 1280x720, SHA256 `3f4bd621faa988bc970827374687b6754e11dd5733b1fdb72c021933dcd9ed92`. The original is unchanged. Preserve only title search ROI (529,122,235,74) and close-hint search ROI (541,646,184,45); all reward contents are masked. Templates use title (539,132,215,54) and stable close hint (551,656,164,25), threshold 0.9. The shared I_UI_REWARD title scores only 0.326177 on this capture (below its helper threshold 0.6), so the simplified outer loop uses local popup identity with the existing C_UI_REWARD blank-area action; this is not a selection transaction. Tests explicitly replace/mask reward contents; they are never confirmation evidence.

## Real summon fixtures (2026-10-04)
Unmodified 1280x720 copies from D:/Documents/MuMu共享文件夹/Screenshots/:
- summon_map_real.png <- MuMu-20261004-140029-481.png (active summon300 button)
- summon_confirm_real.png <- MuMu-20261004-140032-007.png (normal300 confirmation)
- summon_rest_real.png <- MuMu-20261004-140035-664.png (summoned Ningxi on map)
- summon_insufficient_real.png <- MuMu-20261004-140143-450.png (insufficient currency, separate enter outcome)
Original user files are unchanged. These static frames validate UI identities, not interrupted-visit recovery or actual purchasing eligibility after insufficient currency.

## Whole-frame attribute replay (2026-10-04)
- three_attribute_full_scene_real.png: byte-identical copy of D:/Entertainment/Tool/OAS/log/screenshots/1791081818509.png, SHA256 e1cceb73c28402cee8e4341c291880f1c6d751c41a55abc624e4e6d9df9a8e31.
- two_attribute_full_scene_real.png: byte-identical copy of D:/Entertainment/Tool/OAS/log/screenshots/1791089468816.png, SHA256 79456a46db0f8ee824f74c43f55df7eef645229c0aea9e19d581b554f79e1059.
These native1280x720 copies supplement the original masked fixtures; neither originals nor old fixtures/templates are changed. The full-frame TWO also retains the genuine technique level1 badge for the new small-ROI OCR regression. Old transition-probe tests have been backed up and removed from the active suite. The map15 fixtures are mid-run (ordinary completed4 under remaining19-at-start semantics), not fresh-run captures.

## Real TWO technique level2 (2026-10-05)
`two_technique_level_2_real.png` is a byte-identical native1280x720 copy of `D:/Entertainment/Tool/OAS/log/screenshots/sixrealms_attribute_transition/b12efac1441a4422919023db02ecf092/pre_click.png`, SHA256 `ee8d5f45e45aaa78fb4755afbd90f0b939d180fbf8b78a5ac0e708635139d5fd`. The original is unchanged. The left technique badge visibly reads2. Existing single-line CPU OCR at(431,236,19,24), Default, reads2 with confidence0.99989. `two_attribute_full_scene_real.png` is genuine Lv1 and reads1 with confidence0.99779 at the same ROI. No real TWO Lv3 capture has been found; Lv3 policy tests are unit tests, not fabricated real fixtures.

The original boss_a screenshot displays Honglei Lv1; boss_unmarked displays Lv3. The unchanged battle icon body matcher returns0.977802 on both. Digit-edit tests, if used, are explicitly synthetic invariance checks; no real battle Lv2 fixture is claimed.
