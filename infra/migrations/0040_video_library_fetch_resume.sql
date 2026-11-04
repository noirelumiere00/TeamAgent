ALTER TABLE vl_public_fetch_control
    ADD COLUMN paused_at TIMESTAMPTZ,
    ADD COLUMN resumed_by TEXT,
    ADD COLUMN resumed_at TIMESTAMPTZ;

-- The original stop time is unknown for pre-migration pauses; start their cooldown now.
UPDATE vl_public_fetch_control SET paused_at=clock_timestamp() WHERE paused;

CREATE FUNCTION vl_public_fetch_pause_clock() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.paused AND (NOT OLD.paused OR NEW.pause_reason IS DISTINCT FROM OLD.pause_reason) THEN
        NEW.paused_at := clock_timestamp();
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER vl_public_fetch_pause_clock BEFORE UPDATE ON vl_public_fetch_control
    FOR EACH ROW EXECUTE FUNCTION vl_public_fetch_pause_clock();
