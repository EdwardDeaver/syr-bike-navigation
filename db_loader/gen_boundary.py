"""
Generate a GeoJSON polygon tracing the outer boundary of the syr_map.duckdb
coverage area, using a concave hull of all node coordinates.

Output: ../boundary.geojson
"""

import json
from pathlib import Path

import duckdb
import numpy as np
from shapely.geometry import MultiPoint, mapping
from shapely import concave_hull

DB  = Path(__file__).parent.parent / "syr_map.duckdb"
OUT = Path(__file__).parent.parent / "boundary.geojson"

# Controls how "tight" the boundary hugs the data.
# Lower = tighter/more concave. 0.0 = convex hull.
RATIO = 0.3


def main():
    con = duckdb.connect(str(DB), read_only=True)

    print("Loading node coordinates…")
    rows = con.execute("SELECT lng, lat FROM nodes").fetchall()
    con.close()
    print(f"  {len(rows):,} nodes")

    points = MultiPoint(rows)

    print(f"Computing concave hull (ratio={RATIO})…")
    hull = concave_hull(points, ratio=RATIO)

    # Simplify slightly to reduce polygon complexity for the front-end
    hull = hull.simplify(0.0005, preserve_topology=True)

    geojson = {
        "type": "Feature",
        "properties": {},
        "geometry": mapping(hull),
    }

    print(f"Writing {OUT}…")
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(geojson, f, separators=(",", ":"))

    print("Done.")
    print(f"  Bounds: {hull.bounds}")
    print(f"  Polygon vertices: {len(list(hull.exterior.coords))}")


if __name__ == "__main__":
    main()
