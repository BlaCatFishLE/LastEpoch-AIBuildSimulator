# Last Epoch save id resolver (phase-2 save import)

Deterministic, dependency-free resolver that turns save-side **numeric identifiers** from a Last
Epoch offline character save into **human-readable names/effects**, with explicit coverage.
Built for the user save `1CHARACTERSLOT_BETA_2` (level 100 Mage/Sorcerer, expected to match the
imported planner build `o3ZRm9my`).

The **canonical mapping tables** live in this directory (`tools/save_import/data/mappings/`).
The generator is `tools/save_import/scripts/build_mappings.py` (with its `source-data/`
snapshots siblings); the resolution / verification helpers (`resolve.py`, `sanity_check.py`,
`apply_to_save.py`, `coverage_report.py`, `crosscheck_itemdecoder.py`) and the reports they
produce are research tooling, not part of the ordinary run path, and were archived on
2026-11-07 outside the repository — see `作者本地迁移对照表（不随公开仓库分发）` and
`作者本地归档说明（不随公开仓库分发）`. Everything reads the rest of the repository read-only and no name
is ever invented: every lookup returns an explicit `status: resolved | partial | unresolved`
with a reason. Restore path for the helpers:
`作者本地归档目录（不随公开仓库分发）`（`<archive>` = 本机归档根，见 `作者本地归档说明（不随公开仓库分发）`）。

## Files

| file | purpose |
|---|---|
| `build_mappings.py` | deterministic generator: dataset + game enums + szefo94 tables -> `mapping-*.json` |
| `resolve.py` | the reusable resolver (`Resolver` class + CLI); imports only stdlib + the mapping files |
| `sanity_check.py` | validates the resolver against the BuildSpec of build `o3ZRm9my` (353/353 checks incl. every affix id/tier/range/text) -> `sanity-report.json` |
| `apply_to_save.py` | resolves every id class in the user save using the item-decoder's authoritative item fields -> `save-resolution.json`, `SAVE-RESOLUTION.md` |
| `crosscheck_itemdecoder.py` | cross-checks the item-decoder deliverable against this resolver's reference byte decoder -> `crosscheck-report.json` |
| `coverage_report.py` | computes coverage from the mapping tables -> `coverage-report.json`, `COVERAGE.md` |
| `mapping-manifest.json` | source provenance + sha256 of every mapping file |
| `mapping-*.json` | the generated lookup tables (see below) |
| `source-data/enums/` | LE.dll enum snapshots used by the builder (`ContainerID`, `ContainerType`, `TimelineID`, `ItemRarity`) |
| `source-data/szefo94/` | copies of the MIT tables from `szefo94/last-epoch-save-editor` used by the builder |
| `COVERAGE.md` | per-id-class resolved/total counts, evidence and the explicit unresolved list |
| `SAVE-RESOLUTION.md` | human-readable resolution of the actual save |

## Rebuild / verify (Windows, no third-party Python packages)

```powershell
$py = '<repo>\.venv\Scripts\python.exe'
& $py tools\save_import\scripts\build_mappings.py     # regenerate mapping-*.json here (deterministic)
```

The resolver/verification helpers run from the archived research workspace
(`...\LastEpoch-BuildSimulator-archive\2026-11-07\scratch\save-import\resolver\`) where the
`mapping-*.json` working copies and report outputs sit beside them; point them at this directory
with their `--mapping-dir` argument.

`build_mappings.py` needs `data/current.json` + the referenced sqlite dataset and the saved
`source-data/` snapshots; it does not need the game install. Determinism: no timestamps, sorted
keys, byte-identical output for identical inputs.

## Usage

```python
import sys; sys.path.insert(0, r'<archive>\2026-11-07\scratch\save-import\resolver')
from resolve import Resolver, decode_planner_id

r = Resolver()

r.container(2)                          # EQ_HELMET -> planner equipment.head
r.class_name(1)                         # Mage
r.mastery(1, 1)                         # Sorcerer
r.skill_tree('bh2')                     # Black Hole (Mage / Sorcerer, version 0)
r.skill_tree_node('bh2', 2)             # Tidal Force + description + per-point stats
r.passive_node(1, 0)                    # Mage passive node 0 -> Arcanist (+stats)
r.weaver_node(10)                       # Tomes of Insight (out of planner scope)
r.ability('te44')                       # Teleport (movement, Mage)
r.unique(477)                           # Unsated Rage -> anchor Opal Ring
r.base_item(0, 23)                      # Runed Visage (Helmet)
r.affix(13, tier0=6)                    # Fire Resistance, +(61-75)%, suffix
r.blessing(116)                         # Grand Scales of Greed, timeline 1
r.resolve_planner_id('IIwBgzALMwGwvQ')  # planner blessing I-marker -> blessing record
r.describe_decoded_item({'base': 0, 'sub': 23, 'rarity': 9, 'unique': 316,
                         'affixes': [{'id': 13, 'tier0': 6, 'roll': 255}]})
```

CLI examples:

```powershell
& $py 作者本地归档目录（不随公开仓库分发）resolve.py container 2
& $py 作者本地归档目录（不随公开仓库分发）resolve.py node bh2 2
& $py 作者本地归档目录（不随公开仓库分发）resolve.py passive 1 0
& $py 作者本地归档目录（不随公开仓库分发）resolve.py affix 13 --tier0 6
& $py 作者本地归档目录（不随公开仓库分发）resolve.py planner IIwBgzALMwGwvQ
```

## Mapping tables

| file | key | value |
|---|---|---|
| `mapping-containers.json` | ContainerID int | enum name, functional group, `container_type` (only where unambiguous), content-validated planner role, evidence, observed save usage; plus `savedItems` index/position table and the materials `(-4,-4)` sentinel |
| `mapping-classes.json` | class index / class+mastery index | class and mastery names (`mg-1` style passive tree ids) |
| `mapping-skill-trees.json` | tree id (`bh2`, `mg-1`, `weaver`, ...) | tree name, kind, class, mastery, owner skill, version, node count |
| `mapping-skills.json` | ability id (`te44`, ...) | ability name, skill type, class, mastery |
| `mapping-skill-tree-nodes.json` | tree id -> node index | node name, description/alt text, stat values `{name,value,property,downside,no_scaling}`, requirements, position |
| `mapping-passive-nodes.json` | class tree id (`mg-1`, ...) -> node index | same node shape |
| `mapping-weaver-nodes.json` | weaver node index | same node shape (planner scope excluded; dataset resolves it anyway) |
| `mapping-affixes.json` | numeric affix id | scope (`singleAffixes`/`multiAffixes`), title, stat name, prefix/suffix slot, per-tier `{tier0, tier, roll_min, roll_max, level_requirement, text, text_source}` |
| `mapping-item-bases.json` | `<itemTypeIndex>:<baseSubTypeId>` | base item name, item type name, structured implicits, formatted implicit text, level req; includes idols (type 25-33) and blessings (type 34) |
| `mapping-uniques.json` | game unique id (2-byte big-endian in item data) | unique name, base anchor, allowed sub type ids, structured mods, szefo94 mods text, lore |
| `mapping-blessings.json` | blessing sub type id (item type 34) | blessing name, implicit definition, timeline membership; plus the 10 timelines with per-difficulty blessing slots |
| `mapping-properties.json` | property id per namespace | stat display names used to render node stats / implicits / unique mods |
| `mapping-item-rarity.json` | rarity byte 0..9 | rarity byte name + the game's displayed-rarity rule |

## Data sources and priority

1. **Phase-1 dataset** (`data/current.json` -> `phase1-dataset-003`, game 1.5.2): tree/node/affix/
   item-base/unique/blessing/ability identities, tier roll ranges, structured implicits/mods, and
   the game translation strings (`translations` table) for node names/descriptions/stats.
2. **`szefo94/last-epoch-save-editor` data tables** (MIT; vendored under `source-data/szefo94/`):
   formatted per-tier affix stat text, formatted base implicit text, unique mods text. Used as
   readable-text enrichment and cross-check; the 1.5.2 dataset wins on identity.
3. **LE.dll** (IL2CPP dummy assembly, enums preserved): `ContainerID`, `ContainerType`, `TimelineID`
   and `Achievements.Conditions.ItemRarity` enum names/values.
4. **Lead-verified ContainerID values** (`source-data/enums/container-id-map.json`, from the same
   LE.dll enum) and the user save pretty copies (archived: `...-archive\2026-11-07\scratch\save-recon\`) used only to annotate which
   container ids are observed.
5. **o3ZRm9my BuildSpec/import-report** used only for the sanity check (never as a name source).
6. **Item byte layout** (for `decode_item_bytes` / `describe_decoded_item`): ItemData serialisation
   version 6, reconstructed from two independent public parsers (`szefo94/last-epoch-save-editor`
   `lesave.py::decode`, `IgorBabayan/le-save-editor` `ItemDataReader.cs`) and verified against the
   sample build; reference-only here — the authoritative item decoder is the item-decoder teammate's
   deliverable. Fields resolved: `baseTypeID`(byte5), `subTypeID`(byte6), `rarity`(byte7),
   `uniqueID` big-endian (bytes 12-13 for rarity 7/8/9), affixes (12-bit id + 3-bit tier0) and the
   short-form non-equippable items (baseTypeID/subTypeID only) used by materials/keys stacks.

## Known limits / explicit gaps

See `COVERAGE.md` section 14. Summary:
- 46 affix ids above szefo94's max id (1112) have no published per-tier text; the dataset stat name
  is used as the readable text and the exact tier range is still provided.
- 43 unique ids absent from szefo94 have no formatted mods text; names/anchors + structured mods are
  still resolved from the dataset.
- Item type 105 has no published name in the 1.5.2 source (placeholder); no item of that type occurs
  in the save.
- `container_type` is only asserted where the ContainerType enum name is unambiguous; functional
  groups are curated from enum names and documented as such.
- Weaver tree content is resolved from the dataset but is out of the phase-2 planner scope.
- The ring1/ring2 side assignment is inferred from content match against the sample build (the
  planner does not record a physical side).
