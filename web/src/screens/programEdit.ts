/**
 * The editable form of a program, and its checks: what the Program screen's
 * editor holds while the owner changes the live program, and the edit it is
 * turned into on save. Values stay as typed, and are parsed when saved.
 */
import type { DayEditBody, Program, ProgramDay, ProgramEditBody, ProgramExercise } from "../api";

export type Progression = "load" | "reps";

/** "A", "B", … for blocks; a superset's exercises are "A1", "A2", … */
export function blockLetter(index: number): string {
  return String.fromCharCode(65 + index);
}

export interface EditSlot {
  /** A stable key for the list; not the server's id. */
  key: string;
  /** The live program exercise this is, which keeps its progression; null for a new one. */
  id: number | null;
  exercise_id: number;
  name: string;
  measure: string;
  sets: string;
  rep_min: string;
  rep_max: string;
  start_load_kg: string;
  notes: string;
  progression: Progression;
  variations: string;
}

export interface EditBlock {
  key: string;
  rest_s: string;
  exercises: EditSlot[];
}

export interface EditDay {
  key: string;
  /** The live day this is, which keeps its place in the rotation; null for a new one. */
  id: number | null;
  name: string;
  blocks: EditBlock[];
}

export interface EditDraft {
  name: string;
  notes: string;
  days: EditDay[];
  travel_days: EditDay[];
}

/** A catalogue exercise, as the editor offers it. */
export interface Choice {
  id: number;
  name: string;
  measure: string;
}

export type Parsed<T> = { ok: true; value: T } | { ok: false; message: string };

let counter = 0;
function newKey(): string {
  counter += 1;
  return `edit-${String(counter)}`;
}

function slotFrom(exercise: ProgramExercise): EditSlot {
  return {
    key: newKey(),
    id: exercise.id,
    exercise_id: exercise.exercise_id,
    name: exercise.name,
    measure: exercise.measure,
    sets: String(exercise.sets),
    rep_min: String(exercise.rep_min),
    rep_max: String(exercise.rep_max),
    start_load_kg: exercise.start_load_kg === null ? "" : String(exercise.start_load_kg),
    notes: exercise.notes ?? "",
    progression: exercise.progression,
    variations: exercise.variations ?? "",
  };
}

function dayFrom(day: ProgramDay): EditDay {
  return {
    key: newKey(),
    id: day.id,
    name: day.name,
    blocks: day.blocks.map((block) => ({
      key: newKey(),
      rest_s: String(block.rest_s),
      exercises: block.exercises.map(slotFrom),
    })),
  };
}

/** The live program as an editable form: every day, block and exercise as it is. */
export function draftFromProgram(program: Program): EditDraft {
  return {
    name: program.name,
    notes: program.notes ?? "",
    days: program.days.map(dayFrom),
    travel_days: program.travel_days.map(dayFrom),
  };
}

/** A new exercise for a block: a reps slot on a travel day, a load slot in the gym. */
export function newSlot(choice: Choice, progression: Progression): EditSlot {
  const timed = choice.measure === "seconds";
  return {
    key: newKey(),
    id: null,
    exercise_id: choice.id,
    name: choice.name,
    measure: choice.measure,
    sets: "3",
    rep_min: timed ? "30" : "8",
    rep_max: timed ? "45" : "12",
    start_load_kg: "",
    notes: "",
    progression,
    variations: "",
  };
}

export function newBlock(slot: EditSlot): EditBlock {
  return { key: newKey(), rest_s: "90", exercises: [slot] };
}

export function newDay(name: string, block: EditBlock): EditDay {
  return { key: newKey(), id: null, name, blocks: [block] };
}

/** `items` with the one at `index` moved by `delta` places; as is if it cannot move. */
export function moved<T>(items: readonly T[], index: number, delta: -1 | 1): T[] {
  const target = index + delta;
  const [from, to] = [items[index], items[target]];
  if (from === undefined || to === undefined) {
    return [...items];
  }
  return items.map((item, position) => {
    if (position === index) {
      return to;
    }
    return position === target ? from : item;
  });
}

/** `items` without the one at `index`. */
export function without<T>(items: readonly T[], index: number): T[] {
  return items.filter((_item, position) => position !== index);
}

/** `items` with the one at `index` replaced. */
export function replaced<T>(items: readonly T[], index: number, item: T): T[] {
  return items.map((current, position) => (position === index ? item : current));
}

function whole(text: string, label: string, min: number, max: number): Parsed<number> {
  const value = Number(text.trim());
  if (text.trim() === "" || !Number.isInteger(value) || value < min || value > max) {
    return {
      ok: false,
      message: `${label} must be a whole number from ${String(min)} to ${String(max)}.`,
    };
  }
  return { ok: true, value };
}

function load(text: string, name: string): Parsed<number | null> {
  const trimmed = text.trim().replace(",", ".");
  if (trimmed === "") {
    return { ok: true, value: null };
  }
  const value = Number(trimmed);
  if (!Number.isFinite(value) || value < 0 || value > 1000) {
    return { ok: false, message: `${name}: the start load must be from 0 to 1000 kg.` };
  }
  return { ok: true, value };
}

function trimmedOrNull(text: string): string | null {
  const trimmed = text.trim();
  return trimmed === "" ? null : trimmed;
}

type SlotBody = DayEditBody["blocks"][number]["exercises"][number];

interface Numbers {
  sets: number;
  rep_min: number;
  rep_max: number;
  start: number | null;
}

function numbersOf(slot: EditSlot): Parsed<Numbers> {
  const sets = whole(slot.sets, `${slot.name}: sets`, 1, 10);
  if (!sets.ok) {
    return sets;
  }
  const rep_min = whole(slot.rep_min, `${slot.name}: the bottom of the range`, 1, 600);
  if (!rep_min.ok) {
    return rep_min;
  }
  const rep_max = whole(slot.rep_max, `${slot.name}: the top of the range`, 1, 600);
  if (!rep_max.ok) {
    return rep_max;
  }
  const start = load(slot.start_load_kg, slot.name);
  if (!start.ok) {
    return start;
  }
  return {
    ok: true,
    value: { sets: sets.value, rep_min: rep_min.value, rep_max: rep_max.value, start: start.value },
  };
}

function slotBody(slot: EditSlot): Parsed<SlotBody> {
  const numbers = numbersOf(slot);
  if (!numbers.ok) {
    return numbers;
  }
  const { sets, rep_min, rep_max, start } = numbers.value;
  if (rep_max < rep_min) {
    return { ok: false, message: `${slot.name}: the top of the range is below its bottom.` };
  }
  if (slot.progression === "reps" && start !== null) {
    return { ok: false, message: `${slot.name}: a reps progression has no load.` };
  }
  return {
    ok: true,
    value: {
      ...(slot.id === null ? {} : { id: slot.id }),
      exercise_id: slot.exercise_id,
      sets,
      rep_min,
      rep_max,
      start_load_kg: start,
      notes: trimmedOrNull(slot.notes),
      progression: slot.progression,
      variations: slot.progression === "reps" ? trimmedOrNull(slot.variations) : null,
    },
  };
}

function blockBody(block: EditBlock, day: string): Parsed<DayEditBody["blocks"][number]> {
  const rest = whole(block.rest_s, `${day}: rest`, 0, 600);
  if (!rest.ok) {
    return rest;
  }
  const exercises: SlotBody[] = [];
  for (const slot of block.exercises) {
    const body = slotBody(slot);
    if (!body.ok) {
      return body;
    }
    exercises.push(body.value);
  }
  return { ok: true, value: { rest_s: rest.value, exercises } };
}

function dayBody(day: EditDay): Parsed<DayEditBody> {
  const name = day.name.trim();
  if (name === "" || name.length > 40) {
    return { ok: false, message: "Every day needs a name of up to 40 characters." };
  }
  if (day.blocks.length === 0 || day.blocks.some((block) => block.exercises.length === 0)) {
    return { ok: false, message: `${name} needs at least one exercise.` };
  }
  const blocks: DayEditBody["blocks"] = [];
  for (const block of day.blocks) {
    const body = blockBody(block, name);
    if (!body.ok) {
      return body;
    }
    blocks.push(body.value);
  }
  return { ok: true, value: { ...(day.id === null ? {} : { id: day.id }), name, blocks } };
}

function daysBody(days: EditDay[]): Parsed<DayEditBody[]> {
  const bodies: DayEditBody[] = [];
  for (const day of days) {
    const body = dayBody(day);
    if (!body.ok) {
      return body;
    }
    bodies.push(body.value);
  }
  return { ok: true, value: bodies };
}

/** The edit to send: the whole program as it should be, or why it cannot be saved yet. */
export function toBody(draft: EditDraft): Parsed<ProgramEditBody> {
  const name = draft.name.trim();
  if (name === "" || name.length > 60) {
    return { ok: false, message: "The program needs a name of up to 60 characters." };
  }
  if (draft.days.length === 0) {
    return { ok: false, message: "The program needs at least one gym day." };
  }
  const days = daysBody(draft.days);
  if (!days.ok) {
    return days;
  }
  const travel = daysBody(draft.travel_days);
  if (!travel.ok) {
    return travel;
  }
  return {
    ok: true,
    value: { name, notes: trimmedOrNull(draft.notes), days: days.value, travel_days: travel.value },
  };
}
