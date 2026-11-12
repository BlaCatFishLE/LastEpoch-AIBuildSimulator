-- Source-provided translations (e.g. Last Epoch Tools i18n JSON files).
-- Kept as data, not executed: nameKey -> display string resolution is auditable
-- per source snapshot and language.
CREATE TABLE IF NOT EXISTS translations (
    translation_id      TEXT PRIMARY KEY,
    source              TEXT NOT NULL,
    source_snapshot_id  TEXT NOT NULL,
    namespace           TEXT NOT NULL,
    language            TEXT NOT NULL,
    key                 TEXT NOT NULL,
    value               TEXT NOT NULL,
    UNIQUE (source_snapshot_id, namespace, language, key)
);

CREATE INDEX IF NOT EXISTS ix_translations_key ON translations (namespace, language, key);
