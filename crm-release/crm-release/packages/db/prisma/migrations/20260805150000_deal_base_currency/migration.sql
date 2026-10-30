-- AlterTable
ALTER TABLE "deal" ALTER COLUMN "baseAmount" TYPE DECIMAL(24,4);

-- AlterTable
ALTER TABLE "deal" ADD COLUMN     "baseCurrency" TEXT;

-- Backfill: a converted figure that does not say which currency it is in cannot
-- be told apart from a stale one. Every row that already has a baseAmount was
-- converted against the current reporting currency, so name it.
UPDATE "deal"
SET "baseCurrency" = COALESCE(
    (
      SELECT upper(btrim("reportingCurrency"))
      FROM "appSetting"
      WHERE "id" = 'app' AND btrim(COALESCE("reportingCurrency", '')) <> ''
    ),
    'USD'
  )
WHERE "baseAmount" IS NOT NULL;
