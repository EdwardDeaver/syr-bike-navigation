"""
Export syr_map.duckdb → syr_map.json (minified).

JSON schema
-----------
{
  "nodes": { "<id>": [lng, lat, elev_m], ... },
  "graph":  { "<from_id>": [{"t":<to_id>,"c":<cost>,"s":<stress>,"l":<len_m>,"n":<name|null>,"g":<grade>}, ...], ... },
  "ways":   [ {"geometry":{"coordinates":[[lng,lat],...]},"properties":{"stress":<int>}}, ... ]
}

"g" is signed rise/run (decimal, not %).  Positive = uphill from→to.
Node IDs are strings; all numeric fields are compact floats.
"""

import json
from pathlib import Path

import duckdb

DB   = Path(__file__).parent.parent / "syr_map.duckdb"
OUT  = Path(__file__).parent.parent / "syr_map.json"


def _compact_float(v: float | None, decimals: int = 6) -> float | None:
    """Round to `decimals` sig-figs to shrink file size without losing precision."""
    if v is None:
        return None
    return round(v, decimals)


def main():
    con = duckdb.connect(str(DB), read_only=True)

    # ── nodes ────────────────────────────────────────────────────────────
    print("Loading nodes …")
    node_rows = con.execute("""
        SELECT n.node_id,
               n.lng,
               n.lat,
               e.elevation_m
        FROM   nodes n
        LEFT JOIN elevations e ON n.node_id = e.node_id
    """).fetchall()

    nodes: dict[str, list] = {}
    for node_id, lng, lat, elev in node_rows:
        nodes[node_id] = [
            _compact_float(lng, 6),
            _compact_float(lat, 6),
            _compact_float(elev, 1),   # 0.1 m elevation resolution is plenty
        ]
    print(f"  {len(nodes):,} nodes")

    # ── edges ────────────────────────────────────────────────────────────
    print("Loading edges …")
    edge_rows = con.execute("""
        SELECT from_id, to_id, cost, stress, length_m, name, grade
        FROM   edges
        ORDER  BY from_id
    """).fetchall()

    graph: dict[str, list] = {}
    for from_id, to_id, cost, stress, length_m, name, grade in edge_rows:
        entry = {
            "t": int(to_id),
            "c": _compact_float(cost, 1),
            "s": int(stress),
            "l": _compact_float(length_m, 1),
        }
        if name:
            entry["n"] = name
        if grade is not None:
            entry["g"] = _compact_float(grade, 4)
        graph.setdefault(from_id, []).append(entry)
    print(f"  {len(edge_rows):,} edges across {len(graph):,} source nodes")

    # ── ways ─────────────────────────────────────────────────────────────
    print("Loading ways …")
    way_rows = con.execute("SELECT stress, geom_wkt FROM ways ORDER BY way_id").fetchall()

    def wkt_to_coords(wkt: str) -> list:
        # WKT stored without commas: "LINESTRING (lng lat lng lat ...)"
        inner = wkt[wkt.index("(") + 1 : wkt.rindex(")")]
        vals = inner.split()
        return [[round(float(vals[i]), 6), round(float(vals[i + 1]), 6)] for i in range(0, len(vals), 2)]

    ways = [
        {
            "geometry": {"coordinates": wkt_to_coords(geom_wkt)},
            "properties": {"stress": stress},
        }
        for stress, geom_wkt in way_rows
    ]
    print(f"  {len(ways):,} ways")

    con.close()

    # ── write minified JSON ───────────────────────────────────────────────
    payload = {"nodes": nodes, "graph": graph, "ways": ways}
    print(f"Writing {OUT} …")
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"), ensure_ascii=False)

    size_mb = OUT.stat().st_size / 1_048_576
    print(f"Done → {OUT}  ({size_mb:.2f} MB)")


if __name__ == "__main__":
    main()
