"""
Extract GRAPH_DATA and WAYS_DATA from index.html and load into syr_map.duckdb.

Tables created:
  nodes  — node_id, lng, lat
  edges  — from_id, to_id, cost, stress, length_m, name
  ways   — way_id (sequential), stress, geom_wkt (LINESTRING)
"""

import json
import re
import sys
from pathlib import Path

import duckdb

HTML = Path(__file__).parent.parent / "index.html"
DB   = Path(__file__).parent.parent / "syr_map.duckdb"


def extract_js_var(source: str, var_name: str) -> str:
    """Pull the JSON value assigned to a JS const declaration."""
    pattern = rf"const {var_name} = "
    start = source.find(pattern)
    if start == -1:
        raise ValueError(f"{var_name} not found in HTML")
    start += len(pattern)

    # Walk characters to find the matching closing brace/bracket
    depth = 0
    opener = source[start]
    closer = "}" if opener == "{" else "]"
    for i, ch in enumerate(source[start:], start):
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return source[start : i + 1]

    raise ValueError(f"Could not find end of {var_name}")


def coords_to_wkt(coordinates: list) -> str:
    pairs = " ".join(f"{lng} {lat}" for lng, lat in coordinates)
    return f"LINESTRING ({pairs})"


def main():
    print(f"Reading {HTML} …")
    source = HTML.read_text(encoding="utf-8")

    print("Extracting GRAPH_DATA …")
    graph_data = json.loads(extract_js_var(source, "GRAPH_DATA"))

    print("Extracting WAYS_DATA …")
    ways_data = json.loads(extract_js_var(source, "WAYS_DATA"))

    print(f"Connecting to {DB} …")
    con = duckdb.connect(str(DB))

    # ── nodes ──────────────────────────────────────────────────────────
    con.execute("DROP TABLE IF EXISTS nodes")
    con.execute("""
        CREATE TABLE nodes (
            node_id  VARCHAR PRIMARY KEY,
            lng      DOUBLE,
            lat      DOUBLE
        )
    """)

    node_rows = [
        (node_id, coords[0], coords[1])
        for node_id, coords in graph_data["nodes"].items()
    ]
    con.executemany("INSERT INTO nodes VALUES (?, ?, ?)", node_rows)
    print(f"  Inserted {len(node_rows):,} nodes")

    # ── edges ──────────────────────────────────────────────────────────
    con.execute("DROP TABLE IF EXISTS edges")
    con.execute("""
        CREATE TABLE edges (
            from_id   VARCHAR,
            to_id     VARCHAR,
            cost      DOUBLE,
            stress    INTEGER,
            length_m  DOUBLE,
            name      VARCHAR
        )
    """)

    edge_rows = []
    for from_id, neighbors in graph_data["graph"].items():
        for e in neighbors:
            edge_rows.append((
                from_id,
                str(e["t"]),
                e.get("c"),
                e.get("s"),
                e.get("l"),
                e.get("n") or None,
            ))
    con.executemany("INSERT INTO edges VALUES (?, ?, ?, ?, ?, ?)", edge_rows)
    print(f"  Inserted {len(edge_rows):,} edges")

    # ── ways (GeoJSON features → WKT) ──────────────────────────────────
    con.execute("DROP TABLE IF EXISTS ways")
    con.execute("""
        CREATE TABLE ways (
            way_id    INTEGER PRIMARY KEY,
            stress    INTEGER,
            geom_wkt  VARCHAR
        )
    """)

    way_rows = [
        (i, feat["properties"].get("stress"), coords_to_wkt(feat["geometry"]["coordinates"]))
        for i, feat in enumerate(ways_data["features"])
    ]
    con.executemany("INSERT INTO ways VALUES (?, ?, ?)", way_rows)
    print(f"  Inserted {len(way_rows):,} ways")

    # ── summary ────────────────────────────────────────────────────────
    print("\nDatabase summary:")
    for table in ("nodes", "edges", "ways"):
        (count,) = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        print(f"  {table:<8} {count:>8,} rows")

    con.close()
    print(f"\nDone → {DB}")


if __name__ == "__main__":
    main()
