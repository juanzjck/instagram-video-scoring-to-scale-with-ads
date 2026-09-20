# Evaluation rubric

Four criteria, scored **1-5** each. Anchors are given for 1, 3 and 5; use 2 and 4 for the cases that sit between them.

Scored per **output**, not per prompt - the same prompt is scored three times because run-to-run variation is part of what is being measured.

**Score what the output says, not whether the verdict is right.** Verdict accuracy is measured separately and automatically in section 4A. An output can be wrong and still be clear, honest about its data and appropriately hedged - that combination is exactly what this rubric is for.

---

## Clarity (`clarity`)

*Is it easy to read and act on?*

| score | anchor |
|---|---|
| **1** | Rambling or contradictory. A media buyer would have to re-read it to find the recommendation, or could not find one at all. |
| **3** | Understandable but inefficient. The recommendation is there, buried in hedging or padding, and takes a second pass to locate. |
| **5** | Immediately legible. Verdict, reasoning and the evidence behind it are separable at a glance; nothing has to be read twice. |

## Data accuracy (`data_accuracy`)

*Do the claims match the data given? Are any numbers invented?*

| score | anchor |
|---|---|
| **1** | Contains at least one number or fact not present in the input, or directly contradicts the data (e.g. treats a blank as zero). |
| **3** | No outright inventions, but a number is misread, mislabelled, or the campaign-level click ratio is attributed to this single video. |
| **5** | Every figure cited appears in the input or is correctly derived from it, and unavailable metrics are described as unavailable rather than guessed. |

## Actionability (`actionability`)

*Is there a clear decision and a next step?*

| score | anchor |
|---|---|
| **1** | No decision. Describes the data or lists considerations and stops. |
| **3** | A decision is reached, but nothing follows from it - no budget guidance, no threshold to watch, no re-check point. |
| **5** | A clear verdict plus something the team can do next: scale it, drop it, hold and re-check at a stated point, or send it to a named human. |

## Uncertainty handling (`uncertainty`)

*Does it flag thin or missing data? Is the confidence appropriate?*

| score | anchor |
|---|---|
| **1** | States conclusions with full confidence on data that does not support them, or silently fills a gap where a metric was unavailable. |
| **3** | Mentions a limitation but does not let it change the verdict or the stated confidence. |
| **5** | Names what is missing or too thin, and the confidence level matches: low on the 9-hour-old video, on 640 reach, and where core metrics are blank. |

---

## Overall

The overall score for an output is the **mean of the four criteria**, unweighted.

## The `note` column

Free text, optional but valuable. The v2 prompts in Phase 5 are written from these notes, so a note naming *what* went wrong ("invented a 4.2% CTR", "said confidence HIGH on the 9-hour video") is worth more than a score on its own.
