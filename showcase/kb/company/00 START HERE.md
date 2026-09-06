# Welcome to Company OS

**Obsidian, but multiplayer, permissioned and built for agents — on your own Linux box.**

This is a working example for technical teams: documents, delivery work, customer
commitments and small internal tools, together in one workspace you can customize.

## A five-minute tour

You are visiting **Werkraum Systems GmbH**, a fictional German industrial-technology
company. The scenario starts on **6 September 2026**. Rheinwerk has placed a
€148,000 order for energy monitoring in two production halls. Delivery is underway;
an enclosure-quality issue needs attention before the next installation milestone.

1. **Find the problem.** Open [Cockpit](dashboards/management-cockpit.html): real tasks from permitted documents, with blocked work first.
2. **Follow the context.** Open [Project Polaris](../projects/polaris-energy-gateway/README.md): the order, acceptance criteria, delivery plan and enclosure decision connect.
3. **Change real work.** In [Kanban](dashboards/delivery-kanban.html), edit a card and save. The change appears in [kanban.md](dashboards/kanban.md) and then [Tasks](todos.html): views of the same task.
4. **Review before committing.** In [Pipeline](sales/customer-pipeline.html), review Elbe Verpackung. Record requirements, security review and evidence, then advance to Proposal. Rheinwerk is already won.
5. **Make a useful output.** In [Invoice](finance/invoice-generator.html), change a quantity and create a PDF. Totals recalculate; the PDF appears under `finance/_files/` for preview or download.

Changes are shared with other visitors and retained between visits. The operator
can reset this fictional workspace; use sample information when trying it.

## Pick your next route

- **Business evaluator:** [What this replaces, what is live, and how to adopt it](README.md).
- **Technical evaluator:** [Run Claude Code or Codex against the workspace](handbook/agentic-company-os.md).
- **Desktop user:** [Connect it as a network drive](handbook/network-drive.md).
- **Quality lead:** [Follow the management-system evidence](quality/management-system-guide.md).

## Your account changes what you can do

`demo` can browse and edit shared demo files, including these artifacts. It has
no terminal or scheduled jobs. `peter` has a personal workspace, Polaris access,
and Claude Code; `krystof` also has administration access. **Claude** appears in
the top bar only for full accounts and requires the user's own Anthropic sign-in.

Try the same Markdown document in two browser windows to see edits merge live.
For permissions, compare Peter's project tree with the administrator's: the
restricted example project is omitted from Peter's tree and search.
