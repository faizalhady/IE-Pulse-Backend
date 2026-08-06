"""
diagnose.py
───────────
Run this to identify data issues in the mart.
  python diagnose.py
"""

import duckdb

con = duckdb.connect()

print("=" * 60)
print("PRODUCTION - workcells and stages found")
print("=" * 60)
con.execute("""
    SELECT workcell, sub_workcell, COUNT(*) as rows, SUM(qty) as total_qty
    FROM read_parquet('data/mart/ole/raw_production.parquet')
    GROUP BY workcell, sub_workcell
    ORDER BY workcell
""").df().pipe(print)

print()
print("=" * 60)
print("PAID HOURS - workcells found")
print("=" * 60)
con.execute("""
    SELECT workcell, COUNT(*) as rows, SUM(total_input_hours) as total_hrs
    FROM read_parquet('data/mart/ole/raw_paid_hours.parquet')
    GROUP BY workcell
    ORDER BY workcell
""").df().pipe(print)

print()
print("=" * 60)
print("SMH LOOKUP - coverage per workcell")
print("=" * 60)
con.execute("""
    SELECT workcell, scan_stage,
           COUNT(*)                                         AS total_assemblies,
           SUM(CASE WHEN smh_value > 0 THEN 1 ELSE 0 END)  AS with_smh_value
    FROM read_parquet('data/mart/ole/smh_lookup.parquet')
    GROUP BY workcell, scan_stage
    ORDER BY workcell
""").df().pipe(print)

print()
print("=" * 60)
print("ASSEMBLY JOIN CHECK - MES vs SMH (first 30 rows)")
print("=" * 60)
con.execute("""
    SELECT
        p.workcell,
        p.assembly          AS mes_assembly,
        s.assembly          AS smh_assembly,
        s.smh_value,
        CASE WHEN s.assembly IS NULL THEN 'NO MATCH' ELSE 'MATCHED' END AS status
    FROM read_parquet('data/mart/ole/raw_production.parquet') p
    LEFT JOIN read_parquet('data/mart/ole/smh_lookup.parquet') s
           ON p.workcell = s.workcell
          AND p.assembly = s.assembly
    LIMIT 30
""").df().pipe(print)

print()
print("=" * 60)
print("ASSEMBLY JOIN MATCH RATE - per workcell")
print("=" * 60)
con.execute("""
    SELECT
        p.workcell,
        COUNT(*)                                                AS total_rows,
        SUM(CASE WHEN s.assembly IS NOT NULL THEN 1 ELSE 0 END) AS matched,
        SUM(CASE WHEN s.assembly IS NULL     THEN 1 ELSE 0 END) AS unmatched,
        ROUND(
            SUM(CASE WHEN s.assembly IS NOT NULL THEN 1 ELSE 0 END) * 100.0
            / COUNT(*), 1
        )                                                       AS match_pct
    FROM read_parquet('data/mart/ole/raw_production.parquet') p
    LEFT JOIN read_parquet('data/mart/ole/smh_lookup.parquet') s
           ON p.workcell = s.workcell
          AND p.assembly = s.assembly
    GROUP BY p.workcell
    ORDER BY p.workcell
""").df().pipe(print)

print()
print("=" * 60)
print("UNMATCHED ASSEMBLIES - samples per workcell")
print("=" * 60)
con.execute("""
    SELECT DISTINCT
        p.workcell,
        p.assembly  AS mes_assembly
    FROM read_parquet('data/mart/ole/raw_production.parquet') p
    LEFT JOIN read_parquet('data/mart/ole/smh_lookup.parquet') s
           ON p.workcell = s.workcell
          AND p.assembly = s.assembly
    WHERE s.assembly IS NULL
    ORDER BY p.workcell, p.assembly
    LIMIT 30
""").df().pipe(print)

print()
print("=" * 60)
print("SMH SAMPLES - what assembly names look like in SMH")
print("=" * 60)
con.execute("""
    SELECT workcell, assembly, smh_value
    FROM read_parquet('data/mart/ole/smh_lookup.parquet')
    WHERE smh_value > 0
    ORDER BY workcell
    LIMIT 30
""").df().pipe(print)

con.close()