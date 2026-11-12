-- Schema v1.1: fields required by the phase-1 review (P1-6/P2-1) and stat linking (P1-2).
-- Only additive columns; existing queries keep working.

-- Buffs: UI display values must be separated from confirmed mechanics.
ALTER TABLE buffs ADD COLUMN ui_max_instances INTEGER;
ALTER TABLE buffs ADD COLUMN max_stacks_basis TEXT;   -- raw_ui | node_text | confirmed | unknown
ALTER TABLE buffs ADD COLUMN base_duration_ms INTEGER;
ALTER TABLE buffs ADD COLUMN duration_basis TEXT;     -- node_text | confirmed | unknown
ALTER TABLE buffs ADD COLUMN ui_flags TEXT;           -- noFixedDuration/newStacksRefreshDuration raw flags

-- Effects: buff parameter candidates (node -> buff base/modification evidence).
ALTER TABLE effects ADD COLUMN effect_scope TEXT;     -- node|skill|item|buff|...

-- Effect values: stat linking classification (P1-2).
ALTER TABLE effect_values ADD COLUMN stat_link_status TEXT;  -- linked|composite|unknown_namespace|unparsed|not_applicable
ALTER TABLE effect_values ADD COLUMN stat_link_evidence TEXT;

-- Masteries: distinguish base class branch rows from selectable masteries.
ALTER TABLE masteries ADD COLUMN branch_kind TEXT;    -- base_class | mastery | unknown
ALTER TABLE masteries ADD COLUMN is_base INTEGER;

-- Items: game semantics separate from source category (P2-1).
ALTER TABLE items ADD COLUMN is_set INTEGER;
ALTER TABLE items ADD COLUMN gear_category TEXT;      -- weapon|armor|accessory|idol|blessing|lens|other
ALTER TABLE items ADD COLUMN equip_slot TEXT;         -- helm|body|belt|boots|gloves|weapon|offhand|amulet|ring|relic|idol|...

-- Affixes: prefix/suffix is a different dimension from single/multi source category.
ALTER TABLE affixes ADD COLUMN affix_slot TEXT;       -- prefix | suffix | unknown
