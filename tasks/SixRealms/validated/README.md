# SixRealms verified screenshot resources

All rectangles are x,y,width,height in the original 1280x720 game image.
Templates are exact crops, without rescaling or generated pixels. Fixtures preserve the search ROIs and mask unrelated pixels.

Sources:
- three_a: MuMu-20261004-011931-301.png, SHA256 aca56815fdc64343a9099de5b3044d64a2e4ee651f409bea116efc382ea87db8
- three_b: MuMu-20261004-012347-564.png, SHA256 a432b7a4268d99ae27ba5b4da6ba038b9c4a1ca6a589bd40f4d261fb2502bc1a
- two: MuMu-20261004-012358-673.png, SHA256 78eb13f3cfdecd420fed0abbac3f87e6061de0c747a163d74de8ec9a1ab29f73
- boss_a: MuMu-20261004-011946-897.png, SHA256 697f1c5be37bc9948e7c3a6e7bda21254c8b79ceec34aeb893cda4e80987bb19
- boss_b: MuMu-20261004-011953-524.png, SHA256 d6f7bd55d37476eb183ea32fbc9bbaf3ce565f160ac22816c4286820d1a09673
- boss_unmarked: MuMu-20261004-021007-297.png, SHA256 1426f0f9a3831221fefe5312e131dd667dab822d251d320c24bce1f6f42882cf; real negative, also source of jue_target_unmarked.png at the unchanged (604,501,70,66) ROI.
- three_attribute_power_level_3_real: OAS/log/screenshots/1791081818509.png, captured 2026-10-04 10:43:38.509 +08:00, SHA256 e1cceb73c28402cee8e4341c291880f1c6d751c41a55abc624e4e6d9df9a8e31. Source of attribute_page_strength_alt.png at (236,334,114,35), search (232,330,122,43), threshold still 0.9; a rendering variant of the strength heading only, no level badge or body text.

## Evidence limits
- boss_a/b are real marked positives; boss_unmarked is a real unmarked negative. The existing green-marker template correctly rejects the negative and is unchanged.
- The user supplied a manually confirmed hit REGION. The 200x129 small image is not an exact crop of any supplied full screenshot (best normalized match <0.35).
- Target body templates are detection-only. Their centers are NOT used as safe click points. The original two poses missed the new negative's model; a third exact crop adds that pose without changing ROIs or thresholds.
- The small region cannot establish exact registration. Instead, the user captured OAS 1280x720 coordinates and manually verified each hit: P1=(625,544), P2=(624,526), P3=(631,569), in that order. JUE_SAFE_POINTS is now enabled with those points.
- P3 is below the detection ROI and remains valid: detection bounds and the actual hitbox are distinct. Resources still cover only the supplied boss/central-target formation; automated clicking and confirmation await final real-game acceptance.
- At least one attribute heading (including the real power-3 and real TWO rendering variants) establishes page identity. Complete fixed button patterns establish THREE/TWO; individual original heading matches are auxiliary diagnostics. A slot-only image without heading evidence is rejected. All existing templates/ROIs/thresholds are unchanged.
- Card bounds observed: three x=191/546/902, two x=368/725, y=210, approximate width=202,height=385; these are documentation only, not click/search rectangles.

## Real TWO heading rendering variant

`attribute_page_technique_alt.png` is an exact, unscaled crop of `OAS/log/screenshots/1791089468816.png`, captured 2026-10-04 12:51:08.816 +08:00, SHA256 `79456a46db0f8ee824f74c43f55df7eef645229c0aea9e19d581b554f79e1059`. Front `(415,334,114,35)`, search `(411,330,122,43)`, threshold 0.9: identical to the existing TWO technique-heading rectangles and threshold. Only this one heading variant is added; no charm variant, body/level template, OCR or larger search. The heading provides independent attribute-page identity; the two original fixed button slots determine TWO structure and choice mapping. The original technique/charm headings remain diagnostic and continue to recognise the original validated TWO.

The old `I_PEACOCK_SKILL2` matches the new frame (0.999016), but its pixels contain only the common choose button. It is not used as independent attribute-page identity in the simplified flow. The original source and every old template are unchanged.

## Summon dialog identities (2026-10-04)
- summon_confirm_title.png: exact crop (560,302,231,30) from D:/Documents/MuMu共享文件夹/Screenshots/MuMu-20261004-140032-007.png. Search (556,298,239,38), NCC threshold0.9.
- summon_insufficient_title.png: exact crop (429,289,222,28) from D:/Documents/MuMu共享文件夹/Screenshots/MuMu-20261004-140143-450.png. Search (425,285,230,36), NCC threshold0.9.
- These two title identities are mutually exclusive on the supplied real frames. Existing confirm/enter buttons are reused; button-only detection cannot distinguish the dialogs. No attribute/reward/BOSS template was changed.

## Simplified runtime (2026-10-05)
All22 existing PNGs remain useful:12 attribute slots/headings/render variants,2 generic reward identities,2 real boss identities,4 central Jue target/mark templates and2 summon dialog identities. Runtime no longer uses state transactions, layout-phase reconciliation or transition probes. Attribute dispatch returns directly to the original outer loop.

TWO technique level uses existing single-line OCR at (431,236,19,24), Default, confidence>=0.9, no new digit template. Real Lv1 and Lv2 are verified; no real Lv3 fixture exists yet. Original battle_skill101.png front(86,90,30,41)/search(5,83,235,51)/threshold0.8 already excludes the level digit body. Real battle Lv1/Lv3 NCC is0.977802 for both; the old image and ROI are unchanged.
