-- Casefile schema. Idempotent; applied by `casefile db init`.

-- ---------------------------------------------------------------- the "claims system" (seeded)
CREATE TABLE IF NOT EXISTS policies (
    policy_number   text PRIMARY KEY,
    state           text NOT NULL,
    named_insured   text NOT NULL,
    effective_date  date NOT NULL,
    expiry_date     date NOT NULL,
    cancel_date     date,
    cancel_reason   text
);
CREATE TABLE IF NOT EXISTS policy_drivers (
    policy_number   text REFERENCES policies ON DELETE CASCADE,
    driver_name     text NOT NULL
);
CREATE TABLE IF NOT EXISTS policy_coverages (
    policy_number   text REFERENCES policies ON DELETE CASCADE,
    code            text NOT NULL,
    deductible_c    integer NOT NULL
);
CREATE TABLE IF NOT EXISTS vehicles (
    vin             text PRIMARY KEY,
    policy_number   text REFERENCES policies ON DELETE CASCADE,
    year            integer, make text, model text, vehicle_class text,
    acv_c           integer NOT NULL
);
CREATE TABLE IF NOT EXISTS prior_claims (
    prior_claim_number text PRIMARY KEY,
    policy_number   text, vin text NOT NULL, loss_date date NOT NULL,
    damage_areas    text[] NOT NULL, paid_c integer NOT NULL, repair_verified boolean NOT NULL
);
CREATE TABLE IF NOT EXISTS parts_catalog (
    part_number     text PRIMARY KEY,
    vehicle_class   text NOT NULL, code text NOT NULL, description text NOT NULL, areas text[] NOT NULL,
    price_c         integer NOT NULL, labor_hours real NOT NULL, paint_hours real NOT NULL
);
CREATE TABLE IF NOT EXISTS state_rates (
    state text PRIMARY KEY, body_rate_c integer, paint_rate_c integer, materials_rate_c integer, tax_bp integer
);
CREATE TABLE IF NOT EXISTS claims (
    claim_id text PRIMARY KEY, split text NOT NULL, received_date date NOT NULL, documents text[] NOT NULL
);

-- ---------------------------------------------------------------- runs, ledger, cassette
CREATE TABLE IF NOT EXISTS runs (
    run_id          text PRIMARY KEY,
    claim_id        text NOT NULL,
    system          text NOT NULL,           -- casefile | casefile_no_reviewer | single_prompt | single_agent
    batch           text,
    replay_of       text,
    llm_mode        text NOT NULL,           -- live | record | replay
    status          text NOT NULL,           -- running | awaiting_approval | decided | escalated | error
    terminal_kind   text,
    decision        text,
    payout_c        integer,
    reason_codes    text[],
    steps           integer DEFAULT 0,
    revisions       integer DEFAULT 0,
    llm_calls       integer DEFAULT 0,
    tokens_in       integer DEFAULT 0,
    tokens_out      integer DEFAULT 0,
    usd             double precision DEFAULT 0,
    ceiling_usd     double precision,
    ceiling_tokens  integer,
    latency_ms      integer,
    path            text[],
    trace_id        text,
    trace_url       text,
    config_hash     text,
    git_sha         text,
    final_state     jsonb,
    error           text,
    started_at      timestamptz DEFAULT now(),
    ended_at        timestamptz
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id              bigserial PRIMARY KEY,
    run_id          text NOT NULL,
    node            text NOT NULL,
    visit           integer NOT NULL,
    call_idx        integer NOT NULL,
    provider        text, model text,
    request_hash    text NOT NULL,
    request         jsonb, response jsonb,
    tokens_in       integer, tokens_out integer, est_tokens integer,
    usd             double precision,
    latency_ms      integer,
    served_from     text,                    -- provider | cassette:<run_id>
    created_at      timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS llm_calls_key ON llm_calls (run_id, node, visit, call_idx);
CREATE TABLE IF NOT EXISTS tool_calls (
    id bigserial PRIMARY KEY, run_id text, node text, visit integer, tool text,
    args jsonb, result jsonb, latency_ms integer, created_at timestamptz DEFAULT now()
);

-- ---------------------------------------------------------------- human gate + write-back
CREATE TABLE IF NOT EXISTS approvals (
    approval_id         text PRIMARY KEY,
    run_id              text NOT NULL,
    claim_id            text NOT NULL,
    recommendation_hash text NOT NULL,
    action              text NOT NULL CHECK (action IN ('APPROVE', 'REJECT', 'RETURN')),
    approver            text NOT NULL,
    note                text,
    created_at          timestamptz DEFAULT now()
);
CREATE TABLE IF NOT EXISTS decisions (
    decision_id         bigserial PRIMARY KEY,
    claim_id            text NOT NULL,
    run_id              text NOT NULL,
    approval_id         text NOT NULL REFERENCES approvals,   -- no approval row, no write
    recommendation_hash text NOT NULL,
    decision            text NOT NULL,
    payout_c            integer NOT NULL,
    created_at          timestamptz DEFAULT now()
);

-- Second lock, independent of application code: the approval must be an APPROVE for exactly this
-- recommendation of exactly this run.
CREATE OR REPLACE FUNCTION decisions_require_approval() RETURNS trigger AS $$
DECLARE a approvals%ROWTYPE;
BEGIN
    SELECT * INTO a FROM approvals WHERE approval_id = NEW.approval_id;
    IF a.action IS DISTINCT FROM 'APPROVE' THEN
        RAISE EXCEPTION 'decision % has no APPROVE approval', NEW.claim_id;
    END IF;
    IF a.recommendation_hash <> NEW.recommendation_hash OR a.run_id <> NEW.run_id THEN
        RAISE EXCEPTION 'approval % does not match this recommendation', NEW.approval_id;
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS decisions_require_approval ON decisions;
CREATE TRIGGER decisions_require_approval BEFORE INSERT OR UPDATE ON decisions
    FOR EACH ROW EXECUTE FUNCTION decisions_require_approval();
