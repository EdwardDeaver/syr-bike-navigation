"""
Fetch SRTM elevation for every node via OpenTopoData, then compute
per-edge grade (rise/run, signed) and store in syr_map.duckdb.

New table:   elevations  — node_id, elevation_m
New column:  edges.grade — (elev_to - elev_from) / length_m  (dimensionless)
"""

import time
from pathlib import Path

import duckdb
import requests

DB  = Path(__file__).parent.parent / "syr_map.duckdb"
URL = "https://api.opentopodata.org/v1/srtm30m"
BATCH   = 100   # max locations per request (API limit)
DELAY   = 1.1   # seconds between requests


def fetch_batch(latlng_pairs: list[tuple[str, float, float]]) -> dict[str, float | None]:
    """POST a batch of (node_id, lat, lng) → {node_id: elevation_m}."""
    locations = "|".join(f"{lat},{lng}" for _, lat, lng in latlng_pairs)
    try:
        resp = requests.get(URL, params={"locations": locations}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"  [warn] request failed: {exc}")
        return {nid: None for nid, _, _ in latlng_pairs}

    result = {}
    for (nid, _, _), res in zip(latlng_pairs, data.get("results", [])):
        result[nid] = res.get("elevation")
    return result


def main():
    con = duckdb.connect(str(DB))

    # ── load all nodes ──────────────────────────────────────────────────
    rows = con.execute("SELECT node_id, lat, lng FROM nodes").fetchall()
    print(f"Fetching elevation for {len(rows):,} nodes "
          f"({-(-len(rows) // BATCH)} batches) …")

    # ── (re)create elevations table ─────────────────────────────────────
    con.execute("DROP TABLE IF EXISTS elevations")
    con.execute("""
        CREATE TABLE elevations (
            node_id     VARCHAR PRIMARY KEY,
            elevation_m DOUBLE
        )
    """)

    all_elevations: dict[str, float | None] = {}
    batches = [rows[i : i + BATCH] for i in range(0, len(rows), BATCH)]

    for idx, batch in enumerate(batches, 1):
        elevs = fetch_batch(batch)
        all_elevations.update(elevs)

        none_count = sum(1 for v in elevs.values() if v is None)
        print(f"  batch {idx:>3}/{len(batches)}  "
              f"ok={len(elevs)-none_count}  null={none_count}")

        if idx < len(batches):
            time.sleep(DELAY)

    # ── insert into DB ──────────────────────────────────────────────────
    elev_rows = [(nid, elev) for nid, elev in all_elevations.items()]
    con.executemany("INSERT INTO elevations VALUES (?, ?)", elev_rows)
    non_null = sum(1 for _, e in elev_rows if e is not None)
    print(f"\nInserted {len(elev_rows):,} elevation rows ({non_null:,} non-null)")

    # ── add grade column to edges ────────────────────────────────────────
    # grade = (elev_to - elev_from) / length_m  (positive = uphill)
    existing_cols = [r[0] for r in con.execute("PRAGMA table_info(edges)").fetchall()]
    if "grade" in existing_cols:
        con.execute("ALTER TABLE edges DROP COLUMN grade")

    con.execute("""
        ALTER TABLE edges ADD COLUMN grade DOUBLE
    """)

    con.execute("""
        UPDATE edges
        SET grade = (ef.elevation_m - et.elevation_m) / NULLIF(length_m, 0)
        FROM elevations ef, elevations et
        WHERE edges.from_id = ef.node_id
          AND edges.to_id   = et.node_id
    """)

    (updated,) = con.execute(
        "SELECT COUNT(*) FROM edges WHERE grade IS NOT NULL"
    ).fetchone()
    print(f"grade set on {updated:,} edges")

    # ── quick sanity check ───────────────────────────────────────────────
    print("\nGrade distribution (non-null edges):")
    for row in con.execute("""
        SELECT
            MIN(grade)  AS min_grade,
            MAX(grade)  AS max_grade,
            AVG(ABS(grade)) AS mean_abs_grade
        FROM edges WHERE grade IS NOT NULL
    """).fetchall():
        print(f"  min={row[0]:.4f}  max={row[1]:.4f}  mean_abs={row[2]:.4f}")

    con.close()
    print(f"\nDone → {DB}")


if __name__ == "__main__":
    main()
