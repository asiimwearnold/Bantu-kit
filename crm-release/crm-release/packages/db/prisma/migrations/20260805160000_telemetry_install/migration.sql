-- The anonymous install identity, in Postgres rather than on disk.
--
-- All three processes deploy to Vercel, where the filesystem is ephemeral, so
-- the usual ~/.app/telemetry-id file would mint a new UUID on every cold start
-- and we would be counting containers instead of installs.
--
-- One row, id pinned to 'install' by `@crm/telemetry`, the same singleton
-- shape as "appSetting". The row is written here rather than on first boot so
-- that the ID exists from the moment the schema does — "createdAt" is then a
-- real install date, and the first funnel step is stamped by the statement
-- that created the install rather than by whichever process happened to start.
--
-- "version" is upserted from the root package.json on every boot, so an
-- upgrade is visible without redeploying telemetry code. 'unknown' only
-- survives until the API or the agent next starts.
CREATE TABLE "install" (
    "id" TEXT NOT NULL,
    "uuid" TEXT NOT NULL,
    "version" TEXT NOT NULL,
    "lastRollupAt" TIMESTAMP(3),
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "install_pkey" PRIMARY KEY ("id")
);

CREATE UNIQUE INDEX "install_uuid_key" ON "install"("uuid");

INSERT INTO "install" ("id", "uuid", "version", "updatedAt")
VALUES ('install', gen_random_uuid()::text, 'unknown', CURRENT_TIMESTAMP);

-- The setup funnel is one-shot: each step fires once per install, ever. A
-- boolean column read and then written races between the API and the agent;
-- an insert that reports whether it was the one to land does not.
CREATE TABLE "telemetryMilestone" (
    "step" TEXT NOT NULL,
    "reachedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "telemetryMilestone_pkey" PRIMARY KEY ("step")
);

-- Counters for what the daily rollup wants that leaves no other row: today
-- that is a session that stopped because its research budget ran out, and
-- nothing else. Everything else the rollup reports is already in the schema
-- and is counted there.
--
-- Drained with DELETE … RETURNING and recreated by the next `upsert`, so a row
-- holds only what has arrived since the last rollup. An increment landing
-- mid-drain waits on the row lock and then inserts a fresh row, so it is
-- counted tomorrow rather than lost. A rollup that fails to send puts the
-- counts back.
CREATE TABLE "telemetryCounter" (
    "name" TEXT NOT NULL,
    "count" INTEGER NOT NULL DEFAULT 0,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "telemetryCounter_pkey" PRIMARY KEY ("name")
);
