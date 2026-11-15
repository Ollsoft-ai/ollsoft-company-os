// node --test tests/js — question forms (elicitation/create, mode "form").
// The schemas below are the shapes claude-agent-acp sends for Claude's
// AskUserQuestion: before this, the chat drew only each question's header and
// an empty text box, so nobody could see the question or its options.
import { test } from "node:test";
import assert from "node:assert/strict";
import { formFields, formContent } from "../../frontend/src/elicit.js";

const CUSTOM = { _askUserQuestionCustomAnswer: { questionId: "question_0", isCustomAnswer: true } };

// one question: the question text travels in `message`, so the field has no description
const single = {
  type: "object",
  properties: {
    question_0: {
      type: "string", title: "Auth method",
      oneOf: [
        { const: "OAuth", title: "OAuth", description: "Sign in with the provider" },
        { const: "Keys", title: "Keys", description: "API keys per user",
          _meta: { "_claude/askUserQuestionOption": { preview: "KEY=abc" } } },
      ],
    },
    question_0_custom: { type: "string", title: "Other", description: "Type your own answer (optional).", _meta: CUSTOM },
  },
};

// several questions: each field carries its own question in `description`
const several = {
  type: "object",
  properties: {
    question_0: { type: "string", title: "Library", description: "Which library should we use?",
                  oneOf: [{ const: "A", title: "A" }, { const: "B", title: "B" }] },
    question_0_custom: { type: "string", title: "Other", _meta: CUSTOM },
    question_1: { type: "array", title: "Features", description: "Which features do you want?",
                  items: { anyOf: [{ const: "x", title: "X", description: "the x one" }, { const: "y", title: "Y" }] } },
    question_1_custom: { type: "string", title: "Other",
                         _meta: { _askUserQuestionCustomAnswer: { questionId: "question_1", isCustomAnswer: true } } },
  },
};

test("a single-choice question becomes a list of options, each with its explanation", () => {
  const [q, other] = formFields(single);
  assert.equal(q.kind, "one");
  assert.equal(q.title, "Auth method");
  assert.deepEqual(q.options.map((o) => [o.value, o.label, o.description]),
                   [["OAuth", "OAuth", "Sign in with the provider"], ["Keys", "Keys", "API keys per user"]]);
  assert.equal(q.options[1].preview, "KEY=abc", "a preview rides along for the chosen option");
  assert.equal(other.companion, true, "the Other box is known to belong to the question above");
  assert.equal(other.kind, "text");
});

test("each of several questions shows its own question text; a multi-select is a set of checkboxes", () => {
  const f = formFields(several);
  assert.deepEqual(f.map((x) => [x.key, x.kind]), [["question_0", "one"], ["question_0_custom", "text"],
                                                   ["question_1", "many"], ["question_1_custom", "text"]]);
  assert.equal(f[0].description, "Which library should we use?");
  assert.equal(f[2].description, "Which features do you want?");
  assert.deepEqual(f[2].options.map((o) => o.label), ["X", "Y"]);
});

test("answers go back in the shape the agent reads", () => {
  const f = formFields(several);
  assert.deepEqual(formContent(f, { question_0: "B", question_0_custom: "", question_1: ["x", "y"], question_1_custom: "z" }),
                   { question_0: "B", question_1: ["x", "y"], question_1_custom: "z" });
  // nothing picked, own answer typed: the pick is absent, the text is the answer
  assert.deepEqual(formContent(f, { question_0: undefined, question_0_custom: "C", question_1: [], question_1_custom: "" }),
                   { question_0_custom: "C", question_1: [] });
});

test("the older and plainer shapes still work", () => {
  const f = formFields({
    type: "object", required: ["name"],
    properties: {
      color: { type: "string", enum: ["r", "g"], enumNames: ["Red", "Green"] },
      tags: { type: "array", items: { type: "string", enum: ["a", "b"] } },
      ok: { type: "boolean", description: "Proceed?" },
      n: { type: "integer" },
      name: { type: "string" },
      note: { type: "string" },
    },
  });
  assert.deepEqual(f.map((x) => x.kind), ["one", "many", "bool", "integer", "text", "text"]);
  assert.deepEqual(f[0].options.map((o) => o.label), ["Red", "Green"]);
  assert.deepEqual(formContent(f, { color: "g", tags: [], ok: true, n: "4.7", name: "", note: "" }),
                   { color: "g", tags: [], ok: true, n: 4, name: "" },
                   "an empty optional box is left out, an empty number is never sent as 0");
});

test("junk in the schema never throws", () => {
  assert.deepEqual(formFields(null), []);
  assert.deepEqual(formFields({ properties: { a: null, b: { oneOf: [null, { title: "no const" }] } } }).map((x) => x.kind),
                   ["text", "text"]);
});
