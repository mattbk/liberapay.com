CREATE TYPE currency AS ENUM ('EUR', 'USD');
ALTER TABLE participants ADD COLUMN main_currency currency NOT NULL DEFAULT 'EUR';
