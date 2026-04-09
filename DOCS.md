# Syracuse Low-Stress Bike Router — Technical Documentation

## Table of Contents

1. [System Overview](#system-overview)
2. [Data Pipeline](#data-pipeline)
   - [Step 1 — Load raw graph into DuckDB](#step-1--load-raw-graph-into-duckdb)
   - [Step 2 — Fetch elevations and compute grades](#step-2--fetch-elevations-and-compute-grades)
   - [Step 3 — Export to JSON](#step-3--export-to-json)
3. [Navigation Engine (Rust/WASM)](#navigation-engine-rustwasm)
   - [Graph representation](#graph-representation)
   - [Dijkstra's algorithm](#dijkstras-algorithm)
   - [Hill penalty and fitness slider](#hill-penalty-and-fitness-slider)
   - [Nearest-node lookup](#nearest-node-lookup)
4. [WASM Build and Loading](#wasm-build-and-loading)
   - [Compiling with wasm-pack](#compiling-with-wasm-pack)
   - [Generated JS glue layer](#generated-js-glue-layer)
   - [Loading in the browser](#loading-in-the-browser)
5. [Map Rendering](#map-rendering)
   - [PMTiles and local tiles](#pmtiles-and-local-tiles)
   - [MapLibre GL JS style](#maplibre-gl-js-style)
   - [Local glyph files](#local-glyph-files)
   - [Local development server](#local-development-server)
6. [Front-End Routing Flow](#front-end-routing-flow)
7. [Data Schema Reference](#data-schema-reference)

---

## System Overview

The project is a static web application with no server-side routing. All computation runs in the browser using a Rust binary compiled to WebAssembly. The build pipeline is:

```
PeopleForBikes BNA GeoJSON
        │
        ▼
   load.py  ──────────────────▶  syr_map.duckdb
        │                         (nodes, edges, ways)
        │
   fetch_elevations.py  ─────▶  syr_map.duckdb
        │                         + elevations table
        │                         + edges.grade column
        │
   export_json.py  ──────────▶  syr_map.json
        │
        ▼
   index.html  (fetches syr_map.json at runtime)
        │
        ▼
   nav_wasm (Rust)  ──────────▶  pkg/nav_wasm.js + nav_wasm_bg.wasm
        │                         (compiled with wasm-pack)
        ▼
   Browser: init() → nearest_node() → route()
```

---

## Data Pipeline

### Step 1 — Load raw graph into DuckDB

**File:** `db_loader/load.py`

The source data is a pre-processed street graph embedded in `index.html` as two JavaScript constants, `GRAPH_DATA` and `WAYS_DATA`. `load.py` extracts those constants and inserts them into a DuckDB database file (`syr_map.duckdb`).

#### Extracting JS variables from HTML

The function `extract_js_var(source, var_name)` finds the assignment `const <name> = ` in the HTML source, then walks the raw character stream counting brace/bracket depth to locate the matching close delimiter — a simple recursive-descent approach that avoids brittle regex.

```python
def extract_js_var(source: str, var_name: str) -> str:
    pattern = rf"const {var_name} = "
    start = source.find(pattern) + len(pattern)
    depth = 0
    opener = source[start]           # '{' or '['
    closer = "}" if opener == "{" else "]"
    for i, ch in enumerate(source[start:], start):
        if ch == opener: depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
```

#### Tables created

| Table    | Columns                                          | Notes |
|----------|--------------------------------------------------|-------|
| `nodes`  | `node_id VARCHAR PK`, `lng DOUBLE`, `lat DOUBLE` | One row per graph node. Coordinates are WGS-84. |
| `edges`  | `from_id`, `to_id`, `cost`, `stress`, `length_m`, `name` | Directed edges. `cost` is the pre-weighted routing cost from the source data. `stress` is 1–3 (LTS). |
| `ways`   | `way_id INTEGER PK`, `stress INTEGER`, `geom_wkt VARCHAR` | Geometry stored as WKT `LINESTRING` for the stress overlay. |

The `coords_to_wkt` helper converts `[[lng, lat], ...]` arrays from GeoJSON coordinates into `LINESTRING (lng lat lng lat ...)` WKT strings so they can be stored as plain `VARCHAR` without a spatial extension.

---

### Step 2 — Fetch elevations and compute grades

**File:** `db_loader/fetch_elevations.py`

#### Fetching SRTM elevation data

The script calls the [OpenTopoData](https://www.opentopodata.org/) public API (`/v1/srtm30m`) to look up ground elevation for each of the ~15,000 nodes. SRTM30m is the Shuttle Radar Topography Mission dataset at 30-metre horizontal resolution — more than sufficient for per-street grade estimates.

Because the API accepts at most **100 locations per request**, all nodes are split into batches:

```python
BATCH = 100
DELAY = 1.1   # seconds between requests (rate limiting)

batches = [rows[i : i + BATCH] for i in range(0, len(rows), BATCH)]
for batch in batches:
    elevs = fetch_batch(batch)
    ...
    time.sleep(DELAY)
```

Each batch is sent as a single `GET` request with a pipe-delimited `locations` parameter:

```
GET /v1/srtm30m?locations=43.048,-76.147|43.051,-76.150|...
```

Results are inserted into an `elevations(node_id, elevation_m)` table.

#### Computing grade per edge

After all elevations are fetched, a `grade` column is added to the `edges` table and populated with a single SQL `UPDATE`:

```sql
UPDATE edges
SET grade = (ef.elevation_m - et.elevation_m) / NULLIF(length_m, 0)
FROM elevations ef, elevations et
WHERE edges.from_id = ef.node_id
  AND edges.to_id   = et.node_id
```

**Grade is a signed dimensionless decimal (rise ÷ run), not a percentage.**
- `grade = 0.10` means 10% uphill in the direction of travel
- `grade = -0.05` means 5% downhill
- `grade = NULL` when either endpoint has no elevation data or `length_m = 0`

The sign convention is `elev_from − elev_to` rather than `elev_to − elev_from`, so a positive value means you are climbing as you traverse the edge from `from_id` to `to_id`. The hill penalty function (see below) uses `grade.abs()` so direction does not affect the penalty magnitude — both uphills and downhills are penalised equally.

---

### Step 3 — Export to JSON

**File:** `db_loader/export_json.py`

Reads the three tables from DuckDB and writes a single minified JSON file (`syr_map.json`) that the browser fetches at runtime. The schema is:

```json
{
  "nodes": {
    "<id>": [lng, lat, elev_m]
  },
  "graph": {
    "<from_id>": [
      { "t": <to_id_int>, "c": <cost>, "s": <stress 1-3>,
        "l": <length_m>, "n": <street_name|omitted>, "g": <grade|omitted> }
    ]
  },
  "ways": [
    { "geometry": { "coordinates": [[lng, lat], ...] },
      "properties": { "stress": <1-3> } }
  ]
}
```

All float values are rounded to a fixed number of decimal places before writing:

| Field      | Precision | Reason |
|------------|-----------|--------|
| `lng`/`lat` | 6 dp | ~0.1 m at Syracuse latitude — sub-metre |
| `elev_m`   | 1 dp | 0.1 m vertical resolution is sufficient |
| `cost`     | 1 dp | Cost values are large enough that 0.1 precision is fine |
| `length_m` | 1 dp | 0.1 m length resolution |
| `grade`    | 4 dp | 0.0001 rise/run ≈ 0.01% — adequate for penalty thresholds |

The `name` and `g` (grade) keys are **omitted entirely** from an edge object when they are null, keeping the JSON compact. The `separators=(",", ":")` argument to `json.dump` removes all whitespace, further shrinking file size.

---

## Navigation Engine (Rust/WASM)

**File:** `nav_wasm/src/lib.rs`

### Graph representation

After `init()` parses the JSON, the graph is stored in three parallel structures indexed by a contiguous integer (`u32`):

```rust
struct Graph {
    node_ids:  Vec<String>,      // index → OSM node id string
    nodes:     Vec<Node>,        // index → (lng, lat, elev_m)
    id_to_idx: HashMap<String, u32>,  // OSM id → index
    adj:       Vec<Vec<Edge>>,   // adjacency list
}
```

Node IDs come from the JSON as arbitrary strings (OSM node IDs). Sorting them lexicographically and assigning contiguous integer indices allows all hot-path data structures (`dist`, `prev`, `adj`) to be plain `Vec`s with O(1) index access, avoiding the overhead of `HashMap` lookups inside the Dijkstra loop.

Each `Edge` in the adjacency list stores:

```rust
struct Edge {
    to_idx:   u32,
    cost:     f64,    // pre-weighted cost from source data (LTS penalty baked in)
    stress:   u8,     // 1, 2, or 3 (LTS level)
    length_m: f64,    // physical distance in metres
    name:     Option<String>,
    grade:    f64,    // signed rise/run; 0.0 if unknown
}
```

### Dijkstra's algorithm

`route(from_id, to_id, hill_factor)` runs a standard single-source shortest-path search using a binary min-heap.

#### Min-heap ordering

Rust's `BinaryHeap` is a max-heap. To turn it into a min-heap, the `Ord` implementation on `HeapItem` reverses the comparison:

```rust
impl Ord for HeapItem {
    fn cmp(&self, other: &Self) -> Ordering {
        other.cost          // reversed — "other" before "self"
            .partial_cmp(&self.cost)
            .unwrap_or(Ordering::Equal)
    }
}
```

This means the item with the **smallest** cost is popped first.

#### Main loop

```rust
let mut dist = vec![f64::INFINITY; n];   // tentative distance to each node
let mut prev: Vec<Option<(u32, usize)>>  // (predecessor node idx, edge index)
    = vec![None; n];
dist[start] = 0.0;
heap.push(HeapItem { cost: 0.0, idx: start });

while let Some(HeapItem { cost: d, idx: u }) = heap.pop() {
    if u == end { break; }              // early exit when destination is settled
    if d > dist[u as usize] { continue; } // stale heap entry — skip

    for (ei, edge) in g.adj[u].iter().enumerate() {
        // hill_factor 0.0 = no penalty, 1.0 = full penalty
        let penalty = 1.0 + (hill_penalty(edge.grade) - 1.0) * hill_factor;
        let nd = d + edge.cost * penalty;
        let v = edge.to_idx as usize;
        if nd < dist[v] {
            dist[v] = nd;
            prev[v] = Some((u, ei));    // store edge index for path reconstruction
            heap.push(HeapItem { cost: nd, idx: edge.to_idx });
        }
    }
}
```

Key points:

- **Lazy deletion:** Rather than updating existing heap entries (which requires a decrease-key operation not available on Rust's `BinaryHeap`), a new entry is pushed whenever a shorter path is found. When a node is popped, if its recorded cost is higher than the settled `dist[u]`, the entry is stale and skipped immediately.
- **Early termination:** The loop breaks as soon as the destination node is popped, since its distance is then finalized.
- **Edge index in `prev`:** Each `prev[v]` stores a `(predecessor_node_idx, edge_index)` pair rather than just the predecessor node. This lets path reconstruction retrieve the exact edge (with its name, stress, grade, length) in O(1) without a second adjacency-list scan.
- **Cost vs. length:** `edge.cost` is the pre-weighted value from the source data (LTS penalty baked in during pre-processing). The hill penalty is multiplied on top at query time, so avoiding hills applies an **additional multiplier** on the already-LTS-weighted cost.

#### Path reconstruction

The path is recovered by walking the `prev` array backwards from the destination:

```rust
let mut cur = end;
while let Some((p, ei)) = prev[cur] {
    path_idx.push(cur);
    edge_seq.push((p, ei));
    cur = p;
}
path_idx.push(start);
path_idx.reverse();
edge_seq.reverse();
```

The reversed `edge_seq` then maps directly to the `edgePath` returned to JavaScript.

---

### Hill penalty

```rust
fn hill_penalty(grade: f64) -> f64 {
    let pct = grade.abs() * 100.0;   // convert decimal to percentage
    if pct > 12.0 { 50.0 }
    else if pct > 8.0 { 4.0 }
    else if pct > 4.0 { 1.5 }
    else { 1.0 }
}
```

| Grade (abs) | Multiplier | Effect |
|-------------|------------|--------|
| ≤ 4%        | 1.0×       | No penalty — comfortable cycling |
| 4–8%        | 1.5×       | Mild penalty — noticeable climb |
| 8–12%       | 4.0×       | Heavy penalty — steep, most cyclists dismount |
| > 12%       | 50.0×      | Near-prohibitive — equivalent to 50× the distance |

The penalty is applied to `edge.cost` (the already-LTS-weighted cost), so a >12% hill on an LTS-3 street is penalised 50× on top of the LTS-3 penalty. This almost always routes around such segments even at significant detour cost. The multiplier is only applied when `avoid_hills = true`; otherwise all penalties are 1.0.

---

### Nearest-node lookup

```rust
pub fn nearest_node(lat: f64, lng: f64) -> String {
    let g = graph();
    let mut best_idx = 0u32;
    let mut best_dist = f64::INFINITY;
    for (i, node) in g.nodes.iter().enumerate() {
        let d = haversine(lat, lng, node.lat, node.lng);
        if d < best_dist {
            best_dist = d;
            best_idx = i as u32;
        }
    }
    g.node_ids[best_idx as usize].clone()
}
```

This is a brute-force linear scan over all ~15,000 nodes using the haversine formula. At this scale it runs in well under a millisecond — a spatial index (k-d tree, R-tree) would only matter for hundreds of thousands of nodes.

#### Haversine formula

```rust
fn haversine(lat1: f64, lon1: f64, lat2: f64, lon2: f64) -> f64 {
    const R: f64 = 6_371_000.0;  // Earth radius in metres
    let dlat = (lat2 - lat1).to_radians();
    let dlon = (lon2 - lon1).to_radians();
    let a = (dlat / 2.0).sin().powi(2)
        + lat1.to_radians().cos() * lat2.to_radians().cos() * (dlon / 2.0).sin().powi(2);
    R * 2.0 * a.sqrt().atan2((1.0 - a).sqrt())
}
```

Haversine gives the great-circle distance between two points on a sphere. At the scale of Syracuse (~40 km across), the error introduced by treating the Earth as a perfect sphere is under 0.3%, negligible for snapping a clicked map point to the nearest road node.

---

## WASM Build and Loading

### Compiling with wasm-pack

**File:** `nav_wasm/Cargo.toml`

The crate is declared as a `cdylib` (C-compatible dynamic library), which is the format WASM targets require:

```toml
[lib]
crate-type = ["cdylib", "rlib"]
```

`rlib` is included alongside so the crate can also be used as a normal Rust dependency (e.g., for unit tests).

The release profile is tuned for **minimum binary size** rather than speed:

```toml
[profile.release]
opt-level     = "z"   # "z" = smallest binary (vs "3" = fastest)
lto           = true  # link-time optimisation — removes dead code across crates
codegen-units = 1     # single codegen unit enables maximum inlining
panic         = "abort"  # removes stack-unwinding machinery (~10 KB savings)
```

To build:

```bash
cd nav_wasm
wasm-pack build --target web --release
```

`--target web` generates an ES module (not Node.js or bundler format). The output lands in `pkg/`:

| File | Purpose |
|------|---------|
| `nav_wasm.js` | JS glue layer — handles string encoding, memory management, `init()` bootstrap |
| `nav_wasm_bg.wasm` | Compiled binary (~138 KB after `opt-level = "z"`) |
| `nav_wasm.d.ts` | TypeScript type declarations |
| `nav_wasm_bg.wasm.d.ts` | TypeScript declarations for the raw WASM imports |
| `package.json` | Package metadata for npm consumption |

### Generated JS glue layer

**File:** `pkg/nav_wasm.js`

`wasm-bindgen` generates this file automatically. Its main responsibilities are:

**1. Memory management for strings**

WASM linear memory is a flat byte buffer. To pass a JavaScript string into Rust, `wasm-bindgen` encodes it to UTF-8, allocates space in the WASM heap using `__wbindgen_malloc`, copies the bytes in, and passes the pointer + length pair to the Rust function. On return, `__wbindgen_free` deallocates the buffer:

```js
export function init(json) {
    const ptr0 = passStringToWasm0(json, wasm.__wbindgen_malloc, wasm.__wbindgen_realloc);
    const len0 = WASM_VECTOR_LEN;
    const ret = wasm.init(ptr0, len0);
    if (ret[1]) { throw takeFromExternrefTable0(ret[0]); }
}
```

**2. String return values**

When Rust returns a `String`, `wasm-bindgen` uses a two-word (pointer, length) return convention. The glue decodes the UTF-8 slice from WASM memory into a JS string, then frees the WASM allocation:

```js
export function nearest_node(lat, lng) {
    const ret = wasm.nearest_node(lat, lng);
    try {
        return getStringFromWasm0(ret[0], ret[1]);
    } finally {
        wasm.__wbindgen_free(ret[0], ret[1], 1);
    }
}
```

**3. Complex return values (`route`)**

`route()` returns a Rust struct serialised via `serde_wasm_bindgen`. This library serialises Rust types directly into JS values using the WASM-bindgen `externref` table, bypassing JSON serialisation entirely. The result is a plain JS object with `path`, `edgePath`, `distM`, `lengthM` properties directly usable in JavaScript with no `JSON.parse` call.

### Loading in the browser

**File:** `index.html` (lines 500–513)

The WASM module is loaded via the ES module import and the default export (the init function) provided by `wasm-pack`:

```js
import wasmInit, { init as wasmLoad, nearest_node, route }
    from './pkg/nav_wasm.js';

// In DOMContentLoaded:
const [jsonText] = await Promise.all([
    fetch('./syr_map.json').then(r => r.text()),
    wasmInit(),   // fetches and instantiates nav_wasm_bg.wasm
]);

wasmLoad(jsonText);  // parses JSON, builds graph in WASM memory
```

The two operations are parallelised with `Promise.all`:
- `fetch('./syr_map.json')` downloads the ~1–2 MB graph JSON
- `wasmInit()` fetches the ~138 KB `.wasm` binary, compiles it to native code in the browser's WASM JIT, and returns

Both downloads and compilations happen simultaneously. `wasmLoad(jsonText)` (which maps to the Rust `init(json)` function) is only called after both complete, ensuring the WASM module is instantiated before the graph JSON is handed to it.

`wasmInit()` internally calls `WebAssembly.instantiateStreaming` (or `WebAssembly.instantiate` as fallback), which compiles the `.wasm` binary on a background thread in modern browsers, keeping the main thread unblocked.

---

## Front-End Routing Flow

Once the WASM module is initialised, a route request follows this sequence:

```
User clicks "FIND LOWEST STRESS ROUTE"
    │
    ▼
nearest_node(ptA[0], ptA[1])  →  nodeA  (string id)
nearest_node(ptB[0], ptB[1])  →  nodeB  (string id)
    │
    ▼
route(nodeA, nodeB, hillAvoid)
    │   Rust Dijkstra runs in WASM
    ▼
{ path: string[], edgePath: Edge[], distM, lengthM }
    │
    ├── path  →  Leaflet polyline segments (coloured by stress)
    ├── edgePath  →  route stats (distance, ETA, LTS%, max grade)
    └── edgePath  →  turn-by-turn directions
                      (bearing deltas at street-name transitions)
```

The `edgePath` array is parallel to `path`: `edgePath[i]` is the edge traversed between `path[i]` and `path[i+1]`. The front end uses `edgePath[i].s` (stress) to colour each segment, `edgePath[i].l` (length) for stats, `edgePath[i].g` (grade) for elevation stats, and `edgePath[i].n` (name) for turn detection and tooltips.

---

## Data Schema Reference

### `syr_map.json` — nodes

```
nodes["<id>"] = [lng, lat, elev_m]
```

- `id`: OSM node ID as string
- `lng`: WGS-84 longitude, 6 decimal places
- `lat`: WGS-84 latitude, 6 decimal places
- `elev_m`: SRTM elevation in metres, 1 decimal place (null if not available)

### `syr_map.json` — graph edges

```
graph["<from_id>"] = [
  { "t": to_id,  "c": cost,  "s": stress,  "l": length_m,
    "n": name,   "g": grade }
]
```

| Key | Type | Description |
|-----|------|-------------|
| `t` | int | Destination node ID |
| `c` | float | Routing cost (length × LTS penalty, pre-computed) |
| `s` | int | LTS stress level: 1 = comfortable, 2 = moderate, 3 = stressful |
| `l` | float | Physical length in metres |
| `n` | string? | Street name (omitted if null) |
| `g` | float? | Grade as decimal rise/run, signed (omitted if null) |

### `syr_map.json` — ways (stress overlay)

```
ways[i] = {
  "geometry": { "coordinates": [[lng, lat], ...] },
  "properties": { "stress": 1|2|3 }
}
```

Ways are used only for the visual stress overlay on the map. They are not used in routing.

### LTS (Level of Traffic Stress) scale

| LTS | Colour | Description | Typical infrastructure |
|-----|--------|-------------|----------------------|
| 1 | Green | Comfortable — suitable for most cyclists | Shared-use paths, trails, very low-volume streets |
| 2 | Yellow | Moderate — confident cyclists | Protected bike lanes, low-speed roads with lanes |
| 3 | Red | Stressful — experienced cyclists only | No bike infrastructure, higher-speed traffic |

LTS cost penalties baked into `edge.cost` during source data pre-processing: LTS1 = 1×, LTS2 = 2×, LTS3 = 10×.
