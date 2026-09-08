# Hosted showcase maintenance

This is fictional seed content, not production business data. English-language
Werkraum Systems GmbH follows one accepted Rheinwerk order (OP-1042 / PO-RW-8841,
EUR 148,000) through Polaris delivery, an enclosure defect and a partial sample
invoice. The scenario date is 6 September 2026; final acceptance is 30 October.

## Sources of truth

- `dashboards/kanban.md`: the delivery board's real Markdown tasks; Tasks and
  Cockpit read the index. Do not duplicate the same actions in the tour.
- `sales/.pipeline-data.json`: interactive CRM example. Proposal and Won need
  both review assertions and evidence. An explicit Markdown handover connects
  the sample won order to Polaris; no project-creation automation is implied.
- `finance/.invoice-data.json`: draft fields. Amounts round to cents per line,
  then VAT rounds on the net sum. PDF export uses `kb-upload` into `_files/`,
  keeping the artifact sandbox intact. It is not a structured e-invoice.
- `quality/.risk-data.json`: illustrative heatmap snapshot. Keep scores and
  owners consistent with `risk-register.md`; it is not a live Markdown query.
- `_files/01-workspace.png` through `04-invoice.png`: screenshots for README.
  Hidden dot-files contain machine state; visible Markdown contains evidence.

Pipeline and invoice check for stale content before saving, but their read and
write are not an atomic transaction. Do not promise concurrent CRM editing.
The fixture personas are fictional; actionable assignments use actual accounts
`peter` and `krystof`. The `demo` account can edit shared files but has no terminal.
Peter should have Polaris access but not Helios or other users' private folders.

## Applying changes

Use the dedicated demo VM, never the operator's production workspace. Pull the
versioned source and use `scripts/seed-showcase.sh --admin krystof --member peter
--refresh` only when intentionally resetting seeded content. This overwrites
matching seeded files and JSON, but keeps unrelated files. For screenshots or an
isolated correction, install only the changed files with the existing ownership
and permissions. Account/group changes need fresh backend process credentials.

The hosted demo is public at the nginx layer and uses normal HTTPS reverse
proxying; visitors still use the Company OS login. No Cloudflare Tunnel is
needed. Keep passwords and SSH keys outside this repository. The optional
root-to-tour nginx redirect is documented in `docs/SETUP.md`.

## Before calling it ready

Run `python scripts/check-showcase.py` in an environment with Playwright and
Chromium. This checks artifact behavior with a sandboxed mock host. Also check
the real deployment: fresh login opens the tour; links and four README images
work; pipeline refuses an incomplete review and saves a complete one; invoice
rejects invalid quantities and exports a readable PDF; Kanban updates Markdown;
Cockpit filters real tasks; Peter sees Polaris but not Helios. Remove only files
created by your tests and restore any JSON fixtures changed during testing.

Recheck the onboarding in a fresh session for `demo` and a full user, plus a
narrow viewport. Capture screenshots of the delivered version, not stale mocks.
