/*!
Navigation engine compiled to WebAssembly.

Exposes three functions to JS/any WASM host:

  init(json: &str)
      Load the graph JSON produced by export_json.py.
      Must be called once before the other two.

  nearest_node(lat: f64, lng: f64) -> String
      Return the node-id string closest to the given coordinate.

  route(from_id: &str, to_id: &str, avoid_hills: bool) -> JsValue
      Run Dijkstra and return a plain-JS object:
      {
        path:     string[],          // ordered node ids
        edgePath: Edge[],            // parallel array, length = path.length-1
        distM:    number,            // total weighted cost
        lengthM:  number,            // actual metres (unweighted)
      }
      Returns null if no route exists.

JSON schema expected (from export_json.py):
  {
    "nodes": { "<id>": [lng, lat, elev_m], ... },
    "graph":  { "<from_id>": [{"t":<int>,"c":<f64>,"s":<u8>,"l":<f64>,"n":<str|null>,"g":<f64|null>}, ...] }
  }
*/

use std::cmp::Ordering;
use std::collections::{BinaryHeap, HashMap};

use serde::{Deserialize, Serialize};
use wasm_bindgen::prelude::*;

// ── JSON input types ────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct GraphJson {
    nodes: HashMap<String, [f64; 3]>, // [lng, lat, elev_m]
    graph: HashMap<String, Vec<RawEdge>>,
}

#[derive(Deserialize, Clone)]
struct RawEdge {
    t: u32,
    c: f64,
    s: u8,
    l: f64,
    n: Option<String>,
    g: Option<f64>,
}

// ── Internal representation ─────────────────────────────────────────────────

struct Node {
    lng:    f64,
    lat:    f64,
    #[allow(dead_code)]
    elev_m: f64,
}

#[derive(Clone)]
struct Edge {
    to_idx:   u32,
    cost:     f64,
    stress:   u8,
    length_m: f64,
    name:     Option<String>,
    grade:    f64, // signed rise/run decimal; 0.0 if unknown
}

struct Graph {
    node_ids:  Vec<String>,
    nodes:     Vec<Node>,
    id_to_idx: HashMap<String, u32>,
    adj:       Vec<Vec<Edge>>,
}

// ── Dijkstra heap item (min-heap via Reverse) ───────────────────────────────

#[derive(PartialEq)]
struct HeapItem {
    cost: f64,
    idx:  u32,
}

impl Eq for HeapItem {}

impl PartialOrd for HeapItem {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for HeapItem {
    fn cmp(&self, other: &Self) -> Ordering {
        other
            .cost
            .partial_cmp(&self.cost)
            .unwrap_or(Ordering::Equal)
    }
}

// ── Haversine ───────────────────────────────────────────────────────────────

fn haversine(lat1: f64, lon1: f64, lat2: f64, lon2: f64) -> f64 {
    const R: f64 = 6_371_000.0;
    let dlat = (lat2 - lat1).to_radians();
    let dlon = (lon2 - lon1).to_radians();
    let a = (dlat / 2.0).sin().powi(2)
        + lat1.to_radians().cos() * lat2.to_radians().cos() * (dlon / 2.0).sin().powi(2);
    R * 2.0 * a.sqrt().atan2((1.0 - a).sqrt())
}

// ── Hill penalty (mirrors JS hillPenalty) ───────────────────────────────────

fn hill_penalty(grade: f64) -> f64 {
    let pct = grade.abs() * 100.0;
    if pct > 12.0 {
        50.0
    } else if pct > 8.0 {
        4.0
    } else if pct > 4.0 {
        1.5
    } else {
        1.0
    }
}

// ── JS output types ─────────────────────────────────────────────────────────

#[derive(Serialize)]
struct JsEdge<'a> {
    t: u32,
    c: f64,
    s: u8,
    l: f64,
    n: Option<&'a str>,
    g: f64,
}

#[derive(Serialize)]
struct RouteResult<'a> {
    path:      Vec<&'a str>,
    #[serde(rename = "edgePath")]
    edge_path: Vec<JsEdge<'a>>,
    #[serde(rename = "distM")]
    dist_m:    f64,
    #[serde(rename = "lengthM")]
    length_m:  f64,
}

// ── WASM state (singleton) ──────────────────────────────────────────────────

static mut GRAPH: Option<Graph> = None;

fn graph() -> &'static Graph {
    unsafe { GRAPH.as_ref().expect("call init() first") }
}

// ── Public WASM API ─────────────────────────────────────────────────────────

/// Load the graph JSON produced by export_json.py.
/// Must be called once before `nearest_node` or `route`.
#[wasm_bindgen]
pub fn init(json: &str) -> Result<(), JsValue> {
    let raw: GraphJson =
        serde_json::from_str(json).map_err(|e| JsValue::from_str(&e.to_string()))?;

    let mut node_ids: Vec<String> = raw.nodes.keys().cloned().collect();
    node_ids.sort();

    let mut id_to_idx: HashMap<String, u32> = HashMap::with_capacity(node_ids.len());
    let mut nodes: Vec<Node> = Vec::with_capacity(node_ids.len());

    for (i, id) in node_ids.iter().enumerate() {
        id_to_idx.insert(id.clone(), i as u32);
        let coords = &raw.nodes[id];
        nodes.push(Node {
            lng:    coords[0],
            lat:    coords[1],
            elev_m: coords[2],
        });
    }

    let mut adj: Vec<Vec<Edge>> = vec![Vec::new(); nodes.len()];
    for (from_id, raw_edges) in &raw.graph {
        let Some(&from_idx) = id_to_idx.get(from_id) else {
            continue;
        };
        for re in raw_edges {
            let to_id = re.t.to_string();
            let Some(&to_idx) = id_to_idx.get(&to_id) else {
                continue;
            };
            adj[from_idx as usize].push(Edge {
                to_idx,
                cost:     re.c,
                stress:   re.s,
                length_m: re.l,
                name:     re.n.clone(),
                grade:    re.g.unwrap_or(0.0),
            });
        }
    }

    unsafe {
        GRAPH = Some(Graph { node_ids, nodes, id_to_idx, adj });
    }
    Ok(())
}

/// Return the node-id string closest to `(lat, lng)`.
#[wasm_bindgen]
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

/// Run Dijkstra from `from_id` to `to_id`.
/// `hill_factor` is 0.0 (ignore hills entirely) → 1.0 (full avoidance penalties).
/// Returns `{path, edgePath, distM, lengthM}` or `null` if unreachable.
#[wasm_bindgen]
pub fn route(from_id: &str, to_id: &str, hill_factor: f64) -> JsValue {
    let g = graph();
    let Some(&start) = g.id_to_idx.get(from_id) else {
        return JsValue::NULL;
    };
    let Some(&end) = g.id_to_idx.get(to_id) else {
        return JsValue::NULL;
    };

    let n = g.nodes.len();
    let mut dist = vec![f64::INFINITY; n];
    let mut prev: Vec<Option<(u32, usize)>> = vec![None; n];
    dist[start as usize] = 0.0;

    let mut heap = BinaryHeap::new();
    heap.push(HeapItem { cost: 0.0, idx: start });

    while let Some(HeapItem { cost: d, idx: u }) = heap.pop() {
        if u == end {
            break;
        }
        if d > dist[u as usize] {
            continue;
        }
        for (ei, edge) in g.adj[u as usize].iter().enumerate() {
            // Interpolate: 0.0 = no penalty, 1.0 = full hill_penalty
            let penalty = 1.0 + (hill_penalty(edge.grade) - 1.0) * hill_factor.clamp(0.0, 1.0);
            let nd = d + edge.cost * penalty;
            let v = edge.to_idx as usize;
            if nd < dist[v] {
                dist[v] = nd;
                prev[v] = Some((u, ei));
                heap.push(HeapItem { cost: nd, idx: edge.to_idx });
            }
        }
    }

    if dist[end as usize].is_infinite() {
        return JsValue::NULL;
    }

    // Reconstruct path
    let mut path_idx: Vec<u32> = Vec::new();
    let mut edge_seq: Vec<(u32, usize)> = Vec::new();
    let mut cur = end;
    while let Some((p, ei)) = prev[cur as usize] {
        path_idx.push(cur);
        edge_seq.push((p, ei));
        cur = p;
    }
    path_idx.push(start);
    path_idx.reverse();
    edge_seq.reverse();

    let path: Vec<&str> = path_idx
        .iter()
        .map(|&i| g.node_ids[i as usize].as_str())
        .collect();

    let mut total_length = 0.0f64;
    let edge_path: Vec<JsEdge> = edge_seq
        .iter()
        .map(|&(from_idx, ei)| {
            let e = &g.adj[from_idx as usize][ei];
            total_length += e.length_m;
            JsEdge {
                t: e.to_idx,
                c: e.cost,
                s: e.stress,
                l: e.length_m,
                n: e.name.as_deref(),
                g: e.grade,
            }
        })
        .collect();

    let result = RouteResult {
        path,
        edge_path,
        dist_m:   dist[end as usize],
        length_m: total_length,
    };

    serde_wasm_bindgen::to_value(&result).unwrap_or(JsValue::NULL)
}
