# Cost estimates and unknown usage

Rechecked 2026-11-13 against [OpenAI pricing](https://developers.openai.com/api/docs/pricing),
[GPT-5.6 Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol),
[Terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra),
[Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna),
[GPT-5](https://developers.openai.com/api/docs/models/gpt-5),
[GPT-5 Codex](https://developers.openai.com/api/docs/models/gpt-5-codex),
[GPT-5.4](https://developers.openai.com/api/docs/models/gpt-5.4),
[GPT-5.5](https://developers.openai.com/api/docs/models/gpt-5.5),
[GPT-5.4 Mini](https://developers.openai.com/api/docs/models/gpt-5.4-mini),
[GPT-5.3 Codex](https://developers.openai.com/api/docs/models/gpt-5.3-codex), and
[Claude pricing](https://platform.claude.com/docs/en/about-claude/pricing),
[Z.AI pricing](https://docs.z.ai/guides/overview/pricing), and
[Qwen3.8 Max pricing](https://www.alibabacloud.com/help/en/model-studio/qwen3-8-max).
Packaged rates are standard short-context list estimates in USD per million tokens,
with separate uncached input, cached input, cache write and output rates.
OpenAI Sol 5.6 promotional rates expire November 30, 2026. Update configuration when they change.

An exact `pricing.models` entry wins. Otherwise only a trailing `-YYYYMMDD` or
`-YYYY-MM-DD` is stripped. No arbitrary suffix, model-family, or substring matching occurs.
For example `claude-haiku-4-5-20251001` uses `claude-haiku-4-5`; `gpt-6-astra-fast`
remains unpriced unless configured explicitly. Null, synthetic and auto model ids are unknown.

```toml
[pricing.models."my-exact-model-id"]
input_usd_per_mtok = 2.0
cache_read_usd_per_mtok = 0.2
cache_write_usd_per_mtok = 2.5
output_usd_per_mtok = 10.0
```

Overrides merge over defaults. Existing three-rate entries remain valid; an omitted
cache-write rate for a new model cannot price nonzero cache writes.
Zero denotes an unavailable cache-write rate. If nonzero writes have no verified
rate, that usage row stays unpriced; other fully priced rows still contribute to
the session's partial total.
The v4 schema adds nullable `sessions.title VARCHAR` and allows partial cost methods.
The migration preserves history and existing reported costs.
`ingest`/`run` reprice unchanged history using current configuration, retaining reported
Claude `cost_state` totals as authoritative. Coverage, finding costs and rollup amounts
read those stored session costs; run `ingest` or `run` after changing rates. Unknown-model
diagnostics resolve against the current configuration even before that refresh.
Zero known dollars with `unpriced` status
means unknown spend, never free usage. `partial_price_table` means the sum of priced usage
only. Rollups retain those known dollars and label incomplete totals `partial`, with the
number of root session trees with incomplete costs (the same unit as the rollup
`sessions` count). Per-harness status counts individual sessions. Status and reports list unknown ids and
all four token volumes, including null ids, so users can supply missing mappings.

These are estimates, not invoices or claims of wasted dollars. Service tiers, regional
uplifts, context surcharges, historical price changes and cache lifetimes are not
consistently represented in the normalized logs. Cache-write estimates use the shortest
published lifetime (30m OpenAI / 5m Claude); 1h/12h writes can cost more. Haiku 5.5's
large-prompt tier can cost more. Reported cost-state is used when available. Custom
rates can account for the user's actual provider. Session costs are not attributed to
individual findings; findings rank by severity and disclose that ranking basis.

GLM-5.2/5.3 have verified input, cache-hit and output rates. Their published free
cache storage does not establish a cache-creation token rate: rows with nonzero writes
remain unknown. Qwen3.8 Max defaults to Alibaba Global scope with explicit-cache
rates; override the mapping for another scope or implicit caching. This is a declared
list-price estimate, since the normalized logs do not identify deployment scope/cache
mode. Kimi, Spark and other ids without a verified mapping remain unknown.
