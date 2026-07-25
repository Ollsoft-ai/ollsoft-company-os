---
name: kb-todos
description: Use when creating, assigning, or organizing to-do items / tasks / action items anywhere in the knowledgebase. Defines the task notation the platform indexes — checkbox, assignee, and tag syntax — so tasks show up correctly in the To-dos view and can be filtered by person and project.
---

# How to write a to-do

Tasks are plain markdown checkboxes on their own line. The platform indexes every one, so anything you write in this format automatically appears in the **To-dos** view (the `company/todos.html` artifact), filterable by person and folder.

```markdown
- [ ] Draft the Q3 roadmap
- [x] Book the venue
```

- `- [ ]` = open, `- [x]` = done. (`* [ ]` also works.)
- One task per line. The text after the checkbox is the task.

# Assigning people — use `@username`

Mention a person with `@` and their exact platform username. That's what makes a task show up in someone's "Assigned to me" list.

```markdown
- [ ] Finalize the hospital integration spec @bob
- [ ] Compliance review @bob @alice        <!-- multiple assignees are fine -->
```

- Use the person's real login name (the same name they sign in with). `@bob`, not `@Bob` or `@bobsmith`.
- Multiple assignees: just add more `@name` mentions.
- The rest of the line stays free text — assignment never limits what you can write.

# Tagging / grouping — use `#tag`

Add `#labels` to group or categorize tasks (e.g. by theme, priority, or milestone):

```markdown
- [ ] Rotate the production credentials @alice #security #urgent
```

Both `@mentions` and `#tags` are parsed into indexed fields, so the To-dos view and any custom artifact can filter on them with SQL (`WHERE 'bob' = ANY(assignees)`, `WHERE 'urgent' = ANY(tags)`).

# Where tasks live

Put tasks in whatever document they belong to — a project plan under `projects/<name>/`, a personal list in `users/<you>/`, or a shared doc in `company/`. The To-dos view aggregates across **everything the viewer is allowed to read** (row-level security), then lets them filter by folder/project. You don't maintain a separate task list; the checkboxes in your documents *are* the task list.

# Toggling

Checking a box in the To-dos view writes `- [ ]` → `- [x]` back into the source file (as the viewer, so file permissions apply). The document and the aggregated view are two windows on the same line — never copies.

# Permissions reminder

A task is only visible to people who can read the file it lives in. To assign someone a task, make sure they can actually read that document — otherwise `@them` appears in the text but the task never reaches their list. Put shared work in a folder the assignees can read (see **kb-database** / permissions).
