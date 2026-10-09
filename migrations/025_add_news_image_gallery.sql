-- Run in Supabase SQL Editor before deploying the updated backend.
BEGIN;

ALTER TABLE "EgRailway".news
    ADD COLUMN IF NOT EXISTS image_urls TEXT[] NOT NULL DEFAULT '{}';

UPDATE "EgRailway".news
SET image_urls = ARRAY[image_url]
WHERE cardinality(image_urls) = 0
  AND image_url IS NOT NULL
  AND btrim(image_url) <> '';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_news_image_urls_limit'
          AND conrelid = '"EgRailway".news'::regclass
    ) THEN
        ALTER TABLE "EgRailway".news
            ADD CONSTRAINT chk_news_image_urls_limit
            CHECK (cardinality(image_urls) <= 5);
    END IF;
END;
$$;

COMMENT ON COLUMN "EgRailway".news.image_urls IS
    'Ordered gallery of up to five images; image_url is the selected cover.';

COMMIT;
