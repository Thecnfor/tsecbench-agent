/** Planner 持久化：Attempt/Scope 树、findings、mutation/recovery 队列。
 *
 * 独立于人类可读的 log.jsonl：planner.json 是结构化快照，重启后可直接查看
 * “树 + 知识 + 待恢复”，也方便面板/审计；log.jsonl 仍是 append-only 事实流。
 */
import fs from "node:fs";
import path from "node:path";
import type { ScopeState } from "./branch.js";
import type { CorelogEntry, MutationRecord } from "./corelog.js";

export interface PlannerAttemptSnapshot {
  id: number;
  parent: number | null;
  scopeId: number;
  label: string;
  status: string;
  active: boolean;
  pruned: boolean;
  lastCoreTs: number;
  result?: unknown;
}

export interface PlannerSnapshot {
  version: 1;
  updated_at: string;
  task: string;
  scopes: ScopeState[];
  attempts: PlannerAttemptSnapshot[];
  findings: CorelogEntry[];
  mutations: MutationRecord[];
  recovery: {
    owner: number | null;
    pending: string[];
    updated_at: string;
  };
}

export function writePlannerState(file: string, snap: PlannerSnapshot): void {
  const dir = path.dirname(file);
  fs.mkdirSync(dir, { recursive: true });
  const tmp = `${file}.tmp`;
  fs.writeFileSync(tmp, `${JSON.stringify(snap, null, 2)}\n`);
  fs.renameSync(tmp, file);
}

export function readPlannerState(file: string): PlannerSnapshot | null {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8")) as PlannerSnapshot;
  } catch {
    return null;
  }
}
