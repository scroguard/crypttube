-- Revision of an item's generated assets. It is part of the asset URLs' version
-- parameter, so a forced rebuild (which bumps it) makes every browser fetch the new
-- files instead of year-long-cached old ones.
ALTER TABLE scenes ADD COLUMN assets_rev integer NOT NULL DEFAULT 0;
ALTER TABLE images ADD COLUMN assets_rev integer NOT NULL DEFAULT 0;
