# Universe LLM trial — chain

Run: `20260823-053540-chain-chain` · 1 questions · 4/4 checks passed

| Q | question | stopped | rounds | s | prompt tok | grade | failed checks |
|---|---|---|---|---|---|---|---|
| 1 | list all workcells | answered | 8 | 22.0 | 20833 | 4/4 |  |

## Q1 — list all workcells

**Stopped:** answered · rounds 8 · 22.0 s

**Tools:** universe_describe(ok), universe_query(ok, 40 rows), universe_query(ok, 7 rows), universe_query(ok, 40 rows), universe_query(ok, 37 rows), universe_query(ok, 5 rows), universe_query(ok, 1 rows)

```sql
select * from (SELECT workcell_id, workcell, entity_type, status, plant_physical, plant_governing FROM v_workcell ORDER BY entity_type, status, workcell) as q limit 40
```
```sql
select * from (SELECT entity_type, status, COUNT(*) AS count FROM v_workcell GROUP BY entity_type, status ORDER BY entity_type, status) as q limit 40
```
```sql
select * from (SELECT workcell_id, workcell, entity_type, status, plant_physical, plant_governing FROM v_workcell WHERE status = 'active' ORDER BY entity_type, workcell) as q limit 40
```
```sql
select * from (SELECT workcell_id, workcell, plant_physical, plant_governing FROM v_workcell WHERE entity_type = 'customer' AND status = 'active' ORDER BY workcell) as q limit 40
```
```sql
select * from (SELECT workcell_id, workcell, entity_type, plant_physical, plant_governing FROM v_workcell WHERE entity_type != 'customer' AND status = 'active' ORDER BY entity_type, workcell) as q limit 40
```
```sql
select * from (SELECT COUNT(*) FROM v_workcell WHERE status = 'inactive') as q limit 40
```

**Answer:**

There are 37 active customer workcells, plus 4 active support workcells and 1 active shared line (42 active entities total), alongside 68 inactive workcells in the data model. 

### Active Customer Workcells (37)
* ADVANTEST (P1 / P1)
* AKAMAI (BK / BK)
* ALLEGRO (BK / BK)
* AMPHENOL (BK / BK)
* APTIV (BK / BK)
* ARISTA (P1 / P1)
* BOSE (BK / BK)
* CISCO (BK / BK)
* CIRRUS (BK / BK)
* CONTINENTAL (P2 / P2)
* CORNING (BK / BK)
* DELL (P2 / P2)
* ENPHASE (BK / BK)
* ERICSSON (P2 / P2)
* GOOGLE (BK / BK)
* HONEYWELL (BK / BK)
* HP (P2 / P2)
* JABIL_TEST (BK / BK)
* JUNIPER (BK / BK)
* KEYSIGHT (BK / BK)
* LENOVO (P2 / P2)
* LEXMARK (BK / BK)
* LOGITECH (BK / BK)
* LUCENT (BK / BK)
* MOTOROLA (BK / BK)
* NCR (BK / BK)
* NETAPP (BK / BK)
* NOKIA (P2 / P2)
* NXP (BK / BK)
* ORACLE (BK / BK)
* PANASONIC (BK / BK)
* PHILIPS (BK / BK)
* QLOGIC (BK / BK)
* SANMINA (BK / BK)
* SEAGATE (BK / BK)
* SIEMENS (BK / BK)
* WABTEC (P1 / P1)

### Active Support & Shared Entities (5)
* AOP (Shared Line, P1 / P1)
* SUPPORTP1 (Support)
* SUPPORTP2 (Support)
* WAREHOUSEP1 (Support)
* WAREHOUSEP2 (Support)

*(Note: There are also 68 inactive workcells. Plants are referenced by physical and governing designations per record.)*

**Checks:** ✅ answered · ✅ numbers grounded in tool results · ✅ read v_workcell · ✅ says which count it is (active / customer / support)
