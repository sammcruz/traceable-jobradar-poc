-- Open with:  make sql      (or: duckdb -init sql/views.sql)
-- One view per Delta table. DuckDB reads them with its own Delta reader: no jobradar code involved.
INSTALL delta;
LOAD delta;

CREATE OR REPLACE VIEW arbeitnow_raw     AS SELECT * FROM delta_scan('data/lake/bronze/arbeitnow_raw');
CREATE OR REPLACE VIEW bundesagentur_raw AS SELECT * FROM delta_scan('data/lake/bronze/bundesagentur_raw');
CREATE OR REPLACE VIEW jobs              AS SELECT * FROM delta_scan('data/lake/silver/jobs');
CREATE OR REPLACE VIEW jobs_enriched     AS SELECT * FROM delta_scan('data/lake/gold/jobs_enriched');

-- Once you have ingested them, add:
--   CREATE VIEW jobtech_raw AS SELECT * FROM delta_scan('data/lake/bronze/jobtech_raw');
--   CREATE VIEW adzuna_raw  AS SELECT * FROM delta_scan('data/lake/bronze/adzuna_raw');

.print 'Views: arbeitnow_raw, bundesagentur_raw, jobs, jobs_enriched'
.print 'More (once they exist): delta_scan(''data/lake/gold/matches''), delta_scan(''data/lake/lineage/raw_events'')'
.print 'Try:  SELECT category_label, count(*) FROM jobs_enriched GROUP BY 1 ORDER BY 2 DESC;'
