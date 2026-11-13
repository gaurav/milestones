# How priority and effort are labelled

This is how my repositories record how much an issue or a pull request matters, and how much work
it is. It is written for anyone working on one of them, human collaborators and coding agents
alike. [`milestones`](README.md) is the tool that reads and writes these labels, and sorts a
milestone's work by them, but nothing here depends on it. [MILESTONES.md](MILESTONES.md) is the
companion to this file: a milestone says when something is needed, and these labels say what to
pick up first among the things on it.

Both are labels, `Priority: High` and `Effort: M`, because a label is the one record an issue and
a pull request can both carry. GitHub's organisation issue fields of the same names can't be set
on a pull request.

## Priority

How much it matters, whatever the work involved.

| Label              | Colour    | Means                               |
|--------------------|-----------|-------------------------------------|
| `Priority: Urgent` | `#d2042d` | Drop other work for this.           |
| `Priority: High`   | `#b60205` | Do it before ordinary work.         |
| `Priority: Medium` | `#fbca04` | Ordinary work, in its turn.         |
| `Priority: Low`    | `#009800` | Worth doing once the rest is done.  |

## Effort

How much work it is for someone who knows the code, whatever its priority. Judge it from the code,
not from the length of the issue. For a pull request it is the work left: mostly the review.

| Label        | Colour    | Means                                           |
|--------------|-----------|-------------------------------------------------|
| `Effort: XS` | `#00ff00` | Under an hour.                                  |
| `Effort: S`  | `#0e8a16` | About half a day.                               |
| `Effort: M`  | `#fbca04` | A day or two.                                   |
| `Effort: L`  | `#b60205` | About a week.                                   |
| `Effort: XL` | `#ff0000` | More than a week, or needs splitting up first.  |

## Using them

An issue or a pull request carries at most one label of each kind. No label means nobody has
judged it yet, which is different from `Priority: Low`.

Within a milestone, work is taken by priority first and by effort within a priority, smallest
first. So the two labels are what order a milestone: GitHub offers no other order that a tool can
write.

## Older records

Priority and effort have been written in other ways over the years: `Priority: Critical` (now
`Priority: Urgent`), `Size: M` labels (now `Effort: M`), the organisation's Priority and Effort
issue fields, and labels of a repository's own. Read them wherever they are; an effort of High,
Medium or Low reads as L, M or S. Write only the labels above. When one of them is set, an older
record that repeats or contradicts it can go, and where the two disagree the label above stands.
Say so rather than silently choosing, since somebody set each.

## For coding agents

- Label a new issue or pull request with a priority and an effort when each is clear from what
  you know. When one isn't, leave it off: a guessed label is harder to spot than a missing one.
- Set one label of each kind at most. To change a value, remove the old label as you add the new.
- Don't set the organisation's Priority or Effort field, and don't create a label outside the two
  tables above for either.
- Don't change a priority somebody else set. If you think it is wrong, say so in a comment.
- If a label above doesn't exist in the repository yet, create it with the colour in its table.
