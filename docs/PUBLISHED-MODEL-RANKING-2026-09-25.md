# Z Swarm published benchmark ranking

Snapshot: 2026-09-25 UTC (September 24, Chicago). **22 exact model/effort configurations; 10 published evaluations; all 220 component scores captured.** No new capability benchmarks were run for this report.

This replaces the earlier 17/18-question internal screen as the ranking reference. The old screen was too small, included a mislabeled item, and mixed unresolved model identities. Its records are retained only as historical diagnostics.

## How to use this ranking

Use the relevant task scores as the capability requirement, then select the cheapest available Z Swarm configuration that meets it. Escalate within Swarm when quality, credits, tokens or quota require it. Desktop Opus 5.5 remains the orchestrator and final decision maker. Published scores are evidence for selection, not proof that a model will succeed on every task.

The overview retains Artificial Analysis’s published Intelligence Index v4.3.2 and its measured cost per index task. Its weights are Agents 30%, Coding 20%, Scientific Reasoning 20%, General 30%. We do not add this index to its own component scores or invent an overall score. [Methodology](https://artificialanalysis.ai/methodology/intelligence-benchmarking).

The chart’s frontier marks configurations with no cheaper, equally or higher scoring point **within these 22 choices**. It uses rounded published point estimates; it is not statistical proof of superiority. A model below the overall frontier may still be preferred for a particular task.

## Ten evaluation views

| Evaluation | Why it matters | Unit |
| --- | --- | --- |
| [AA-Briefcase v1.1](https://artificialanalysis.ai/evaluations/aa-briefcase) | Professional analysis and deliverables | Elo |
| [GDPval-AA v2.1](https://artificialanalysis.ai/evaluations/gdpval-aa) | Judgment in real professional tasks | Elo |
| [AutomationBench-AA](https://artificialanalysis.ai/evaluations/automationbench-aa) | Cross-app decisions and rule-compliant tool use | % objectives, violations score zero |
| [Terminal-Bench 4.0](https://artificialanalysis.ai/evaluations/terminalbench-4-0) | Difficult coding, debugging and terminal execution | % pass@1 |
| [SciCode](https://artificialanalysis.ai/evaluations/scicode) | Executable scientific code | % subproblems passed |
| [Humanity’s Last Exam](https://artificialanalysis.ai/evaluations/humanitys-last-exam) | Difficult multidisciplinary reasoning | % correct; AA text, no tools |
| [GDP.pdf](https://artificialanalysis.ai/evaluations/gdp-pdf) | Grounded professional document reasoning | % all-pass |
| [AA-LCR v1.1](https://artificialanalysis.ai/evaluations/artificial-analysis-long-context-reasoning) | Reasoning over long documents | % correct |
| [AA-Omniscience](https://artificialanalysis.ai/evaluations/omniscience) | Knowledge, uncertainty and hallucination | index points |
| [CritPt](https://artificialanalysis.ai/evaluations/critpt) | Research-level physics reasoning; under review | % correct |

CritPt is explicitly **under review**, and contributes 10% to AA’s composite. Treat that dimension as provisional. Prefer the relevant coding, automation and professional-work results when making close task-specific decisions. HLE here is AA’s text-only, no-tools run; vendor “HLE with tools” scores are a different comparison.

## Overview and prices

Cost/task is AA’s weighted average for its benchmark workload, incorporating observed token use. It is not our internal test spend or an estimate of a normal production request. Input/output rates below are USD per million tokens, standard service and ordinary context; they do not include separate tools, long-context surcharges, batch discounts or subscription allowances. Provider and cache conditions matter.

| Exact configuration | AA index ↑ | AA $/task ↓ | Input $/M | Output $/M | Observed frontier |
| --- | ---: | ---: | ---: | ---: | --- |
| [Claude Opus 5.5 (max with fallback)](https://artificialanalysis.ai/models/claude-opus-5-5) | 58 | $5.98 | $4 | $20 | Yes |
| [Claude Opus 5.5 (xhigh with fallback)](https://artificialanalysis.ai/models/claude-opus-5-5-xhigh) | 56 | $3.46 | $4 | $20 | Yes |
| [Claude Opus 5.5 (high with fallback)](https://artificialanalysis.ai/models/claude-opus-5-5-high) | 54 | $1.82 | $4 | $20 | Yes |
| [Claude Fable 5.1 (max with fallback)](https://artificialanalysis.ai/models/claude-fable-5-1) | 53 | $7.63 | $10 | $50 | — |
| [GPT-6 Astra (max)](https://artificialanalysis.ai/models/gpt-6-astra) | 53 | $3.26 | $10 | $50 | — |
| [Claude Opus 5.5 (medium with fallback)](https://artificialanalysis.ai/models/claude-opus-5-5-medium) | 51 | $1.34 | $4 | $20 | Yes |
| [GPT-6 Astra (high)](https://artificialanalysis.ai/models/gpt-6-astra-high) | 51 | $1.73 | $10 | $50 | — |
| [GPT-6 Sol (max)](https://artificialanalysis.ai/models/gpt-6-sol) | 48 | $1.06 | $2 | $10 | Yes |
| [MiMo-V2.6-Pro](https://artificialanalysis.ai/models/mimo-v2-6-pro) | 46 | $0.13 | $0.43 | $0.87 | Yes |
| [GLM-5.3 (max)](https://artificialanalysis.ai/models/glm-5-3) | 45 | $2.01 | $1.4 | $4.4 | — |
| [GPT-6 Sol (high)](https://artificialanalysis.ai/models/gpt-6-sol-high) | 43 | $0.37 | $2 | $10 | — |
| [Claude Opus 5.5 (low with fallback)](https://artificialanalysis.ai/models/claude-opus-5-5-low) | 42 | $0.55 | $4 | $20 | — |
| [GLM-5.3-Flash](https://artificialanalysis.ai/models/glm-5-3-flash) | 42 | $0.25 | $0.15 | $0.5 | — |
| [Gemini 3.8 Flash (high)](https://artificialanalysis.ai/models/gemini-3-8-flash) | 41 | $1.24 | $0.75 | $3.75 | — |
| [DeepSeek V4.1 Flash (max)](https://artificialanalysis.ai/models/deepseek-v4-1-flash) | 39 | $0.27 | $0.3 | $1.2 | — |
| [Claude Sonnet 5 (max)](https://artificialanalysis.ai/models/claude-sonnet-5) | 38 | $5.09 | $2 | $10 | — |
| [GPT-6 Luna (max)](https://artificialanalysis.ai/models/gpt-6-luna) | 37 | $0.07 | $0.1 | $0.5 | Yes |
| [DeepSeek V4 Pro 0813 (max)](https://artificialanalysis.ai/models/deepseek-v4-pro) | 36 | $0.67 | $1.32 | $3.96 | — |
| [Qwen3.8 27B (xhigh)](https://artificialanalysis.ai/models/qwen3-8-27b) | 34 | $1.01 | $0.5 | $3 | — |
| [GPT-6 Luna (high)](https://artificialanalysis.ai/models/gpt-6-luna-high) | 32 | $0.03 | $0.1 | $0.5 | Yes |
| [MiniMax-M3](https://artificialanalysis.ai/models/minimax-m3) | 29 | $0.51 | $0.3 | $1.2 | — |
| [gpt-oss-120b (high)](https://artificialanalysis.ai/models/gpt-oss-120b) | 12 | $0.11 | $0.15 | $0.59 | — |

Prices were cross-checked for [Anthropic](https://claude.com/pricing), [OpenAI](https://developers.openai.com/api/docs/pricing), [Google](https://ai.google.dev/gemini-api/docs/pricing), and [DeepSeek](https://api-docs.deepseek.com/quick_start/pricing). Other model rates are the linked AA provider snapshot.

DeepSeek rows show **peak** rates. Off-peak input/output are $0.15/$0.60 for V4.1 Flash and $0.66/$1.98 for V4 Pro; cached input also halves. Gemini 3.8 Flash’s $0.75/$3.75 introductory rates last through December 31, 2026. OpenAI rates shown are Standard, not Batch/Flex. These price changes are not retroactively applied to AA’s published measured cost.

## Coding, decisions and agent work

Each row links to the exact evaluated configuration. Briefcase and GDPval are Elo ratings; the other columns are percentages. Automation uses AA’s objective-completion score with a zero for a task with guardrail violations, not Zapier’s strict all-objectives task completion rate.

| Configuration | Briefcase Elo | GDPval Elo | Automation % | Terminal 4.0 % | SciCode % |
| --- | ---: | ---: | ---: | ---: | ---: |
| [Claude Opus 5.5 (max with fallback)](https://artificialanalysis.ai/models/claude-opus-5-5) | 1822 | 1846 | 69.5 | 59.6 | 66.9 |
| [Claude Opus 5.5 (xhigh with fallback)](https://artificialanalysis.ai/models/claude-opus-5-5-xhigh) | 1780 | 1820 | 65.0 | 59.6 | 65.0 |
| [Claude Opus 5.5 (high with fallback)](https://artificialanalysis.ai/models/claude-opus-5-5-high) | 1704 | 1692 | 63.2 | 56.6 | 60.4 |
| [Claude Fable 5.1 (max with fallback)](https://artificialanalysis.ai/models/claude-fable-5-1) | 1678 | 1735 | 59.4 | 52.0 | 63.1 |
| [GPT-6 Astra (max)](https://artificialanalysis.ai/models/gpt-6-astra) | 1569 | 1542 | 68.5 | 59.1 | 56.5 |
| [Claude Opus 5.5 (medium with fallback)](https://artificialanalysis.ai/models/claude-opus-5-5-medium) | 1642 | 1576 | 61.2 | 52.5 | 59.3 |
| [GPT-6 Astra (high)](https://artificialanalysis.ai/models/gpt-6-astra-high) | 1507 | 1485 | 66.6 | 54.0 | 55.4 |
| [GPT-6 Sol (max)](https://artificialanalysis.ai/models/gpt-6-sol) | 1483 | 1487 | 61.6 | 43.9 | 57.6 |
| [MiMo-V2.6-Pro](https://artificialanalysis.ai/models/mimo-v2-6-pro) | 1517 | 1673 | 58.6 | 34.8 | 60.9 |
| [GLM-5.3 (max)](https://artificialanalysis.ai/models/glm-5-3) | 1517 | 1646 | 62.2 | 41.9 | 59.0 |
| [GPT-6 Sol (high)](https://artificialanalysis.ai/models/gpt-6-sol-high) | 1289 | 1376 | 60.1 | 26.3 | 54.9 |
| [Claude Opus 5.5 (low with fallback)](https://artificialanalysis.ai/models/claude-opus-5-5-low) | 1285 | 1224 | 52.9 | 31.3 | 58.6 |
| [GLM-5.3-Flash](https://artificialanalysis.ai/models/glm-5-3-flash) | 1452 | 1641 | 60.4 | 32.8 | 51.6 |
| [Gemini 3.8 Flash (high)](https://artificialanalysis.ai/models/gemini-3-8-flash) | 1202 | 1412 | 59.9 | 19.7 | 56.6 |
| [DeepSeek V4.1 Flash (max)](https://artificialanalysis.ai/models/deepseek-v4-1-flash) | 1425 | 1600 | 68.9 | 26.8 | 51.9 |
| [Claude Sonnet 5 (max)](https://artificialanalysis.ai/models/claude-sonnet-5) | 1359 | 1449 | 36.5 | 14.1 | 54.3 |
| [GPT-6 Luna (max)](https://artificialanalysis.ai/models/gpt-6-luna) | 1299 | 1367 | 53.2 | 12.6 | 54.6 |
| [DeepSeek V4 Pro 0813 (max)](https://artificialanalysis.ai/models/deepseek-v4-pro) | 1258 | 1441 | 56.7 | 14.1 | 51.0 |
| [Qwen3.8 27B (xhigh)](https://artificialanalysis.ai/models/qwen3-8-27b) | 1400 | 1409 | 48.2 | 5.6 | 46.6 |
| [GPT-6 Luna (high)](https://artificialanalysis.ai/models/gpt-6-luna-high) | 1176 | 1290 | 47.8 | 4.5 | 50.3 |
| [MiniMax-M3](https://artificialanalysis.ai/models/minimax-m3) | 1090 | 1230 | 21.3 | 2.0 | 47.1 |
| [gpt-oss-120b (high)](https://artificialanalysis.ai/models/gpt-oss-120b) | 0 | 596 | 0.2 | 0.0 | 34.0 |

## Reasoning, documents and reliability

| Configuration | HLE % | GDP.pdf % | Long context % | Omniscience index | CritPt % † |
| --- | ---: | ---: | ---: | ---: | ---: |
| [Claude Opus 5.5 (max with fallback)](https://artificialanalysis.ai/models/claude-opus-5-5) | 61.4 | 26.2 | 84.7 | 46.4 | 31.7 |
| [Claude Opus 5.5 (xhigh with fallback)](https://artificialanalysis.ai/models/claude-opus-5-5-xhigh) | 57.5 | 26.6 | 84.7 | 42.6 | 31.7 |
| [Claude Opus 5.5 (high with fallback)](https://artificialanalysis.ai/models/claude-opus-5-5-high) | 55.6 | 28.8 | 82.7 | 40.6 | 30.9 |
| [Claude Fable 5.1 (max with fallback)](https://artificialanalysis.ai/models/claude-fable-5-1) | 59.1 | 26.2 | 85.3 | 43.5 | 29.7 |
| [GPT-6 Astra (max)](https://artificialanalysis.ai/models/gpt-6-astra) | 54.7 | 31.0 | 80.7 | 43.4 | 31.7 |
| [Claude Opus 5.5 (medium with fallback)](https://artificialanalysis.ai/models/claude-opus-5-5-medium) | 54.7 | 25.6 | 84.3 | 40.3 | 27.7 |
| [GPT-6 Astra (high)](https://artificialanalysis.ai/models/gpt-6-astra-high) | 53.1 | 31.0 | 80.0 | 43.7 | 28.9 |
| [GPT-6 Sol (max)](https://artificialanalysis.ai/models/gpt-6-sol) | 47.9 | 24.8 | 83.7 | 27.1 | 30.9 |
| [MiMo-V2.6-Pro](https://artificialanalysis.ai/models/mimo-v2-6-pro) | 49.4 | 19.2 | 86.3 | 8.4 | 26.6 |
| [GLM-5.3 (max)](https://artificialanalysis.ai/models/glm-5-3) | 42.3 | 11.2 | 79.7 | 14.3 | 19.1 |
| [GPT-6 Sol (high)](https://artificialanalysis.ai/models/gpt-6-sol-high) | 44.1 | 28.0 | 83.7 | 26.8 | 25.4 |
| [Claude Opus 5.5 (low with fallback)](https://artificialanalysis.ai/models/claude-opus-5-5-low) | 48.3 | 25.6 | 80.7 | 38.9 | 17.7 |
| [GLM-5.3-Flash](https://artificialanalysis.ai/models/glm-5-3-flash) | 39.9 | 15.4 | 80.0 | 7.5 | 15.4 |
| [Gemini 3.8 Flash (high)](https://artificialanalysis.ai/models/gemini-3-8-flash) | 47.8 | 21.0 | 81.3 | 29.6 | 18.3 |
| [DeepSeek V4.1 Flash (max)](https://artificialanalysis.ai/models/deepseek-v4-1-flash) | 39.2 | 12.8 | 84.0 | -5.3 | 14.3 |
| [Claude Sonnet 5 (max)](https://artificialanalysis.ai/models/claude-sonnet-5) | 41.3 | 13.2 | 82.0 | 16.4 | 16.9 |
| [GPT-6 Luna (max)](https://artificialanalysis.ai/models/gpt-6-luna) | 38.5 | 20.4 | 83.3 | 0.7 | 19.4 |
| [DeepSeek V4 Pro 0813 (max)](https://artificialanalysis.ai/models/deepseek-v4-pro) | 41.0 | 11.4 | 80.3 | 0.8 | 18.0 |
| [Qwen3.8 27B (xhigh)](https://artificialanalysis.ai/models/qwen3-8-27b) | 33.9 | 16.6 | 82.0 | -10.0 | 5.4 |
| [GPT-6 Luna (high)](https://artificialanalysis.ai/models/gpt-6-luna-high) | 32.9 | 13.8 | 79.3 | -5.5 | 15.4 |
| [MiniMax-M3](https://artificialanalysis.ai/models/minimax-m3) | 39.0 | 9.8 | 83.0 | 1.4 | 3.7 |
| [gpt-oss-120b (high)](https://artificialanalysis.ai/models/gpt-oss-120b) | 19.6 | 4.0 | 52.0 | -49.2 | 1.1 |

† Under review. Omniscience is an index, not an accuracy percentage. The accompanying JSON preserves accuracy, hallucination rate and answer-attempt rate separately, plus available Elo confidence intervals.

## Selection implications

- **Overall capability:** Opus 5.5 max leads this published snapshot. Keep its max/high/medium/low configurations distinct; high-effort evidence must not be attributed to a cheap low-effort call.
- **Cost-sensitive candidates:** GPT-6 Luna, MiMo-V2.6-Pro and DeepSeek V4.1 Flash merit consideration at their demonstrated task level. Cost advantages do not establish suitability for difficult code or decisions.
- **Coding and automation:** inspect the corresponding columns before choosing among Opus, Astra, Sol, DeepSeek and other candidates. Overall-index domination is not a reason to exclude a model from every role.
- **Availability:** these are public evaluations, not successful calls through our current keys. Confirm the exact route, model version and required features before dispatch, and retain the existing Swarm-first escalation policy.

## Identity and comparability corrections

The official [DeepSeek update](https://api-docs.deepseek.com/updates/) identifies `deepseek-flash` as V4.1 Flash; direct legacy V4 Flash aliases also route to it. Consequently the earlier direct “DeepSeek Flash” result cannot be treated as a frozen V4 Flash comparison. The present Pro row is explicitly V4-Pro-0813.

AA’s Opus 5.5 and Fable 5.1 configurations include the named default fallback. Their figures are not isolated-model results with fallback disabled. [Anthropic’s Opus 5.5 release](https://www.anthropic.com/claude-opus-5-5) also discloses safeguard fallbacks and its own harness conditions; those vendor results are not mixed into the AA columns.

Same-named effort levels are not equal compute across vendors. Compare the measured configuration and actual task cost; do not match models merely because both settings are called “high.” No universal guarantee is implied by a small score difference.

## Reproducibility

Sources were captured from public evaluator pages and public page data. Rows join by exact AA configuration slug. Component scores extracted from model pages were checked against matching evaluation-page records. Six Briefcase scores missing in model-page data were recovered from the complete [AA-Briefcase leaderboard](https://artificialanalysis.ai/evaluations/aa-briefcase) by exact full model/configuration name; these Elo values are published rounded to integers. Displayed index and cost/task were independently cross-checked against each model page’s summary. Missing values are never replaced with zero.

The dated data snapshot and this report establish the published ranking reference. They do not change production routing code, API model aliases or the desktop model setting.
