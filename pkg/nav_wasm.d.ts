/* tslint:disable */
/* eslint-disable */

/**
 * Load the graph JSON produced by export_json.py.
 * Must be called once before `nearest_node` or `route`.
 */
export function init(json: string): void;

/**
 * Return the node-id string closest to `(lat, lng)`.
 */
export function nearest_node(lat: number, lng: number): string;

/**
 * Run Dijkstra from `from_id` to `to_id`.
 * Returns `{path, edgePath, distM, lengthM}` or `null` if unreachable.
 */
export function route(from_id: string, to_id: string, avoid_hills: boolean): any;

export type InitInput = RequestInfo | URL | Response | BufferSource | WebAssembly.Module;

export interface InitOutput {
    readonly memory: WebAssembly.Memory;
    readonly init: (a: number, b: number) => [number, number];
    readonly nearest_node: (a: number, b: number) => [number, number];
    readonly route: (a: number, b: number, c: number, d: number, e: number) => any;
    readonly __wbindgen_externrefs: WebAssembly.Table;
    readonly __wbindgen_malloc: (a: number, b: number) => number;
    readonly __wbindgen_realloc: (a: number, b: number, c: number, d: number) => number;
    readonly __externref_table_dealloc: (a: number) => void;
    readonly __wbindgen_free: (a: number, b: number, c: number) => void;
    readonly __wbindgen_start: () => void;
}

export type SyncInitInput = BufferSource | WebAssembly.Module;

/**
 * Instantiates the given `module`, which can either be bytes or
 * a precompiled `WebAssembly.Module`.
 *
 * @param {{ module: SyncInitInput }} module - Passing `SyncInitInput` directly is deprecated.
 *
 * @returns {InitOutput}
 */
export function initSync(module: { module: SyncInitInput } | SyncInitInput): InitOutput;

/**
 * If `module_or_path` is {RequestInfo} or {URL}, makes a request and
 * for everything else, calls `WebAssembly.instantiate` directly.
 *
 * @param {{ module_or_path: InitInput | Promise<InitInput> }} module_or_path - Passing `InitInput` directly is deprecated.
 *
 * @returns {Promise<InitOutput>}
 */
export default function __wbg_init (module_or_path?: { module_or_path: InitInput | Promise<InitInput> } | InitInput | Promise<InitInput>): Promise<InitOutput>;
