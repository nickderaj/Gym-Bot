import { describe, expect, it } from "vitest";

import { programsSchema } from "../api";
import type { Program } from "../api";
import {
  blockLetter,
  draftFromProgram,
  moved,
  newBlock,
  newDay,
  newSlot,
  replaced,
  toBody,
  without,
} from "./programEdit";
import type { EditDraft } from "./programEdit";

const BENCH = { id: 101, name: "Bench Press", measure: "reps" };
const HANG = { id: 104, name: "Dead Hang", measure: "seconds" };

function program(): Program {
  const parsed = programsSchema.parse({
    active: {
      id: 1,
      name: "Hybrid",
      notes: "Two sets.",
      training_weeks: 6,
      status: "active",
      started_at: "2026-09-28T07:00:00+00:00",
      version: 2,
      change: "edit",
      days: [
        {
          id: 11,
          name: "Upper",
          blocks: [
            {
              rest_s: 120,
              exercises: [
                {
                  id: 1,
                  exercise_id: 101,
                  name: "Bench Press",
                  equipment: "barbell",
                  measure: "reps",
                  sets: 3,
                  rep_min: 8,
                  rep_max: 12,
                  start_load_kg: 60,
                  notes: "pause",
                },
              ],
            },
          ],
        },
      ],
      travel_days: [
        {
          id: 12,
          name: "Travel A",
          blocks: [
            {
              rest_s: 60,
              exercises: [
                {
                  id: 2,
                  exercise_id: 105,
                  name: "Push-up",
                  equipment: "bodyweight",
                  measure: "reps",
                  sets: 4,
                  rep_min: 10,
                  rep_max: 15,
                  start_load_kg: null,
                  notes: null,
                  progression: "reps",
                  variations: "push-up -> archer",
                },
              ],
            },
          ],
        },
      ],
    },
    proposed: null,
    next: null,
  });
  if (parsed.active === null) {
    throw new Error("fixture");
  }
  return parsed.active;
}

function draft(): EditDraft {
  return draftFromProgram(program());
}

function firstSlot(
  of: EditDraft,
): EditDraft["days"][number]["blocks"][number]["exercises"][number] {
  const slot = of.days[0]?.blocks[0]?.exercises[0];
  if (slot === undefined) {
    throw new Error("fixture");
  }
  return slot;
}

describe("the editable form", () => {
  it("holds the live program as it is, with the ids that keep it", () => {
    const form = draft();

    expect(form.name).toBe("Hybrid");
    expect(form.notes).toBe("Two sets.");
    expect(form.days.map((day) => [day.id, day.name])).toEqual([[11, "Upper"]]);
    expect(form.travel_days.map((day) => [day.id, day.name])).toEqual([[12, "Travel A"]]);
    expect(firstSlot(form)).toMatchObject({
      id: 1,
      exercise_id: 101,
      sets: "3",
      rep_min: "8",
      rep_max: "12",
      start_load_kg: "60",
      notes: "pause",
      progression: "load",
      variations: "",
    });
    const pushup = form.travel_days[0]?.blocks[0]?.exercises[0];
    expect(pushup).toMatchObject({
      start_load_kg: "",
      notes: "",
      progression: "reps",
      variations: "push-up -> archer",
    });
    expect(form.days[0]?.blocks[0]?.rest_s).toBe("120");
  });

  it("has no notes as empty text", () => {
    const live = program();
    expect(draftFromProgram({ ...live, notes: null }).notes).toBe("");
  });

  it("keys every list item apart", () => {
    const keys = [
      ...draft().days.flatMap((day) => [day.key, ...day.blocks.flatMap((b) => [b.key])]),
      ...draft().days.flatMap((day) => day.blocks.flatMap((b) => b.exercises.map((e) => e.key))),
    ];

    expect(new Set(keys).size).toBe(keys.length);
  });

  it("starts a new exercise with sensible defaults, as a new one", () => {
    const reps = newSlot(BENCH, "load");
    const timed = newSlot(HANG, "reps");

    expect(reps).toMatchObject({
      id: null,
      exercise_id: 101,
      name: "Bench Press",
      sets: "3",
      rep_min: "8",
      rep_max: "12",
      progression: "load",
    });
    expect(timed).toMatchObject({ rep_min: "30", rep_max: "45", progression: "reps" });
    expect(reps.key).not.toBe(timed.key);
  });

  it("makes a new block and day, with no id to keep", () => {
    const block = newBlock(newSlot(BENCH, "load"));
    const day = newDay("Day 3", block);

    expect(block).toMatchObject({ rest_s: "90" });
    expect(block.exercises).toHaveLength(1);
    expect(day).toMatchObject({ id: null, name: "Day 3" });
    expect(day.blocks).toEqual([block]);
  });

  it("letters blocks", () => {
    expect([0, 1, 25].map(blockLetter)).toEqual(["A", "B", "Z"]);
  });
});

describe("list changes", () => {
  it("moves an item one place", () => {
    expect(moved(["a", "b", "c"], 0, 1)).toEqual(["b", "a", "c"]);
    expect(moved(["a", "b", "c"], 2, -1)).toEqual(["a", "c", "b"]);
    expect(moved(["a", "b", "c"], 1, 1)).toEqual(["a", "c", "b"]);
  });

  it("does not move an item off either end", () => {
    expect(moved(["a", "b"], 0, -1)).toEqual(["a", "b"]);
    expect(moved(["a", "b"], 1, 1)).toEqual(["a", "b"]);
    expect(moved([], 0, 1)).toEqual([]);
  });

  it("removes and replaces by place, leaving the original alone", () => {
    const items = ["a", "b", "c"];

    expect(without(items, 1)).toEqual(["a", "c"]);
    expect(replaced(items, 2, "z")).toEqual(["a", "b", "z"]);
    expect(items).toEqual(["a", "b", "c"]);
  });
});

describe("the edit to send", () => {
  it("is the whole program, ids kept, numbers parsed and text trimmed", () => {
    const form = draft();
    form.name = "  Hybrid v2 ";
    form.notes = "  ";
    const slot = firstSlot(form);
    slot.sets = "4";
    slot.start_load_kg = "62,5";
    slot.notes = " slow ";

    const result = toBody(form);

    expect(result).toEqual({
      ok: true,
      value: {
        name: "Hybrid v2",
        notes: null,
        days: [
          {
            id: 11,
            name: "Upper",
            blocks: [
              {
                rest_s: 120,
                exercises: [
                  {
                    id: 1,
                    exercise_id: 101,
                    sets: 4,
                    rep_min: 8,
                    rep_max: 12,
                    start_load_kg: 62.5,
                    notes: "slow",
                    progression: "load",
                    variations: null,
                  },
                ],
              },
            ],
          },
        ],
        travel_days: [
          {
            id: 12,
            name: "Travel A",
            blocks: [
              {
                rest_s: 60,
                exercises: [
                  {
                    id: 2,
                    exercise_id: 105,
                    sets: 4,
                    rep_min: 10,
                    rep_max: 15,
                    start_load_kg: null,
                    notes: null,
                    progression: "reps",
                    variations: "push-up -> archer",
                  },
                ],
              },
            ],
          },
        ],
      },
    });
  });

  it("leaves the id out of what is new", () => {
    const form = draft();
    const added = newDay("Day 2", newBlock(newSlot(BENCH, "load")));
    form.days = [...form.days, added];

    const result = toBody(form);

    expect(result.ok).toBe(true);
    if (result.ok) {
      const day = result.value.days[1];
      expect(day).toBeDefined();
      expect(day).not.toHaveProperty("id");
      expect(day?.blocks[0]?.exercises[0]).not.toHaveProperty("id");
      expect(day?.blocks[0]?.exercises[0]).toMatchObject({ exercise_id: 101, sets: 3 });
    }
  });

  it("drops variations from a slot that progresses by load", () => {
    const form = draft();
    const slot = firstSlot(form);
    slot.variations = "left over from a reps slot";

    const result = toBody(form);

    expect(result.ok && result.value.days[0]?.blocks[0]?.exercises[0]?.variations).toBeNull();
  });

  it.each([
    [
      (form: EditDraft): void => {
        form.name = "  ";
      },
      "The program needs a name of up to 60 characters.",
    ],
    [
      (form: EditDraft): void => {
        form.name = "x".repeat(61);
      },
      "The program needs a name of up to 60 characters.",
    ],
    [
      (form: EditDraft): void => {
        form.days = [];
      },
      "The program needs at least one gym day.",
    ],
    [
      (form: EditDraft): void => {
        form.days = [{ ...firstDay(form), name: " " }];
      },
      "Every day needs a name of up to 40 characters.",
    ],
    [
      (form: EditDraft): void => {
        form.days = [{ ...firstDay(form), name: "x".repeat(41) }];
      },
      "Every day needs a name of up to 40 characters.",
    ],
    [
      (form: EditDraft): void => {
        form.days = [{ ...firstDay(form), blocks: [] }];
      },
      "Upper needs at least one exercise.",
    ],
    [
      (form: EditDraft): void => {
        form.days = [{ ...firstDay(form), blocks: [{ key: "b", rest_s: "90", exercises: [] }] }];
      },
      "Upper needs at least one exercise.",
    ],
    [
      (form: EditDraft): void => {
        blockOf(form).rest_s = "601";
      },
      "Upper: rest must be a whole number from 0 to 600.",
    ],
    [
      (form: EditDraft): void => {
        blockOf(form).rest_s = "";
      },
      "Upper: rest must be a whole number from 0 to 600.",
    ],
    [
      (form: EditDraft): void => {
        firstSlot(form).sets = "0";
      },
      "Bench Press: sets must be a whole number from 1 to 10.",
    ],
    [
      (form: EditDraft): void => {
        firstSlot(form).sets = "11";
      },
      "Bench Press: sets must be a whole number from 1 to 10.",
    ],
    [
      (form: EditDraft): void => {
        firstSlot(form).sets = "2.5";
      },
      "Bench Press: sets must be a whole number from 1 to 10.",
    ],
    [
      (form: EditDraft): void => {
        firstSlot(form).rep_min = "0";
      },
      "Bench Press: the bottom of the range must be a whole number from 1 to 600.",
    ],
    [
      (form: EditDraft): void => {
        firstSlot(form).rep_max = "601";
      },
      "Bench Press: the top of the range must be a whole number from 1 to 600.",
    ],
    [
      (form: EditDraft): void => {
        firstSlot(form).rep_max = "7";
      },
      "Bench Press: the top of the range is below its bottom.",
    ],
    [
      (form: EditDraft): void => {
        firstSlot(form).start_load_kg = "heavy";
      },
      "Bench Press: the start load must be from 0 to 1000 kg.",
    ],
    [
      (form: EditDraft): void => {
        firstSlot(form).start_load_kg = "-5";
      },
      "Bench Press: the start load must be from 0 to 1000 kg.",
    ],
    [
      (form: EditDraft): void => {
        firstSlot(form).start_load_kg = "1001";
      },
      "Bench Press: the start load must be from 0 to 1000 kg.",
    ],
    [
      (form: EditDraft): void => {
        const slot = firstSlot(form);
        slot.progression = "reps";
      },
      "Bench Press: a reps progression has no load.",
    ],
    [
      (form: EditDraft): void => {
        const travel = form.travel_days[0];
        if (travel !== undefined) {
          travel.name = "";
        }
      },
      "Every day needs a name of up to 40 characters.",
    ],
  ])("says what is wrong: %#", (break_, message) => {
    const form = draft();
    break_(form);

    expect(toBody(form)).toEqual({ ok: false, message });
  });

  it("allows no gym day names to be blank only when travel days are fine", () => {
    const form = draft();
    form.travel_days = [];

    expect(toBody(form).ok).toBe(true);
  });
});

function firstDay(form: EditDraft): EditDraft["days"][number] {
  const day = form.days[0];
  if (day === undefined) {
    throw new Error("fixture");
  }
  return day;
}

function blockOf(form: EditDraft): EditDraft["days"][number]["blocks"][number] {
  const block = firstDay(form).blocks[0];
  if (block === undefined) {
    throw new Error("fixture");
  }
  return block;
}
