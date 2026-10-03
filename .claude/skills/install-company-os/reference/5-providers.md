# Phase 5 — Branding and optional providers

One question per block, in this order. Every one can be skipped and added later by rerunning this phase. `admin` and *key drop* are the helpers in SKILL.md.

## Branding

**Ask:** "What name should the app show (max 40 characters)? Dark, light, or the default deep-blue look?"

```bash
admin POST /admin/settings '{"set":{"brand.name":"<name>","ui.theme":"deep-blue"}}'
```

Tell them: **a logo and custom colours can be set later** in the app, under Settings → Company.

Verify: they reload the page and see the name.

## Voice dictation — ElevenLabs

**Ask:** "Do you want voice dictation — talk instead of type, in documents and agent chats? It uses ElevenLabs speech-to-text, billed by ElevenLabs per minute of audio, capped at about 1 hour per person per day."

If yes, they create the key:

1. elevenlabs.io → sign in → **Developers → API Keys → Create key**, name `Company OS dictation`.
2. **Permissions: Speech to Text = Access, everything else = No access.** Every signed-in user can spend this key, so it must do one thing only.
3. Optional: a monthly credit limit on the key.

Key drop (SKILL.md), name `elevenlabs`, then:

```bash
ssh companyos 'sudo bash ~/ollsoft-company-os/scripts/install-dictation-key.sh --from /root/.cos-elevenlabs.key \
  && sudo shred -u /root/.cos-elevenlabs.key && sudo systemctl restart kb-hub'
```

- The script tests the key before installing it.
- Verify: they click the mic in a document, say a sentence, and the text appears.

## Search by meaning — embeddings + reranking

**Ask:** "Do you want search by meaning — 'travel approval' finds the page about 'business trip sign-off', across languages? Without it, search matches exact words only. It needs an OpenAI key (indexing ~700 documents cost $0.46 once, then cents a month) and optionally a Cohere key for reranking (noticeably better top hits, ~$0.003 per search)."

| Choice | Needs |
|---|---|
| **OpenAI + Cohere** (recommend) | OpenAI key, Cohere production key |
| OpenAI only | OpenAI key |
| Azure AI Foundry | an Azure resource with `text-embedding-3-large` + Cohere rerank; `az login` on the server; run `install-search-keys.sh --from-azure <account> <resource-group>` |

Keys:

- **OpenAI:** platform.openai.com → **API keys → Create new secret key**, in a project with a **monthly budget** set.
- **Cohere:** dashboard.cohere.com → **API keys** → a **production** key (trial keys are rate-limited to a trickle).

Key drops `openai` and `cohere`, then:

```bash
ssh companyos 'sudo bash ~/ollsoft-company-os/scripts/install-search-keys.sh --embed-provider openai \
  --embed-url https://api.openai.com/v1 --embed-model text-embedding-3-large \
  --embed-key-file /root/.cos-openai.key \
  --rerank-url https://api.cohere.com/v2/rerank --rerank-model rerank-v3.5 \
  --rerank-key-file /root/.cos-cohere.key \
  && sudo sh -c "shred -u /root/.cos-*.key" && sudo systemctl restart kb-embedd kb-indexer'
```

- OpenAI only: drop the three `--rerank-*` flags.
- **Budgets — ask:** "Default caps: $10/day and $100/month for indexing, $20/day for reranking, 200 searches per person per day. Keep them?" Change with `admin POST /admin/settings '{"set":{"search.budget.embed_day_usd":10,"search.budget.embed_month_usd":100,"search.budget.rerank_day_usd":20,"search.rerank.per_user_day":200}}'`.
- Verify: `ssh companyos kb-search --status` shows the provider and no `unconfigured`; after a few minutes `kb-search "something they wrote"` returns `[meaning]` hits.
