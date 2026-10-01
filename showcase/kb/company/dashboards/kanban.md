# Delivery control board

<!-- KANBAN BOARD - machine-readable, edited by delivery-kanban.html in this folder.
  Format (also for AI agents editing this file directly):
  * every "## Heading" is a column, left-to-right in file order.
  * every "- [ ] ..." checkbox line under a heading is one card; "- [x]" = done.
  * optional inline card metadata, anywhere in the line:
      "@username"          assignee (platform login; indexed by Company OS)
      "#tagname"           label (indexed by Company OS)
      (due: YYYY-MM-DD)    deadline
      (color: red|orange|yellow|green|teal|blue|purple|pink|gray)
  * keep one card per line; keep this comment. -->

Pull work through verification. Keep delivery, quality, customer and security
evidence attached to the work rather than in a separate reporting silo.

## Ready

- [ ] Prepare the Hall A installation kit @{{member}} #polaris #delivery (due: 2026-09-24) (color: blue)
- [ ] Run the customer acceptance rehearsal @{{admin}} #polaris #customer (due: 2026-10-21) (color: teal)
- [ ] Install and commission Hall A @{{member}} #polaris #delivery (due: 2026-09-30) (color: blue)
- [ ] Install and commission Hall B @{{member}} #polaris #delivery (due: 2026-10-14) (color: blue)
- [ ] Publish the operator quick guide @{{member}} #polaris #customer (due: 2026-10-09) (color: teal)

## In progress

- [ ] Approve the gateway enclosure sample @{{admin}} #polaris #quality #blocked (due: 2026-09-18) (color: yellow)
- [ ] Close coating supplier action CA-2026-014 @{{member}} #qms #quality (due: 2026-09-18) (color: orange)

## Verification

- [ ] Verify the telemetry-retention control @{{admin}} #polaris #security (due: 2026-09-12) (color: red)

## Done

- [x] Release gateway firmware 2.4 @{{member}} #platform #product (color: green)
