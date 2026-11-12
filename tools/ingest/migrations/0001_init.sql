-- Schema v1: normalized Last Epoch phase-1 database.
-- One database file == one dataset snapshot (workguide 7.1).
-- Conventions:
--   * *_id columns are stable internal TEXT ids, never SQLite rowids or names;
--   * unknown values are NULL plus a field_evidence row stating known/absent/
--     unparsed/conflict; 0 and '' are only used when the source really says 0/'';
--   * percentages are stored as the normalized ratio (0.18) with the raw value
--     in field_evidence.raw_value;
--   * time values are milliseconds in *_ms columns.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dataset_meta (
    dataset_id              TEXT PRIMARY KEY,
    schema_version          TEXT NOT NULL,
    normalizer_version      TEXT NOT NULL,
    mapping_revision        TEXT NOT NULL,
    status                  TEXT NOT NULL,            -- staging|valid|published|failed|partial
    created_at              TEXT NOT NULL,
    target_game_version     TEXT,
    target_season           TEXT,
    compatibility_conclusion TEXT,
    notes                   TEXT
);

CREATE TABLE IF NOT EXISTS dataset_source_snapshots (
    dataset_id          TEXT NOT NULL REFERENCES dataset_meta(dataset_id),
    source_snapshot_id  TEXT NOT NULL,
    source              TEXT NOT NULL,
    PRIMARY KEY (dataset_id, source_snapshot_id)
);

CREATE TABLE IF NOT EXISTS source_snapshots (
    source_snapshot_id  TEXT PRIMARY KEY,
    source              TEXT NOT NULL,
    claimed_version     TEXT,
    version_evidence    TEXT,                          -- JSON
    fetch_mode          TEXT,                          -- auto|manual|cache|mixed
    fetch_started_at    TEXT,
    fetch_finished_at   TEXT,
    status              TEXT NOT NULL,                 -- ok|partial|failed|blocked
    entry_url           TEXT,
    local_game_build    TEXT,
    notes               TEXT
);

CREATE TABLE IF NOT EXISTS raw_resources (
    resource_id         TEXT PRIMARY KEY,
    source_snapshot_id  TEXT NOT NULL REFERENCES source_snapshots(source_snapshot_id),
    source              TEXT NOT NULL,
    resource_name       TEXT NOT NULL,
    url                 TEXT,
    local_path          TEXT,
    sha256              TEXT,
    size_bytes          INTEGER,
    content_type        TEXT,
    http_status         INTEGER,
    etag                TEXT,
    last_modified       TEXT,
    fetched_at          TEXT,
    fetch_status        TEXT NOT NULL,                 -- fetched|not_modified|cache_hit|failed
    role                TEXT,
    notes               TEXT,
    UNIQUE (source_snapshot_id, resource_name)
);

CREATE TABLE IF NOT EXISTS source_records (
    record_id           TEXT PRIMARY KEY,
    source_snapshot_id  TEXT NOT NULL REFERENCES source_snapshots(source_snapshot_id),
    source              TEXT NOT NULL,
    resource_name       TEXT,
    entity_kind         TEXT NOT NULL,
    source_scope        TEXT NOT NULL,
    source_id           TEXT NOT NULL,
    locator             TEXT,
    raw_payload         TEXT NOT NULL,                 -- JSON, complete source object
    parse_status        TEXT NOT NULL,                 -- parsed|unparsed|failed|skipped
    notes               TEXT,
    UNIQUE (source_snapshot_id, entity_kind, source_scope, source_id)
);

CREATE INDEX IF NOT EXISTS ix_source_records_kind
    ON source_records (entity_kind, source_snapshot_id);

CREATE TABLE IF NOT EXISTS entities (
    entity_id       TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    canonical_name  TEXT,
    record_status   TEXT NOT NULL,                     -- active|provisional|unresolved|merged
    primary_source  TEXT,
    extensions      TEXT,                              -- JSON, raw extras that are not typed
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_entities_kind ON entities (kind);
CREATE INDEX IF NOT EXISTS ix_entities_name ON entities (canonical_name);

CREATE TABLE IF NOT EXISTS entity_names (
    name_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id   TEXT NOT NULL REFERENCES entities(entity_id),
    language    TEXT NOT NULL,                         -- en|zh|und
    name        TEXT NOT NULL,
    is_alias    INTEGER NOT NULL DEFAULT 0,
    source      TEXT,
    name_key    TEXT,
    UNIQUE (entity_id, language, name)
);

CREATE INDEX IF NOT EXISTS ix_entity_names_name ON entity_names (name);

CREATE TABLE IF NOT EXISTS source_id_map (
    mapping_id          TEXT PRIMARY KEY,
    source_snapshot_id  TEXT NOT NULL,
    source              TEXT NOT NULL,
    kind                TEXT NOT NULL,
    source_scope        TEXT NOT NULL,
    source_id           TEXT NOT NULL,
    entity_id           TEXT NOT NULL REFERENCES entities(entity_id),
    mapping_method      TEXT NOT NULL,                 -- direct|source_authoritative|crosswalk|name_in_scope
    evidence            TEXT,
    created_at          TEXT NOT NULL,
    UNIQUE (source_snapshot_id, kind, source_scope, source_id)
);

CREATE INDEX IF NOT EXISTS ix_source_id_map_entity ON source_id_map (entity_id);

CREATE TABLE IF NOT EXISTS classes (
    entity_id           TEXT PRIMARY KEY REFERENCES entities(entity_id),
    source_class_id     TEXT,
    unlock_notes        TEXT,
    extensions          TEXT
);

CREATE TABLE IF NOT EXISTS masteries (
    entity_id               TEXT PRIMARY KEY REFERENCES entities(entity_id),
    class_entity_id         TEXT REFERENCES entities(entity_id),
    source_mastery_id       TEXT,
    unlock_notes            TEXT,
    extensions              TEXT
);

CREATE TABLE IF NOT EXISTS skills (
    entity_id           TEXT PRIMARY KEY REFERENCES entities(entity_id),
    skill_type          TEXT,                          -- active|passive|proc|summon|movement|...
    class_entity_id     TEXT REFERENCES entities(entity_id),
    mastery_entity_id   TEXT REFERENCES entities(entity_id),
    source_skill_id     TEXT,
    cast_time_ms        INTEGER,
    mana_cost           REAL,
    cooldown_ms         INTEGER,
    channel_time_ms     INTEGER,
    tags_raw            TEXT,                          -- original tag bitmask/list JSON
    extensions          TEXT
);

CREATE TABLE IF NOT EXISTS skill_components (
    component_id        TEXT PRIMARY KEY,
    skill_entity_id     TEXT NOT NULL REFERENCES entities(entity_id),
    component_type      TEXT,
    component_index     INTEGER,
    locator             TEXT,
    damage_type         TEXT,
    base_damage         REAL,
    added_damage_effectiveness REAL,
    config              TEXT,                          -- JSON, full component object
    extensions          TEXT
);

CREATE INDEX IF NOT EXISTS ix_skill_components_skill ON skill_components (skill_entity_id);

CREATE TABLE IF NOT EXISTS trees (
    entity_id           TEXT PRIMARY KEY REFERENCES entities(entity_id),
    tree_kind           TEXT NOT NULL,                 -- skill|passive|weaver|other
    owner_entity_id     TEXT REFERENCES entities(entity_id),
    class_entity_id     TEXT REFERENCES entities(entity_id),
    source_tree_id      TEXT,
    node_count          INTEGER,
    extensions          TEXT
);

CREATE TABLE IF NOT EXISTS nodes (
    entity_id           TEXT PRIMARY KEY REFERENCES entities(entity_id),
    tree_entity_id      TEXT NOT NULL REFERENCES entities(entity_id),
    node_type           TEXT,
    max_points          INTEGER,
    node_index          INTEGER,
    position_x          REAL,
    position_y          REAL,
    position_w          REAL,
    position_h          REAL,
    description         TEXT,
    extensions          TEXT
);

CREATE INDEX IF NOT EXISTS ix_nodes_tree ON nodes (tree_entity_id, node_index);

CREATE TABLE IF NOT EXISTS node_edges (
    edge_id             TEXT PRIMARY KEY,
    tree_entity_id      TEXT NOT NULL REFERENCES entities(entity_id),
    from_node_entity_id TEXT REFERENCES entities(entity_id),
    to_node_entity_id   TEXT REFERENCES entities(entity_id),
    requirement_type    TEXT,
    points_required     INTEGER,
    raw                 TEXT,
    UNIQUE (tree_entity_id, from_node_entity_id, to_node_entity_id, requirement_type)
);

CREATE TABLE IF NOT EXISTS tags (
    tag_id      TEXT PRIMARY KEY,
    name        TEXT,
    semantics   TEXT,
    raw_value   TEXT,
    extensions  TEXT
);

CREATE TABLE IF NOT EXISTS entity_tags (
    entity_id   TEXT NOT NULL REFERENCES entities(entity_id),
    tag_id      TEXT NOT NULL REFERENCES tags(tag_id),
    source      TEXT,
    raw_bits    TEXT,
    PRIMARY KEY (entity_id, tag_id)
);

CREATE TABLE IF NOT EXISTS stats (
    stat_id         TEXT PRIMARY KEY,
    name            TEXT,
    unit            TEXT,
    description     TEXT,
    raw_enum        TEXT,
    source_scope    TEXT,
    extensions      TEXT
);

CREATE TABLE IF NOT EXISTS effects (
    effect_id           TEXT PRIMARY KEY,
    owner_entity_id     TEXT REFERENCES entities(entity_id),
    owner_component_id  TEXT,
    source_entity_id    TEXT,
    effect_category     TEXT,                          -- stat_mod|buff_grant|trigger|convert|ailment|summon|unknown
    raw_text            TEXT,
    parse_status        TEXT,
    extensions          TEXT
);

CREATE INDEX IF NOT EXISTS ix_effects_owner ON effects (owner_entity_id);

CREATE TABLE IF NOT EXISTS effect_values (
    value_id        TEXT PRIMARY KEY,
    effect_id       TEXT NOT NULL REFERENCES effects(effect_id),
    stat_id         TEXT REFERENCES stats(stat_id),
    value_kind      TEXT,                              -- flat|increased|more|conversion|chance|duration|...
    value_numeric   REAL,
    value_min       REAL,
    value_max       REAL,
    per_level       REAL,
    level           INTEGER,
    tier            INTEGER,
    unit            TEXT,
    expression      TEXT,
    raw             TEXT
);

CREATE INDEX IF NOT EXISTS ix_effect_values_effect ON effect_values (effect_id);

CREATE TABLE IF NOT EXISTS buffs (
    entity_id           TEXT PRIMARY KEY REFERENCES entities(entity_id),
    buff_category       TEXT,
    duration_ms         INTEGER,
    max_stacks          INTEGER,
    target_scope        TEXT,                          -- player|minion|enemy|unknown
    lifecycle           TEXT,                          -- JSON: refresh/consume/stack rules, raw text
    extensions          TEXT
);

CREATE TABLE IF NOT EXISTS ailments (
    entity_id           TEXT PRIMARY KEY REFERENCES entities(entity_id),
    damage_type         TEXT,
    base_damage         REAL,
    duration_ms         INTEGER,
    stack_rule          TEXT,
    scaling_tags        TEXT,
    extensions          TEXT
);

CREATE TABLE IF NOT EXISTS items (
    entity_id           TEXT PRIMARY KEY REFERENCES entities(entity_id),
    item_category       TEXT,                          -- base|unique|set|idol|...
    item_type           TEXT,                          -- weapon/armor/...
    base_type           TEXT,
    is_unique           INTEGER,
    level_requirement   INTEGER,
    class_requirement   TEXT,
    implicit_text       TEXT,
    extensions          TEXT
);

CREATE TABLE IF NOT EXISTS affixes (
    entity_id           TEXT PRIMARY KEY REFERENCES entities(entity_id),
    affix_type          TEXT,                          -- prefix|suffix|idol|corruption|...
    source_affix_id     TEXT,
    affix_title         TEXT,
    group_id            TEXT,
    levels              TEXT,                          -- JSON list of level requirements per tier
    can_roll_on         TEXT,                          -- JSON
    affix_display_name  TEXT,
    extensions          TEXT
);

CREATE TABLE IF NOT EXISTS affix_tiers (
    tier_id             TEXT PRIMARY KEY,
    affix_entity_id     TEXT NOT NULL REFERENCES entities(entity_id),
    tier_index          INTEGER NOT NULL,
    roll_min            REAL,
    roll_max            REAL,
    extra_rolls         TEXT,                          -- JSON
    level_requirement   INTEGER,
    extensions          TEXT,
    UNIQUE (affix_entity_id, tier_index)
);

CREATE TABLE IF NOT EXISTS item_affixes (
    item_entity_id      TEXT NOT NULL REFERENCES entities(entity_id),
    affix_entity_id     TEXT NOT NULL REFERENCES entities(entity_id),
    relation            TEXT NOT NULL,                 -- implicit|explicit|unique_mod|set_mod
    display_order       INTEGER,
    raw                 TEXT,
    PRIMARY KEY (item_entity_id, affix_entity_id, relation)
);

CREATE TABLE IF NOT EXISTS set_memberships (
    set_entity_id       TEXT NOT NULL REFERENCES entities(entity_id),
    item_entity_id      TEXT NOT NULL REFERENCES entities(entity_id),
    raw                 TEXT,
    PRIMARY KEY (set_entity_id, item_entity_id)
);

CREATE TABLE IF NOT EXISTS idols (
    entity_id           TEXT PRIMARY KEY REFERENCES entities(entity_id),
    idol_size           TEXT,
    restrictions        TEXT,
    extensions          TEXT
);

CREATE TABLE IF NOT EXISTS blessings (
    entity_id           TEXT PRIMARY KEY REFERENCES entities(entity_id),
    blessing_type       TEXT,
    restrictions        TEXT,
    extensions          TEXT
);

CREATE TABLE IF NOT EXISTS season_entities (
    entity_id           TEXT PRIMARY KEY REFERENCES entities(entity_id),
    season_system       TEXT,
    season_id           TEXT,
    extensions          TEXT
);

CREATE TABLE IF NOT EXISTS minions (
    entity_id           TEXT PRIMARY KEY REFERENCES entities(entity_id),
    minion_type         TEXT,
    base_stats          TEXT,
    inherits            TEXT,
    extensions          TEXT
);

CREATE TABLE IF NOT EXISTS entity_relationships (
    relationship_id     TEXT PRIMARY KEY,
    source_snapshot_id  TEXT,
    source              TEXT,
    from_entity_id      TEXT REFERENCES entities(entity_id),
    relationship_type    TEXT NOT NULL,
    to_entity_id        TEXT REFERENCES entities(entity_id),
    raw_target_ref      TEXT,
    resolution_status   TEXT NOT NULL,                 -- resolved|unresolved|ambiguous
    evidence            TEXT,
    qualifiers          TEXT,                          -- JSON: conditions, tags, level req
    UNIQUE (source, from_entity_id, relationship_type, to_entity_id, raw_target_ref)
);

CREATE INDEX IF NOT EXISTS ix_rel_from ON entity_relationships (from_entity_id, relationship_type);
CREATE INDEX IF NOT EXISTS ix_rel_to ON entity_relationships (to_entity_id, relationship_type);
CREATE INDEX IF NOT EXISTS ix_rel_unresolved ON entity_relationships (resolution_status);

CREATE TABLE IF NOT EXISTS field_evidence (
    evidence_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id           TEXT,
    owner_type          TEXT,
    owner_id            TEXT,
    field_path          TEXT NOT NULL,
    source_record_id    TEXT,
    source              TEXT,
    source_snapshot_id  TEXT,
    raw_value           TEXT,
    normalized_value    TEXT,
    unit                TEXT,
    extraction_method   TEXT,                          -- parsed|inferred|converted|manual
    evidence_status     TEXT NOT NULL,                 -- known|absent|not_applicable|unparsed|conflict|inferred
    locator             TEXT,
    notes               TEXT
);

CREATE INDEX IF NOT EXISTS ix_field_evidence_entity ON field_evidence (entity_id, field_path);
CREATE INDEX IF NOT EXISTS ix_field_evidence_status ON field_evidence (evidence_status);

CREATE TABLE IF NOT EXISTS field_selections (
    selection_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_type      TEXT NOT NULL,
    owner_id        TEXT NOT NULL,
    field_path      TEXT NOT NULL,
    evidence_id     INTEGER REFERENCES field_evidence(evidence_id),
    reason          TEXT,
    alternatives    TEXT,                              -- JSON list of rejected candidates
    UNIQUE (owner_type, owner_id, field_path)
);

CREATE TABLE IF NOT EXISTS data_issues (
    issue_id            TEXT PRIMARY KEY,
    issue_type          TEXT NOT NULL,
    severity            TEXT NOT NULL,                 -- error|warning|info
    source              TEXT,
    source_snapshot_id  TEXT,
    entity_id           TEXT,
    field_path          TEXT,
    message             TEXT,
    expected            TEXT,
    actual              TEXT,
    locator             TEXT,
    status              TEXT NOT NULL,                 -- open|resolved|wontfix
    created_at          TEXT NOT NULL,
    resolved_at         TEXT
);

CREATE INDEX IF NOT EXISTS ix_data_issues_status ON data_issues (status, severity);

CREATE TABLE IF NOT EXISTS mapping_registry (
    mapping_key     TEXT PRIMARY KEY,
    mapping_data    TEXT NOT NULL,
    mapping_revision TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dataset_fingerprints (
    dataset_id      TEXT PRIMARY KEY REFERENCES dataset_meta(dataset_id),
    logical_sha256  TEXT NOT NULL,
    file_sha256     TEXT,
    table_counts    TEXT
);

-- Convenience view used by the query CLI and reports.
CREATE VIEW IF NOT EXISTS v_entity_overview AS
SELECT e.entity_id, e.kind, e.canonical_name, e.record_status, e.primary_source,
       (SELECT COUNT(*) FROM entity_names n WHERE n.entity_id = e.entity_id) AS name_count,
       (SELECT COUNT(*) FROM source_id_map m WHERE m.entity_id = e.entity_id) AS source_id_count
FROM entities e;
