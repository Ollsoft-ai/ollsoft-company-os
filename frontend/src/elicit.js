// Elicitation forms (ACP `elicitation/create`, mode "form"). The agent sends a
// flat JSON Schema object; formFields() turns each property into a field the
// chat can draw, and formContent() turns the answers back into the `content`
// the agent reads. Pure — no DOM — so the rules are tested on their own
// (tests/js/elicit.test.mjs).
//
// The shapes agents send — Claude's AskUserQuestion through claude-agent-acp,
// and MCP servers' elicitations:
//   string + oneOf [{const, title, description}]        one choice
//   string + enum [...] (+ enumNames)                    one choice, the older shape
//   array  + items.anyOf / items.oneOf / items.enum      several choices
//   boolean · number · integer · string                  plain inputs
// A field's `title` is a short header; the question itself is its `description`.

// Claude's per-option preview (a mockup or snippet shown when the option is chosen)
const OPTION_META = "_claude/askUserQuestionOption";
// Marks the free-text "Other" box that belongs to the choice field before it
const CUSTOM_ANSWER_META = "_askUserQuestionCustomAnswer";

const str = (v) => (typeof v === "string" ? v : "");
const obj = (v) => (v && typeof v === "object" && !Array.isArray(v) ? v : {});

function choices(def) {
  const list = Array.isArray(def.oneOf) ? def.oneOf : Array.isArray(def.anyOf) ? def.anyOf : null;
  if (list) {
    const opts = list.filter((o) => o && typeof o === "object" && "const" in o).map((o) => ({
      value: o.const,
      label: str(o.title) || String(o.const),
      description: str(o.description),
      preview: str(obj(obj(o._meta)[OPTION_META]).preview),
    }));
    return opts.length ? opts : null;
  }
  if (Array.isArray(def.enum) && def.enum.length) {
    const names = Array.isArray(def.enumNames) ? def.enumNames : [];
    return def.enum.map((v, i) => ({ value: v, label: str(names[i]) || String(v), description: "", preview: "" }));
  }
  return null;
}

// [{key, kind, title, description, required, options, default, companion}]
// kind: "one" | "many" | "bool" | "number" | "integer" | "text"
export function formFields(schema) {
  const s = obj(schema);
  const props = obj(s.properties);
  const required = new Set(Array.isArray(s.required) ? s.required : []);
  const out = [];
  for (const [key, raw] of Object.entries(props)) {
    const def = obj(raw);
    const f = {
      key, kind: "text", title: str(def.title) || key, description: str(def.description),
      required: required.has(key), options: null, default: def.default,
      companion: !!obj(obj(def._meta)[CUSTOM_ANSWER_META]).isCustomAnswer,
    };
    if (def.type === "array") {
      const many = choices(obj(def.items));
      if (many) { f.kind = "many"; f.options = many; }
    } else {
      const one = choices(def);
      if (one) { f.kind = "one"; f.options = one; }
      else if (def.type === "boolean") f.kind = "bool";
      else if (def.type === "number" || def.type === "integer") f.kind = def.type;
    }
    out.push(f);
  }
  return out;
}

// values: key → what the form holds — the chosen option's value (one), a list
// of values (many), true/false (bool), or the text typed (everything else).
// Empty optional answers are left out rather than sent as "" or 0.
export function formContent(fields, values) {
  const content = {};
  for (const f of fields) {
    const v = values[f.key];
    if (f.kind === "bool") content[f.key] = !!v;
    else if (f.kind === "many") content[f.key] = Array.isArray(v) ? v : [];
    else if (f.kind === "one") { if (v !== undefined && v !== null) content[f.key] = v; }
    else if (f.kind === "number" || f.kind === "integer") {
      if (v === undefined || v === null || String(v).trim() === "") continue;
      const n = Number(v);
      if (Number.isFinite(n)) content[f.key] = f.kind === "integer" ? Math.trunc(n) : n;
    } else if (typeof v === "string" && (v !== "" || f.required)) content[f.key] = v;
  }
  return content;
}
