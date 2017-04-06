from yoyo import step

step(
    "CREATE TABLE IF NOT EXISTS sniper_mons (unique_id text, expiration_timestamp_ms real default 0, last_modified_timestamp_ms real default 0, encounter_id real default 0, spawn_point_id text default '', pokemon_name text,latitude real, longitude real, expiration real, iv real, pokemon_id real, dated datetime DEFAULT CURRENT_TIMESTAMP, verified integer default 0)"
)
