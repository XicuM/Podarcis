---
name: overleaf-suggest
description: >-
  Propose edits to an Overleaf LaTeX project as native track-changes suggestions
  that a human accepts or rejects, and attach review comments to passages. Also
  reads documents, lists files, uploads figures and fetches the compiled PDF.
  Cannot make untracked edits.
---

# Overleaf suggestions

> [!WARNING]
> **Disclaimer**: This tool is in active experimental development. Always review critical suggestions directly in Overleaf's web UI.

Use `overleaf-suggest` (MCP tools `overleaf_*`, or the CLI) to propose changes to
someone's paper without changing it. Every write becomes a suggestion in the
Review panel; the tool refuses rather than editing directly.

## Always

1. **Pass `expectTitle`** on every call, with a distinctive part of the project
   name. Projects can share a file layout and a `\title{}`.
2. **`overleaf_list_files` first** — it returns canonical paths. Do not guess
   them and do not read them off the file tree.
3. **`overleaf_list_comments` before `overleaf_comment`** — read what is already
   on the document. Posting blind duplicates notes the author has already
   written, and you cannot take a comment back: `overleaf_withdraw_suggestion`
   rejects suggestion cards only and refuses comments, so a duplicate has to be
   resolved by hand in the browser.
4. **`overleaf_read_file` before suggesting**, and copy `find` from that output.
   It must match exactly and occur **exactly once**; the tool refuses ambiguous
   matches because they could land the edit in the wrong place.
5. **One self-contained change per suggestion.** A reviewer accepts or rejects
   the whole thing, so a suggestion that mixes two ideas cannot be half-accepted.
   If two edits must go together (e.g. `\begin{figure*}` and its `\end{figure*}`),
   say so explicitly in your message to the user.
6. **Explain each suggestion to the user** — what changed and why. A suggestion
   carries no note of its own; use `overleaf_comment` if the reasoning belongs in
   the document for a co-author to read.

## Failure is normal and safe

The tool throws instead of editing when the project name does not match, the file
cannot be confirmed open, `find` is missing or ambiguous, or Reviewing mode is
unavailable. **Read the message: it says what to fix.** A common one is that the
project's plan does not include track changes, in which case suggestions are
impossible there and no retry will help.

## What you cannot verify from here

Whether a **suggestion** is still pending is **only** reliably visible in the
Review panel in a browser. Ask the user to confirm rather than asserting it
landed.

`overleaf_list_comments` reads **comments**, and it sweeps the document to beat
the panel's virtualisation — but it still under-reports. Treat its output as a
floor, never as the complete set, and never read an empty result as "there are
no comments". `coverage` says how hard it looked.

## Suggestion or comment?

They are different objects and are not interchangeable.

- **`overleaf_suggest`** proposes a concrete edit. The reviewer sees Accept /
  Reject, and accepting applies your text.
- **`overleaf_comment`** attaches a threaded note to a passage. The reviewer sees
  Resolve. It changes no text — if the document moves by so much as a character
  the call fails and restores it.

Raise a question, a doubt, or anything needing the author's judgement as a
**comment**. Propose wording you are confident about as a **suggestion**. Do not
express an edit as a comment saying "you should write X" when you could suggest X.

## Taking a suggestion back

`overleaf_withdraw_suggestion` rejects one of **your own** pending cards, for when
you got one wrong. It refuses if the card belongs to anyone else, so it can never
discard a co-author's work. It cannot accept anything — only a human does that.

## Deleting text

Pass `replace: ""`. Prefer commenting LaTeX out (`%`) over deleting it when the
author may want it back — a commented block does not typeset, so the saving is
the same and the content survives review.
