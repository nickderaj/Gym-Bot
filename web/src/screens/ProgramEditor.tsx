import { useRef, useState } from "react";
import type { ChangeEvent, ReactElement } from "react";

import { editProgram, exerciseListSchema, useApi } from "../api";
import type { Program } from "../api";
import { Load } from "../components";
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
import type { Choice, EditBlock, EditDay, EditDraft, EditSlot, Progression } from "./programEdit";

interface Move {
  /** Null where it cannot move that way (first, or last). */
  up: (() => void) | null;
  down: (() => void) | null;
  remove: () => void;
}

/** Move up, move down and remove, named for what they act on. */
function Arrows({ what, move }: { what: string; move: Move }): ReactElement {
  return (
    <span className="edit-arrows">
      <button
        type="button"
        className="link"
        aria-label={`Move ${what} up`}
        disabled={move.up === null}
        onClick={move.up ?? undefined}
      >
        ↑
      </button>
      <button
        type="button"
        className="link"
        aria-label={`Move ${what} down`}
        disabled={move.down === null}
        onClick={move.down ?? undefined}
      >
        ↓
      </button>
      <button
        type="button"
        className="link danger"
        aria-label={`Remove ${what}`}
        onClick={move.remove}
      >
        Remove
      </button>
    </span>
  );
}

/** What to do with item `index` of `count`, in a list the parent keeps. */
function movesOf<T>(items: T[], index: number, change: (next: T[]) => void): Move {
  return {
    up:
      index === 0
        ? null
        : (): void => {
            change(moved(items, index, -1));
          },
    down:
      index === items.length - 1
        ? null
        : (): void => {
            change(moved(items, index, 1));
          },
    remove: (): void => {
      change(without(items, index));
    },
  };
}

function ExercisePicker({
  choices,
  label,
  onPick,
}: {
  choices: Choice[];
  label: string;
  onPick: (choice: Choice) => void;
}): ReactElement {
  const pick = (event: ChangeEvent<HTMLSelectElement>): void => {
    const found = choices.find((choice) => String(choice.id) === event.target.value);
    if (found !== undefined) {
      onPick(found);
    }
  };
  return (
    <select aria-label={label} value="" onChange={pick}>
      <option value="">{`${label}…`}</option>
      {choices.map((choice) => (
        <option key={choice.id} value={choice.id}>
          {choice.name}
        </option>
      ))}
    </select>
  );
}

function Field({
  label,
  value,
  onChange,
  inputMode,
  wide = false,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  inputMode?: "numeric" | "decimal";
  wide?: boolean;
}): ReactElement {
  return (
    <label className={wide ? "field edit-wide" : "field"}>
      {label}
      <input
        value={value}
        inputMode={inputMode}
        onChange={(event) => {
          onChange(event.target.value);
        }}
      />
    </label>
  );
}

function SlotEditor({
  slot,
  label,
  move,
  onChange,
}: {
  slot: EditSlot;
  label: string;
  move: Move;
  onChange: (slot: EditSlot) => void;
}): ReactElement {
  const set = (change: Partial<EditSlot>): void => {
    onChange({ ...slot, ...change });
  };
  const timed = slot.measure === "seconds";
  const reps = slot.progression === "reps";
  return (
    <li className="edit-slot">
      <div className="edit-head">
        <strong>
          <span className="block-letter">{label}</span> {slot.name}
        </strong>
        <Arrows what={slot.name} move={move} />
      </div>
      <div className="edit-grid">
        <Field
          label={`${slot.name} sets`}
          value={slot.sets}
          inputMode="numeric"
          onChange={(sets) => {
            set({ sets });
          }}
        />
        <Field
          label={`${slot.name} ${timed ? "seconds" : "reps"} from`}
          value={slot.rep_min}
          inputMode="numeric"
          onChange={(rep_min) => {
            set({ rep_min });
          }}
        />
        <Field
          label={`${slot.name} to`}
          value={slot.rep_max}
          inputMode="numeric"
          onChange={(rep_max) => {
            set({ rep_max });
          }}
        />
        {reps ? null : (
          <Field
            label={`${slot.name} start kg`}
            value={slot.start_load_kg}
            inputMode="decimal"
            onChange={(start_load_kg) => {
              set({ start_load_kg });
            }}
          />
        )}
      </div>
      <label className="field">
        {`${slot.name} progression`}
        <select
          value={slot.progression}
          onChange={(event) => {
            // A reps exercise has no kg: its load field goes, and so does what was in it.
            set(
              event.target.value === "reps"
                ? { progression: "reps", start_load_kg: "" }
                : { progression: "load" },
            );
          }}
        >
          <option value="load">Add load (double progression)</option>
          <option value="reps">Reps, then a harder variation (no kg)</option>
        </select>
      </label>
      {reps ? (
        <Field
          label={`${slot.name} variations`}
          value={slot.variations}
          wide
          onChange={(variations) => {
            set({ variations });
          }}
        />
      ) : null}
      <Field
        label={`${slot.name} notes`}
        value={slot.notes}
        wide
        onChange={(notes) => {
          set({ notes });
        }}
      />
    </li>
  );
}

const SUPERSET_MAX = 3;

function BlockEditor({
  block,
  index,
  move,
  choices,
  progression,
  onChange,
}: {
  block: EditBlock;
  index: number;
  move: Move;
  choices: Choice[];
  progression: Progression;
  onChange: (block: EditBlock) => void;
}): ReactElement {
  const letter = blockLetter(index);
  const superset = block.exercises.length > 1;
  const slots = (exercises: EditSlot[]): void => {
    if (exercises.length === 0) {
      move.remove();
    } else {
      onChange({ ...block, exercises });
    }
  };
  return (
    <li className={superset ? "edit-block superset" : "edit-block"}>
      <div className="edit-head">
        <span className="muted">
          Block {letter}
          {superset ? " · superset" : ""}
        </span>
        <Arrows what={`block ${letter}`} move={move} />
      </div>
      <ul className="edit-slots">
        {block.exercises.map((slot, position) => (
          <SlotEditor
            key={slot.key}
            slot={slot}
            label={superset ? `${letter}${String(position + 1)}` : letter}
            move={movesOf(block.exercises, position, slots)}
            onChange={(next) => {
              onChange({ ...block, exercises: replaced(block.exercises, position, next) });
            }}
          />
        ))}
      </ul>
      <Field
        label={`Rest after block ${letter} (s)`}
        value={block.rest_s}
        inputMode="numeric"
        onChange={(rest_s) => {
          onChange({ ...block, rest_s });
        }}
      />
      {block.exercises.length < SUPERSET_MAX ? (
        <ExercisePicker
          choices={choices}
          label={`Superset with block ${letter}`}
          onPick={(choice) => {
            onChange({ ...block, exercises: [...block.exercises, newSlot(choice, progression)] });
          }}
        />
      ) : null}
    </li>
  );
}

function DayEditor({
  day,
  number,
  kind,
  move,
  choices,
  onChange,
}: {
  day: EditDay;
  number: number;
  kind: "gym" | "travel";
  move: Move;
  choices: Choice[];
  onChange: (day: EditDay) => void;
}): ReactElement {
  const progression: Progression = kind === "travel" ? "reps" : "load";
  const title = `${kind === "travel" ? "Travel" : "Gym"} day ${String(number)}`;
  const blocks = (next: EditBlock[]): void => {
    if (next.length === 0) {
      move.remove();
    } else {
      onChange({ ...day, blocks: next });
    }
  };
  return (
    <li className="card edit-day">
      <div className="edit-head">
        <h3>
          <span className="muted">{title}</span>
        </h3>
        <Arrows what={title.toLowerCase()} move={move} />
      </div>
      <Field
        label={`${title} name`}
        value={day.name}
        wide
        onChange={(name) => {
          onChange({ ...day, name });
        }}
      />
      <ol className="edit-blocks">
        {day.blocks.map((block, index) => (
          <BlockEditor
            key={block.key}
            block={block}
            index={index}
            move={movesOf(day.blocks, index, blocks)}
            choices={choices}
            progression={progression}
            onChange={(next) => {
              onChange({ ...day, blocks: replaced(day.blocks, index, next) });
            }}
          />
        ))}
      </ol>
      <ExercisePicker
        choices={choices}
        label={`Add an exercise to ${title.toLowerCase()}`}
        onPick={(choice) => {
          onChange({ ...day, blocks: [...day.blocks, newBlock(newSlot(choice, progression))] });
        }}
      />
    </li>
  );
}

const MAX_DAYS = 7;

function DaySet({
  title,
  kind,
  days,
  choices,
  onChange,
}: {
  title: string;
  kind: "gym" | "travel";
  days: EditDay[];
  choices: Choice[];
  onChange: (days: EditDay[]) => void;
}): ReactElement {
  const progression: Progression = kind === "travel" ? "reps" : "load";
  const noun = kind === "travel" ? "travel day" : "gym day";
  return (
    <section aria-label={title} className="edit-set">
      <h2>{title}</h2>
      {days.length === 0 ? <p className="muted">No {noun}s.</p> : null}
      <ol className="list">
        {days.map((day, index) => (
          <DayEditor
            key={day.key}
            day={day}
            number={index + 1}
            kind={kind}
            move={movesOf(days, index, onChange)}
            choices={choices}
            onChange={(next) => {
              onChange(replaced(days, index, next));
            }}
          />
        ))}
      </ol>
      {days.length < MAX_DAYS ? (
        <ExercisePicker
          choices={choices}
          label={`Add a ${noun} starting with`}
          onPick={(choice) => {
            const name = `${kind === "travel" ? "Travel" : "Day"} ${String(days.length + 1)}`;
            onChange([...days, newDay(name, newBlock(newSlot(choice, progression)))]);
          }}
        />
      ) : null}
    </section>
  );
}

function Form({
  program,
  choices,
  onSaved,
  onCancel,
}: {
  program: Program;
  choices: Choice[];
  onSaved: () => void;
  onCancel: () => void;
}): ReactElement {
  const [draft, setDraft] = useState<EditDraft>(() => draftFromProgram(program));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // One save at a time: a second tap must not race the first to the server.
  const saving = useRef(false);

  const save = async (): Promise<void> => {
    const body = toBody(draft);
    if (!body.ok) {
      setError(body.message);
      return;
    }
    if (saving.current) {
      return;
    }
    saving.current = true;
    setBusy(true);
    setError(null);
    const result = await editProgram(body.value);
    saving.current = false;
    setBusy(false);
    if (result.kind === "ok") {
      onSaved();
      return;
    }
    setError(result.message);
  };

  return (
    <form
      className="editor"
      aria-label="Edit program"
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <p className="muted">
        Your week, next days, history and each kept exercise&rsquo;s progression stay. The version
        you leave is kept.
      </p>
      <div className="card edit-head-card">
        <Field
          label="Program name"
          value={draft.name}
          wide
          onChange={(name) => {
            setDraft({ ...draft, name });
          }}
        />
        <Field
          label="Program notes"
          value={draft.notes}
          wide
          onChange={(notes) => {
            setDraft({ ...draft, notes });
          }}
        />
      </div>
      <DaySet
        title="Gym days"
        kind="gym"
        days={draft.days}
        choices={choices}
        onChange={(days) => {
          setDraft({ ...draft, days });
        }}
      />
      <DaySet
        title="Travel days"
        kind="travel"
        days={draft.travel_days}
        choices={choices}
        onChange={(travel_days) => {
          setDraft({ ...draft, travel_days });
        }}
      />
      {error === null ? null : (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <div className="row-actions">
        <button type="submit" className="primary" disabled={busy}>
          Save changes
        </button>
        <button type="button" className="link" disabled={busy} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

/** The editor for the live program: days, blocks and exercises, gym and travel. */
export function ProgramEditor({
  program,
  onSaved,
  onCancel,
}: {
  program: Program;
  onSaved: () => void;
  onCancel: () => void;
}): ReactElement {
  const state = useApi("/api/exercises", exerciseListSchema);
  return (
    <Load state={state}>
      {(exercises) => (
        <Form
          program={program}
          choices={exercises
            .filter((exercise) => exercise.measure !== "distance")
            .map(({ id, name, measure }) => ({ id, name, measure }))}
          onSaved={onSaved}
          onCancel={onCancel}
        />
      )}
    </Load>
  );
}
