/**
 * Contract between the console and the tool registry. `tool-schemas.json` is generated from the tools' output
 * models (functions/tests/test_console_contract.py). Type checking proves the console's types declare only fields
 * the tools return; the tests prove the mock fixtures, which those types describe, are real tool results.
 */
import { describe, expect, expectTypeOf, it, vi } from "vitest";
import type { AgentSnapshot } from "./agui";
import { PROGRAM_STATUSES, type Epg, type EpgProgram, type Plan, type PlannedBreak, type PlannedProgram, type Violation } from "./schedule";
import schemas from "./tool-schemas.json";
import type {
  Asset,
  CdnPlayback,
  ChannelAlert,
  ChannelDetail,
  ChannelStateChange,
  ChannelSummary,
  Ladder,
  LadderKit,
  SkippedProgram,
  StartChannelResult,
} from "./tools";

// The mock modules read `window` when they load.
vi.hoisted(() => {
  vi.stubGlobal("window", {
    location: { href: "http://localhost:5173/", search: "" },
    setTimeout: (resolve: () => void) => {
      resolve();
      return 0;
    },
    clearTimeout: () => undefined,
  });
});

const { ASSETS, mockPlan, mockPublished, mockSchedule } = await import("../mock/fixtures");
const { mockGateway } = await import("../mock/gateway");

/** The JSON Schema subset the generator emits. */
interface Schema {
  readonly type?: string;
  readonly enum?: readonly unknown[];
  readonly const?: unknown;
  readonly properties?: Readonly<Record<string, Schema>>;
  readonly required?: readonly string[];
  readonly items?: Schema;
  readonly additionalProperties?: Schema | boolean;
  readonly anyOf?: readonly Schema[];
}

type ToolName = keyof typeof schemas.tools;
const TOOLS: Readonly<Record<ToolName, Schema>> = schemas.tools;

const isObject = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);

const TYPES: Readonly<Record<string, (value: unknown) => boolean>> = {
  object: isObject,
  array: Array.isArray,
  string: (value) => typeof value === "string",
  integer: (value) => Number.isInteger(value),
  number: (value) => typeof value === "number",
  boolean: (value) => typeof value === "boolean",
};

/** Every way `value` breaks `schema`; a property the schema does not declare is a problem too. */
function problems(schema: Schema, value: unknown, path = "$"): string[] {
  if (schema.anyOf) {
    return schema.anyOf.some((variant) => problems(variant, value, path).length === 0) ? [] : [`${path}: matches no variant`];
  }
  if (schema.const !== undefined && value !== schema.const) {
    return [`${path}: expected ${JSON.stringify(schema.const)}`];
  }
  if (schema.enum && !schema.enum.includes(value)) {
    return [`${path}: ${JSON.stringify(value)} is not one of ${schema.enum.join(", ")}`];
  }
  if (schema.type && !TYPES[schema.type]?.(value)) {
    return [`${path}: expected ${schema.type}, got ${JSON.stringify(value)}`];
  }
  const { items, additionalProperties: extra } = schema;
  if (Array.isArray(value)) {
    return items ? value.flatMap((item, index) => problems(items, item, `${path}[${String(index)}]`)) : [];
  }
  if (!isObject(value)) {
    return [];
  }
  const missing = (schema.required ?? []).filter((key) => !(key in value)).map((key) => `${path}.${key}: missing`);
  const fields = Object.entries(value).flatMap(([key, child]) => {
    const declared = schema.properties?.[key] ?? (typeof extra === "object" ? extra : undefined);
    if (declared) {
      return problems(declared, child, `${path}.${key}`);
    }
    return extra === true ? [] : [`${path}.${key}: not in the tool output`];
  });
  return [...missing, ...fields];
}

/** The wire format of a successful call: the dispatcher adds `ok: true`. */
const result = (output: object): object => ({ ok: true, ...output });

describe("mock Gateway answers are real tool results", () => {
  const gateway = mockGateway();
  it.each<[ToolName, Record<string, unknown>]>([
    ["channels___list_channels", {}],
    ["channels___get_channel", { channel: "fast-demo" }],
    ["channels___start_channel", { channel: "fast-kids" }],
    ["channels___stop_channel", { channel: "fast-demo" }],
    ["library___find_assets", { limit: 100 }],
    ["encoding___list_ladders", {}],
  ])("%s", async (tool, args) => {
    expect(problems(TOOLS[tool], result(await gateway.call<object>(tool, args)))).toEqual([]);
  });
});

describe("mock agent snapshots are real schedule tool results", () => {
  const plan = mockPlan("fast-demo");
  it.each<[ToolName, object]>([
    ["schedule___plan", plan],
    ["schedule___publish", mockPublished(plan)],
    ["schedule___get_schedule", mockSchedule("fast-demo")],
  ])("%s", (tool, output) => {
    expect(problems(TOOLS[tool], result(output))).toEqual([]);
  });
});

describe("console statuses", () => {
  it.each<ToolName>(["schedule___publish", "schedule___get_schedule"])("cover every EPG program status %s returns", (tool) => {
    const status = TOOLS[tool].properties?.programs?.items?.properties?.status;
    expect(status?.enum?.length).toBeGreaterThan(0);
    expect(PROGRAM_STATUSES).toEqual(expect.arrayContaining([...(status?.enum ?? [])]));
  });
});

describe("problems", () => {
  const schema = TOOLS.library___find_assets;
  const [asset] = ASSETS;

  it("reports fields the tool does not return, and missing ones", () => {
    const legacy = { ...Object.fromEntries(Object.entries(asset ?? {}).filter(([key]) => key !== "break_offset_count")), break_offsets_ms: [] };
    expect(problems(schema, result({ assets: [legacy] }))).toEqual([
      "$.assets[0].break_offset_count: missing",
      "$.assets[0].break_offsets_ms: not in the tool output",
    ]);
  });

  it("reports wrong types and enum values", () => {
    expect(problems(schema, result({ assets: [{ ...asset, kind: "movie", break_offset_count: 1.5 }] }))).toEqual([
      '$.assets[0].kind: "movie" is not one of content, slate, ad',
      "$.assets[0].break_offset_count: expected integer, got 1.5",
    ]);
  });
});

// --- types: every field a console type declares exists in the tool output (checked by `tsc`, not at runtime) ------

type Tools = typeof schemas.tools;
type Props<S> = S extends { readonly properties: infer P } ? P : never;
type Items<S> = S extends { readonly items: infer I } ? I : never;
type Field<S, K extends keyof Props<S>> = Props<S>[K];
/** Keys of `T` the output schema `S` does not have; `never` when the console type matches the tool. */
type Undeclared<T, S> = Exclude<keyof T, keyof Props<S>>;

type ListedChannel = Items<Field<Tools["channels___list_channels"], "channels">>;
type GetChannel = Tools["channels___get_channel"];
type StartChannel = Tools["channels___start_channel"];
type StopChannel = Tools["channels___stop_channel"];
type ListedAsset = Items<Field<Tools["library___find_assets"], "assets">>;
type ListedLadder = Items<Field<Tools["encoding___list_ladders"], "ladders">>;
type PlanResult = Tools["schedule___plan"];
type PlanRow = Items<Field<PlanResult, "programs">>;
type PublishResult = Tools["schedule___publish"];
type ScheduleResult = Tools["schedule___get_schedule"];

describe("console types", () => {
  it("declare only fields the tools return", () => {
    // A mismatch fails `npm run typecheck` with TS2554 on the line of the type that declares an unknown field.
    expectTypeOf<Undeclared<ChannelSummary, ListedChannel>>().toEqualTypeOf<never>();
    expectTypeOf<Undeclared<ChannelDetail, GetChannel>>().toEqualTypeOf<never>();
    expectTypeOf<Undeclared<CdnPlayback, Items<Field<GetChannel, "cdn_playback">>>>().toEqualTypeOf<never>();
    expectTypeOf<Undeclared<ChannelStateChange, StopChannel>>().toEqualTypeOf<never>();
    expectTypeOf<Undeclared<StartChannelResult, StartChannel>>().toEqualTypeOf<never>();
    expectTypeOf<Undeclared<SkippedProgram, Items<Field<StartChannel, "skipped">>>>().toEqualTypeOf<never>();
    expectTypeOf<Undeclared<ChannelAlert, Items<Field<StartChannel, "alerts">>>>().toEqualTypeOf<never>();
    expectTypeOf<Undeclared<Asset, ListedAsset>>().toEqualTypeOf<never>();
    expectTypeOf<Undeclared<Ladder, ListedLadder>>().toEqualTypeOf<never>();
    expectTypeOf<Undeclared<LadderKit, Field<ListedLadder, "kit">>>().toEqualTypeOf<never>();
    expectTypeOf<Undeclared<Plan, PlanResult>>().toEqualTypeOf<never>();
    expectTypeOf<Undeclared<PlannedProgram, PlanRow>>().toEqualTypeOf<never>();
    expectTypeOf<Undeclared<PlannedBreak, Items<Field<PlanRow, "breaks">>>>().toEqualTypeOf<never>();
    expectTypeOf<Undeclared<Violation, Items<Field<PlanResult, "violations">>>>().toEqualTypeOf<never>();
    expectTypeOf<Undeclared<Epg, PublishResult>>().toEqualTypeOf<never>();
    expectTypeOf<Undeclared<Epg, ScheduleResult>>().toEqualTypeOf<never>();
    expectTypeOf<Undeclared<EpgProgram, Items<Field<PublishResult, "programs">>>>().toEqualTypeOf<never>();
  });

  it("type the shared state as the schedule tools' results", () => {
    expectTypeOf<AgentSnapshot["plan"]>().toEqualTypeOf<Plan | null>();
    expectTypeOf<AgentSnapshot["schedule"]>().toEqualTypeOf<Epg | null>();
  });
});
