/** 策略集合：current（bdd4e69 原样，segment_continue 保留）+ A/C/D 消融 + 待验证 B' 变体。 */
import type { Policy } from "./engine.js";

const base: Omit<Policy, "name"> = {
  earlyCloseOnComplete: false,
  segmentMode: "turnProxy",
  fixedShortSec: 600,
  fixedLongSec: 1800,
  inPlaceMax: Infinity, // 原语义：无等待者时原地续到 bench 时限
  forceRotateOnZeroProgress: false,
  slotPartition: false,
  resumeAgingMin: 0,
  reclassify: false,
  rotateQuotaLong: true,
};

export const POLICIES: Policy[] = [
  // 基线：当前实现
  { ...base, name: "current" },
  // 单因子消融
  { ...base, name: "A", earlyCloseOnComplete: true },
  { ...base, name: "C", segmentMode: "fixed" },
  {
    ...base,
    name: "D",
    slotPartition: true,
    resumeAgingMin: 10,
    reclassify: true,
    rotateQuotaLong: false, // 配额内长题原地续（仅超配额让位）——D 的配额语义一部分
  },
  // 组合
  { ...base, name: "AC", earlyCloseOnComplete: true, segmentMode: "fixed" },
  {
    ...base,
    name: "ACD",
    earlyCloseOnComplete: true,
    segmentMode: "fixed",
    slotPartition: true,
    resumeAgingMin: 10,
    reclassify: true,
    rotateQuotaLong: false,
  },
  // ACD + B'（原地续上限 2 + 零进展轮转）——是否保留 B' 由 bench 决定
  {
    ...base,
    name: "ACD+B'",
    earlyCloseOnComplete: true,
    segmentMode: "fixed",
    slotPartition: true,
    resumeAgingMin: 10,
    reclassify: true,
    rotateQuotaLong: false,
    inPlaceMax: 2,
    forceRotateOnZeroProgress: true,
  },
  // 保险丝变体：只留零进展轮转（无段数上限），对照 B' 是否过度打断
  {
    ...base,
    name: "ACD+rot",
    earlyCloseOnComplete: true,
    segmentMode: "fixed",
    slotPartition: true,
    resumeAgingMin: 10,
    reclassify: true,
    rotateQuotaLong: false,
    forceRotateOnZeroProgress: true,
  },
];
