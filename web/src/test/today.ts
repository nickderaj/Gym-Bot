import type { PlannedDay, TodayPlan } from "../api";

/** A planned exercise as /api/today sends it. */
function planned(
  id: number,
  name: string,
  extra: Partial<PlannedDay["blocks"][number]["exercises"][number]> = {},
): PlannedDay["blocks"][number]["exercises"][number] {
  return {
    block_exercise_id: id,
    exercise_id: id + 100,
    name,
    equipment: "barbell",
    measure: "reps",
    carried_kg: 0,
    sets: 3,
    rep_min: 8,
    rep_max: 12,
    notes: null,
    progression: "load",
    variations: null,
    target: { decision: "progress", load_kg: 62.5, reps: [8, 8, 8] },
    last: null,
    ...extra,
  };
}

/** Upper A: bench alone, then rows and curls as a superset. */
export const UPPER: PlannedDay = {
  id: 11,
  position: 1,
  name: "Upper A",
  week: 2,
  deload: false,
  kind: "gym",
  deload_rpe: null,
  blocks: [
    {
      rest_s: 120,
      exercises: [
        planned(1, "Bench Press", {
          last: {
            started_at: "2026-09-24T08:00:00+00:00",
            sets: [
              {
                set_number: 1,
                reps: 12,
                load_kg: 60,
                duration_s: null,
                rpe: null,
                notes: null,
                client_id: "x",
              },
            ],
          },
        }),
      ],
    },
    {
      rest_s: 60,
      exercises: [
        planned(2, "Cable Row", {
          equipment: "cable",
          sets: 2,
          target: { decision: "repeat", load_kg: 40, reps: [11, 10] },
        }),
        planned(3, "Dead Hang", {
          equipment: "bodyweight",
          measure: "seconds",
          sets: 2,
          rep_min: 30,
          rep_max: 45,
          target: { decision: "start", load_kg: null, reps: [30, 30] },
        }),
      ],
    },
  ],
};

export const TODAY: TodayPlan = {
  program_id: 1,
  program_name: "Upper / Lower",
  training_weeks: 6,
  days: 4,
  day: UPPER,
  travel_days: 0,
  travel_day: null,
  workout_client_id: null,
  training_kind: null,
  left_over: null,
};

/** Travel A: push-ups, which progress by reps and have harder variations. */
export const TRAVEL: PlannedDay = {
  id: 21,
  position: 1,
  name: "Travel A",
  week: 2,
  deload: false,
  kind: "travel",
  deload_rpe: null,
  blocks: [
    {
      rest_s: 60,
      exercises: [
        planned(5, "Push-up", {
          equipment: "bodyweight",
          sets: 4,
          rep_min: 10,
          rep_max: 15,
          progression: "reps",
          variations: "push-up -> feet-elevated -> archer",
          target: { decision: "repeat", load_kg: null, reps: [12, 11, 10, 10] },
        }),
      ],
    },
  ],
};

/** A program with three travel days alongside its gym days. */
export const HYBRID: TodayPlan = { ...TODAY, travel_days: 3, travel_day: TRAVEL };

/** Travel A at the top of its range: the next target is the next variation. */
export const TRAVEL_ADVANCING: PlannedDay = {
  ...TRAVEL,
  blocks: [
    {
      rest_s: 60,
      exercises: [
        planned(5, "Push-up", {
          equipment: "bodyweight",
          sets: 4,
          rep_min: 10,
          rep_max: 15,
          progression: "reps",
          variations: "push-up -> feet-elevated -> archer",
          target: { decision: "advance", load_kg: null, reps: [10, 10, 10, 10] },
        }),
      ],
    },
  ],
};
